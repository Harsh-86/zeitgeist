"""Thin confluent-kafka factories. Real broker behavior is covered by integration tests."""

import logging

from confluent_kafka import Consumer, Producer

logger = logging.getLogger("zeitgeist.kafka_utils")


def make_producer(bootstrap: str) -> Producer:
    return Producer({"bootstrap.servers": bootstrap, "linger.ms": 100})


def make_consumer(
    bootstrap: str,
    topic: str,
    group_id: str,
    auto_commit: bool = True,
    offset_reset: str = "earliest",
) -> Consumer:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group_id,
            "auto.offset.reset": offset_reset,
            "enable.auto.commit": auto_commit,
        }
    )
    consumer.subscribe([topic])
    return consumer


class Batcher:
    """Commits consumer offsets only after the producer has flushed pending messages.

    Offsets must never be committed for messages whose output hasn't actually made
    it to Kafka, so every commit point flushes the producer first. Shared by every
    consume-transform-produce service (extractor, sampler, ...).
    """

    def __init__(self, producer, consumer, threshold: int = 100) -> None:
        self._producer = producer
        self._consumer = consumer
        self._threshold = threshold
        self.pending = 0

    def record(self) -> bool | None:
        """Returns the result of commit() if the threshold was hit this call,
        else None (no commit was attempted).
        """
        self.pending += 1
        if self.pending >= self._threshold:
            return self.commit()
        return None

    def maybe_commit_idle(self) -> bool | None:
        """Returns the result of commit() if a commit was attempted (pending > 0),
        else None (nothing was pending, so no commit was attempted).
        """
        if self.pending > 0:
            return self.commit()
        return None

    def commit(self) -> bool:
        """Flushes the producer, then commits offsets iff nothing was left
        undelivered. Returns True when consumer.commit actually ran, False
        when it was skipped. Callers that keep batch-local counters (e.g. for
        durable-only metrics) should only fold them into durable state on True.
        """
        undelivered = self._producer.flush(10)
        if undelivered > 0:
            logger.error(
                "producer flush left %d message(s) undelivered; skipping commit "
                "so pending offsets retry at the next commit point",
                undelivered,
            )
            return False
        self._consumer.commit(asynchronous=False)
        self.pending = 0
        return True
