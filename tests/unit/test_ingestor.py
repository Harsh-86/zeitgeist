import json

import httpx
import pytest

from tests.unit.test_parser import make_row
from zeitgeist.gdelt.client import export_url_for, stamp_from_url
from zeitgeist.ingestor import main as ingestor_main
from zeitgeist.ingestor.main import DeliveryTracker, IngestorState, run_cycle


class FakeGdeltClient:
    """Fake client keyed by 14-digit stamp. fetch_map values are either a list of
    row strings, or the literal string "404" to simulate a not-yet-published window.
    """

    def __init__(self, latest_stamp, fetch_map):
        self.latest_stamp = latest_stamp
        self.fetch_map = fetch_map
        self.fetched = []  # stamps actually fetched, in order

    def latest_export_url(self):
        if self.latest_stamp is None:
            return None
        return export_url_for(self.latest_stamp)

    def fetch_rows(self, url):
        stamp = stamp_from_url(url)
        self.fetched.append(stamp)
        value = self.fetch_map[stamp]
        if value == "404":
            request = httpx.Request("GET", url)
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("404", request=request, response=response)
        return iter(value)


def make_state(tmp_path):
    return IngestorState(tmp_path / "state.json")


# -- IngestorState -----------------------------------------------------------


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    IngestorState(path).save("20260818144500")
    assert IngestorState(path).last_stamp == "20260818144500"


def test_state_migrates_old_last_url_format(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {"last_url": "https://data.gdeltproject.org/gdeltv2/20260818143000.export.CSV.zip"}
        )
    )
    assert IngestorState(path).last_stamp == "20260818143000"


def test_state_missing_file_has_no_last_stamp(tmp_path):
    assert make_state(tmp_path).last_stamp is None


# -- run_cycle: first run / no backfill --------------------------------------


def test_run_cycle_first_run_publishes_only_latest_window(tmp_path):
    client = FakeGdeltClient("20260818143000", {"20260818143000": [make_row(), "malformed\trow"]})
    sent = []
    published, skipped = run_cycle(
        client, make_state(tmp_path), lambda t, k, v: sent.append((t, k))
    )
    assert published == 1
    assert skipped == 1
    assert sent == [("raw.events", "1234567890")]
    assert client.fetched == ["20260818143000"]


def test_run_cycle_handles_no_url(tmp_path):
    client = FakeGdeltClient(None, {})
    assert run_cycle(client, make_state(tmp_path), lambda t, k, v: None) == (0, 0)


def test_run_cycle_skips_when_state_already_at_latest(tmp_path):
    state = make_state(tmp_path)
    state.save("20260818143000")
    client = FakeGdeltClient("20260818143000", {"20260818143000": [make_row()]})
    assert run_cycle(client, state, lambda t, k, v: None) == (0, 0)
    assert client.fetched == []


# -- run_cycle: multi-window backfill -----------------------------------------


def test_run_cycle_backfills_multiple_pending_windows_in_order(tmp_path):
    state = make_state(tmp_path)
    state.save("20260818140000")
    stamps = ["20260818141500", "20260818143000", "20260818144500"]
    fetch_map = {s: [make_row()] for s in stamps}
    client = FakeGdeltClient(stamps[-1], fetch_map)
    sent = []
    published, skipped = run_cycle(
        client, state, lambda t, k, v: sent.append((t, k))
    )
    assert client.fetched == stamps
    assert published == 3
    assert skipped == 0
    assert state.last_stamp == stamps[-1]
    assert len(sent) == 3


# -- run_cycle: 404 on the newest (latest) window -----------------------------


def test_run_cycle_latest_window_404_no_state_change_no_exception(tmp_path):
    state = make_state(tmp_path)
    client = FakeGdeltClient("20260818143000", {"20260818143000": "404"})
    published, skipped = run_cycle(client, state, lambda t, k, v: None)
    assert (published, skipped) == (0, 0)
    assert state.last_stamp is None


