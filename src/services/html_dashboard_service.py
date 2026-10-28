# -*- coding: utf-8 -*-
"""Self-contained HTML dashboard for one analysis day.

``python main.py --dashboard`` writes ``reports/dashboard_YYYYMMDD.html``: a
single file that opens in any browser (no server, no network, no scripts) and
shows

1. the latest session's AI call per stock (action, score, confidence, entry /
   stop / target) with a price sparkline marking those levels;
2. the prediction scorecard as charts (hit rate by window, beat the index,
   confidence check), built by ``PredictionScorecardService``;
3. the day's Markdown reports (stock reports and market recap), rendered and
   collapsible per stock.

The file is saved next to the Markdown reports (so it lands in the GitHub
Actions artifact) and can be sent to Telegram as a document. Every data step
is fail-open: a missing scorecard or report only removes that section.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import html
import json
import logging
import math
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import markdown2
from sqlalchemy import select

from src.formatters import strip_hidden_markdown_metadata, strip_markdown_reference_definitions
from src.report_language import normalize_report_language
from src.services.prediction_scorecard_service import confidence_level
from src.storage import DatabaseManager, DecisionSignalRecord

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"
CALL_LOOKBACK_DAYS = 10
SPARKLINE_SESSIONS = 30

_CURRENCY = {"in": "₹", "us": "$", "hk": "HK$", "cn": "¥", "jp": "¥", "kr": "₩", "tw": "NT$"}
_MARKET_TZ = {
    "in": "Asia/Kolkata",
    "us": "America/New_York",
    "hk": "Asia/Hong_Kong",
    "cn": "Asia/Shanghai",
    "jp": "Asia/Tokyo",
    "kr": "Asia/Seoul",
    "tw": "Asia/Taipei",
}
_ACTION_TONE = {
    "buy": "up",
    "add": "up",
    "hold": "info",
    "watch": "warn",
    "alert": "warn",
    "reduce": "down",
    "sell": "down",
    "avoid": "down",
}
_ACTION_RANK = {"buy": 0, "add": 1, "hold": 2, "watch": 3, "alert": 4, "reduce": 5, "sell": 6, "avoid": 7}

_LABELS: Dict[str, Dict[str, Any]] = {
    "en": {
        "html_lang": "en",
        "title": "Daily Dashboard",
        "stocks": "{n} stocks",
        "session": "session {d}",
        "generated": "generated {t}",
        "calls": "Today's calls",
        "no_calls": "No analysis calls found for the latest session.",
        "score": "Score",
        "confidence": "Confidence",
        "window": "Window",
        "entry": "Entry",
        "stop": "Stop",
        "target": "Target",
        "full": "Full analysis",
        "scorecard": "Prediction scorecard",
        "scorecard_sub": "AI calls from the last {days} days, checked against what prices did next.",
        "scorecard_missing": "Scorecard unavailable for this run.",
        "scorecard_empty": (
            "No checked predictions yet. Each call needs a few trading days to play out, "
            "so the first results appear about a week after the first run."
        ),
        "tracking": "{n} calls tracked so far",
        "hit_rate": "Hit rate",
        "hit_rate_sub": "{hit} hit · {miss} miss · {neutral} neutral",
        "beat": "Beat {bench}",
        "beat_sub": "{beat} of {n} calls",
        "edge": "Avg edge vs {bench}",
        "edge_sub": "average per call",
        "by_window": "Hit rate by window",
        "by_stock": "By stock ({window})",
        "confidence_check": "Confidence check ({window})",
        "coin_flip": "50% = coin flip",
        "decided": "{n} decided",
        "pending": "not enough data",
        "market_index": "the index",
        "window_names": {"1d": "1 day", "3d": "3 days", "5d": "5 days", "10d": "10 days"},
        "conf_names": {"high": "High", "medium": "Medium", "low": "Low", "unknown": "Not given"},
        "verdict": {
            "higher_better": "Higher-confidence calls did better.",
            "lower_better": "Higher-confidence calls did worse, so don't lean on the confidence label.",
            "no_difference": "No clear difference between confidence levels.",
            "insufficient": "Not enough data yet to judge the confidence label.",
        },
        "reports": "Full reports",
        "market_recap": "Market recap",
        "summary": "Summary",
        "footer": (
            "Hit = price moved the predicted way by more than 2% within the window; moves within ±2% are "
            "neutral and not counted. Edge = how much better the call did than just holding the index. "
            "Not investment advice."
        ),
        "actions": {
            "buy": "Buy", "add": "Add", "hold": "Hold", "watch": "Watch",
            "alert": "Alert", "reduce": "Reduce", "sell": "Sell", "avoid": "Avoid",
        },
        "caption": "📊 Dashboard · {d}\n{summary}\nOpen the file in your browser.",
    },
    "zh": {
        "html_lang": "zh-CN",
        "title": "每日看板",
        "stocks": "{n} 只股票",
        "session": "交易日 {d}",
        "generated": "生成于 {t}",
        "calls": "今日建议",
        "no_calls": "最近一个交易日没有找到分析建议。",
        "score": "评分",
        "confidence": "置信度",
        "window": "周期",
        "entry": "买入区间",
        "stop": "止损",
        "target": "目标",
        "full": "完整分析",
        "scorecard": "预测准确率记分卡",
        "scorecard_sub": "最近 {days} 天的 AI 建议，与之后的真实走势对照。",
        "scorecard_missing": "本次运行未能生成记分卡。",
        "scorecard_empty": "暂无已完成评估的预测。每条建议需要几个交易日验证，首次结果约在首次运行一周后出现。",
        "tracking": "已跟踪 {n} 条建议",
        "hit_rate": "命中率",
        "hit_rate_sub": "命中 {hit} · 未中 {miss} · 中性 {neutral}",
        "beat": "跑赢{bench}",
        "beat_sub": "{n} 条中 {beat} 条",
        "edge": "相对{bench}平均超额",
        "edge_sub": "每条建议平均",
        "by_window": "按窗口命中率",
        "by_stock": "按个股（{window}）",
        "confidence_check": "置信度检验（{window}）",
        "coin_flip": "50% = 抛硬币",
        "decided": "有效 {n} 条",
        "pending": "数据不足",
        "market_index": "大盘指数",
        "window_names": {"1d": "1 日", "3d": "3 日", "5d": "5 日", "10d": "10 日"},
        "conf_names": {"high": "高", "medium": "中", "low": "低", "unknown": "未给出"},
        "verdict": {
            "higher_better": "高置信度建议表现更好。",
            "lower_better": "高置信度建议反而更差，不要依赖置信度标签。",
            "no_difference": "不同置信度之间无明显差异。",
            "insufficient": "样本不足，暂无法判断置信度标签是否有效。",
        },
        "reports": "完整报告",
        "market_recap": "大盘复盘",
        "summary": "概览",
        "footer": "命中 = 在窗口内按预测方向波动超过 2%；±2% 以内为中性，不计入命中率。超额 = 该建议相对直接持有指数的收益差。不构成投资建议。",
        "actions": {
            "buy": "买入", "add": "加仓", "hold": "持有", "watch": "观望",
            "alert": "预警", "reduce": "减仓", "sell": "卖出", "avoid": "回避",
        },
        "caption": "📊 每日看板 · {d}\n{summary}\n请用浏览器打开文件。",
    },
}


@dataclass
class DashboardCall:
    """The latest AI call for one stock, plus recent closes for the sparkline."""

    signal_id: int
    stock_code: str
    stock_name: Optional[str]
    market: str
    action: str
    score: Optional[int]
    confidence: Optional[float]
    horizon: Optional[str]
    entry_low: Optional[float]
    entry_high: Optional[float]
    stop_loss: Optional[float]
    target_price: Optional[float]
    reason: Optional[str]
    session_date: Optional[date]
    closes: List[Tuple[date, float]] = field(default_factory=list)

    @property
    def last_close(self) -> Optional[float]:
        return self.closes[-1][1] if self.closes else None

    @property
    def change_pct(self) -> Optional[float]:
        if len(self.closes) < 2 or not self.closes[-2][1]:
            return None
        return (self.closes[-1][1] / self.closes[-2][1] - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def signal_session_date(metadata_json: Optional[str], created_at: Optional[datetime]) -> Optional[date]:
    """Trading session a signal belongs to (same rule as the outcome engine)."""

    try:
        metadata = json.loads(metadata_json) if metadata_json else None
    except (TypeError, ValueError):
        metadata = None
    if isinstance(metadata, dict):
        summary = metadata.get("market_phase_summary")
        if isinstance(summary, dict) and summary.get("session_date"):
            try:
                return date.fromisoformat(str(summary["session_date"])[:10])
            except ValueError:
                pass
    if isinstance(created_at, datetime):
        return created_at.date()
    return None


def load_latest_calls(
    db: DatabaseManager,
    *,
    today: Optional[date] = None,
    lookback_days: int = CALL_LOOKBACK_DAYS,
) -> Tuple[Optional[date], List[DashboardCall]]:
    """Latest session's daily-analysis call per stock (later runs win)."""

    as_of = today or date.today()
    since = datetime.combine(as_of - timedelta(days=max(1, lookback_days)), time.min)
    with db.get_session() as session:
        rows = session.execute(
            select(DecisionSignalRecord)
            .where(DecisionSignalRecord.created_at >= since)
            .where(DecisionSignalRecord.source_type == "analysis")
            .order_by(DecisionSignalRecord.id)
        ).scalars().all()
        calls = [
            DashboardCall(
                signal_id=int(row.id),
                stock_code=str(row.stock_code),
                stock_name=row.stock_name,
                market=str(row.market or ""),
                action=str(row.action or ""),
                score=row.score,
                confidence=row.confidence,
                horizon=row.horizon,
                entry_low=row.entry_low,
                entry_high=row.entry_high,
                stop_loss=row.stop_loss,
                target_price=row.target_price,
                reason=row.reason,
                session_date=signal_session_date(row.metadata_json, row.created_at),
            )
            for row in rows
        ]
    dated = [call for call in calls if call.session_date is not None]
    if not dated:
        return None, []
    latest_session = max(call.session_date for call in dated)
    latest: Dict[str, DashboardCall] = {}
    for call in dated:
        if call.session_date == latest_session:
            latest[call.stock_code] = call  # rows are id-ordered, so the last run of the day wins
    result = list(latest.values())
    for call in result:
        call.closes = _recent_closes(db, call.stock_code, latest_session)
    result.sort(key=lambda c: (-(c.score if c.score is not None else -1), _ACTION_RANK.get(c.action, 99), c.stock_code))
    return latest_session, result


