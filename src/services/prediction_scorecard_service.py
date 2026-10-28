# -*- coding: utf-8 -*-
"""Prediction scorecard: how good were the AI's past calls?

Builds on the DecisionSignal forward-outcome engine
(``src/services/decision_signal_outcome_service.py``), which already stores each
AI call (action, score, confidence, horizon) and later grades it ``hit`` /
``miss`` / ``neutral`` against real daily bars. This module adds the
user-facing scorecard on top:

1. refresh recent daily bars for the stocks behind recent calls (fail-open), so
   outcomes can complete even for stocks that left the watchlist;
2. evaluate pending outcomes on all four windows (1d / 3d / 5d / 10d), not
   only the signal's own horizon, so hit rate can be compared by window;
3. hit rate by window, stock and action;
4. a benchmark check: did each call do better than simply holding the market
   index (Nifty 50 for Indian stocks, S&P 500 for US, ...)?
5. a confidence check: did higher-confidence calls actually do better?

The result renders as Markdown and can be pushed through ``NotificationService``.
Every network / data step is fail-open: a missing benchmark or provider error
only removes that section, it never aborts the scorecard.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
import logging
import math
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, select

from src.report_language import normalize_report_language
from src.services.decision_signal_outcome_service import (
    DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
    DecisionSignalOutcomeService,
)
from src.storage import DatabaseManager, DecisionSignalOutcomeRecord, DecisionSignalRecord

logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS = 90
# Post-close daily-analysis signals default to a 3-trading-day horizon
# (decision_signal_service), so the headline grades calls on their own stated
# window; 1d / 5d / 10d are shown alongside to see whether calls hold up.
HEADLINE_HORIZON = "3d"
HORIZON_ORDER = ("1d", "3d", "5d", "10d")
HORIZON_DAYS = {"1d": 1, "3d": 3, "5d": 5, "10d": 10}
# Minimum completed hit/miss calls per group before the confidence verdict is
# allowed to claim a difference.
MIN_CONFIDENCE_SAMPLE = 5
# Hit-rate gap (percentage points) needed to call one confidence group better.
CONFIDENCE_GAP_PP = 5.0
NEUTRAL_BAND_PCT = 2.0  # mirrors the outcome engine's neutral band

# Market -> (Yahoo Finance symbol, display name) of the "just hold the market"
# benchmark each call is compared against.
MARKET_BENCHMARKS: Dict[str, Tuple[str, str]] = {
    "in": ("^NSEI", "Nifty 50"),
    "us": ("^GSPC", "S&P 500"),
    "hk": ("^HSI", "Hang Seng"),
    "jp": ("^N225", "Nikkei 225"),
    "kr": ("^KS11", "KOSPI"),
    "tw": ("^TWII", "TAIEX"),
    "cn": ("000300.SS", "CSI 300"),
}

# Benchmark loader: (yahoo_symbol, start, end) -> [(date, close), ...] ascending.
BenchmarkLoader = Callable[[str, date, date], List[Tuple[date, float]]]


@dataclass(frozen=True)
class ScoredCall:
    """One graded call for one horizon, plus optional benchmark comparison."""

    signal_id: int
    stock_code: str
    stock_name: Optional[str]
    market: str
    action: str
    horizon: str
    direction: str
    outcome: str
    stock_return_pct: Optional[float]
    anchor_date: Optional[date]
    eval_window_days: Optional[int]
    score: Optional[int]
    confidence: Optional[float]
    benchmark_return_pct: Optional[float] = None
    edge_pct: Optional[float] = None
    beat_benchmark: Optional[bool] = None


@dataclass
class Tally:
    total: int = 0
    hit: int = 0
    miss: int = 0
    neutral: int = 0
    returns: List[float] = field(default_factory=list)
    beat: int = 0
    compared: int = 0
    edges: List[float] = field(default_factory=list)

    @property
    def hit_rate_pct(self) -> Optional[float]:
        decided = self.hit + self.miss
        return round(self.hit / decided * 100, 1) if decided else None

    @property
    def avg_return_pct(self) -> Optional[float]:
        return round(sum(self.returns) / len(self.returns), 2) if self.returns else None

    @property
    def beat_rate_pct(self) -> Optional[float]:
        return round(self.beat / self.compared * 100, 1) if self.compared else None

    @property
    def avg_edge_pct(self) -> Optional[float]:
        return round(sum(self.edges) / len(self.edges), 2) if self.edges else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "hit": self.hit,
            "miss": self.miss,
            "neutral": self.neutral,
            "hit_rate_pct": self.hit_rate_pct,
            "avg_return_pct": self.avg_return_pct,
            "compared": self.compared,
            "beat": self.beat,
            "beat_rate_pct": self.beat_rate_pct,
            "avg_edge_pct": self.avg_edge_pct,
        }


def tally(calls: Iterable[ScoredCall]) -> Tally:
    result = Tally()
    for call in calls:
        result.total += 1
        if call.outcome == "hit":
            result.hit += 1
        elif call.outcome == "miss":
            result.miss += 1
        elif call.outcome == "neutral":
            result.neutral += 1
        if _finite(call.stock_return_pct):
            result.returns.append(float(call.stock_return_pct))
        if call.beat_benchmark is not None:
            result.compared += 1
            result.beat += 1 if call.beat_benchmark else 0
        if _finite(call.edge_pct):
            result.edges.append(float(call.edge_pct))
    return result


# ---------------------------------------------------------------------------
# Benchmark comparison
# ---------------------------------------------------------------------------

def benchmark_edge(direction: str, stock_return_pct: float, benchmark_return_pct: float) -> Tuple[float, bool]:
    """Return ``(edge_pct, beat)`` for one call versus holding the benchmark.

    The edge measures how much better the call's implied choice did than just
    holding the index over the same window:

    - ``up`` (buy/add) and ``not_down`` (hold): the call says *own the stock*,
      so edge = stock return - index return.
    - ``not_up`` (reduce/sell/avoid): the call says *get out*; switching to the
      index was the alternative, so edge = index return - stock return.
    """

    if direction == "not_up":
        edge = benchmark_return_pct - stock_return_pct
    else:
        edge = stock_return_pct - benchmark_return_pct
    return round(edge, 4), edge > 0


def benchmark_window_return(
    closes: Sequence[Tuple[date, float]],
    anchor: date,
    window_days: int,
) -> Optional[float]:
    """Index return from the close on/before ``anchor`` to ``window_days`` sessions later.

    Uses the index's own sessions, matching how the outcome engine walks
    ``eval_window_days`` forward stock bars. Returns None when the window has
    not fully elapsed or prices are missing.
    """

    if not closes or window_days <= 0:
        return None
    start_index = None
    for index, (day, _close) in enumerate(closes):
        if day <= anchor:
            start_index = index
        else:
            break
    if start_index is None:
        return None
    end_index = start_index + window_days
    if end_index >= len(closes):
        return None
    start_close = closes[start_index][1]
    end_close = closes[end_index][1]
    if not (_finite(start_close) and _finite(end_close)) or start_close <= 0:
        return None
    return round((end_close / start_close - 1.0) * 100.0, 4)


def attach_benchmarks(
    calls: Sequence[ScoredCall],
    loader: Optional[BenchmarkLoader],
) -> Tuple[List[ScoredCall], Dict[str, str], List[str]]:
    """Attach benchmark returns / edges. Returns (calls, market->index name, warnings)."""

    if loader is None or not calls:
        return list(calls), {}, []

    warnings: List[str] = []
    names: Dict[str, str] = {}
    by_market: Dict[str, List[ScoredCall]] = defaultdict(list)
    for call in calls:
        by_market[call.market].append(call)

    closes_by_market: Dict[str, List[Tuple[date, float]]] = {}
    for market, market_calls in by_market.items():
        spec = MARKET_BENCHMARKS.get(market)
        anchors = [c.anchor_date for c in market_calls if c.anchor_date is not None]
        if spec is None or not anchors:
            continue
        symbol, name = spec
        start = min(anchors) - timedelta(days=10)
        end = max(anchors) + timedelta(days=30)
        try:
            closes = sorted(
                (day, float(close))
                for day, close in loader(symbol, start, end)
                if _finite(close)
            )
        except Exception as exc:  # noqa: BLE001 - benchmark is optional, fail-open
            logger.warning("[scorecard] benchmark %s load failed: %s", symbol, exc)
            warnings.append(f"benchmark_unavailable:{market}")
            continue
        if not closes:
            warnings.append(f"benchmark_empty:{market}")
            continue
        closes_by_market[market] = closes
        names[market] = name

    enriched: List[ScoredCall] = []
    for call in calls:
        closes = closes_by_market.get(call.market)
        if (
            closes is None
            or call.anchor_date is None
            or not call.eval_window_days
            or not _finite(call.stock_return_pct)
        ):
            enriched.append(call)
            continue
        bench = benchmark_window_return(closes, call.anchor_date, int(call.eval_window_days))
        if bench is None:
            enriched.append(call)
            continue
        edge, beat = benchmark_edge(call.direction, float(call.stock_return_pct), bench)
        enriched.append(replace(call, benchmark_return_pct=bench, edge_pct=edge, beat_benchmark=beat))
    return enriched, names, warnings


def yfinance_benchmark_loader(symbol: str, start: date, end: date) -> List[Tuple[date, float]]:
    """Default benchmark loader backed by yfinance (needs network)."""

    import yfinance as yf

    history = yf.Ticker(symbol).history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,
    )
    if history is None or history.empty or "Close" not in history:
        return []
    rows: List[Tuple[date, float]] = []
    for index, close in history["Close"].items():
        day = index.date() if hasattr(index, "date") else index
        rows.append((day, float(close)))
    return rows


# ---------------------------------------------------------------------------
# Confidence check
# ---------------------------------------------------------------------------

CONFIDENCE_LEVELS = ("high", "medium", "low")


def confidence_level(value: Optional[float]) -> str:
    """Map the extractor's numeric confidence (0.8 / 0.6 / 0.4) back to a level."""

    if not _finite(value):
        return "unknown"
    number = float(value)
    if number > 1:  # tolerate 0-100 inputs
        number /= 100.0
    if number >= 0.7:
        return "high"
    if number >= 0.5:
        return "medium"
    return "low"


