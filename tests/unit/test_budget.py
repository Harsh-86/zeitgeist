"""Tests for the shared daily spend budget (used by sampler and llm-extractor)."""

import json
import os
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


def test_corrupted_state_file_is_treated_as_fresh_and_self_repairs(tmp_path, caplog):
    path = tmp_path / "budget.json"
    path.write_bytes(b"\x00not-json-garbage{{{")

    with caplog.at_level("ERROR", logger="zeitgeist.budget"):
        budget = DailyBudget(path, limit=2, today=lambda: date(2026, 1, 1))

    assert any(str(path) in r.message for r in caplog.records)

    # Fresh state despite the corrupt file: try_spend works normally.
    assert budget.try_spend() is True
    assert budget.try_spend() is True
    assert budget.try_spend() is False

    # The file is repaired (valid JSON) after a successful write.
    data = json.loads(path.read_text())
    assert data == {"date": "2026-01-01", "count": 2}


def test_save_writes_atomically_via_tempfile_and_os_replace(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    budget = DailyBudget(path, limit=5, today=lambda: date(2026, 1, 1))

    replace_calls = []
    real_replace = os.replace

    def spy_replace(src, dst):
        # At the moment of replace, the temp source must already hold the
        # fully-written new content, and it must live in the same directory
        # as the destination (so the replace is on the same filesystem).
        assert os.path.dirname(src) == os.path.dirname(str(dst))
        assert src != str(dst)
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    budget.try_spend()

    assert len(replace_calls) == 1
    data = json.loads(path.read_text())
    assert data == {"date": "2026-01-01", "count": 1}
    # No leftover temp file after a successful replace.
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_no_partial_content_on_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"date": "2026-01-01", "count": 1}))
    budget = DailyBudget(path, limit=5, today=lambda: date(2026, 1, 1))

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    try:
        budget.try_spend()
    except OSError:
        pass

    # Original file content must be untouched (atomic: either fully old or
    # fully new, never truncated/partial).
    data = json.loads(path.read_text())
    assert data == {"date": "2026-01-01", "count": 1}
