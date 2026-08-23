import pytest

from tests.unit.test_models import make_event
from zeitgeist.extractor import main as extractor_main
from zeitgeist.extractor.main import process_message
from zeitgeist.kafka_utils import Batcher as RealBatcher
from zeitgeist.models import Claim


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


# -- metrics instrumentation: process_message never touches counters directly --


def test_process_message_never_touches_prometheus_counters():
    """process_message must be commit-agnostic: only main()'s batch accumulator,
    flushed on a successful Batcher commit, may move the durable counters.
    """
    before_messages = extractor_main.EXTRACTOR_MESSAGES_TOTAL._value.get()
    before_claims = extractor_main.EXTRACTOR_CLAIMS_TOTAL._value.get()
    process_message(make_event().to_json())
    process_message(b"not json at all")
    assert extractor_main.EXTRACTOR_MESSAGES_TOTAL._value.get() == before_messages
    assert extractor_main.EXTRACTOR_CLAIMS_TOTAL._value.get() == before_claims


def test_flush_batch_counts_increments_and_resets():
    before_messages = extractor_main.EXTRACTOR_MESSAGES_TOTAL._value.get()
    before_claims = extractor_main.EXTRACTOR_CLAIMS_TOTAL._value.get()
    batch_counts = {"messages": 4, "claims": 5}

    extractor_main._flush_batch_counts(batch_counts)

    assert extractor_main.EXTRACTOR_MESSAGES_TOTAL._value.get() == before_messages + 4
    assert extractor_main.EXTRACTOR_CLAIMS_TOTAL._value.get() == before_claims + 5
    assert batch_counts == {"messages": 0, "claims": 0}


# -- main(): batch-local accumulation across commit failure/success ------------


class FakeMessage:
    def __init__(self, value, key=b"k", error=None):
        self._value = value
        self._key = key
        self._error = error

    def value(self):
        return self._value

    def key(self):
        return self._key

    def error(self):
        return self._error


class _StopLoop(Exception):
    """Escapes main()'s infinite loop after the first iteration in tests."""


class ScriptedConsumer:
    """Yields messages from a fixed queue, then raises _StopLoop once exhausted."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.committed = 0

    def poll(self, timeout):
        if self._messages:
            return self._messages.pop(0)
        raise _StopLoop()

    def commit(self, asynchronous=False):
        self.committed += 1


class ScriptedProducer:
    """flush() returns each value from `flush_script` in order, then 0 forever."""

    def __init__(self, flush_script):
        self._flush_script = list(flush_script)

    def produce(self, *args, **kwargs):
        pass

    def poll(self, timeout):
        pass

    def flush(self, timeout=None):
        if self._flush_script:
            return self._flush_script.pop(0)
        return 0


def test_messages_and_claims_hold_on_undelivered_commit_then_advance_on_success(monkeypatch):
    event1 = make_event(event_id="1")
    event2 = make_event(event_id="2")
    consumer = ScriptedConsumer([FakeMessage(event1.to_json()), FakeMessage(event2.to_json())])
    producer = ScriptedProducer(flush_script=[2, 0])  # 1st commit fails, 2nd succeeds

    monkeypatch.setattr(extractor_main, "make_consumer", lambda *a, **k: consumer)
    monkeypatch.setattr(extractor_main, "make_producer", lambda bootstrap: producer)
    monkeypatch.setattr(
        extractor_main, "Batcher", lambda p, c: RealBatcher(p, c, threshold=1)
    )

    before_messages = extractor_main.EXTRACTOR_MESSAGES_TOTAL._value.get()
    before_claims = extractor_main.EXTRACTOR_CLAIMS_TOTAL._value.get()

    with pytest.raises(_StopLoop):
        extractor_main.main()

    # Both messages were processed (1 claim each); only the second commit
    # actually succeeded, so the durable counters should jump by the full
    # accumulated batch exactly once -- never partially, never lost.
    assert extractor_main.EXTRACTOR_MESSAGES_TOTAL._value.get() == before_messages + 2
    assert extractor_main.EXTRACTOR_CLAIMS_TOTAL._value.get() == before_claims + 2
    assert consumer.committed == 1


# -- main(): metrics server wiring ---------------------------------------------


class _RaisingConsumer:
    def poll(self, timeout):
        raise _StopLoop()


class _NoopProducer:
    def flush(self, timeout=None):
        return 0


def _run_main_one_iteration(monkeypatch):
    monkeypatch.setattr(extractor_main, "make_consumer", lambda *a, **k: _RaisingConsumer())
    monkeypatch.setattr(extractor_main, "make_producer", lambda bootstrap: _NoopProducer())
    with pytest.raises(_StopLoop):
        extractor_main.main()


def test_main_starts_metrics_server_when_port_configured(monkeypatch):
    monkeypatch.setenv("METRICS_PORT", "9302")
    calls = []
    monkeypatch.setattr(extractor_main, "start_metrics_server", lambda port: calls.append(port))
    _run_main_one_iteration(monkeypatch)
    assert calls == [9302]


def test_main_does_not_start_metrics_server_when_port_is_zero(monkeypatch):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    calls = []
    monkeypatch.setattr(extractor_main, "start_metrics_server", lambda port: calls.append(port))
    _run_main_one_iteration(monkeypatch)
    assert calls == []
