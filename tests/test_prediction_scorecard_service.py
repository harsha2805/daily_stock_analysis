# -*- coding: utf-8 -*-
"""Tests for the prediction scorecard built on DecisionSignal outcomes."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

import pytest

from src.config import Config
from src.services.prediction_scorecard_service import (
    PredictionScorecardService,
    ScoredCall,
    attach_benchmarks,
    benchmark_edge,
    benchmark_window_return,
    build_scorecard,
    confidence_level,
    confidence_verdict,
    dedupe_same_day_calls,
    render_markdown,
    run_prediction_scorecard,
    score_band,
    tally,
)
from src.storage import DatabaseManager, DecisionSignalRecord, StockDaily


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------

def _call(**overrides) -> ScoredCall:
    fields = dict(
        signal_id=1,
        stock_code="YESBANK.NS",
        stock_name="Yes Bank",
        market="in",
        action="buy",
        horizon="3d",
        direction="up",
        outcome="hit",
        stock_return_pct=4.0,
        anchor_date=date(2026, 10, 1),
        eval_window_days=3,
        score=70,
        confidence=0.8,
    )
    fields.update(overrides)
    return ScoredCall(**fields)


def test_benchmark_edge_is_direction_aware() -> None:
    # Buy call: the stock must beat the index.
    assert benchmark_edge("up", 4.0, 1.0) == (3.0, True)
    assert benchmark_edge("up", 1.0, 4.0) == (-3.0, False)
    # Hold call also says "own the stock".
    assert benchmark_edge("not_down", 0.5, 1.5) == (-1.0, False)
    # Sell call: right if switching to the index did better than the stock.
    assert benchmark_edge("not_up", -5.0, 1.0) == (6.0, True)
    assert benchmark_edge("not_up", 3.0, 1.0) == (-2.0, False)


def test_benchmark_window_return_walks_index_sessions() -> None:
    closes = [
        (date(2026, 10, 1), 100.0),
        (date(2026, 10, 3), 101.0),  # 10/2 is a holiday: no bar
        (date(2026, 10, 5), 102.0),
        (date(2026, 10, 6), 110.0),
    ]
    # Anchor on a session: three sessions forward -> 110.
    assert benchmark_window_return(closes, date(2026, 10, 1), 3) == pytest.approx(10.0)
    # Anchor on a non-session day uses the last close before it (10/1 -> 100),
    # then walks two sessions forward (10/5 -> 102).
    assert benchmark_window_return(closes, date(2026, 10, 2), 2) == pytest.approx(2.0)
    # Window not finished yet.
    assert benchmark_window_return(closes, date(2026, 10, 5), 3) is None
    # Anchor before any data.
    assert benchmark_window_return(closes, date(2026, 9, 1), 1) is None


def test_confidence_level_and_score_band() -> None:
    assert confidence_level(0.8) == "high"
    assert confidence_level(0.6) == "medium"
    assert confidence_level(0.4) == "low"
    assert confidence_level(80) == "high"  # tolerate 0-100
    assert confidence_level(None) == "unknown"
    assert score_band(85) == "80-100"
    assert score_band(60) == "60-79"
    assert score_band(45) == "40-59"
    assert score_band(20) == "20-39"
    assert score_band(5) == "0-19"
    assert score_band(None) == "unknown"


def test_dedupe_same_day_calls_keeps_latest_run() -> None:
    day = date(2026, 10, 28)
    calls = [
        _call(signal_id=10, anchor_date=day, outcome="miss"),  # manual run
        _call(signal_id=11, anchor_date=day, outcome="hit"),  # late scheduled run, same day
        _call(signal_id=12, anchor_date=day, horizon="5d"),  # other window stays
        _call(signal_id=13, anchor_date=day, stock_code="BEL.NS"),  # other stock stays
        _call(signal_id=14, anchor_date=date(2026, 10, 29)),  # next day stays
        _call(signal_id=15, anchor_date=None),  # unmatched, kept
    ]
    kept, dropped = dedupe_same_day_calls(calls)
    assert dropped == 1
    assert sorted(c.signal_id for c in kept) == [11, 12, 13, 14, 15]
    assert tally(kept).hit == 5


def test_tally_hit_rate_excludes_neutral() -> None:
    result = tally([_call(outcome="hit"), _call(outcome="miss"), _call(outcome="neutral")])
    assert (result.hit, result.miss, result.neutral) == (1, 1, 1)
    assert result.hit_rate_pct == 50.0


def test_confidence_verdict_needs_samples_and_gap() -> None:
    high = tally([_call(outcome="hit")] * 4 + [_call(outcome="miss")])
    low = tally([_call(outcome="hit")] * 2 + [_call(outcome="miss")] * 3)
    assert confidence_verdict({"high": high, "low": low}) == "higher_better"
    assert confidence_verdict({"high": low, "low": high}) == "lower_better"
    assert confidence_verdict({"high": high, "low": high}) == "no_difference"
    small = tally([_call(outcome="hit")] * 2)
    assert confidence_verdict({"high": high, "low": small}) == "insufficient"
    # Too few "low" calls falls back to "medium".
    assert confidence_verdict({"high": high, "low": small, "medium": low}) == "higher_better"


def test_attach_benchmarks_is_fail_open() -> None:
    calls = [_call()]

    def broken_loader(symbol, start, end):
        raise RuntimeError("network down")

    enriched, names, warnings = attach_benchmarks(calls, broken_loader)
    assert enriched[0].beat_benchmark is None
    assert names == {}
    assert warnings == ["benchmark_unavailable:in"]


def test_attach_benchmarks_uses_market_index() -> None:
    requested = []

    def loader(symbol, start, end):
        requested.append(symbol)
        return [
            (date(2026, 10, 1), 100.0),
            (date(2026, 10, 2), 100.5),
            (date(2026, 10, 3), 100.8),
            (date(2026, 10, 6), 101.0),
        ]

    enriched, names, warnings = attach_benchmarks([_call(stock_return_pct=4.0)], loader)
    assert requested == ["^NSEI"]
    assert names == {"in": "Nifty 50"}
    assert warnings == []
    assert enriched[0].benchmark_return_pct == pytest.approx(1.0)
    assert enriched[0].edge_pct == pytest.approx(3.0)
    assert enriched[0].beat_benchmark is True


def test_build_scorecard_and_render_english() -> None:
    calls = [
        _call(signal_id=1, outcome="hit", stock_return_pct=4.0, edge_pct=3.0, beat_benchmark=True),
        _call(signal_id=2, outcome="miss", stock_return_pct=-3.0, edge_pct=-4.0, beat_benchmark=False,
              stock_code="PCJEWELLER.NS", stock_name="PC Jeweller", confidence=0.4, score=55),
        _call(signal_id=3, outcome="neutral", stock_return_pct=0.5, action="hold", direction="not_down",
              horizon="1d", eval_window_days=1),
    ]
    scorecard = build_scorecard(
        calls,
        as_of=date(2026, 10, 30),
        lookback_days=90,
        benchmark_names={"in": "Nifty 50"},
    )
    assert scorecard["headline_horizon"] == "3d"
    assert scorecard["headline"]["total"] == 2
    assert scorecard["headline"]["hit_rate_pct"] == 50.0
    assert scorecard["headline"]["beat_rate_pct"] == 50.0
    assert scorecard["benchmark_label"] == "Nifty 50"
    assert [item["key"] for item in scorecard["by_horizon"]] == ["1d", "3d"]
    assert scorecard["total_calls"] == 3

    text = render_markdown(scorecard, "en")
    assert "Prediction Scorecard" in text
    assert "Hit rate: **50%**" in text
    assert "Beat Nifty 50: **50%**" in text
    assert "Yes Bank (YESBANK.NS)" in text
    assert "Confidence check" in text
    assert "not enough data yet" in text
    assert "Not investment advice" in text


def test_render_empty_scorecard_in_chinese() -> None:
    scorecard = build_scorecard([], as_of=date(2026, 10, 30), lookback_days=90)
    text = render_markdown(scorecard, "zh")
    assert "预测准确率记分卡" in text
    assert "暂无已完成评估的预测" in text


# ---------------------------------------------------------------------------
# end-to-end against a temporary database
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path):
    old_database_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "scorecard.db")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if old_database_path is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_database_path


def _add_signal(db, *, code, action, session_date, confidence=0.8, score=70) -> int:
    with db.session_scope() as session:
        row = DecisionSignalRecord(
            stock_code=code,
            stock_name=code.split(".")[0].title(),
            market="in",
            source_type="analysis",
            source_report_id=1,
            trace_id=f"trace-{code}-{action}-{session_date}",
            market_phase="postmarket",
            trigger_source="cli",
            action=action,
            action_label=action,
            horizon="3d",
            confidence=confidence,
            score=score,
            reason="unit test",
            data_quality_summary_json='{"level": "good"}',
            metadata_json=json.dumps({"market_phase_summary": {"session_date": session_date}}),
            plan_quality="complete",
            status="active",
        )
        session.add(row)
        session.flush()
        return int(row.id)


def _add_bars(db, code, start: date, closes):
    with db.session_scope() as session:
        for offset, close in enumerate(closes):
            day = start + timedelta(days=offset)
            session.add(StockDaily(code=code, date=day, open=close, high=close, low=close, close=close))


def test_service_counts_one_call_per_stock_when_analysis_ran_twice(isolated_db) -> None:
    anchor = date(2026, 10, 1)
    # Same stock analysed twice on the same day (manual run + scheduled run).
    _add_signal(isolated_db, code="YESBANK.NS", action="buy", session_date=anchor.isoformat())
    second = _add_signal(isolated_db, code="YESBANK.NS", action="buy", session_date=anchor.isoformat())
    _add_bars(isolated_db, "YESBANK.NS", anchor, [100.0 + i for i in range(11)])

    service = PredictionScorecardService(
        db_manager=isolated_db, fetcher_manager=None, benchmark_loader=lambda *_: []
    )
    scorecard = service.build(lookback_days=90, today=date(2026, 10, 30))

    assert scorecard["duplicates_ignored"] == 4  # one extra call per window
    assert scorecard["headline"]["total"] == 1
    assert [item["total"] for item in scorecard["by_horizon"]] == [1, 1, 1, 1]
    assert {c.signal_id for c in service.load_scored_calls(anchor - timedelta(days=1))} >= {second}


def test_service_evaluates_all_windows_and_scores_against_benchmark(isolated_db) -> None:
    anchor = date(2026, 10, 1)
    # Buy call on a stock that rises 10% over 10 sessions; index rises 2%.
    _add_signal(isolated_db, code="YESBANK.NS", action="buy", session_date=anchor.isoformat())
    _add_bars(isolated_db, "YESBANK.NS", anchor, [100.0 + i for i in range(11)])
    # Sell call on a stock that also rises: a miss, and worse than the index.
    _add_signal(isolated_db, code="PCJEWELLER.NS", action="sell", session_date=anchor.isoformat(), confidence=0.4)
    _add_bars(isolated_db, "PCJEWELLER.NS", anchor, [20.0 + 0.5 * i for i in range(11)])

    def loader(symbol, start, end):
        assert symbol == "^NSEI"
        return [(anchor + timedelta(days=i), 100.0 + 0.2 * i) for i in range(11)]

    service = PredictionScorecardService(db_manager=isolated_db, fetcher_manager=None, benchmark_loader=loader)
    scorecard = service.build(lookback_days=90, today=date(2026, 10, 30))

    assert scorecard["evaluation"]["evaluated"] == 8  # 2 signals x 4 windows
    assert [item["key"] for item in scorecard["by_horizon"]] == ["1d", "3d", "5d", "10d"]
    headline = scorecard["headline"]
    assert scorecard["headline_horizon"] == "3d"
    assert (headline["hit"], headline["miss"]) == (1, 1)
    assert headline["compared"] == 2
    assert headline["beat"] == 1
    by_stock = {item["key"]: item for item in scorecard["by_stock"]}
    assert by_stock["YESBANK.NS"]["beat_rate_pct"] == 100.0
    assert by_stock["PCJEWELLER.NS"]["beat_rate_pct"] == 0.0
    assert scorecard["benchmark_label"] == "Nifty 50"


class _FakeNotifier:
    def __init__(self):
        self.saved = []
        self.sent = []

    def save_report_to_file(self, content, filename):
        self.saved.append(filename)
        return filename

    def is_available(self):
        return True

    def send(self, content, route_type=None):
        self.sent.append(content)
        return True


class _Cfg:
    report_language = "en"


def test_run_prediction_scorecard_saves_and_sends(isolated_db) -> None:
    notifier = _FakeNotifier()
    result = run_prediction_scorecard(
        _Cfg(),
        notifier=notifier,
        fetcher_manager=object(),  # no get_daily_data needed: no recent signals
        benchmark_loader=None,
        db_manager=isolated_db,
        today=date(2026, 10, 30),
    )
    assert notifier.saved == ["prediction_scorecard_20261030.md"]
    assert len(notifier.sent) == 1
    assert "No checked predictions yet" in notifier.sent[0]
    assert result["headline"]["total"] == 0


def test_run_prediction_scorecard_respects_no_notify(isolated_db) -> None:
    notifier = _FakeNotifier()
    run_prediction_scorecard(
        _Cfg(),
        send_notification=False,
        notifier=notifier,
        fetcher_manager=None,
        benchmark_loader=None,
        db_manager=isolated_db,
        today=date(2026, 10, 30),
    )
    assert notifier.saved and notifier.sent == []