def count_tracked_calls(db: DatabaseManager, *, today: Optional[date] = None, lookback_days: int = 90) -> int:
    """Daily-analysis calls saved in the lookback, one per stock per session (repeat runs count once)."""

    as_of = today or date.today()
    since = datetime.combine(as_of - timedelta(days=max(1, lookback_days)), time.min)
    with db.get_session() as session:
        rows = session.execute(
            select(DecisionSignalRecord.stock_code, DecisionSignalRecord.metadata_json, DecisionSignalRecord.created_at)
            .where(DecisionSignalRecord.created_at >= since)
            .where(DecisionSignalRecord.source_type == "analysis")
        ).all()
    return len({(code, signal_session_date(meta, created)) for code, meta, created in rows})


def _recent_closes(db: DatabaseManager, code: str, session_day: date) -> List[Tuple[date, float]]:
    try:
        bars = db.get_data_range(code, session_day - timedelta(days=SPARKLINE_SESSIONS * 2 + 10), session_day)
    except Exception as exc:  # noqa: BLE001 - a missing chart must not drop the card
        logger.debug("[dashboard] no bars for %s: %s", code, exc)
        return []
    closes = [(bar.date, float(bar.close)) for bar in bars if _finite(bar.close)]
    return closes[-SPARKLINE_SESSIONS:]


