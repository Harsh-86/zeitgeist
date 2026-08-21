from tests.unit.test_models import make_event
from zeitgeist.extractor.main import Batcher, process_message
from zeitgeist.models import Claim


class RecordingProducer:
    def __init__(self, calls):
        self.calls = calls

    def flush(self, timeout=None):
        self.calls.append(("flush", timeout))
        return 0


class RecordingConsumer:
    def __init__(self, calls):
        self.calls = calls

    def commit(self, asynchronous=False):
        self.calls.append(("commit", asynchronous))


def test_batcher_flushes_before_committing_at_threshold():
    calls = []
    batcher = Batcher(RecordingProducer(calls), RecordingConsumer(calls), threshold=3)
    batcher.record()
    batcher.record()
    assert calls == []
    assert batcher.pending == 2

    batcher.record()

    assert calls == [("flush", 10), ("commit", False)]
    assert batcher.pending == 0


def test_batcher_idle_commit_flushes_before_committing():
    calls = []
    batcher = Batcher(RecordingProducer(calls), RecordingConsumer(calls), threshold=100)
    batcher.record()
    batcher.record()
    assert batcher.pending == 2

    batcher.maybe_commit_idle()

    assert calls == [("flush", 10), ("commit", False)]
    assert batcher.pending == 0


def test_batcher_idle_commit_is_noop_when_nothing_pending():
    calls = []
    batcher = Batcher(RecordingProducer(calls), RecordingConsumer(calls))
    batcher.maybe_commit_idle()
    assert calls == []


def test_process_message_produces_claim_bytes():
    payloads = process_message(make_event().to_json())
    assert len(payloads) == 1
    claim = Claim.from_json(payloads[0])
    assert claim.subject == "UNITED STATES"
    assert claim.relation == "CONSULTED"


def test_process_message_actorless_event_produces_nothing():
    event = make_event(actor1_name=None, actor2_name=None)
    assert process_message(event.to_json()) == []


def test_process_message_garbage_input_produces_nothing():
    assert process_message(b"not json at all") == []
