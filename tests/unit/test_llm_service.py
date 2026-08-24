"""Disposition-table and commit-ordering tests for the llm-extractor service."""

import httpx
import pytest

from tests.unit.test_models import make_event
from zeitgeist.config import CLAIMS_TOPIC
from zeitgeist.llm import main as llm_main
from zeitgeist.llm.extract import LlmClaim
from zeitgeist.llm.main import main, process_event, process_one
from zeitgeist.models import Claim

FIXTURE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Test Article</title></head>
<body>
<article>
    <h1>Breaking News: Central Bank Raises Rates</h1>
    <p>This is the beginning of the article body with important information.</p>
    <p>The bank cited persistent inflation pressure across the eurozone.</p>
    <p>Analysts expect further tightening later this year given current trends.</p>
</article>
</body>
</html>
"""


def make_http(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_html_client() -> httpx.Client:
    def handler(request):
        return httpx.Response(200, text=FIXTURE_HTML, headers={"content-type": "text/html"})

    return make_http(handler)


def failing_client() -> httpx.Client:
    def handler(request):
        return httpx.Response(500, text="boom")

    return make_http(handler)


class ExplodingHttp:
    """A fake http client that fails the test if it is ever touched."""

    def get(self, *args, **kwargs):
        raise AssertionError("http.get should not have been called")


class AlwaysUnderBudget:
    def __init__(self):
        self.calls = 0

    def try_spend(self) -> bool:
        self.calls += 1
        return True


class AlwaysExhausted:
    def __init__(self):
        self.calls = 0

    def try_spend(self) -> bool:
        self.calls += 1
        return False


class RaisesIfCalledBudget:
    def try_spend(self) -> bool:
        raise AssertionError("budget.try_spend should not have been called")


class FakeExtractor:
    def __init__(self, llm_claims=None, usage=None):
        self._llm_claims = llm_claims if llm_claims is not None else []
        self._usage = usage if usage is not None else {}
        self.calls = []

    def extract(self, event, article_text):
        self.calls.append((event, article_text))
        return self._llm_claims, self._usage


class RaisesIfCalledExtractor:
    def extract(self, event, article_text):
        raise AssertionError("extractor.extract should not have been called")


ONE_CLAIM = [LlmClaim(subject="A", relation="R", object="B", detail="d", confidence=1.0)]
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0}


def recording_produce():
    produced = []
    return produced, produced.append


# ---- process_event: disposition table ----


def test_undecodable_message_returns_undecodable(caplog):
    produced, produce = recording_produce()
    disposition = process_event(
        b"not json", ExplodingHttp(), RaisesIfCalledExtractor(), RaisesIfCalledBudget(), produce
    )
    assert disposition == "undecodable"
    assert produced == []


def test_missing_source_url_returns_fetch_failed_without_touching_http_or_budget():
    event = make_event(source_url=None)
    produced, produce = recording_produce()
    disposition = process_event(
        event.to_json(), ExplodingHttp(), RaisesIfCalledExtractor(), RaisesIfCalledBudget(), produce
    )
    assert disposition == "fetch_failed"
    assert produced == []


def test_fetch_failure_returns_fetch_failed_and_never_spends_budget():
    event = make_event()
    produced, produce = recording_produce()

    disposition = process_event(
        event.to_json(),
        failing_client(),
        RaisesIfCalledExtractor(),
        RaisesIfCalledBudget(),
        produce,
    )

    assert disposition == "fetch_failed"
    assert produced == []


def test_budget_exhausted_after_successful_fetch_does_not_call_extractor():
    event = make_event()
    produced, produce = recording_produce()
    budget = AlwaysExhausted()

    disposition = process_event(
        event.to_json(), ok_html_client(), RaisesIfCalledExtractor(), budget, produce
    )

    assert disposition == "budget_exhausted"
    assert budget.calls == 1
    assert produced == []


def test_no_claims_returns_no_claims():
    event = make_event()
    produced, produce = recording_produce()
    extractor = FakeExtractor(llm_claims=[], usage=USAGE)

    disposition = process_event(
        event.to_json(), ok_html_client(), extractor, AlwaysUnderBudget(), produce
    )

    assert disposition == "no_claims"
    assert produced == []
    assert len(extractor.calls) == 1


def test_extracted_produces_each_claim_with_llm_tier_and_returns_extracted():
    event = make_event()
    produced, produce = recording_produce()
    extractor = FakeExtractor(llm_claims=ONE_CLAIM, usage=USAGE)

    disposition = process_event(
        event.to_json(), ok_html_client(), extractor, AlwaysUnderBudget(), produce
    )

    assert disposition == "extracted"
    assert len(produced) == 1
    claim = Claim.from_json(produced[0])
    assert claim.tier == "llm"
    assert claim.subject == "A"
    assert claim.event_id == f"{event.event_id}-llm-0"


def test_budget_is_spent_exactly_once_per_successful_fetch():
    event = make_event()
    _, produce = recording_produce()
    budget = AlwaysUnderBudget()
    extractor = FakeExtractor(llm_claims=ONE_CLAIM, usage=USAGE)

    process_event(event.to_json(), ok_html_client(), extractor, budget, produce)

    assert budget.calls == 1


def test_usage_log_guards_none_cache_read_tokens(caplog):
    """SDK 1.0: cache_read_input_tokens can be present but None, not just absent."""
    event = make_event()
    _, produce = recording_produce()
    extractor = FakeExtractor(
        llm_claims=ONE_CLAIM,
        usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": None},
    )

    with caplog.at_level("INFO"):
        disposition = process_event(
            event.to_json(), ok_html_client(), extractor, AlwaysUnderBudget(), produce
        )

    assert disposition == "extracted"
    assert any("cached=0" in record.message for record in caplog.records)


def test_usage_log_handles_missing_usage_keys(caplog):
    event = make_event()
    _, produce = recording_produce()
    extractor = FakeExtractor(llm_claims=[], usage={})

    with caplog.at_level("INFO"):
        process_event(event.to_json(), ok_html_client(), extractor, AlwaysUnderBudget(), produce)

    assert any("llm call: in=0 out=0 cached=0" in record.message for record in caplog.records)


# ---- process_one: commit-after-flush ordering ----


class FakeMessage:
    def __init__(self, value=None, key=None, error=None):
        self._value = value
        self._key = key
        self._error = error

    def value(self):
        return self._value

    def key(self):
        return self._key

    def error(self):
        return self._error


class RecordingProducer:
    def __init__(self, order, flush_return=0):
        self._order = order
        self._flush_return = flush_return

    def produce(self, topic, key=None, value=None):
        self._order.append(("produce", topic, key, value))

    def flush(self, timeout=None):
        self._order.append(("flush",))
        return self._flush_return


class RecordingConsumer:
    def __init__(self, order):
        self._order = order

    def commit(self, message=None, asynchronous=False):
        self._order.append(("commit", message))


def test_process_one_produces_then_flushes_then_commits_in_order():
    event = make_event()
    order = []
    producer = RecordingProducer(order)
    consumer = RecordingConsumer(order)
    message = FakeMessage(value=event.to_json(), key=b"k", error=None)
    extractor = FakeExtractor(llm_claims=ONE_CLAIM, usage=USAGE)
    dispositions = {}

    disposition = process_one(
        ok_html_client(), extractor, AlwaysUnderBudget(), producer, consumer, message, dispositions
    )

    assert disposition == "extracted"
    assert dispositions == {"extracted": 1}
    assert order[-2] == ("flush",)
    assert order[-1] == ("commit", message)
    produce_calls = [entry for entry in order if entry[0] == "produce"]
    assert len(produce_calls) == 1
    assert produce_calls[0][1] == CLAIMS_TOPIC
    assert produce_calls[0][2] == b"k"
    flush_index = order.index(("flush",))
    assert all(order.index(call) < flush_index for call in produce_calls)


def test_process_one_does_not_commit_when_flush_reports_undelivered(caplog):
    event = make_event()
    order = []
    producer = RecordingProducer(order, flush_return=3)
    consumer = RecordingConsumer(order)
    message = FakeMessage(value=event.to_json(), key=b"k", error=None)
    extractor = FakeExtractor(llm_claims=ONE_CLAIM, usage=USAGE)
    dispositions = {}

    with caplog.at_level("ERROR"):
        disposition = process_one(
            ok_html_client(),
            extractor,
            AlwaysUnderBudget(),
            producer,
            consumer,
            message,
            dispositions,
        )

    assert disposition is None
    assert dispositions == {}
    assert order[-1] == ("flush",)
    assert not any(entry[0] == "commit" for entry in order)
    assert any("3" in r.message for r in caplog.records)


def test_process_one_skips_and_does_not_commit_on_consumer_error():
    order = []
    producer = RecordingProducer(order)
    consumer = RecordingConsumer(order)
    message = FakeMessage(value=None, key=None, error="broker down")
    dispositions = {}

    disposition = process_one(
        ExplodingHttp(),
        RaisesIfCalledExtractor(),
        RaisesIfCalledBudget(),
        producer,
        consumer,
        message,
        dispositions,
    )

    assert disposition is None
    assert order == []
    assert dispositions == {}


def test_process_one_still_flushes_and_commits_on_undecodable_message():
    order = []
    producer = RecordingProducer(order)
    consumer = RecordingConsumer(order)
    message = FakeMessage(value=b"not json", key=None, error=None)
    dispositions = {}

    disposition = process_one(
        ExplodingHttp(),
        RaisesIfCalledExtractor(),
        RaisesIfCalledBudget(),
        producer,
        consumer,
        message,
        dispositions,
    )

    assert disposition == "undecodable"
    assert order == [("flush",), ("commit", message)]
    assert dispositions == {"undecodable": 1}


def test_process_one_accumulates_dispositions_across_calls():
    order = []
    producer = RecordingProducer(order)
    consumer = RecordingConsumer(order)
    dispositions = {}

    process_one(
        ExplodingHttp(),
        RaisesIfCalledExtractor(),
        RaisesIfCalledBudget(),
        producer,
        consumer,
        FakeMessage(value=b"not json", error=None),
        dispositions,
    )
    process_one(
        ExplodingHttp(),
        RaisesIfCalledExtractor(),
        RaisesIfCalledBudget(),
        producer,
        consumer,
        FakeMessage(value=b"also not json", error=None),
        dispositions,
    )

    assert dispositions == {"undecodable": 2}


# ---- metrics instrumentation ----


def test_extract_call_increments_token_counters_from_usage_dict():
    event = make_event()
    _, produce = recording_produce()
    extractor = FakeExtractor(llm_claims=ONE_CLAIM, usage=USAGE)
    before_in = llm_main.LLM_INPUT_TOKENS_TOTAL._value.get()
    before_out = llm_main.LLM_OUTPUT_TOKENS_TOTAL._value.get()
    before_cached = llm_main.LLM_CACHED_TOKENS_TOTAL._value.get()

    process_event(event.to_json(), ok_html_client(), extractor, AlwaysUnderBudget(), produce)

    assert llm_main.LLM_INPUT_TOKENS_TOTAL._value.get() == before_in + USAGE["input_tokens"]
    assert llm_main.LLM_OUTPUT_TOKENS_TOTAL._value.get() == before_out + USAGE["output_tokens"]
    assert llm_main.LLM_CACHED_TOKENS_TOTAL._value.get() == before_cached


def test_usage_with_none_cache_tokens_increments_cached_counter_by_zero():
    event = make_event()
    _, produce = recording_produce()
    extractor = FakeExtractor(
        llm_claims=ONE_CLAIM,
        usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": None},
    )
    before_cached = llm_main.LLM_CACHED_TOKENS_TOTAL._value.get()

    process_event(event.to_json(), ok_html_client(), extractor, AlwaysUnderBudget(), produce)

    assert llm_main.LLM_CACHED_TOKENS_TOTAL._value.get() == before_cached


def test_process_one_increments_disposition_counter_for_its_label():
    event = make_event()
    order = []
    producer = RecordingProducer(order)
    consumer = RecordingConsumer(order)
    message = FakeMessage(value=event.to_json(), key=b"k", error=None)
    extractor = FakeExtractor(llm_claims=ONE_CLAIM, usage=USAGE)
    dispositions = {}
    before = llm_main.LLM_DISPOSITIONS_TOTAL.labels(disposition="extracted")._value.get()

    process_one(
        ok_html_client(), extractor, AlwaysUnderBudget(), producer, consumer, message, dispositions
    )

    assert llm_main.LLM_DISPOSITIONS_TOTAL.labels(disposition="extracted")._value.get() == (
        before + 1
    )


def test_process_one_does_not_increment_disposition_counter_when_commit_skipped():
    event = make_event()
    order = []
    producer = RecordingProducer(order, flush_return=3)
    consumer = RecordingConsumer(order)
    message = FakeMessage(value=event.to_json(), key=b"k", error=None)
    extractor = FakeExtractor(llm_claims=ONE_CLAIM, usage=USAGE)
    dispositions = {}
    before = llm_main.LLM_DISPOSITIONS_TOTAL.labels(disposition="extracted")._value.get()

    process_one(
        ok_html_client(), extractor, AlwaysUnderBudget(), producer, consumer, message, dispositions
    )

    assert llm_main.LLM_DISPOSITIONS_TOTAL.labels(disposition="extracted")._value.get() == before


# ---- main(): missing ANTHROPIC_API_KEY ----


def test_main_exits_nonzero_when_anthropic_api_key_missing(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code != 0
    assert any("ANTHROPIC_API_KEY" in record.message for record in caplog.records)


# ---- main(): metrics server wiring ----


class _StopLoop(Exception):
    """Escapes main()'s infinite loop after the first iteration in tests."""


class _RaisingConsumer:
    def poll(self, timeout):
        raise _StopLoop()


def _run_main_one_iteration(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(llm_main.anthropic, "Anthropic", lambda **k: object())
    monkeypatch.setattr(llm_main, "LlmExtractor", lambda *a, **k: object())
    monkeypatch.setattr(llm_main, "make_consumer", lambda *a, **k: _RaisingConsumer())
    monkeypatch.setattr(llm_main, "make_producer", lambda bootstrap: object())
    with pytest.raises(_StopLoop):
        main()


def test_main_starts_metrics_server_when_port_configured(monkeypatch):
    monkeypatch.setenv("METRICS_PORT", "9303")
    calls = []
    monkeypatch.setattr(llm_main, "start_metrics_server", lambda port: calls.append(port))
    _run_main_one_iteration(monkeypatch)
    assert calls == [9303]


def test_main_does_not_start_metrics_server_when_port_is_zero(monkeypatch):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    calls = []
    monkeypatch.setattr(llm_main, "start_metrics_server", lambda port: calls.append(port))
    _run_main_one_iteration(monkeypatch)
    assert calls == []
