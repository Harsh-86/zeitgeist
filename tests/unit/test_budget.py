"""Tests for the shared daily spend budget (used by sampler and llm-extractor)."""

import json
from datetime import date

from zeitgeist.budget import DailyBudget


def test_try_spend_true_while_under_limit(tmp_path):
    budget = DailyBudget(tmp_path / "budget.json", limit=3, today=lambda: date(2026, 1, 1))
    assert budget.try_spend() is True
    assert budget.try_spend() is True
    assert budget.try_spend() is True


def test_try_spend_false_once_limit_reached(tmp_path):
    budget = DailyBudget(tmp_path / "budget.json", limit=2, today=lambda: date(2026, 1, 1))
    assert budget.try_spend() is True
    assert budget.try_spend() is True
    assert budget.try_spend() is False
    assert budget.try_spend() is False


def test_resets_on_date_change(tmp_path):
    current = {"value": date(2026, 1, 1)}
    budget = DailyBudget(tmp_path / "budget.json", limit=1, today=lambda: current["value"])
    assert budget.try_spend() is True
    assert budget.try_spend() is False

    current["value"] = date(2026, 1, 2)
    assert budget.try_spend() is True


def test_state_persists_to_disk_in_documented_shape(tmp_path):
    path = tmp_path / "budget.json"
    budget = DailyBudget(path, limit=5, today=lambda: date(2026, 1, 1))
    budget.try_spend()
    budget.try_spend()

    data = json.loads(path.read_text())
    assert data == {"date": "2026-01-01", "count": 2}


def test_state_loaded_from_existing_file(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"date": "2026-01-01", "count": 4}))

    budget = DailyBudget(path, limit=5, today=lambda: date(2026, 1, 1))
    assert budget.try_spend() is True
    assert budget.try_spend() is False


def test_exhaustion_logged_once_then_suppressed(tmp_path, caplog):
    budget = DailyBudget(tmp_path / "budget.json", limit=1, today=lambda: date(2026, 1, 1))
    budget.try_spend()

    with caplog.at_level("INFO", logger="zeitgeist.budget"):
        budget.try_spend()
        first_count = sum("budget exhausted" in r.message for r in caplog.records)
        budget.try_spend()
        second_count = sum("budget exhausted" in r.message for r in caplog.records)

    assert first_count == 1
    assert second_count == 1
