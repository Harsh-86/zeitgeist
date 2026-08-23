"""llm-extractor service: llm.queue -> fetch article -> Claude extraction -> extracted.claims.

Canonical processing order for each admitted event is decode -> fetch -> budget ->
extract -> produce. Budget is checked (and spent) only *after* a successful fetch,
so a dead link or a 404 never consumes a day's LLM allowance.
"""

import logging
import sys
from collections.abc import Callable
from pathlib import Path

import anthropic
import httpx

from zeitgeist.articles.fetch import fetch_article_text
from zeitgeist.budget import DailyBudget
from zeitgeist.config import CLAIMS_TOPIC, LLM_TOPIC, Settings
from zeitgeist.kafka_utils import make_consumer, make_producer
from zeitgeist.llm.extract import LlmExtractor, claims_from_llm
from zeitgeist.metrics import get_counter, start_metrics_server
from zeitgeist.models import GdeltEvent

logger = logging.getLogger("zeitgeist.llm")

_LOG_EVERY = 25

LLM_DISPOSITIONS_TOTAL = get_counter(
    "zeitgeist_llm_dispositions_total",
    "llm.queue messages by terminal disposition",
    labelnames=("disposition",),
)
LLM_INPUT_TOKENS_TOTAL = get_counter(
    "zeitgeist_llm_input_tokens_total", "Input tokens consumed by llm-extractor calls"
)
LLM_OUTPUT_TOKENS_TOTAL = get_counter(
    "zeitgeist_llm_output_tokens_total", "Output tokens consumed by llm-extractor calls"
)
LLM_CACHED_TOKENS_TOTAL = get_counter(
    "zeitgeist_llm_cached_tokens_total", "Cache-read input tokens consumed by llm-extractor calls"
)


def process_event(
    raw: bytes,
    http: httpx.Client,
    extractor: LlmExtractor,
    budget: DailyBudget,
    produce: Callable[[bytes], None],
) -> str:
    """Handle one llm.queue message. Returns a disposition string for logging/metrics.

    Order: decode -> fetch -> budget -> extract -> produce. A free failure (bad
    JSON, missing URL, fetch error) never touches the budget; only a successful
    fetch is allowed to spend it, right before the LLM call it's guarding.
    """
    try:
        event = GdeltEvent.from_json(raw)
    except (ValueError, TypeError):
        logger.warning("undecodable message skipped")
        return "undecodable"

    if not event.source_url:
        return "fetch_failed"

    article_text = fetch_article_text(http, event.source_url)
    if article_text is None:
        return "fetch_failed"

    if not budget.try_spend():
        return "budget_exhausted"

    llm_claims, usage = extractor.extract(event, article_text)
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    cached_tokens = usage.get("cache_read_input_tokens") or 0
    LLM_INPUT_TOKENS_TOTAL.inc(input_tokens)
    LLM_OUTPUT_TOKENS_TOTAL.inc(output_tokens)
    LLM_CACHED_TOKENS_TOTAL.inc(cached_tokens)
    logger.info("llm call: in=%d out=%d cached=%d", input_tokens, output_tokens, cached_tokens)

    if not llm_claims:
        return "no_claims"

    for claim in claims_from_llm(event, llm_claims):
        produce(claim.to_json())

    return "extracted"


def process_one(
    http: httpx.Client,
    extractor: LlmExtractor,
    budget: DailyBudget,
    producer,
    consumer,
    message,
    dispositions: dict[str, int],
) -> str | None:
    """Handle a single polled message: extract, produce, flush, then commit.

    Returns the disposition (also tallied into `dispositions`), or None if the
    message itself was a consumer-level error (nothing to commit).
    """
    if message.error():
        logger.warning("consumer error: %s", message.error())
        return None

    def produce(payload: bytes) -> None:
        producer.produce(CLAIMS_TOPIC, key=message.key(), value=payload)

    disposition = process_event(message.value(), http, extractor, budget, produce)
    undelivered = producer.flush(10)
    if undelivered > 0:
        logger.error(
            "producer flush left %d message(s) undelivered; skipping commit "
            "so this message redelivers (downstream is idempotent)",
            undelivered,
        )
        return None
    consumer.commit(message=message, asynchronous=False)
    dispositions[disposition] = dispositions.get(disposition, 0) + 1
    LLM_DISPOSITIONS_TOTAL.labels(disposition=disposition).inc()
    return disposition


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()

    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY not set; exiting")
        sys.exit(1)

    if settings.metrics_port > 0:
        start_metrics_server(settings.metrics_port)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    extractor = LlmExtractor(client, settings.llm_model)
    consumer = make_consumer(
        settings.kafka_bootstrap, LLM_TOPIC, group_id="llm-extractor", auto_commit=False
    )
    producer = make_producer(settings.kafka_bootstrap)
    http = httpx.Client(follow_redirects=True)
    budget = DailyBudget(Path(settings.llm_budget_state_path), settings.llm_max_calls_per_day)

    dispositions: dict[str, int] = {}
    processed = 0
    logger.info("llm-extractor consuming %s", LLM_TOPIC)
    while True:
        message = consumer.poll(1.0)
        if message is None:
            continue
        disposition = process_one(
            http, extractor, budget, producer, consumer, message, dispositions
        )
        if disposition is None:
            continue
        processed += 1
        if processed % _LOG_EVERY == 0:
            logger.info("dispositions so far: %s", dispositions)


if __name__ == "__main__":
    main()