def score_band(score: Optional[int]) -> str:
    """Bands of the canonical decision-scale-v1 score (see docs/decision-signals.md)."""

    if score is None:
        return "unknown"
    if score >= 80:
        return "80-100"
    if score >= 60:
        return "60-79"
    if score >= 40:
        return "40-59"
    if score >= 20:
        return "20-39"
    return "0-19"


SCORE_BANDS = ("80-100", "60-79", "40-59", "20-39", "0-19")


def confidence_verdict(groups: Mapping[str, Tally]) -> str:
    """Compare high- vs low-confidence hit rates.

    Returns one of ``higher_better`` / ``lower_better`` / ``no_difference`` /
    ``insufficient``. ``low`` falls back to ``medium`` when there are too few
    low-confidence calls, so a run with only high/medium labels still gets a
    verdict.
    """

    high = groups.get("high")
    low = groups.get("low")
    if low is None or (low.hit + low.miss) < MIN_CONFIDENCE_SAMPLE:
        low = groups.get("medium")
    if high is None or low is None:
        return "insufficient"
    if (high.hit + high.miss) < MIN_CONFIDENCE_SAMPLE or (low.hit + low.miss) < MIN_CONFIDENCE_SAMPLE:
        return "insufficient"
    gap = (high.hit_rate_pct or 0.0) - (low.hit_rate_pct or 0.0)
    if gap >= CONFIDENCE_GAP_PP:
        return "higher_better"
    if gap <= -CONFIDENCE_GAP_PP:
        return "lower_better"
    return "no_difference"


