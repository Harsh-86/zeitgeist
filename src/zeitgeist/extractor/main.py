"""Extractor service: raw.events -> rules tier -> extracted.claims."""

import logging

from zeitgeist.config import CLAIMS_TOPIC, RAW_TOPIC, Settings
from zeitgeist.extractor.rules import event_to_claims
from zeitgeist.kafka_utils import make_consumer, make_producer
from zeitgeist.models import GdeltEvent

logger = logging.getLogger("zeitgeist.extractor")


def process_message(raw: bytes) -> list[bytes]:
    try:
        event = GdeltEvent.from_json(raw)
    except (ValueError, TypeError):
        logger.warning("undecodable message skipped")
        return []
    return [claim.to_json() for claim in event_to_claims(event)]


class Batcher:
    """Commits consumer offsets only after the producer has flushed pending claims.

    Offsets must never be committed for messages whose claims haven't actually made
    it to Kafka, so every commit point flushes the producer first.
    """

    def __init__(self, producer, consumer, threshold: int = 100) -> None:
        self._producer = producer
        self._consumer = consumer
        self._threshold = threshold
        self.pending = 0

    def record(self) -> None:
        self.pending += 1
        if self.pending >= self._threshold:
            self.commit()

    def maybe_commit_idle(self) -> None:
        if self.pending > 0:
            self.commit()

    def commit(self) -> None:
        self._producer.flush(10)
        self._consumer.commit(asynchronous=False)
        self.pending = 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    consumer = make_consumer(
        settings.kafka_bootstrap, RAW_TOPIC, group_id="extractor", auto_commit=False
    )
    producer = make_producer(settings.kafka_bootstrap)
    batcher = Batcher(producer, consumer)
    logger.info("extractor consuming %s", RAW_TOPIC)
    try:
        while True:
            message = consumer.poll(1.0)
            if message is None:
                batcher.maybe_commit_idle()
                continue
            if message.error():
                logger.warning("consumer error: %s", message.error())
                continue
            for payload in process_message(message.value()):
                producer.produce(CLAIMS_TOPIC, key=message.key(), value=payload)
            producer.poll(0)
            batcher.record()
    finally:
        producer.flush(10)


if __name__ == "__main__":
    main()
