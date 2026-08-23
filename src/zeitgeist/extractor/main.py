"""Extractor service: raw.events -> rules tier -> extracted.claims."""

import logging

from zeitgeist.config import CLAIMS_TOPIC, RAW_TOPIC, Settings
from zeitgeist.extractor.rules import event_to_claims
from zeitgeist.kafka_utils import Batcher, make_consumer, make_producer
from zeitgeist.metrics import get_counter, start_metrics_server
from zeitgeist.models import GdeltEvent

logger = logging.getLogger("zeitgeist.extractor")

EXTRACTOR_MESSAGES_TOTAL = get_counter(
    "zeitgeist_extractor_messages_total", "raw.events messages processed by the rules tier"
)
EXTRACTOR_CLAIMS_TOTAL = get_counter(
    "zeitgeist_extractor_claims_total", "Claims produced by the rules tier"
)


def process_message(raw: bytes) -> list[bytes]:
    """Returns the claim payloads produced from one raw.events message (empty on
    undecodable input). Never touches the Prometheus counters directly -- main()
    owns metrics accounting via a batch-local accumulator, folded into the
    durable counters only once a Kafka commit has actually succeeded, so a
    message redelivered after an uncommitted offset can't be double-counted.
    """
    try:
        event = GdeltEvent.from_json(raw)
    except (ValueError, TypeError):
        logger.warning("undecodable message skipped")
        return []
    return [claim.to_json() for claim in event_to_claims(event)]


def _flush_batch_counts(batch_counts: dict[str, int]) -> None:
    """Folds batch-local message/claim counts into the durable Prometheus
    counters, then resets them. Must only be called once a Kafka commit has
    actually succeeded -- see main()'s call sites.
    """
    if batch_counts["messages"]:
        EXTRACTOR_MESSAGES_TOTAL.inc(batch_counts["messages"])
    if batch_counts["claims"]:
        EXTRACTOR_CLAIMS_TOTAL.inc(batch_counts["claims"])
    batch_counts["messages"] = 0
    batch_counts["claims"] = 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    if settings.metrics_port > 0:
        start_metrics_server(settings.metrics_port)
    consumer = make_consumer(
        settings.kafka_bootstrap, RAW_TOPIC, group_id="extractor", auto_commit=False
    )
    producer = make_producer(settings.kafka_bootstrap)
    batcher = Batcher(producer, consumer)
    batch_counts = {"messages": 0, "claims": 0}
    logger.info("extractor consuming %s", RAW_TOPIC)
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
            claims = process_message(message.value())
            batch_counts["messages"] += 1
            batch_counts["claims"] += len(claims)
            for payload in claims:
                producer.produce(CLAIMS_TOPIC, key=message.key(), value=payload)
            producer.poll(0)
            if batcher.record():
                _flush_batch_counts(batch_counts)
    finally:
        producer.flush(10)


if __name__ == "__main__":
    main()
