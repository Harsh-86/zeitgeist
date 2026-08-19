"""Graph-writer service: extracted.claims -> Neo4j."""

import logging

from neo4j import GraphDatabase

from zeitgeist.config import CLAIMS_TOPIC, Settings
from zeitgeist.graph.writer import ensure_schema, write_claim
from zeitgeist.kafka_utils import make_consumer
from zeitgeist.models import Claim

logger = logging.getLogger("zeitgeist.graph")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    consumer = make_consumer(settings.kafka_bootstrap, CLAIMS_TOPIC, group_id="graph-writer")
    with driver.session() as session:
        ensure_schema(session)
        logger.info("graph writer consuming %s", CLAIMS_TOPIC)
        while True:
            message = consumer.poll(1.0)
            if message is None or message.error():
                continue
            try:
                write_claim(session, Claim.from_json(message.value()))
            except (ValueError, TypeError):
                logger.warning("undecodable claim skipped")


if __name__ == "__main__":
    main()
