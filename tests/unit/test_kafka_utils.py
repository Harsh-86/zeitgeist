from zeitgeist.kafka_utils import Batcher


class RecordingProducer:
    def __init__(self, calls, flush_return=0):
        self.calls = calls
        self._flush_return = flush_return

    def flush(self, timeout=None):
        self.calls.append(("flush", timeout))
        return self._flush_return


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


def test_batcher_commit_skips_commit_when_flush_reports_undelivered(caplog):
    calls = []
    producer = RecordingProducer(calls, flush_return=3)
    consumer = RecordingConsumer(calls)
    batcher = Batcher(producer, consumer, threshold=1)

    with caplog.at_level("ERROR"):
        batcher.record()

    assert calls == [("flush", 10)]
    assert not any(c[0] == "commit" for c in calls)
    assert any("3" in r.message for r in caplog.records)
    # pending is kept so a later commit point retries.
    assert batcher.pending == 1


# -- bool-return contract -------------------------------------------------


def test_commit_returns_true_when_consumer_commit_ran():
    calls = []
    batcher = Batcher(RecordingProducer(calls), RecordingConsumer(calls))
    assert batcher.commit() is True


def test_commit_returns_false_when_flush_reports_undelivered():
    calls = []
    producer = RecordingProducer(calls, flush_return=1)
    batcher = Batcher(producer, RecordingConsumer(calls))
    assert batcher.commit() is False


def test_record_returns_none_below_threshold():
    calls = []
    batcher = Batcher(RecordingProducer(calls), RecordingConsumer(calls), threshold=3)
    assert batcher.record() is None
    assert calls == []


def test_record_returns_commit_result_at_threshold():
    calls = []
    batcher = Batcher(RecordingProducer(calls), RecordingConsumer(calls), threshold=1)
    assert batcher.record() is True


def test_record_returns_false_at_threshold_when_undelivered():
    calls = []
    producer = RecordingProducer(calls, flush_return=2)
    batcher = Batcher(producer, RecordingConsumer(calls), threshold=1)
    assert batcher.record() is False


def test_maybe_commit_idle_returns_none_when_nothing_pending():
    calls = []
    batcher = Batcher(RecordingProducer(calls), RecordingConsumer(calls))
    assert batcher.maybe_commit_idle() is None


def test_maybe_commit_idle_returns_commit_result_when_pending():
    calls = []
    batcher = Batcher(RecordingProducer(calls), RecordingConsumer(calls), threshold=100)
    batcher.record()
    assert batcher.maybe_commit_idle() is True