def load_day_reports(reports_dir: Path, day: date) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """Return ([(file name, markdown)] stock reports, market recap markdown) for one day."""

    stamp = day.strftime("%Y%m%d")
    stock_reports: List[Tuple[str, str]] = []
    recap: Optional[str] = None
    if not reports_dir.is_dir():
        return stock_reports, recap
    combined = reports_dir / f"report_{stamp}.md"
    # The combined daily report already holds every stock; per-stock files are a fallback.
    paths = [combined] if combined.is_file() else sorted(reports_dir.glob(f"report_{stamp}_*.md"))
    for path in paths:
        stock_reports.append((path.name, _read_text(path)))
    recap_path = reports_dir / f"market_review_{stamp}.md"
    if recap_path.is_file():
        recap = _read_text(recap_path)
    return [(name, text) for name, text in stock_reports if text.strip()], (recap if recap and recap.strip() else None)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[dashboard] cannot read %s: %s", path, exc)
        return ""


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------

def render_dashboard(
    *,
    calls: Sequence[DashboardCall],
    session_day: Optional[date],
    scorecard: Optional[Mapping[str, Any]],
    stock_reports: Sequence[Tuple[str, str]] = (),
    market_recap: Optional[str] = None,
    language: Optional[str] = "en",
    generated_at: Optional[datetime] = None,
    tracked_calls: Optional[int] = None,
) -> str:
    """Render the whole dashboard as one HTML document (no scripts, no network)."""

    lang = "en" if normalize_report_language(language) == "en" else "zh"
    labels = _LABELS[lang]
    market = _dominant_market(calls)
    generated = _format_generated(generated_at, market)
    day_text = session_day.isoformat() if session_day else "—"

    meta = [labels["stocks"].format(n=len(calls)), labels["session"].format(d=day_text), labels["generated"].format(t=generated)]
    chips = _action_chips(calls, labels)
    report_ids = {call.stock_code: _anchor(call.stock_code) for call in calls}

    parts = [
        "<header>",
        f"<h1>{_e(labels['title'])} <span class='muted'>· {_e(day_text)}</span></h1>",
        f"<p class='sub'>{' · '.join(_e(m) for m in meta)}</p>",
        chips,
        "</header>",
        f"<section><h2>{_e(labels['calls'])}</h2>",
        _render_calls(calls, labels, report_ids) if calls else f"<p class='empty'>{_e(labels['no_calls'])}</p>",
        "</section>",
        _render_scorecard(scorecard, labels, tracked_calls),
    ]
    if stock_reports or market_recap:
        parts.append(_render_reports(stock_reports, labels, report_ids, market_recap=market_recap))
    parts.append(f"<footer>{_e(labels['footer'])}</footer>")

    title = f"{labels['title']} · {day_text}"
    return (
        "<!doctype html>\n"
        f"<html lang='{labels['html_lang']}'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta http-equiv='Content-Security-Policy' "
        "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
        "<meta name='referrer' content='no-referrer'>"
        f"<title>{_e(title)}</title><style>{_CSS}</style></head>"
        f"<body><main class='wrap'>{''.join(parts)}</main></body></html>\n"
    )


