"""Sampler service: raw.events -> score/dedup/budget gate -> llm.queue.

Free-signal scoring (Task 1) picks candidates; the dedup and budget gates here
keep the LLM tier's daily spend predictable regardless of news volume.
"""

import logging
from pathlib import Path

from zeitgeist.budget import DailyBudget
from zeitgeist.config import LLM_TOPIC, RAW_TOPIC, Settings
from zeitgeist.kafka_utils import Batcher, make_consumer, make_producer
from zeitgeist.metrics import get_counter, start_metrics_server
from zeitgeist.models import GdeltEvent
from zeitgeist.sampler.dedup import RecentKeys
from zeitgeist.sampler.scoring import score

logger = logging.getLogger("zeitgeist.sampler")

SAMPLER_ADMITTED_TOTAL = get_counter(
    "zeitgeist_sampler_admitted_total", "Events admitted to the LLM queue after all gates"
)
SAMPLER_DEDUPED_TOTAL = get_counter(
    "zeitgeist_sampler_deduped_total", "Events dropped as a duplicate actor pair or source URL"
)
SAMPLER_LOW_SCORE_TOTAL = get_counter(
    "zeitgeist_sampler_low_score_total",
    "Events dropped for scoring below the admission threshold",
)
SAMPLER_BUDGET_DENIED_TOTAL = get_counter(
    "zeitgeist_sampler_budget_denied_total",
    "Events dropped because the daily LLM budget was exhausted",
)


def process_message(
    raw: bytes, recent: RecentKeys, budget: DailyBudget, min_score: float
) -> tuple[GdeltEvent | None, str]:
    """Returns (event_or_None, disposition). Disposition is one of "undecodable",
    "low_score", "deduped", "budget_denied", "admitted" -- callers own metrics
    accounting (this function never touches the Prometheus counters directly, so
    a message that's later redelivered because the consumer offset wasn't
    committed can't be double-counted).
    """
    try:
        event = GdeltEvent.from_json(raw)
    except (ValueError, TypeError):
        logger.warning("undecodable message skipped")
        return None, "undecodable"

    if score(event) < min_score:
        return None, "low_score"

    actor_key = f"{event.actor1_name}|{event.event_root_code}|{event.actor2_name}"
    actor_dup = recent.seen(actor_key)
    url_dup = recent.seen(event.source_url) if event.source_url else False
    if actor_dup or url_dup:
        return None, "deduped"

    if not budget.try_spend():
        return None, "budget_denied"

    return event, "admitted"


_BATCH_COUNTER_BY_DISPOSITION = {
    "admitted": SAMPLER_ADMITTED_TOTAL,
    "deduped": SAMPLER_DEDUPED_TOTAL,
    "low_score": SAMPLER_LOW_SCORE_TOTAL,
    "budget_denied": SAMPLER_BUDGET_DENIED_TOTAL,
}


def _flush_batch_counts(batch_counts: dict[str, int]) -> None:
    """Folds batch-local disposition counts into the durable Prometheus counters,
    then resets them. Must only be called once a Kafka commit has actually
    succeeded -- see main()'s call sites.
    """
    for disposition, counter in _BATCH_COUNTER_BY_DISPOSITION.items():
        count = batch_counts.get(disposition, 0)
        if count:
            counter.inc(count)
        batch_counts[disposition] = 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    if settings.metrics_port > 0:
        start_metrics_server(settings.metrics_port)
    consumer = make_consumer(
        settings.kafka_bootstrap,
        RAW_TOPIC,
        group_id="sampler",
        auto_commit=False,
        offset_reset="latest",
    )
    producer = make_producer(settings.kafka_bootstrap)
    batcher = Batcher(producer, consumer)
    recent = RecentKeys()
    budget = DailyBudget(
        Path(settings.sampler_budget_state_path), settings.llm_max_calls_per_day
    )
    batch_counts: dict[str, int] = dict.fromkeys(_BATCH_COUNTER_BY_DISPOSITION, 0)
    logger.info("sampler consuming %s", RAW_TOPIC)
    try:
        while True:
            message = consumer.poll(1.0)
            if message is None:
                if batcher.maybe_commit_idle():
                    _flush_batch_counts(batch_counts)
                continue
            if message.error():
                logger.warning("consumer error: %s", message.error())
                continue
            event, disposition = process_message(
                message.value(), recent, budget, settings.sampler_min_score
            )
            if event is not None:
                producer.produce(LLM_TOPIC, key=message.key(), value=event.to_json())
            if disposition in batch_counts:
                batch_counts[disposition] += 1
            producer.poll(0)
            if batcher.record():
                _flush_batch_counts(batch_counts)
    finally:
        producer.flush(10)


if __name__ == "__main__":
    main()
