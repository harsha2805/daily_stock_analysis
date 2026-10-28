# -*- coding: utf-8 -*-
"""Tests for the self-contained HTML dashboard (``python main.py --dashboard``)."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from unittest import mock

import pytest

from src.config import Config
from src.notification_sender.telegram_sender import TelegramSender
from src.services.html_dashboard_service import (
    DashboardCall,
    count_tracked_calls,
    dashboard_caption,
    load_day_reports,
    load_latest_calls,
    render_dashboard,
    run_html_dashboard,
)
from src.services.prediction_scorecard_service import ScoredCall, build_scorecard
from src.storage import DatabaseManager, DecisionSignalRecord, StockDaily

DAY = date(2026, 10, 28)


def _call(**overrides) -> DashboardCall:
    fields = dict(
        signal_id=1,
        stock_code="BEL.NS",
        stock_name="BHARAT ELECTRONICS LTD",
        market="in",
        action="buy",
        score=61,
        confidence=0.8,
        horizon="3d",
        entry_low=400.0,
        entry_high=405.0,
        stop_loss=388.0,
        target_price=425.0,
        reason="Breakout above the range.",
        session_date=DAY,
        closes=[(DAY - timedelta(days=1), 400.0), (DAY, 409.35)],
    )
    fields.update(overrides)
    return DashboardCall(**fields)


def _scored(**overrides) -> ScoredCall:
    fields = dict(
        signal_id=1, stock_code="BEL.NS", stock_name="BHARAT ELECTRONICS LTD", market="in", action="buy",
        horizon="3d", direction="up", outcome="hit", stock_return_pct=4.0, anchor_date=DAY,
        eval_window_days=3, score=61, confidence=0.8, benchmark_return_pct=1.0, edge_pct=3.0, beat_benchmark=True,
    )
    fields.update(overrides)
    return ScoredCall(**fields)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def test_render_is_self_contained_and_escapes_untrusted_text() -> None:
    report = (
        "# 🎯 2026-10-28 Decision Dashboard\n\n> intro\n\n"
        "## ⚪ BHARAT ELECTRONICS LTD (BEL.NS)\n\nNews: <script>alert(1)</script> "
        "[bad](javascript:alert(2)) [ok](https://example.com)\n"
    )
    page = render_dashboard(
        calls=[_call(reason="<img src=x onerror=alert(3)>")],
        session_day=DAY,
        scorecard=None,
        stock_reports=[("report_20261028.md", report)],
        language="en",
    )
    assert page.startswith("<!doctype html>")
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert "<script" not in page.lower()  # no scripts at all, untrusted ones escaped
    assert "&lt;script&gt;" in page
    assert "&lt;img src=x onerror=alert(3)&gt;" in page
    assert "javascript:" not in page
    assert "https://example.com" in page and "noopener" in page
    assert "http://" not in page.replace("http://www.w3.org", "")  # nothing loaded from the network
    # card -> report anchor, and the untitled intro is dropped
    assert "href='#r-bel-ns'" in page and "id='r-bel-ns'" in page
    assert "intro" not in page


def test_render_call_card_shows_levels_price_and_sparkline() -> None:
    page = render_dashboard(calls=[_call()], session_day=DAY, scorecard=None, language="en")
    assert "₹409.35" in page and "+2.34%" in page
    assert "Entry ₹400.00–405.00" in page and "Stop ₹388.00" in page and "Target ₹425.00" in page
    assert "<svg class='spark'" in page and "s-stop" in page and "s-target" in page and "s-entry" in page
    assert "pill up'>Buy<" in page
    assert "Confidence <b>High</b>" in page
    assert "Scorecard unavailable" in page


def test_render_scorecard_charts_and_empty_state() -> None:
    calls = [_scored(signal_id=i, outcome="hit" if i % 3 else "miss") for i in range(1, 10)]
    scorecard = build_scorecard(calls, as_of=DAY, lookback_days=90, benchmark_names={"in": "Nifty 50"})
    page = render_dashboard(calls=[_call()], session_day=DAY, scorecard=scorecard, language="en")
    assert "Beat Nifty 50" in page
    assert "67%" in page  # 6 hit / 3 miss
    assert "Hit rate by window" in page and "Confidence check (3 days)" in page and "By stock (3 days)" in page
    assert "50% = coin flip" in page

    empty = {**build_scorecard([], as_of=DAY, lookback_days=90), "total_calls": 6}
    page = render_dashboard(calls=[_call()], session_day=DAY, scorecard=empty, language="en")
    assert "No checked predictions yet" in page and "6 calls tracked so far" in page
    page = render_dashboard(calls=[_call()], session_day=DAY, scorecard=empty, language="en", tracked_calls=12)
    assert "12 calls tracked so far" in page


def test_render_chinese_labels_and_caption() -> None:
    page = render_dashboard(calls=[_call(action="watch")], session_day=DAY, scorecard=None, language="zh")
    assert "<html lang='zh-CN'>" in page and "今日建议" in page and "观望" in page
    caption = dashboard_caption([_call(), _call(stock_code="NHPC.NS", action="sell")], DAY, "en")
    assert caption.startswith("📊 Dashboard · 2026-10-28") and "Buy 1 · Sell 1" in caption


# ---------------------------------------------------------------------------
# data loading
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path):
    old_database_path = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "dashboard.db")
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


def _add_signal(db, *, code, action, session_date, score=60, source_type="analysis") -> int:
    with db.session_scope() as session:
        row = DecisionSignalRecord(
            stock_code=code,
            stock_name=code.split(".")[0],
            market="in",
            source_type=source_type,
            source_report_id=1,
            trace_id=f"t-{code}-{action}-{session_date}-{score}",
            market_phase="postmarket",
            trigger_source="cli",
            action=action,
            action_label=action,
            horizon="3d",
            confidence=0.6,
            score=score,
            stop_loss=10.0,
            target_price=12.0,
            reason="unit test",
            data_quality_summary_json='{"level": "good"}',
            metadata_json=json.dumps({"market_phase_summary": {"session_date": session_date.isoformat()}}),
            plan_quality="complete",
            status="active",
        )
        session.add(row)
        session.flush()
        return int(row.id)


def test_load_latest_calls_keeps_last_run_of_latest_session(isolated_db) -> None:
    _add_signal(isolated_db, code="BEL.NS", action="buy", session_date=DAY - timedelta(days=1))
    _add_signal(isolated_db, code="BEL.NS", action="buy", session_date=DAY, score=50)  # manual run
    later = _add_signal(isolated_db, code="BEL.NS", action="watch", session_date=DAY, score=47)  # scheduled run
    _add_signal(isolated_db, code="NHPC.NS", action="hold", session_date=DAY, score=45)
    _add_signal(isolated_db, code="SUZLON.NS", action="sell", session_date=DAY - timedelta(days=1))  # stale
    _add_signal(isolated_db, code="YESBANK.NS", action="alert", session_date=DAY, source_type="alert")
    with isolated_db.session_scope() as session:
        for offset in range(3):
            session.add(StockDaily(code="BEL.NS", date=DAY - timedelta(days=2 - offset), close=400.0 + offset))

    session_day, calls = load_latest_calls(isolated_db, today=DAY)
    # BEL x2 sessions, NHPC, SUZLON; the repeat BEL run and the alert signal don't count.
    assert count_tracked_calls(isolated_db, today=DAY) == 4

    assert session_day == DAY
    assert [c.stock_code for c in calls] == ["BEL.NS", "NHPC.NS"]  # score order, analysis only
    bel = calls[0]
    assert bel.signal_id == later and bel.action == "watch"
    assert [value for _, value in bel.closes] == [400.0, 401.0, 402.0]
    assert calls[1].closes == []


def test_load_day_reports_prefers_combined_file(tmp_path) -> None:
    (tmp_path / "report_20261028.md").write_text("## A (A.NS)\nbody", encoding="utf-8")
    (tmp_path / "report_20261028_A.NS.md").write_text("## A (A.NS)\nsingle", encoding="utf-8")
    (tmp_path / "market_review_20261028.md").write_text("# India Market Recap\nflat", encoding="utf-8")
    reports, recap = load_day_reports(tmp_path, DAY)
    assert [name for name, _ in reports] == ["report_20261028.md"]
    assert recap.startswith("# India Market Recap")

    (tmp_path / "report_20261028.md").unlink()
    reports, _ = load_day_reports(tmp_path, DAY)
    assert [name for name, _ in reports] == ["report_20261028_A.NS.md"]
    assert load_day_reports(tmp_path / "missing", DAY) == ([], None)


# ---------------------------------------------------------------------------
# CLI entry and delivery
# ---------------------------------------------------------------------------

class _Cfg:
    report_language = "en"


class _FakeSender:
    def __init__(self, configured=True):
        self.configured = configured
        self.sent = []

    def _is_telegram_configured(self):
        return self.configured

    def send_telegram_document(self, data, filename, caption=None):
        self.sent.append((filename, len(data), caption))
        return True


def test_run_saves_and_sends_only_fresh_dashboards(isolated_db, tmp_path) -> None:
    _add_signal(isolated_db, code="BEL.NS", action="buy", session_date=DAY)
    sender = _FakeSender()

    def broken_scorecard(**_kwargs):
        raise RuntimeError("benchmark down")

    result = run_html_dashboard(
        _Cfg(), db_manager=isolated_db, reports_dir=tmp_path, scorecard_builder=broken_scorecard,
        telegram_sender=sender, today=DAY,
    )
    page = (tmp_path / "dashboard_20261028.html").read_text(encoding="utf-8")
    assert result["calls"] == 1 and result["sent"] is True
    assert "Scorecard unavailable" in page  # scorecard failure is fail-open
    assert sender.sent[0][0] == "dashboard_20261028.html" and "Buy 1" in sender.sent[0][2]

    # Next day is a holiday: nothing new ran, so the file is saved but not re-sent.
    holiday = DAY + timedelta(days=1)
    result = run_html_dashboard(
        _Cfg(), db_manager=isolated_db, reports_dir=tmp_path, scorecard_builder=lambda **_: None,
        telegram_sender=sender, today=holiday,
    )
    assert result["sent"] is False and result["fresh"] is False
    assert (tmp_path / "dashboard_20261029.html").is_file() and len(sender.sent) == 1

    # --no-notify and an unconfigured Telegram never send.
    assert run_html_dashboard(
        _Cfg(), send_notification=False, db_manager=isolated_db, reports_dir=tmp_path,
        scorecard_builder=lambda **_: None, telegram_sender=sender, today=DAY,
    )["sent"] is False
    assert run_html_dashboard(
        _Cfg(), db_manager=isolated_db, reports_dir=tmp_path, scorecard_builder=lambda **_: None,
        telegram_sender=_FakeSender(configured=False), today=DAY,
    )["sent"] is False
    assert len(sender.sent) == 1


def test_telegram_send_document_posts_file() -> None:
    cfg = mock.Mock(telegram_bot_token="TOKEN", telegram_chat_id="42", telegram_message_thread_id=None)
    sender = TelegramSender(cfg)
    response = mock.Mock(status_code=200)
    response.json.return_value = {"ok": True}
    with mock.patch("src.notification_sender.telegram_sender.requests.post", return_value=response) as post:
        assert sender.send_telegram_document(b"<html></html>", "dashboard.html", caption="x" * 2000) is True
    url = post.call_args.args[0]
    assert url.endswith("/sendDocument")
    kwargs = post.call_args.kwargs
    assert kwargs["files"]["document"][0] == "dashboard.html"
    assert kwargs["data"]["chat_id"] == "42" and len(kwargs["data"]["caption"]) == 1024

    unconfigured = TelegramSender(mock.Mock(telegram_bot_token=None, telegram_chat_id=None, telegram_message_thread_id=None))
    assert unconfigured.send_telegram_document(b"x", "a.html") is False