def dashboard_caption(calls: Sequence[DashboardCall], session_day: Optional[date], language: Optional[str]) -> str:
    lang = "en" if normalize_report_language(language) == "en" else "zh"
    labels = _LABELS[lang]
    counts: Dict[str, int] = {}
    for call in calls:
        counts[call.action] = counts.get(call.action, 0) + 1
    summary = " · ".join(
        f"{labels['actions'].get(action, action)} {n}"
        for action, n in sorted(counts.items(), key=lambda item: _ACTION_RANK.get(item[0], 99))
    ) or labels["no_calls"]
    return labels["caption"].format(d=session_day.isoformat() if session_day else "—", summary=summary)[:1000]


def _action_chips(calls: Sequence[DashboardCall], labels: Mapping[str, Any]) -> str:
    counts: Dict[str, int] = {}
    for call in calls:
        counts[call.action] = counts.get(call.action, 0) + 1
    if not counts:
        return ""
    chips = [
        f"<span class='pill {_tone(action)}'>{_e(labels['actions'].get(action, action))} {n}</span>"
        for action, n in sorted(counts.items(), key=lambda item: _ACTION_RANK.get(item[0], 99))
    ]
    return f"<div class='chips'>{''.join(chips)}</div>"


def _render_calls(calls: Sequence[DashboardCall], labels: Mapping[str, Any], report_ids: Mapping[str, str]) -> str:
    cards = []
    for call in calls:
        currency = _CURRENCY.get(call.market, "")
        name = call.stock_name or call.stock_code
        change = call.change_pct
        change_html = ""
        if change is not None:
            tone = "up" if change > 0 else "down" if change < 0 else "muted"
            change_html = f"<span class='chg {tone}'>{_signed(change)}%</span>"
        price_html = (
            f"<span class='price'>{_e(currency)}{_num(call.last_close)}</span>{change_html}"
            if call.last_close is not None else ""
        )
        facts = [
            f"{_e(labels['score'])} <b>{call.score if call.score is not None else '—'}</b>",
            f"{_e(labels['confidence'])} <b>{_e(labels['conf_names'].get(confidence_level(call.confidence), '—'))}</b>",
        ]
        if call.horizon:
            facts.append(f"{_e(labels['window'])} <b>{_e(str(call.horizon))}</b>")
        levels = []
        if call.entry_low is not None or call.entry_high is not None:
            low, high = call.entry_low, call.entry_high
            entry = _num(low) if high is None or low == high else _num(high) if low is None else f"{_num(low)}–{_num(high)}"
            levels.append(f"<span class='lv entry'>{_e(labels['entry'])} {_e(currency)}{entry}</span>")
        if call.stop_loss is not None:
            levels.append(f"<span class='lv stop'>{_e(labels['stop'])} {_e(currency)}{_num(call.stop_loss)}</span>")
        if call.target_price is not None:
            levels.append(f"<span class='lv target'>{_e(labels['target'])} {_e(currency)}{_num(call.target_price)}</span>")
        reason = f"<p class='reason'>{_e(call.reason.strip())}</p>" if call.reason and call.reason.strip() else ""
        link = (
            f"<a class='more' href='#{report_ids[call.stock_code]}'>{_e(labels['full'])} ↓</a>"
            if call.stock_code in report_ids else ""
        )
        cards.append(
            "<article class='card call'>"
            "<div class='call-head'>"
            f"<div><div class='name'>{_e(name)}</div><div class='code'>{_e(call.stock_code)}</div></div>"
            f"<span class='pill {_tone(call.action)}'>{_e(labels['actions'].get(call.action, call.action or '—'))}</span>"
            "</div>"
            f"<div class='quote'>{price_html}</div>"
            f"{_sparkline(call)}"
            f"<div class='facts'>{' · '.join(facts)}</div>"
            f"<div class='levels'>{''.join(levels)}</div>"
            f"{reason}{link}"
            "</article>"
        )
    return f"<div class='grid'>{''.join(cards)}</div>"


