from zeitgeist.kafka_utils import Batcher


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
