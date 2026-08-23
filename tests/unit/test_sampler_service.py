"""Routing-table tests for the sampler's score/dedup/budget gate."""

import pytest

from tests.unit.test_models import make_event
from zeitgeist.budget import DailyBudget
from zeitgeist.kafka_utils import Batcher as RealBatcher
from zeitgeist.models import GdeltEvent
from zeitgeist.sampler import main as sampler_main
from zeitgeist.sampler.dedup import RecentKeys
from zeitgeist.sampler.main import process_message

MIN_SCORE = 1.2


class AlwaysUnderBudget:
    """Fake budget that never blocks — used when a test isn't exercising budget logic."""

    def try_spend(self) -> bool:
        return True


class AlwaysExhausted:
    """Fake budget that always reports exhausted."""

    def try_spend(self) -> bool:
        return False


def high_scoring_event(**overrides) -> GdeltEvent:
    # goldstein -9 -> impact 0.9, num_mentions 40 -> prominence 1.0, both actors -> +0.5 => 2.4
    base = {"goldstein": -9.0, "num_mentions": 40}
    base.update(overrides)
    return make_event(**base)


def test_undecodable_message_returns_none_and_undecodable_disposition(caplog):
    event, disposition = process_message(
        b"not json", RecentKeys(), AlwaysUnderBudget(), min_score=MIN_SCORE
    )
    assert event is None
    assert disposition == "undecodable"


def test_low_score_event_returns_none_and_low_score_disposition():
    event_in = make_event(goldstein=0.0, num_mentions=0, actor2_name=None)  # score 0.0
    event, disposition = process_message(
        event_in.to_json(), RecentKeys(), AlwaysUnderBudget(), min_score=MIN_SCORE
    )
    assert event is None
    assert disposition == "low_score"


def test_duplicate_source_url_returns_none_and_deduped_disposition():
    recent = RecentKeys()
    event1 = high_scoring_event(event_id="1", source_url="https://example.com/dup")
    event2 = high_scoring_event(event_id="2", source_url="https://example.com/dup")

    first, first_disposition = process_message(
        event1.to_json(), recent, AlwaysUnderBudget(), min_score=MIN_SCORE
    )
    second, second_disposition = process_message(
        event2.to_json(), recent, AlwaysUnderBudget(), min_score=MIN_SCORE
    )

    assert first == event1
    assert first_disposition == "admitted"
    assert second is None
    assert second_disposition == "deduped"


def test_duplicate_actor_pair_returns_none_and_deduped_disposition():
    recent = RecentKeys()
    event1 = high_scoring_event(
        event_id="1",
        actor1_name="UNITED STATES",
        actor2_name="EUROPEAN CENTRAL BANK",
        event_root_code="04",
        source_url="https://example.com/a",
    )
    event2 = high_scoring_event(
        event_id="2",
        actor1_name="UNITED STATES",
        actor2_name="EUROPEAN CENTRAL BANK",
        event_root_code="04",
        source_url="https://example.com/b",
    )

    first, _ = process_message(event1.to_json(), recent, AlwaysUnderBudget(), min_score=MIN_SCORE)
    second, second_disposition = process_message(
        event2.to_json(), recent, AlwaysUnderBudget(), min_score=MIN_SCORE
    )

    assert first == event1
    assert second is None
    assert second_disposition == "deduped"


def test_budget_exhausted_returns_none_and_budget_denied_disposition():
    event_in = high_scoring_event()
    event, disposition = process_message(
        event_in.to_json(), RecentKeys(), AlwaysExhausted(), min_score=MIN_SCORE
    )
    assert event is None
    assert disposition == "budget_denied"


def test_happy_path_returns_event_and_admitted_disposition():
    event_in = high_scoring_event()
    event, disposition = process_message(
        event_in.to_json(), RecentKeys(), AlwaysUnderBudget(), min_score=MIN_SCORE
    )
    assert event == event_in
    assert disposition == "admitted"


def test_real_budget_integrates_with_process_message(tmp_path):
    from datetime import date

    budget = DailyBudget(tmp_path / "budget.json", limit=1, today=lambda: date(2026, 1, 1))
    event1 = high_scoring_event(event_id="1", source_url="https://example.com/x")
    event2 = high_scoring_event(event_id="2", source_url="https://example.com/y")

    first, first_disposition = process_message(
        event1.to_json(), RecentKeys(), budget, min_score=MIN_SCORE
    )
    second, second_disposition = process_message(
        event2.to_json(), RecentKeys(), budget, min_score=MIN_SCORE
    )

    assert first == event1
    assert first_disposition == "admitted"
    assert second is None
    assert second_disposition == "budget_denied"