def _sparkline(call: DashboardCall) -> str:
    closes = [value for _, value in call.closes]
    if len(closes) < 2:
        return ""
    levels = [v for v in (call.stop_loss, call.target_price, call.entry_low, call.entry_high) if _finite(v)]
    lo = min(closes + levels)
    hi = max(closes + levels)
    if hi - lo <= 0:
        hi, lo = hi + 1, lo - 1
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    width, height = 300.0, 72.0

    def y(value: float) -> float:
        return round(height - (value - lo) / (hi - lo) * height, 2)

    step = width / (len(closes) - 1)
    points = " ".join(f"{round(i * step, 2)},{y(v)}" for i, v in enumerate(closes))
    shapes = []
    if _finite(call.entry_low) or _finite(call.entry_high):
        a = call.entry_low if _finite(call.entry_low) else call.entry_high
        b = call.entry_high if _finite(call.entry_high) else call.entry_low
        top, bottom = y(max(a, b)), y(min(a, b))
        shapes.append(
            f"<rect class='s-entry' x='0' y='{top}' width='{width}' height='{max(1.5, round(bottom - top, 2))}'/>"
        )
    if _finite(call.stop_loss):
        shapes.append(f"<line class='s-stop' x1='0' x2='{width}' y1='{y(call.stop_loss)}' y2='{y(call.stop_loss)}'/>")
    if _finite(call.target_price):
        shapes.append(
            f"<line class='s-target' x1='0' x2='{width}' y1='{y(call.target_price)}' y2='{y(call.target_price)}'/>"
        )
    trend = "up" if closes[-1] >= closes[0] else "down"
    last_x, last_y = round((len(closes) - 1) * step, 2), y(closes[-1])
    return (
        f"<svg class='spark' viewBox='0 0 {int(width)} {int(height)}' preserveAspectRatio='none' role='img' "
        f"aria-label='{len(closes)} recent closes'>"
        f"{''.join(shapes)}"
        f"<polyline class='s-line {trend}' points='{points}'/>"
        f"<circle class='s-dot {trend}' cx='{last_x}' cy='{last_y}' r='2.6'/>"
        "</svg>"
    )


def _render_scorecard(
    scorecard: Optional[Mapping[str, Any]],
    labels: Mapping[str, Any],
    tracked_calls: Optional[int] = None,
) -> str:
    head = f"<section><h2>{_e(labels['scorecard'])}</h2>"
    if not scorecard:
        return f"{head}<p class='empty'>{_e(labels['scorecard_missing'])}</p></section>"
    sub = f"<p class='sub'>{_e(labels['scorecard_sub'].format(days=scorecard.get('lookback_days', 90)))}</p>"
    headline = scorecard.get("headline") or {}
    if not headline.get("total"):
        tracked = tracked_calls if tracked_calls is not None else (scorecard.get("total_calls") or 0)
        body = f"<div class='card empty'>{_e(labels['scorecard_empty'])}"
        if tracked:
            body += f"<div class='muted small'>{_e(labels['tracking'].format(n=tracked))}</div>"
        return f"{head}{sub}{body}</div></section>"

    bench = scorecard.get("benchmark_label") or labels["market_index"]
    window = labels["window_names"].get(scorecard.get("headline_horizon"), scorecard.get("headline_horizon"))
    tiles = [
        _tile(
            labels["hit_rate"] + f" · {window}",
            _pct(headline.get("hit_rate_pct")),
            labels["hit_rate_sub"].format(
                hit=headline.get("hit", 0), miss=headline.get("miss", 0), neutral=headline.get("neutral", 0)
            ),
            _rate_tone(headline.get("hit_rate_pct")),
        ),
        _tile(
            labels["beat"].format(bench=bench),
            _pct(headline.get("beat_rate_pct")),
            labels["beat_sub"].format(beat=headline.get("beat", 0), n=headline.get("compared", 0)),
            _rate_tone(headline.get("beat_rate_pct")),
        ),
        _tile(
            labels["edge"].format(bench=bench),
            "—" if headline.get("avg_edge_pct") is None else f"{_signed(headline['avg_edge_pct'])}%",
            labels["edge_sub"],
            "muted" if headline.get("avg_edge_pct") is None else ("up" if headline["avg_edge_pct"] > 0 else "down"),
        ),
    ]
    windows = [
        (labels["window_names"].get(item["key"], item["key"]), item.get("hit_rate_pct"), item.get("hit", 0) + item.get("miss", 0))
        for item in scorecard.get("by_horizon") or []
    ]
    names = scorecard.get("stock_names") or {}
    stocks = [
        (names.get(item["key"]) or item["key"], item.get("hit_rate_pct"), item.get("hit", 0) + item.get("miss", 0))
        for item in scorecard.get("by_stock") or []
    ]
    confidence = [
        (labels["conf_names"].get(item["key"], item["key"]), item.get("hit_rate_pct"), item.get("hit", 0) + item.get("miss", 0))
        for item in scorecard.get("by_confidence") or []
    ]
    verdict = labels["verdict"].get(scorecard.get("confidence_verdict") or "insufficient", "")
    return (
        f"{head}{sub}<div class='tiles'>{''.join(tiles)}</div>"
        "<div class='grid two'>"
        f"{_bar_card(labels['by_window'], windows, labels)}"
        f"{_bar_card(labels['confidence_check'].format(window=window), confidence, labels, note=verdict)}"
        "</div>"
        f"{_bar_card(labels['by_stock'].format(window=window), stocks, labels)}"
        "</section>"
    )