def test_run_cycle_latest_window_404_past_ten_attempts_still_no_exception(tmp_path):
    state = make_state(tmp_path)
    client = FakeGdeltClient("20260818143000", {"20260818143000": "404"})
    misses = {"20260818143000": 10}
    published, skipped = run_cycle(client, state, lambda t, k, v: None, misses=misses)
    assert (published, skipped) == (0, 0)
    assert misses["20260818143000"] == 11
    assert state.last_stamp is None


# -- run_cycle: 404 on a middle (older) window ---------------------------------


def test_run_cycle_middle_window_404_stops_walk_before_five_attempts(tmp_path):
    state = make_state(tmp_path)
    state.save("20260818140000")
    stamps = ["20260818141500", "20260818143000"]
    fetch_map = {"20260818141500": "404", "20260818143000": [make_row()]}
    client = FakeGdeltClient(stamps[-1], fetch_map)
    misses: dict[str, int] = {}
    published, skipped = run_cycle(client, state, lambda t, k, v: None, misses=misses)
    assert (published, skipped) == (0, 0)
    assert client.fetched == ["20260818141500"]  # newer window NOT fetched
    assert state.last_stamp == "20260818140000"  # unchanged
    assert misses["20260818141500"] == 1


def test_run_cycle_middle_window_404_skipped_after_five_attempts_continues(tmp_path):
    state = make_state(tmp_path)
    state.save("20260818140000")
    stamps = ["20260818141500", "20260818143000"]
    fetch_map = {"20260818141500": "404", "20260818143000": [make_row()]}
    client = FakeGdeltClient(stamps[-1], fetch_map)
    misses = {"20260818141500": 4}  # this attempt makes it the 5th
    sent = []
    published, skipped = run_cycle(
        client, state, lambda t, k, v: sent.append((t, k)), misses=misses
    )
    assert client.fetched == ["20260818141500", "20260818143000"]
    assert state.last_stamp == "20260818143000"
    assert "20260818141500" not in misses
    assert published == 1
    assert skipped == 0


# -- run_cycle: undelivered flush mid-backfill ---------------------------------


def test_run_cycle_flush_undelivered_mid_backfill_stops_walk(tmp_path):
    state = make_state(tmp_path)
    state.save("20260818140000")
    stamps = ["20260818141500", "20260818143000"]
    fetch_map = {s: [make_row()] for s in stamps}
    client = FakeGdeltClient(stamps[-1], fetch_map)
    published, skipped = run_cycle(client, state, lambda t, k, v: None, flush=lambda: 3)
    assert client.fetched == ["20260818141500"]  # stopped after first window
    assert state.last_stamp == "20260818140000"  # unchanged
    assert published == 1  # message was sent even though undelivered
    assert skipped == 0


def test_run_cycle_does_not_save_state_on_undelivered_messages(tmp_path):
    """When flush returns > 0, state is NOT saved and cycle is retried."""
    state = make_state(tmp_path)
    client = FakeGdeltClient("20260818143000", {"20260818143000": [make_row()]})
    published, skipped = run_cycle(client, state, lambda t, k, v: None, flush=lambda: 5)
    assert published == 1
    assert skipped == 0
    assert state.last_stamp is None


def test_run_cycle_saves_state_when_all_messages_delivered(tmp_path):
    """When flush returns 0, state IS saved normally."""
    state = make_state(tmp_path)
    client = FakeGdeltClient("20260818143000", {"20260818143000": [make_row()]})
    published, skipped = run_cycle(client, state, lambda t, k, v: None, flush=lambda: 0)
    assert published == 1
    assert skipped == 0
    assert state.last_stamp == "20260818143000"


def test_run_cycle_without_flush_still_saves_state(tmp_path):
    """Existing behavior preserved: when flush is None, state is saved."""
    state = make_state(tmp_path)
    client = FakeGdeltClient("20260818143000", {"20260818143000": [make_row()]})
    published, skipped = run_cycle(client, state, lambda t, k, v: None)
    assert published == 1
    assert skipped == 0
    assert state.last_stamp == "20260818143000"


