"""End-to-end smoke test: synthetic event in -> entity node out. Run against `make up`."""

import sys
import time
import uuid

from neo4j import GraphDatabase

from zeitgeist.config import RAW_TOPIC, Settings
from zeitgeist.kafka_utils import make_producer
from zeitgeist.models import GdeltEvent


def main() -> int:
    settings = Settings.from_env()
    marker = f"SMOKE TEST ENTITY {uuid.uuid4().hex[:8].upper()}"
    event = GdeltEvent(
        event_id=f"smoke-{uuid.uuid4().hex}",
        occurred_on="2026-08-18",
        actor1_code="SMK",
        actor1_name=marker,
        actor2_code=None,
        actor2_name=None,
        event_code="010",
        event_root_code="01",
        quad_class=1,
        goldstein=0.0,
        num_mentions=1,
        avg_tone=0.0,
        geo_name=None,
        geo_lat=None,
        geo_lon=None,
        observed_at="2026-08-18T00:00:00Z",
        source_url=None,
    )
    producer = make_producer(settings.kafka_bootstrap)
    producer.produce(RAW_TOPIC, key=event.event_id, value=event.to_json())
    producer.flush(10)
    print(f"produced synthetic event for {marker!r}; polling Neo4j...")

    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    deadline = time.time() + 60
    with driver.session() as session:
        while time.time() < deadline:
            record = session.run(
                "MATCH (e:Entity {name: $name}) RETURN count(e) AS n", name=marker
            ).single()
            if record["n"] == 1:
                print("SMOKE TEST PASSED: entity found in graph")
                return 0
            time.sleep(2)
    print("SMOKE TEST FAILED: entity never appeared", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