def _tile(label: str, value: str, sub: str, tone: str) -> str:
    return (
        f"<div class='card tile'><div class='t-label'>{_e(label)}</div>"
        f"<div class='t-value {tone}'>{_e(value)}</div><div class='t-sub'>{_e(sub)}</div></div>"
    )


def _bar_card(title: str, rows: Sequence[Tuple[str, Optional[float], int]], labels: Mapping[str, Any], note: str = "") -> str:
    if not rows:
        return ""
    lines = []
    for label, rate, decided in rows:
        if rate is None:
            bar = "<div class='bar'><span class='mid'></span></div>"
            value = f"<span class='muted'>{_e(labels['pending'])}</span>"
        else:
            width = max(0.0, min(100.0, float(rate)))
            bar = f"<div class='bar'><i class='{_rate_tone(rate)}' style='width:{width:.1f}%'></i><span class='mid'></span></div>"
            value = f"<b>{_pct(rate)}</b> <span class='muted'>{_e(labels['decided'].format(n=decided))}</span>"
        lines.append(f"<div class='row'><span class='r-label'>{_e(label)}</span>{bar}<span class='r-val'>{value}</span></div>")
    note_html = f"<p class='note'>{_e(note)}</p>" if note else ""
    return (
        f"<div class='card bars'><div class='b-head'><h3>{_e(title)}</h3>"
        f"<span class='muted small'>{_e(labels['coin_flip'])}</span></div>{''.join(lines)}{note_html}</div>"
    )


def _render_reports(
    stock_reports: Sequence[Tuple[str, str]],
    labels: Mapping[str, Any],
    report_ids: Mapping[str, str],
    *,
    market_recap: Optional[str] = None,
) -> str:
    blocks = []
    if market_recap:
        title = _h1_title(market_recap) or labels["market_recap"]
        blocks.append(
            f"<details class='card report'><summary>{_markdown_inline(title)}</summary>"
            f"<div class='prose'>{_markdown(_drop_h1(market_recap))}</div></details>"
        )
    used_ids: set = set()
    for _, text in stock_reports:
        for heading, body in _split_sections(text):
            anchor = ""
            for code, anchor_id in report_ids.items():
                if anchor_id not in used_ids and f"({code})" in heading:
                    anchor = anchor_id
                    used_ids.add(anchor_id)
                    break
            title = heading or labels["summary"]
            id_attr = f" id='{anchor}'" if anchor else ""
            blocks.append(
                f"<details class='card report'{id_attr}><summary>{_markdown_inline(title)}</summary>"
                f"<div class='prose'>{_markdown(body)}</div></details>"
            )
    return f"<section><h2>{_e(labels['reports'])}</h2>{''.join(blocks)}</section>"


def _split_sections(markdown_text: str) -> List[Tuple[str, str]]:
    """Split a report at ``## `` headings; the H1 title line is dropped."""

    text = _clean_markdown(markdown_text)
    sections: List[Tuple[str, List[str]]] = [("", [])]
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif line.startswith("# ") and not sections[-1][0] and not any(sections[-1][1]):
            continue
        else:
            sections[-1][1].append(line)
    result = []
    for heading, lines in sections:
        body = "\n".join(lines).strip().strip("-").strip()
        if heading or body:
            result.append((heading, body))
    if len(result) > 1 and not result[0][0]:
        result = result[1:]  # the untitled intro repeats the action counts shown in the header
    return result


def _clean_markdown(text: str) -> str:
    return strip_markdown_reference_definitions(strip_hidden_markdown_metadata(text or ""))


def _h1_title(text: str) -> str:
    for line in _clean_markdown(text).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _drop_h1(text: str) -> str:
    return "\n".join(line for line in _clean_markdown(text).splitlines() if not line.startswith("# "))


def _markdown(text: str) -> str:
    # safe_mode escapes raw HTML from LLM / news text; target-blank-links adds rel="noopener".
    return str(
        markdown2.markdown(
            text or "",
            safe_mode="escape",
            extras=["tables", "fenced-code-blocks", "break-on-newline", "cuddled-lists", "target-blank-links"],
        )
    )


def _markdown_inline(text: str) -> str:
    rendered = _markdown(text).strip()
    if rendered.startswith("<p>") and rendered.endswith("</p>"):
        rendered = rendered[3:-4]
    return rendered


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _num(value: Optional[float]) -> str:
    if not _finite(value):
        return "—"
    value = float(value)
    return f"{value:,.2f}" if abs(value) < 100000 else f"{value:,.0f}"


def _signed(value: float) -> str:
    rounded = round(float(value), 2)
    if rounded == 0:
        return "0.00"
    return f"{rounded:+.2f}"


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{float(value):.0f}%"


def _rate_tone(rate: Optional[float]) -> str:
    if rate is None:
        return "muted"
    return "up" if rate >= 55 else "down" if rate < 45 else "warn"


def _tone(action: str) -> str:
    return _ACTION_TONE.get(action, "muted")