def test_run_cycle_blocks_state_advance_when_hard_failures_counted_via_flush(tmp_path):
    """Simulates main()'s flush() wiring: producer.flush() returns 0 remaining in the
    queue (message was dequeued via the error callback) but the tracked failure count
    is added in, so run_cycle still sees undelivered > 0 and does not advance state.
    """
    state = make_state(tmp_path)
    client = FakeGdeltClient("20260818143000", {"20260818143000": [make_row()]})
    tracker = DeliveryTracker()
    tracker.on_delivery(Exception("hard failure"), "msg-bad")

    def flush() -> int:
        producer_flush_remaining = 0  # e.g. producer.flush(30) returning 0
        return producer_flush_remaining + tracker.failed

    published, skipped = run_cycle(client, state, lambda t, k, v: None, flush=flush)
    assert published == 1
    assert skipped == 0
    assert state.last_stamp is None


# -- DeliveryTracker -----------------------------------------------------------


def test_delivery_tracker_counts_failed_deliveries():
    """on_delivery increments failed only when an error is reported."""
    tracker = DeliveryTracker()
    tracker.on_delivery(None, "msg-ok")
    assert tracker.failed == 0
    tracker.on_delivery(Exception("boom"), "msg-bad")
    assert tracker.failed == 1
    tracker.on_delivery(Exception("boom again"), "msg-bad-2")
    assert tracker.failed == 2


def test_delivery_tracker_reset_clears_failed_count():
    tracker = DeliveryTracker()
    tracker.on_delivery(Exception("boom"), "msg-bad")
    assert tracker.failed == 1
    tracker.reset()
    assert tracker.failed == 0


# -- metrics instrumentation ---------------------------------------------------


def test_run_cycle_processed_window_increments_windows_total_and_freshness(tmp_path):
    """A window whose fetch succeeded: windows_total +1 and freshness gauge set."""
    state = make_state(tmp_path)
    client = FakeGdeltClient("20260818143000", {"20260818143000": [make_row()]})
    before_windows = ingestor_main.WINDOWS_TOTAL._value.get()
    run_cycle(client, state, lambda t, k, v: None)
    assert ingestor_main.WINDOWS_TOTAL._value.get() == before_windows + 1
    assert ingestor_main.LAST_SUCCESS_TIMESTAMP._value.get() > 0


def test_run_cycle_increments_published_and_skipped_counters(tmp_path):
    client = FakeGdeltClient(
        "20260818143000", {"20260818143000": [make_row(), "malformed\trow"]}
    )
    before_published = ingestor_main.EVENTS_PUBLISHED_TOTAL._value.get()
    before_skipped = ingestor_main.ROWS_SKIPPED_TOTAL._value.get()
    run_cycle(client, make_state(tmp_path), lambda t, k, v: None)
    assert ingestor_main.EVENTS_PUBLISHED_TOTAL._value.get() == before_published + 1
    assert ingestor_main.ROWS_SKIPPED_TOTAL._value.get() == before_skipped + 1


def test_run_cycle_skip_and_advance_increments_skipped_upstream_and_freshness_not_windows_total(
    tmp_path,
):
    """Skip-and-advance (missing upstream after 5 attempts) still advances state — so
    freshness updates (loop liveness) — but is NOT a processed window, so windows_total
    must stay flat; only windows_skipped_upstream_total counts it. The newest window is
    also left 404 here (not-yet-published) so nothing else in this cycle can touch
    windows_total, isolating the skip-and-advance path's effect.
    """
    state = make_state(tmp_path)
    state.save("20260818140000")
    stamps = ["20260818141500", "20260818143000"]
    fetch_map = {"20260818141500": "404", "20260818143000": "404"}
    client = FakeGdeltClient(stamps[-1], fetch_map)
    misses = {"20260818141500": 4}  # this attempt makes it the 5th: skip-and-advance
    before_skipped_upstream = ingestor_main.WINDOWS_SKIPPED_UPSTREAM_TOTAL._value.get()
    before_windows = ingestor_main.WINDOWS_TOTAL._value.get()
    run_cycle(client, state, lambda t, k, v: None, misses=misses)
    assert ingestor_main.WINDOWS_SKIPPED_UPSTREAM_TOTAL._value.get() == (
        before_skipped_upstream + 1
    )
    assert ingestor_main.WINDOWS_TOTAL._value.get() == before_windows
    assert ingestor_main.LAST_SUCCESS_TIMESTAMP._value.get() > 0
    assert state.last_stamp == "20260818141500"  # skip-and-advance saved this stamp


