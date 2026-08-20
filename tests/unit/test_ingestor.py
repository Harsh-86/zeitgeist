from tests.unit.test_parser import make_row
from zeitgeist.ingestor.main import DeliveryTracker, IngestorState, run_cycle


class FakeGdeltClient:
    def __init__(self, url, rows):
        self.url = url
        self.rows = rows

    def latest_export_url(self):
        return self.url

    def fetch_rows(self, url):
        return iter(self.rows)


def make_state(tmp_path):
    return IngestorState(tmp_path / "state.json")


def test_run_cycle_publishes_parsed_events(tmp_path):
    sent = []
    client = FakeGdeltClient("http://x/1.export.CSV.zip", [make_row(), "malformed\trow"])
    published, skipped = run_cycle(
        client, make_state(tmp_path), lambda t, k, v: sent.append((t, k))
    )
    assert published == 1
    assert skipped == 1
    assert sent == [("raw.events", "1234567890")]


def test_run_cycle_skips_already_seen_url(tmp_path):
    state = make_state(tmp_path)
    state.save("http://x/1.export.CSV.zip")
    client = FakeGdeltClient("http://x/1.export.CSV.zip", [make_row()])
    assert run_cycle(client, state, lambda t, k, v: None) == (0, 0)


def test_run_cycle_handles_no_url(tmp_path):
    client = FakeGdeltClient(None, [])
    assert run_cycle(client, make_state(tmp_path), lambda t, k, v: None) == (0, 0)


def test_state_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    IngestorState(path).save("http://x/2.export.CSV.zip")
    assert IngestorState(path).last_url == "http://x/2.export.CSV.zip"


def test_run_cycle_does_not_save_state_on_undelivered_messages(tmp_path):
    """When flush returns > 0, state is NOT saved and cycle is retried."""
    state = make_state(tmp_path)
    client = FakeGdeltClient("http://x/1.export.CSV.zip", [make_row()])
    published, skipped = run_cycle(
        client, state, lambda t, k, v: None, flush=lambda: 5
    )
    assert published == 1
    assert skipped == 0
    # State should NOT be saved when messages are undelivered
    assert state.last_url is None


def test_run_cycle_saves_state_when_all_messages_delivered(tmp_path):
    """When flush returns 0, state IS saved normally."""
    state = make_state(tmp_path)
    client = FakeGdeltClient("http://x/1.export.CSV.zip", [make_row()])
    published, skipped = run_cycle(
        client, state, lambda t, k, v: None, flush=lambda: 0
    )
    assert published == 1
    assert skipped == 0
    # State should be saved when all messages are delivered
    assert state.last_url == "http://x/1.export.CSV.zip"


def test_run_cycle_without_flush_still_saves_state(tmp_path):
    """Existing behavior preserved: when flush is None, state is saved."""
    state = make_state(tmp_path)
    client = FakeGdeltClient("http://x/1.export.CSV.zip", [make_row()])
    published, skipped = run_cycle(client, state, lambda t, k, v: None)
    assert published == 1
    assert skipped == 0
    # State should be saved when flush is not provided
    assert state.last_url == "http://x/1.export.CSV.zip"


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


def test_run_cycle_blocks_state_advance_when_hard_failures_counted_via_flush(tmp_path):
    """Simulates main()'s flush() wiring: producer.flush() returns 0 remaining in the
    queue (message was dequeued via the error callback) but the tracked failure count
    is added in, so run_cycle still sees undelivered > 0 and does not advance state.
    """
    state = make_state(tmp_path)
    client = FakeGdeltClient("http://x/1.export.CSV.zip", [make_row()])
    tracker = DeliveryTracker()
    tracker.on_delivery(Exception("hard failure"), "msg-bad")

    def flush() -> int:
        producer_flush_remaining = 0  # e.g. producer.flush(30) returning 0
        return producer_flush_remaining + tracker.failed

    published, skipped = run_cycle(client, state, lambda t, k, v: None, flush=flush)
    assert published == 1
    assert skipped == 0
    assert state.last_url is None
