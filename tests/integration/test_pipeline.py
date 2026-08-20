"""Integration tests against real Kafka and Neo4j (testcontainers). Requires Docker."""

import pytest
from neo4j import GraphDatabase
from testcontainers.community.kafka import KafkaContainer
from testcontainers.community.neo4j import Neo4jContainer

from tests.unit.test_models import make_event
from zeitgeist.extractor.main import process_message
from zeitgeist.extractor.rules import event_to_claims
from zeitgeist.graph.writer import ensure_schema, write_claim
from zeitgeist.kafka_utils import make_consumer, make_producer
from zeitgeist.models import Claim

pytestmark = pytest.mark.integration


def test_kafka_round_trip():
    with KafkaContainer() as kafka:
        bootstrap = kafka.get_bootstrap_server()
        producer = make_producer(bootstrap)
        event = make_event()
        producer.produce("raw.events", key=event.event_id, value=event.to_json())
        producer.flush(10)

        consumer = make_consumer(bootstrap, "raw.events", group_id="it-test")
        message = consumer.poll(30)
        assert message is not None and not message.error()
        payloads = process_message(message.value())
        assert Claim.from_json(payloads[0]).relation == "CONSULTED"
        consumer.close()


def test_writer_is_idempotent_against_real_neo4j():
    with Neo4jContainer("neo4j:5-community") as neo4j:
        driver = GraphDatabase.driver(
            neo4j.get_connection_url(), auth=(neo4j.username, neo4j.password)
        )
        claim = event_to_claims(make_event())[0]
        with driver.session() as session:
            ensure_schema(session)
            write_claim(session, claim)
            write_claim(session, claim)  # redelivery must not duplicate
            events = session.run("MATCH (ev:Event) RETURN count(ev) AS n").single()["n"]
            entities = session.run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
            linked = session.run(
                "MATCH (:Entity {name:'UNITED STATES'})-[:ACTOR1_IN]->(ev:Event)"
                "-[:ACTOR2]->(:Entity {name:'EUROPEAN CENTRAL BANK'}) "
                "RETURN count(ev) AS n"
            ).single()["n"]
        assert events == 1
        assert entities == 2
        assert linked == 1
        driver.close()