def test_run_cycle_undelivered_flush_does_not_increment_any_durable_metric(tmp_path):
    """Mirrors test_run_cycle_does_not_save_state_on_undelivered_messages: when flush()
    reports undelivered > 0, state is not saved and the cycle retries next time — so
    none of the durable, save-gated metrics (windows_total, events_published_total,
    rows_skipped_total, freshness) may move. Without this, published/skipped would be
    double-counted on the eventual successful retry of the same window.
    """
    state = make_state(tmp_path)
    client = FakeGdeltClient(
        "20260818143000", {"20260818143000": [make_row(), "malformed\trow"]}
    )
    before_windows = ingestor_main.WINDOWS_TOTAL._value.get()
    before_published = ingestor_main.EVENTS_PUBLISHED_TOTAL._value.get()
    before_skipped = ingestor_main.ROWS_SKIPPED_TOTAL._value.get()
    before_freshness = ingestor_main.LAST_SUCCESS_TIMESTAMP._value.get()

    published, skipped = run_cycle(client, state, lambda t, k, v: None, flush=lambda: 5)

    assert published == 1  # messages were sent even though undelivered
    assert skipped == 1
    assert state.last_stamp is None
    assert ingestor_main.WINDOWS_TOTAL._value.get() == before_windows
    assert ingestor_main.EVENTS_PUBLISHED_TOTAL._value.get() == before_published
    assert ingestor_main.ROWS_SKIPPED_TOTAL._value.get() == before_skipped
    assert ingestor_main.LAST_SUCCESS_TIMESTAMP._value.get() == before_freshness


def test_run_cycle_does_not_increment_windows_total_when_no_state_change(tmp_path):
    state = make_state(tmp_path)
    client = FakeGdeltClient("20260818143000", {"20260818143000": "404"})
    before_windows = ingestor_main.WINDOWS_TOTAL._value.get()
    run_cycle(client, state, lambda t, k, v: None)
    assert ingestor_main.WINDOWS_TOTAL._value.get() == before_windows


# -- main(): metrics server wiring ---------------------------------------------


class _StopLoop(Exception):
    """Escapes main()'s infinite loop after the first iteration in tests."""


def _run_main_one_iteration(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(ingestor_main, "make_producer", lambda bootstrap: object())
    monkeypatch.setattr(ingestor_main, "GdeltClient", lambda http_client: object())
    monkeypatch.setattr(ingestor_main, "run_cycle", lambda *a, **k: (0, 0))
    monkeypatch.setattr(
        ingestor_main.time, "sleep", lambda seconds: (_ for _ in ()).throw(_StopLoop())
    )
    with pytest.raises(_StopLoop):
        ingestor_main.main()


def test_main_starts_metrics_server_when_port_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("METRICS_PORT", "9200")
    calls = []
    monkeypatch.setattr(ingestor_main, "start_metrics_server", lambda port: calls.append(port))
    _run_main_one_iteration(monkeypatch, tmp_path)
    assert calls == [9200]


def test_main_does_not_start_metrics_server_when_port_is_zero(monkeypatch, tmp_path):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    calls = []
    monkeypatch.setattr(ingestor_main, "start_metrics_server", lambda port: calls.append(port))
    _run_main_one_iteration(monkeypatch, tmp_path)
    assert calls == []