# ---------------------------------------------------------------------------
# Scorecard assembly
# ---------------------------------------------------------------------------

def build_scorecard(
    calls: Sequence[ScoredCall],
    *,
    as_of: date,
    lookback_days: int,
    benchmark_names: Optional[Mapping[str, str]] = None,
    warnings: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Aggregate graded calls into the scorecard payload (pure, no I/O)."""

    by_horizon: Dict[str, List[ScoredCall]] = defaultdict(list)
    for call in calls:
        by_horizon[call.horizon].append(call)

    headline_horizon = HEADLINE_HORIZON
    if not by_horizon.get(headline_horizon):
        available = [h for h in HORIZON_ORDER if by_horizon.get(h)]
        headline_horizon = available[-1] if available else HEADLINE_HORIZON
    headline_calls = by_horizon.get(headline_horizon, [])

    def _grouped(key: Callable[[ScoredCall], str], order: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        groups: Dict[str, List[ScoredCall]] = defaultdict(list)
        for call in headline_calls:
            groups[key(call)].append(call)
        items = [{"key": k, **tally(v).to_dict()} for k, v in groups.items()]
        if order is not None:
            rank = {k: i for i, k in enumerate(order)}
            items.sort(key=lambda item: (rank.get(item["key"], len(rank)), item["key"]))
        else:
            items.sort(key=lambda item: (-item["total"], item["key"]))
        return items

    stock_names: Dict[str, str] = {}
    for call in calls:
        if call.stock_name and call.stock_code not in stock_names:
            stock_names[call.stock_code] = call.stock_name

    confidence_tallies = {
        level: tally(c for c in headline_calls if confidence_level(c.confidence) == level)
        for level in (*CONFIDENCE_LEVELS, "unknown")
    }

    names = dict(benchmark_names or {})
    markets = sorted({call.market for call in calls})
    if len(markets) == 1 and markets[0] in names:
        benchmark_label = names[markets[0]]
    elif names:
        benchmark_label = None  # mixed markets: render a generic "market index"
    else:
        benchmark_label = None

    return {
        "as_of": as_of.isoformat(),
        "lookback_days": lookback_days,
        "engine_version": DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
        "headline_horizon": headline_horizon,
        "headline": tally(headline_calls).to_dict(),
        "by_horizon": [
            {"key": h, **tally(by_horizon[h]).to_dict()}
            for h in HORIZON_ORDER
            if by_horizon.get(h)
        ],
        "by_stock": _grouped(lambda c: c.stock_code),
        "by_action": _grouped(lambda c: c.action or "unknown"),
        "by_confidence": [
            {"key": level, **confidence_tallies[level].to_dict()}
            for level in (*CONFIDENCE_LEVELS, "unknown")
            if confidence_tallies[level].total
        ],
        "by_score_band": _grouped(lambda c: score_band(c.score), order=(*SCORE_BANDS, "unknown")),
        "confidence_verdict": confidence_verdict(confidence_tallies),
        "benchmark_label": benchmark_label,
        "benchmark_names": names,
        "stock_names": stock_names,
        "total_calls": len({call.signal_id for call in calls}),
        "warnings": list(warnings or []),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_LABELS = {
    "en": {
        "title": "📊 **Prediction Scorecard** · {as_of}",
        "subtitle": "AI calls from the last {days} days, checked against what prices actually did.",
        "empty": (
            "No checked predictions yet. The scorecard fills in as daily runs build history; "
            "the first results appear about a week after the first run."
        ),
        "headline": "**Main window: {window}** (the AI's usual horizon) · {n} calls checked",
        "hit_rate": "✅ Hit rate: **{rate}** ({hit} hit · {miss} miss · {neutral} neutral)",
        "beat": "📈 Beat {bench}: **{rate}** of calls · avg edge **{edge}**",
        "beat_pending": "📈 Beat {bench}: not enough completed windows yet",
        "by_window": "**By window**",
        "by_stock": "**By stock** ({window})",
        "by_action": "**By action** ({window})",
        "confidence": "**Confidence check** ({window})",
        "score_band": "Score bands: {bands}",
        "line_window": "- {key}: {rate} hit rate ({decided} decided) · beat index {beat} · edge {edge}",
        "line_stock": "- {name}: {rate} hit rate · {calls} calls · avg move {ret} · beat index {beat}",
        "line_action": "- {key}: {rate} hit rate ({calls} calls)",
        "line_conf": "- {key}: {rate} hit rate ({decided} decided)",
        "verdict": {
            "higher_better": "Verdict: higher-confidence calls did better ✓",
            "lower_better": "Verdict: higher-confidence calls did *worse* ✗ (don't lean on the confidence label)",
            "no_difference": "Verdict: no clear difference between confidence levels",
            "insufficient": "Verdict: not enough data yet (need {n}+ decided calls per group)",
        },
        "conf_names": {"high": "High", "medium": "Medium", "low": "Low", "unknown": "Not given"},
        "window_names": {"1d": "1 trading day", "3d": "3 trading days", "5d": "5 trading days", "10d": "10 trading days"},
        "market_index": "market index",
        "footer": (
            "_Hit = price moved the predicted way by more than {band}%; neutral = within ±{band}% "
            "(not counted in hit rate). Edge = how much better the call's choice did than just "
            "holding the index (positive is good). Not investment advice._"
        ),
        "warn_benchmark": "_Benchmark data unavailable for: {markets}._",
    },
    "zh": {
        "title": "📊 **预测准确率记分卡** · {as_of}",
        "subtitle": "统计最近 {days} 天的 AI 建议，并与之后的真实走势对照。",
        "empty": "暂无已完成评估的预测。记分卡会随每日运行积累历史，首次结果约在首次运行一周后出现。",
        "headline": "**主窗口：{window}**（AI 默认预测周期）· 已评估 {n} 条",
        "hit_rate": "✅ 命中率：**{rate}**（命中 {hit} · 未中 {miss} · 中性 {neutral}）",
        "beat": "📈 跑赢{bench}：**{rate}** · 平均超额 **{edge}**",
        "beat_pending": "📈 跑赢{bench}：已完成窗口不足，暂无数据",
        "by_window": "**按窗口**",
        "by_stock": "**按个股**（{window}）",
        "by_action": "**按建议动作**（{window}）",
        "confidence": "**置信度检验**（{window}）",
        "score_band": "评分区间：{bands}",
        "line_window": "- {key}：命中率 {rate}（有效 {decided} 条）· 跑赢指数 {beat} · 超额 {edge}",
        "line_stock": "- {name}：命中率 {rate} · {calls} 条 · 平均涨跌 {ret} · 跑赢指数 {beat}",
        "line_action": "- {key}：命中率 {rate}（{calls} 条）",
        "line_conf": "- {key}：命中率 {rate}（有效 {decided} 条）",
        "verdict": {
            "higher_better": "结论：高置信度建议表现更好 ✓",
            "lower_better": "结论：高置信度建议反而更差 ✗（不要依赖置信度标签）",
            "no_difference": "结论：不同置信度之间无明显差异",
            "insufficient": "结论：样本不足（每组需 {n} 条以上有效结果）",
        },
        "conf_names": {"high": "高", "medium": "中", "low": "低", "unknown": "未给出"},
        "window_names": {"1d": "1 个交易日", "3d": "3 个交易日", "5d": "5 个交易日", "10d": "10 个交易日"},
        "market_index": "大盘指数",
        "footer": (
            "_命中 = 按预测方向波动超过 {band}%；中性 = 波动在 ±{band}% 以内（不计入命中率）。"
            "超额 = 该建议相对直接持有指数的收益差（正数为好）。不构成投资建议。_"
        ),
        "warn_benchmark": "_以下市场的基准指数数据暂不可用：{markets}。_",
    },
}


def render_markdown(scorecard: Mapping[str, Any], language: Optional[str] = "zh") -> str:
    lang = "en" if normalize_report_language(language) == "en" else "zh"
    labels = _LABELS[lang]
    lines: List[str] = [
        labels["title"].format(as_of=scorecard.get("as_of")),
        labels["subtitle"].format(days=scorecard.get("lookback_days")),
        "",
    ]

    headline = scorecard.get("headline") or {}
    if not headline.get("total"):
        lines.append(labels["empty"])
        return "\n".join(lines).strip() + "\n"

    windows = labels["window_names"]
    window = windows.get(scorecard.get("headline_horizon"), scorecard.get("headline_horizon"))
    bench = scorecard.get("benchmark_label") or labels["market_index"]

    lines.append(labels["headline"].format(window=window, n=headline.get("total")))
    lines.append(
        labels["hit_rate"].format(
            rate=_pct(headline.get("hit_rate_pct")),
            hit=headline.get("hit", 0),
            miss=headline.get("miss", 0),
            neutral=headline.get("neutral", 0),
        )
    )
    if headline.get("compared"):
        lines.append(
            labels["beat"].format(
                bench=bench,
                rate=_pct(headline.get("beat_rate_pct")),
                edge=_signed_pct(headline.get("avg_edge_pct")),
            )
        )
    else:
        lines.append(labels["beat_pending"].format(bench=bench))

    by_horizon = scorecard.get("by_horizon") or []
    if by_horizon:
        lines += ["", labels["by_window"]]
        for item in by_horizon:
            lines.append(
                labels["line_window"].format(
                    key=windows.get(item["key"], item["key"]),
                    rate=_pct(item.get("hit_rate_pct")),
                    decided=item.get("hit", 0) + item.get("miss", 0),
                    beat=_pct(item.get("beat_rate_pct")),
                    edge=_signed_pct(item.get("avg_edge_pct")),
                )
            )

    stock_names = scorecard.get("stock_names") or {}
    by_stock = scorecard.get("by_stock") or []
    if by_stock:
        lines += ["", labels["by_stock"].format(window=window)]
        for item in by_stock:
            code = item["key"]
            name = stock_names.get(code)
            display = f"{name} ({code})" if name and name != code else code
            lines.append(
                labels["line_stock"].format(
                    name=display,
                    rate=_pct(item.get("hit_rate_pct")),
                    calls=item.get("total", 0),
                    ret=_signed_pct(item.get("avg_return_pct")),
                    beat=_pct(item.get("beat_rate_pct")),
                )
            )

    by_action = scorecard.get("by_action") or []
    if by_action:
        lines += ["", labels["by_action"].format(window=window)]
        for item in by_action:
            lines.append(
                labels["line_action"].format(
                    key=item["key"],
                    rate=_pct(item.get("hit_rate_pct")),
                    calls=item.get("total", 0),
                )
            )

    by_confidence = scorecard.get("by_confidence") or []
    lines += ["", labels["confidence"].format(window=window)]
    conf_names = labels["conf_names"]
    for item in by_confidence:
        lines.append(
            labels["line_conf"].format(
                key=conf_names.get(item["key"], item["key"]),
                rate=_pct(item.get("hit_rate_pct")),
                decided=item.get("hit", 0) + item.get("miss", 0),
            )
        )
    bands = [
        f"{item['key']} {_pct(item.get('hit_rate_pct'))} ({item.get('hit', 0) + item.get('miss', 0)})"
        for item in scorecard.get("by_score_band") or []
        if item["key"] != "unknown"
    ]
    if bands:
        lines.append(labels["score_band"].format(bands=" · ".join(bands)))
    verdict_key = scorecard.get("confidence_verdict") or "insufficient"
    lines.append(labels["verdict"].get(verdict_key, verdict_key).format(n=MIN_CONFIDENCE_SAMPLE))

    missing = sorted(
        {w.split(":", 1)[1] for w in scorecard.get("warnings") or [] if w.startswith("benchmark_")}
    )
    lines.append("")
    if missing:
        lines.append(labels["warn_benchmark"].format(markets=", ".join(missing)))
    lines.append(labels["footer"].format(band=_trim_number(NEUTRAL_BAND_PCT)))
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# Service / CLI entry
# ---------------------------------------------------------------------------

class PredictionScorecardService:
    """Refreshes data, evaluates pending outcomes, and builds the scorecard."""

    def __init__(
        self,
        *,
        db_manager: Optional[DatabaseManager] = None,
        outcome_service: Optional[DecisionSignalOutcomeService] = None,
        fetcher_manager: Any = None,
        benchmark_loader: Optional[BenchmarkLoader] = yfinance_benchmark_loader,
    ):
        self.db = db_manager or DatabaseManager.get_instance()
        self.outcome_service = outcome_service or DecisionSignalOutcomeService(db_manager=self.db)
        self.fetcher_manager = fetcher_manager
        self.benchmark_loader = benchmark_loader

    def recent_signal_codes(self, since: date) -> List[str]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(DecisionSignalRecord.stock_code)
                .where(DecisionSignalRecord.created_at >= datetime.combine(since, datetime.min.time()))
                .distinct()
            ).all()
        return sorted({str(row[0]) for row in rows if row[0]})

    def refresh_daily_bars(self, codes: Sequence[str], days: int) -> Dict[str, str]:
        """Fetch recent daily bars so forward outcomes can complete (fail-open)."""

        status: Dict[str, str] = {}
        if not codes or self.fetcher_manager is None:
            return status
        for code in codes:
            try:
                df, source = self.fetcher_manager.get_daily_data(code, days=days)
                if df is None or getattr(df, "empty", True):
                    status[code] = "empty"
                    continue
                self.db.save_daily_data(df, code, source)
                status[code] = "ok"
            except Exception as exc:  # noqa: BLE001 - one stock must not stop the scorecard
                logger.warning("[scorecard] refresh daily bars failed for %s: %s", code, exc)
                status[code] = "failed"
        return status

    def evaluate_pending(self) -> Dict[str, Any]:
        try:
            return self.outcome_service.run_outcomes(horizons=list(HORIZON_ORDER), limit=500)
        except Exception as exc:  # noqa: BLE001 - keep scoring what is already stored
            logger.warning("[scorecard] outcome evaluation failed: %s", exc)
            return {"evaluated": 0, "error": str(exc)}

    def load_scored_calls(self, since: date) -> List[ScoredCall]:
        with self.db.get_session() as session:
            rows = session.execute(
                select(DecisionSignalOutcomeRecord, DecisionSignalRecord)
                .join(DecisionSignalRecord, DecisionSignalRecord.id == DecisionSignalOutcomeRecord.signal_id)
                .where(
                    and_(
                        DecisionSignalOutcomeRecord.engine_version == DECISION_SIGNAL_OUTCOME_ENGINE_VERSION,
                        DecisionSignalOutcomeRecord.eval_status == "completed",
                        DecisionSignalOutcomeRecord.anchor_date >= since,
                    )
                )
            ).all()
            calls = [
                ScoredCall(
                    signal_id=int(signal.id),
                    stock_code=str(signal.stock_code),
                    stock_name=signal.stock_name,
                    market=str(outcome.market or signal.market or ""),
                    action=str(outcome.action or signal.action or ""),
                    horizon=str(outcome.horizon),
                    direction=str(outcome.direction_expected or ""),
                    outcome=str(outcome.outcome or ""),
                    stock_return_pct=outcome.stock_return_pct,
                    anchor_date=outcome.anchor_date,
                    eval_window_days=outcome.eval_window_days or HORIZON_DAYS.get(str(outcome.horizon)),
                    score=signal.score,
                    confidence=signal.confidence,
                )
                for outcome, signal in rows
            ]
        return calls

    def build(self, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS, today: Optional[date] = None) -> Dict[str, Any]:
        as_of = today or date.today()
        since = as_of - timedelta(days=max(1, int(lookback_days)))
        codes = self.recent_signal_codes(since)
        refresh = self.refresh_daily_bars(codes, days=int(lookback_days) + 20)
        evaluation = self.evaluate_pending()
        calls = self.load_scored_calls(since)
        calls, names, warnings = attach_benchmarks(calls, self.benchmark_loader)
        scorecard = build_scorecard(
            calls,
            as_of=as_of,
            lookback_days=int(lookback_days),
            benchmark_names=names,
            warnings=warnings,
        )
        scorecard["refresh"] = refresh
        scorecard["evaluation"] = {
            key: evaluation.get(key)
            for key in ("evaluated", "created", "updated", "skipped", "error")
            if key in evaluation
        }
        return scorecard


def run_prediction_scorecard(
    config: Any,
    *,
    send_notification: bool = True,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    notifier: Any = None,
    fetcher_manager: Any = None,
    benchmark_loader: Optional[BenchmarkLoader] = yfinance_benchmark_loader,
    db_manager: Optional[DatabaseManager] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """CLI entry (``python main.py --scorecard``): build, save and optionally push."""

    if fetcher_manager is None:
        try:
            from data_provider import DataFetcherManager

            fetcher_manager = DataFetcherManager()
        except Exception as exc:  # noqa: BLE001 - scoring stored data still works
            logger.warning("[scorecard] data fetcher unavailable, skipping bar refresh: %s", exc)

    service = PredictionScorecardService(
        db_manager=db_manager,
        fetcher_manager=fetcher_manager,
        benchmark_loader=benchmark_loader,
    )
    scorecard = service.build(lookback_days=lookback_days, today=today)
    markdown = render_markdown(scorecard, getattr(config, "report_language", "zh"))
    scorecard["markdown"] = markdown
    _deliver(markdown, scorecard["as_of"], send_notification, notifier)
    return scorecard


def _deliver(markdown: str, as_of: str, send_notification: bool, notifier: Any) -> None:
    """Save and push the scorecard; failures are logged, never raised."""

    try:
        if notifier is None:
            from src.notification import NotificationService

            notifier = NotificationService()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scorecard] notification service init failed; report not saved or sent: %s\n%s", exc, markdown)
        return

    filename = f"prediction_scorecard_{as_of.replace('-', '')}.md"
    try:
        path = notifier.save_report_to_file(markdown, filename)
        logger.info("[scorecard] report saved: %s", path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scorecard] report save failed: %s\n%s", exc, markdown)

    if not send_notification:
        return
    try:
        if not notifier.is_available():
            logger.info("[scorecard] no notification channel configured; skipping push")
        elif not notifier.send(markdown, route_type="report"):
            logger.warning("[scorecard] notification push failed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[scorecard] notification push error: %s", exc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{_trim_number(value)}%"


def _signed_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    text = _trim_number(value)
    return f"{'+' if text != '0' and value > 0 else ''}{text}%"


def _trim_number(value: float) -> str:
    text = f"{float(value):.1f}"
    if text in ("-0.0", "0.0"):
        return "0"
    return text[:-2] if text.endswith(".0") else text
