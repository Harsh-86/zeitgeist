"""Thin confluent-kafka factories. Real broker behavior is covered by integration tests."""

from confluent_kafka import Consumer, Producer


def make_producer(bootstrap: str) -> Producer:
    return Producer({"bootstrap.servers": bootstrap, "linger.ms": 100})


def make_consumer(bootstrap: str, topic: str, group_id: str, auto_commit: bool = True) -> Consumer:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
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
