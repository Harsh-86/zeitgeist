"""Graph-writer service: extracted.claims -> Neo4j."""

import logging
import time
from collections.abc import Callable

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from zeitgeist.config import CLAIMS_TOPIC, Settings
from zeitgeist.graph.writer import ensure_schema, write_claim
from zeitgeist.kafka_utils import make_consumer
from zeitgeist.models import Claim

logger = logging.getLogger("zeitgeist.graph")

RETRYABLE = (ServiceUnavailable, SessionExpired, TransientError)


def _run_with_retry(driver, session, op: Callable, sleep=time.sleep):
    """Run op(session), retrying in place while Neo4j is unavailable.

    On a RETRYABLE exception: log one warning, sleep, close the old session
    (ignoring close errors), open a fresh session from driver, and retry.
    Returns the (possibly recreated) session.
    """
    while True:
        try:
            op(session)
            return session
        except RETRYABLE as exc:
            logger.warning("neo4j unavailable (%s); retrying in 2s", exc)
            sleep(2)
            try:
                session.close()
            except Exception:
                logger.debug("session close failed during retry", exc_info=True)
            session = driver.session()


def write_with_retry(driver, session, claim: Claim, sleep=time.sleep):
    """Write claim, retrying in place while Neo4j is unavailable.

    Returns the (possibly recreated) session.
    """
    return _run_with_retry(driver, session, lambda s: write_claim(s, claim), sleep)


def process_one(driver, session, consumer, message):
    """Handle a single polled message. Returns the (possibly recreated) session.

    Undecodable claims are committed anyway (a poison message must not redeliver
    forever); consumer errors are logged and skipped; valid claims are written
    with retry then committed.
    """
    if message.error():
        logger.warning("consumer error: %s", message.error())
        return session
    try:
        claim = Claim.from_json(message.value())
    except (ValueError, TypeError):
        logger.warning("undecodable claim skipped")
        consumer.commit(message=message, asynchronous=False)
        return session
    session = write_with_retry(driver, session, claim)
    consumer.commit(message=message, asynchronous=False)
    return session


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    consumer = make_consumer(
        settings.kafka_bootstrap, CLAIMS_TOPIC, group_id="graph-writer", auto_commit=False
    )
    session = driver.session()
    session = _run_with_retry(driver, session, ensure_schema)
    logger.info("graph writer consuming %s", CLAIMS_TOPIC)
    while True:
        message = consumer.poll(1.0)
        if message is None:
            continue
        session = process_one(driver, session, consumer, message)


if __name__ == "__main__":
    main()
