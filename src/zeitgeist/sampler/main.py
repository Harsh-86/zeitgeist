"""Sampler service: raw.events -> score/dedup/budget gate -> llm.queue.

Free-signal scoring (Task 1) picks candidates; the dedup and budget gates here
keep the LLM tier's daily spend predictable regardless of news volume.
"""

import logging
from pathlib import Path

from zeitgeist.budget import DailyBudget
from zeitgeist.config import LLM_TOPIC, RAW_TOPIC, Settings
from zeitgeist.kafka_utils import Batcher, make_consumer, make_producer
from zeitgeist.models import GdeltEvent
from zeitgeist.sampler.dedup import RecentKeys
from zeitgeist.sampler.scoring import score

logger = logging.getLogger("zeitgeist.sampler")


def process_message(
    raw: bytes, recent: RecentKeys, budget: DailyBudget, min_score: float
) -> GdeltEvent | None:
    try:
        event = GdeltEvent.from_json(raw)
    except (ValueError, TypeError):
        logger.warning("undecodable message skipped")
        return None

    if score(event) < min_score:
        return None

    actor_key = f"{event.actor1_name}|{event.event_root_code}|{event.actor2_name}"
    actor_dup = recent.seen(actor_key)
    url_dup = recent.seen(event.source_url) if event.source_url else False
    if actor_dup or url_dup:
        return None

    if not budget.try_spend():
        return None

    return event


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
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
    logger.info("sampler consuming %s", RAW_TOPIC)
    try:
        while True:
            message = consumer.poll(1.0)
            if message is None:
                batcher.maybe_commit_idle()
                continue
            if message.error():
                logger.warning("consumer error: %s", message.error())
                continue
            event = process_message(
                message.value(), recent, budget, settings.sampler_min_score
            )
            if event is not None:
                producer.produce(LLM_TOPIC, key=message.key(), value=event.to_json())
            producer.poll(0)
            batcher.record()
    finally:
        producer.flush(10)


if __name__ == "__main__":
    main()
