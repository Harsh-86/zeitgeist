"""Ingestor service: poll GDELT every cycle, publish parsed events to raw.events."""

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from zeitgeist.config import RAW_TOPIC, Settings
from zeitgeist.gdelt.client import GdeltClient
from zeitgeist.gdelt.parser import parse_event_row
from zeitgeist.kafka_utils import make_producer

logger = logging.getLogger("zeitgeist.ingestor")

Send = Callable[[str, str, bytes], None]


class DeliveryTracker:
    """Counts hard-failed Kafka deliveries reported via the producer's on_delivery callback.

    Producer.flush() only returns the remaining queue length, which can be 0 even when
    messages were dequeued due to a delivery error (not actually delivered). Tracking
    failures separately lets callers add them to flush()'s return value so undelivered
    counts are never silently dropped.
    """

    def __init__(self) -> None:
        self.failed = 0

    def on_delivery(self, err, msg) -> None:
        if err is not None:
            self.failed += 1
            logger.error("delivery failed: %s", err)

    def reset(self) -> None:
        self.failed = 0


class IngestorState:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def last_url(self) -> str | None:
        if not self._path.exists():
            return None
        return json.loads(self._path.read_text()).get("last_url")

    def save(self, url: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"last_url": url}))


def run_cycle(
    client: GdeltClient,
    state: IngestorState,
    send: Send,
    flush: Callable[[], int] | None = None,
) -> tuple[int, int]:
    url = client.latest_export_url()
    if url is None or url == state.last_url:
        return (0, 0)
    published = skipped = 0
    for row in client.fetch_rows(url):
        event = parse_event_row(row)
        if event is None:
            skipped += 1
            continue
        send(RAW_TOPIC, event.event_id, event.to_json())
        published += 1
    if flush is not None:
        undelivered = flush()
        if undelivered > 0:
            logger.error(
                "%d messages undelivered; state not advanced — cycle will retry",
                undelivered,
            )
            return (published, skipped)
    state.save(url)
    logger.info("cycle done url=%s published=%d skipped=%d", url, published, skipped)
    return (published, skipped)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    client = GdeltClient(httpx.Client())
    state = IngestorState(Path(settings.state_path))
    producer = make_producer(settings.kafka_bootstrap)
    tracker = DeliveryTracker()

    def send(topic: str, key: str, value: bytes) -> None:
        producer.produce(topic, key=key, value=value, on_delivery=tracker.on_delivery)

    def flush() -> int:
        undelivered = producer.flush(30) + tracker.failed
        tracker.reset()
        return undelivered

    while True:
        try:
            run_cycle(client, state, send, flush=flush)
        except Exception:
            logger.exception("cycle failed; retrying next interval")
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
