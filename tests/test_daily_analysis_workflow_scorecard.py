# -*- coding: utf-8 -*-
"""Contract tests: the daily workflow keeps its database and runs the scorecard."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "00-daily-analysis.yml"


def _load():
    text = WORKFLOW.read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def _steps(doc):
    return doc["jobs"]["analyze"]["steps"]


def test_database_is_restored_before_and_saved_after_analysis() -> None:
    _text, doc = _load()
    steps = _steps(doc)
    names = [step.get("name") for step in steps]
    restore = next(step for step in steps if step.get("uses", "").startswith("actions/cache/restore"))
    save = next(step for step in steps if step.get("uses", "").startswith("actions/cache/save"))
    analyze_index = next(i for i, step in enumerate(steps) if "python main.py" in str(step.get("run", "")))

    assert names.index(restore["name"]) < analyze_index < names.index(save["name"])
    assert restore["with"]["path"] == "data/stock_analysis.db"
    assert save["with"]["path"] == "data/stock_analysis.db"
    # Rolling keys: restore falls back to the newest saved database; every save
    # uses a fresh, unique key (cache entries are immutable).
    assert restore["with"]["restore-keys"].strip() == "dsa-db-${{ github.ref_name }}-"
    assert "${{ github.run_id }}" in save["with"]["key"]
    assert "always()" in save["if"]


def test_scorecard_mode_and_weekly_schedule_are_wired() -> None:
    text, doc = _load()
    triggers = doc.get("on") or doc.get(True)  # PyYAML parses bare `on` as True
    options = triggers["workflow_dispatch"]["inputs"]["mode"]["options"]
    assert "scorecard" in options
    assert "python main.py --scorecard" in text
    assert "SCORECARD_WEEKDAY" in text
    assert "TZ=Asia/Kolkata date +%u" in text
    # Runs after the NSE/BSE 15:30 IST close (UTC 10:00) so Indian bars are final.
    cron = triggers["schedule"][0]["cron"]
    minute, hour = cron.split()[:2]
    assert int(hour) * 60 + int(minute) > 10 * 60