def _anchor(code: str) -> str:
    return "r-" + re.sub(r"[^a-z0-9]+", "-", code.lower()).strip("-")


def _dominant_market(calls: Sequence[DashboardCall]) -> str:
    counts: Dict[str, int] = {}
    for call in calls:
        counts[call.market] = counts.get(call.market, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def _format_generated(generated_at: Optional[datetime], market: str) -> str:
    moment = generated_at or datetime.now().astimezone()
    tz_name = _MARKET_TZ.get(market)
    if tz_name and moment.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo

            moment = moment.astimezone(ZoneInfo(tz_name))
        except Exception:  # noqa: BLE001 - fall back to the given time zone
            pass
    zone = moment.strftime("%Z") if moment.tzinfo is not None else ""
    if tz_name == "Asia/Kolkata":
        zone = "IST"
    return f"{moment:%Y-%m-%d %H:%M} {zone}".strip()


_CSS = """
:root{--bg:#f5f6f8;--card:#fff;--ink:#141820;--muted:#5d6675;--line:#e2e5ea;--soft:#eef1f5;
--up:#0e7f49;--up-bg:#e3f4ea;--down:#c23a30;--down-bg:#fbe7e5;--warn:#9a5b00;--warn-bg:#fbf0dc;
--info:#2d5bc4;--info-bg:#e6edfb;--entry:rgba(45,91,196,.2)}
@media (prefers-color-scheme:dark){:root{--bg:#0d1015;--card:#151a21;--ink:#e7eaef;--muted:#99a3b1;
--line:#28303b;--soft:#1d232c;--up:#43c486;--up-bg:#11301f;--down:#ff776b;--down-bg:#3a1714;
--warn:#e7a948;--warn-bg:#352611;--info:#86a8ff;--info-bg:#18233d;--entry:rgba(134,168,255,.22)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
-webkit-text-size-adjust:100%}
.wrap{max-width:1000px;margin:0 auto;padding:22px 16px 40px}
h1{font-size:22px;line-height:1.25;margin:0 0 4px}
h2{font-size:17px;margin:30px 0 4px}
h3{font-size:14px;margin:0}
.sub{color:var(--muted);font-size:13px;margin:0 0 12px}
.muted{color:var(--muted);font-weight:400}.small{font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.pill{display:inline-block;font-size:12px;font-weight:650;padding:2px 10px;border-radius:999px;
background:var(--soft);color:var(--muted);white-space:nowrap}
.pill.up{background:var(--up-bg);color:var(--up)}.pill.down{background:var(--down-bg);color:var(--down)}
.pill.warn{background:var(--warn-bg);color:var(--warn)}.pill.info{background:var(--info-bg);color:var(--info)}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));margin-top:10px}
.grid.two{grid-template-columns:repeat(auto-fit,minmax(300px,1fr));margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}
.call{display:flex;flex-direction:column;gap:8px}
.call-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.name{font-weight:650;line-height:1.3}.code{color:var(--muted);font-size:12px}
.quote{display:flex;align-items:baseline;gap:8px;font-variant-numeric:tabular-nums}
.price{font-size:20px;font-weight:650}.chg{font-size:13px;font-weight:600}
.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}
.spark{width:100%;height:72px;display:block;overflow:visible}
.s-line{fill:none;stroke-width:1.8;vector-effect:non-scaling-stroke;stroke-linejoin:round;stroke-linecap:round}
.s-line.up{stroke:var(--up)}.s-line.down{stroke:var(--down)}
.s-dot.up{fill:var(--up)}.s-dot.down{fill:var(--down)}
.s-entry{fill:var(--entry)}
.s-stop,.s-target{stroke-width:1.2;stroke-dasharray:4 3;vector-effect:non-scaling-stroke}
.s-stop{stroke:var(--down)}.s-target{stroke:var(--up)}
.facts{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}.facts b{color:var(--ink)}
.levels{display:flex;flex-wrap:wrap;gap:6px}
.lv{font-size:12px;padding:2px 8px;border-radius:6px;background:var(--soft);font-variant-numeric:tabular-nums}
.lv.stop{color:var(--down)}.lv.target{color:var(--up)}.lv.entry{color:var(--info)}
.reason{margin:0;font-size:13.5px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.more{font-size:13px;color:var(--info);text-decoration:none;margin-top:auto}
.tiles{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));margin:10px 0 12px}
.tile .t-label{font-size:12.5px;color:var(--muted)}
.tile .t-value{font-size:28px;font-weight:700;line-height:1.2;font-variant-numeric:tabular-nums}
.tile .t-value.muted{color:var(--muted)}
.tile .t-sub{font-size:12px;color:var(--muted)}
.bars{margin-bottom:12px}.grid.two .bars{margin-bottom:0}
.b-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:8px}
.row{display:grid;grid-template-columns:minmax(70px,30%) 1fr auto;align-items:center;gap:10px;padding:5px 0;font-size:13px}
.r-label{line-height:1.25;overflow-wrap:anywhere}
.r-val{font-variant-numeric:tabular-nums;white-space:nowrap;text-align:right}
.bar{position:relative;height:10px;border-radius:5px;background:var(--soft);overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:5px;background:var(--muted)}
.bar i.up{background:var(--up)}.bar i.down{background:var(--down)}.bar i.warn{background:var(--warn)}
.bar .mid{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--ink);opacity:.35}
.note{margin:10px 0 0;font-size:13px}
.empty{color:var(--muted)}.empty .small{margin-top:6px}
details.report{margin-top:10px;padding:0}
details.report summary{cursor:pointer;padding:12px 14px;font-weight:650;list-style-position:inside}
details.report[open] summary{border-bottom:1px solid var(--line)}
.prose{padding:4px 14px 12px;font-size:14px;overflow-wrap:anywhere}
.card.prose{padding:4px 14px 12px}
.prose h3{font-size:15px;margin:16px 0 6px}.prose h4{font-size:14px;margin:12px 0 4px}
.prose table{border-collapse:collapse;display:block;overflow-x:auto;max-width:100%;font-size:13px;margin:8px 0}
.prose th,.prose td{border:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}
.prose th{white-space:nowrap}.prose td{min-width:64px}
.prose th{background:var(--soft)}
.prose blockquote{margin:8px 0;padding:6px 12px;border-left:3px solid var(--info);background:var(--soft);border-radius:0 6px 6px 0}
.prose a{color:var(--info)}
.prose code{background:var(--soft);padding:1px 4px;border-radius:4px}
.prose hr{border:0;border-top:1px solid var(--line)}
footer{margin-top:28px;color:var(--muted);font-size:12px}
"""


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def run_html_dashboard(
    config: Any,
    *,
    send_notification: bool = True,
    lookback_days: int = 90,
    db_manager: Optional[DatabaseManager] = None,
    reports_dir: Optional[Path] = None,
    scorecard_builder: Any = None,
    telegram_sender: Any = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """``python main.py --dashboard``: build, save and optionally send the dashboard."""

    db = db_manager or DatabaseManager.get_instance()
    as_of = today or date.today()
    language = getattr(config, "report_language", "zh")
    out_dir = reports_dir or REPORTS_DIR

    scorecard: Optional[Dict[str, Any]] = None
    try:
        if scorecard_builder is None:
            from src.services.prediction_scorecard_service import (
                PredictionScorecardService,
                default_fetcher_manager,
            )

            service = PredictionScorecardService(db_manager=db, fetcher_manager=default_fetcher_manager())
            scorecard = service.build(lookback_days=lookback_days, today=as_of)
        else:
            scorecard = scorecard_builder(lookback_days=lookback_days, today=as_of)
    except Exception as exc:  # noqa: BLE001 - the dashboard still shows calls and reports
        logger.warning("[dashboard] scorecard unavailable: %s", exc)

    try:
        session_day, calls = load_latest_calls(db, today=as_of)
        tracked: Optional[int] = count_tracked_calls(db, today=as_of, lookback_days=lookback_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[dashboard] could not load calls: %s", exc)
        session_day, calls, tracked = None, [], None

    stock_reports, recap = load_day_reports(out_dir, as_of)
    if not stock_reports and session_day and session_day != as_of:
        stock_reports, recap = load_day_reports(out_dir, session_day)

    document = render_dashboard(
        calls=calls,
        session_day=session_day,
        scorecard=scorecard,
        stock_reports=stock_reports,
        market_recap=recap,
        language=language,
        tracked_calls=tracked,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"dashboard_{as_of.strftime('%Y%m%d')}.html"
    path.write_text(document, encoding="utf-8")
    logger.info("[dashboard] saved %s (%d calls, %d reports)", path, len(calls), len(stock_reports))

    sent = False
    fresh = session_day == as_of or bool(stock_reports) or bool(recap)
    if send_notification and not fresh:
        # e.g. a market holiday: the analysis was skipped, so don't resend yesterday's calls.
        logger.info("[dashboard] no new analysis for %s; saved but not sent", as_of.isoformat())
    elif send_notification:
        sent = _send_to_telegram(config, path, dashboard_caption(calls, session_day, language), telegram_sender)
    return {"path": str(path), "calls": len(calls), "reports": len(stock_reports), "sent": sent, "fresh": fresh}


def _send_to_telegram(config: Any, path: Path, caption: str, sender: Any = None) -> bool:
    try:
        if sender is None:
            from src.notification_sender.telegram_sender import TelegramSender

            sender = TelegramSender(config)
        if not sender._is_telegram_configured():
            logger.info("[dashboard] Telegram not configured; file saved only")
            return False
        return bool(sender.send_telegram_document(path.read_bytes(), path.name, caption=caption))
    except Exception as exc:  # noqa: BLE001 - delivery failure never fails the run
        logger.warning("[dashboard] Telegram send failed: %s", exc)
        return False
