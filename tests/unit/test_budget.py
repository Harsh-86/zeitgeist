"""Tests for the shared daily spend budget (used by sampler and llm-extractor)."""

import json
import os
from datetime import date, datetime

from zeitgeist.budget import DailyBudget


def local_dt(*args: int) -> datetime:
    """Naive local wall-clock datetime -- pacing deliberately mirrors
    production's naive `datetime.now` clock."""
    return datetime(*args)  # noqa: DTZ001


class FakeClock:
    """Stateful fake `now` callable: tests mutate `.dt` to advance time."""

    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


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


# ---- paced budget (now=... enables clock-proportional allowance) -----------


def test_paced_burst_floor_spendable_just_after_midnight(tmp_path, caplog):
    clock = FakeClock(local_dt(2026, 1, 1, 0, 1, 0))
    budget = DailyBudget(tmp_path / "budget.json", limit=100, now=clock)

    # burst_floor = max(1, ceil(100 * 0.05)) = 5, and floor(100 * frac) is 0
    # at 00:01, so exactly 5 spends are allowed.
    for _ in range(5):
        assert budget.try_spend() is True

    with caplog.at_level("INFO", logger="zeitgeist.budget"):
        assert budget.try_spend() is False

    paced_denials = [r for r in caplog.records if "paced budget" in r.message]
    assert len(paced_denials) == 1
    assert not any("budget exhausted" in r.message for r in caplog.records)


def test_paced_midday_allowance_is_half_the_limit(tmp_path):
    clock = FakeClock(local_dt(2026, 1, 1, 12, 0, 0))
    budget = DailyBudget(tmp_path / "budget.json", limit=100, now=clock)

    # At exactly 12:00, allowance = floor(100 * 0.5) = 50.
    for _ in range(50):
        assert budget.try_spend() is True
    assert budget.try_spend() is False


def test_paced_unused_allowance_accumulates_as_clock_advances(tmp_path):
    clock = FakeClock(local_dt(2026, 1, 1, 6, 0, 0))
    budget = DailyBudget(tmp_path / "budget.json", limit=100, now=clock)

    # At 06:00, allowance = floor(100 * 0.25) = 25.
    for _ in range(25):
        assert budget.try_spend() is True
    assert budget.try_spend() is False

    # By 12:00 the allowance has grown to 50; spending resumes.
    clock.dt = local_dt(2026, 1, 1, 12, 0, 0)
    assert budget.try_spend() is True


def test_paced_near_midnight_nearly_full_limit_and_hard_cap_at_limit(tmp_path, caplog):
    clock = FakeClock(local_dt(2026, 1, 1, 23, 59, 0))
    budget = DailyBudget(tmp_path / "budget.json", limit=100, now=clock)

    # At 23:59, allowance = floor(100 * 86340/86400) = 99.
    for _ in range(99):
        assert budget.try_spend() is True
    assert budget.try_spend() is False

    # Hard cap still enforced at exactly `limit`: a paced budget whose state
    # already holds count == limit is denied via the exhausted path.
    capped_path = tmp_path / "capped.json"
    capped_path.write_text(json.dumps({"date": "2026-01-01", "count": 100}))
    capped = DailyBudget(capped_path, limit=100, now=clock)
    with caplog.at_level("INFO", logger="zeitgeist.budget"):
        assert capped.try_spend() is False
    assert any("budget exhausted" in r.message for r in caplog.records)


def test_paced_midnight_rollover_resets_count_and_restores_burst_floor(tmp_path):
    clock = FakeClock(local_dt(2026, 1, 1, 12, 0, 0))
    budget = DailyBudget(tmp_path / "budget.json", limit=100, now=clock)

    for _ in range(50):
        assert budget.try_spend() is True
    assert budget.try_spend() is False

    # Next day just after midnight: count resets, burst floor spendable again.
    clock.dt = local_dt(2026, 1, 2, 0, 1, 0)
    for _ in range(5):
        assert budget.try_spend() is True
    assert budget.try_spend() is False

    data = json.loads((tmp_path / "budget.json").read_text())
    assert data == {"date": "2026-01-02", "count": 5}


def test_paced_state_file_format_unchanged(tmp_path):
    path = tmp_path / "budget.json"
    clock = FakeClock(local_dt(2026, 1, 1, 12, 0, 0))
    budget = DailyBudget(path, limit=100, now=clock)

    assert budget.try_spend() is True

    data = json.loads(path.read_text())
    assert set(data.keys()) == {"date", "count"}


def test_try_spend_is_thread_safe_under_concurrency(tmp_path):
    # /ask calls try_spend from threadpool threads; the check-then-increment
    # must never let concurrent callers race past the cap.
    import threading

    path = tmp_path / "budget.json"
    budget = DailyBudget(path, limit=10)
    granted = []

    def worker():
        for _ in range(5):
            if budget.try_spend():
                granted.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 10
    data = json.loads(path.read_text())
    assert data["count"] == 10
