"""Thin confluent-kafka factories. Real broker behavior is covered by integration tests."""

from confluent_kafka import Consumer, Producer


def make_producer(bootstrap: str) -> Producer:
    return Producer({"bootstrap.servers": bootstrap, "linger.ms": 100})


def make_consumer(bootstrap: str, topic: str, group_id: str) -> Consumer:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group_id,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
        }
    )
    consumer.subscribe([topic])
    return consumer