# -- metrics instrumentation: process_message never touches counters directly --


def _snapshot_batch_counters() -> dict[str, float]:
    return {
        name: counter._value.get()
        for name, counter in sampler_main._BATCH_COUNTER_BY_DISPOSITION.items()
    }


def test_process_message_never_touches_prometheus_counters():
    """process_message must be commit-agnostic: only main()'s batch accumulator,
    flushed on a successful Batcher commit, may move the durable counters.
    """
    before = _snapshot_batch_counters()
    process_message(b"not json", RecentKeys(), AlwaysUnderBudget(), min_score=MIN_SCORE)
    process_message(
        make_event(goldstein=0.0, num_mentions=0, actor2_name=None).to_json(),
        RecentKeys(),
        AlwaysUnderBudget(),
        min_score=MIN_SCORE,
    )
    process_message(
        high_scoring_event().to_json(), RecentKeys(), AlwaysUnderBudget(), min_score=MIN_SCORE
    )
    after = _snapshot_batch_counters()
    assert after == before


def test_flush_batch_counts_increments_and_resets():
    before_admitted = sampler_main.SAMPLER_ADMITTED_TOTAL._value.get()
    before_deduped = sampler_main.SAMPLER_DEDUPED_TOTAL._value.get()
    batch_counts = {"admitted": 3, "deduped": 2, "low_score": 0, "budget_denied": 0}

    sampler_main._flush_batch_counts(batch_counts)

    assert sampler_main.SAMPLER_ADMITTED_TOTAL._value.get() == before_admitted + 3
    assert sampler_main.SAMPLER_DEDUPED_TOTAL._value.get() == before_deduped + 2
    assert batch_counts == {"admitted": 0, "deduped": 0, "low_score": 0, "budget_denied": 0}


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


def test_admitted_counter_holds_on_undelivered_commit_then_advances_by_batch_on_success(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SAMPLER_BUDGET_STATE_PATH", str(tmp_path / "budget.json"))
    event1 = high_scoring_event(event_id="1", source_url="https://example.com/batch-a")
    event2 = high_scoring_event(
        event_id="2",
        source_url="https://example.com/batch-b",
        actor1_name="JAPAN",
        actor2_name="AUSTRALIA",
        event_root_code="05",
    )
    consumer = ScriptedConsumer([FakeMessage(event1.to_json()), FakeMessage(event2.to_json())])
    producer = ScriptedProducer(flush_script=[3, 0])  # 1st commit fails, 2nd succeeds

    monkeypatch.setattr(sampler_main, "make_consumer", lambda *a, **k: consumer)
    monkeypatch.setattr(sampler_main, "make_producer", lambda bootstrap: producer)
    monkeypatch.setattr(
        sampler_main, "Batcher", lambda p, c: RealBatcher(p, c, threshold=1)
    )

    before = sampler_main.SAMPLER_ADMITTED_TOTAL._value.get()

    with pytest.raises(_StopLoop):
        sampler_main.main()

    # Both messages were admitted; only the second commit actually succeeded, so
    # the durable counter should jump by 2 exactly once -- never by 1 then 1,
    # and never lost.
    assert sampler_main.SAMPLER_ADMITTED_TOTAL._value.get() == before + 2
    assert consumer.committed == 1


# -- main(): metrics server wiring ---------------------------------------------


class _RaisingConsumer:
    def poll(self, timeout):
        raise _StopLoop()


class _NoopProducer:
    def flush(self, timeout=None):
        return 0


def _run_main_one_iteration(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMPLER_BUDGET_STATE_PATH", str(tmp_path / "budget.json"))
    monkeypatch.setattr(sampler_main, "make_consumer", lambda *a, **k: _RaisingConsumer())
    monkeypatch.setattr(sampler_main, "make_producer", lambda bootstrap: _NoopProducer())
    with pytest.raises(_StopLoop):
        sampler_main.main()


def test_main_starts_metrics_server_when_port_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("METRICS_PORT", "9301")
    calls = []
    monkeypatch.setattr(sampler_main, "start_metrics_server", lambda port: calls.append(port))
    _run_main_one_iteration(monkeypatch, tmp_path)
    assert calls == [9301]


def test_main_does_not_start_metrics_server_when_port_is_zero(monkeypatch, tmp_path):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    calls = []
    monkeypatch.setattr(sampler_main, "start_metrics_server", lambda port: calls.append(port))
    _run_main_one_iteration(monkeypatch, tmp_path)
    assert calls == []
