"""Ingestor service: poll GDELT every cycle, publish parsed events to raw.events."""

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from zeitgeist.config import RAW_TOPIC, Settings
from zeitgeist.gdelt.client import (
    MAX_BACKFILL_WINDOWS,
    GdeltClient,
    export_url_for,
    stamp_from_url,
    stamps_between,
)
from zeitgeist.gdelt.parser import parse_event_row
from zeitgeist.kafka_utils import make_producer
from zeitgeist.metrics import get_counter, get_gauge, start_metrics_server

logger = logging.getLogger("zeitgeist.ingestor")

Send = Callable[[str, str, bytes], None]

# Consecutive-404 thresholds (see run_cycle).
LATEST_WINDOW_LOG_ESCALATION_ATTEMPTS = 10
UPSTREAM_GAP_SKIP_ATTEMPTS = 5

WINDOWS_TOTAL = get_counter(
    "zeitgeist_windows_total", "GDELT windows successfully processed and saved to state"
)
EVENTS_PUBLISHED_TOTAL = get_counter(
    "zeitgeist_events_published_total", "Events published to raw.events"
)
ROWS_SKIPPED_TOTAL = get_counter(
    "zeitgeist_rows_skipped_total", "Malformed GDELT rows skipped during parsing"
)
WINDOWS_SKIPPED_UPSTREAM_TOTAL = get_counter(
    "zeitgeist_windows_skipped_upstream_total",
    "Windows skipped after repeated 404s from upstream GDELT",
)
LAST_SUCCESS_TIMESTAMP = get_gauge(
    "zeitgeist_last_success_timestamp", "Unix timestamp of the last successful state save"
)


def _mark_freshness() -> None:
    """Loop-liveness signal: set on any successful state.save, whether the window was
    fully processed or skip-and-advanced past a persistent upstream gap. Freshness
    measures the ingest loop making forward progress, not GDELT's delivery record.
    """
    LAST_SUCCESS_TIMESTAMP.set(time.time())


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
    """Persists the last successfully processed GDELT window stamp.

    Transparently migrates the old `{"last_url": ...}` file shape to the current
    `{"last_stamp": ...}` shape on read.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def last_stamp(self) -> str | None:
        if not self._path.exists():
            return None
        data = json.loads(self._path.read_text())
        if "last_stamp" in data:
            return data["last_stamp"]
        last_url = data.get("last_url")
        return stamp_from_url(last_url) if last_url else None

    def save(self, stamp: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"last_stamp": stamp}))


def run_cycle(
    client: GdeltClient,
    state: IngestorState,
    send: Send,
    flush: Callable[[], int] | None = None,
    misses: dict[str, int] | None = None,
) -> tuple[int, int]:
    if misses is None:
        misses = {}

    latest_url = client.latest_export_url()
    if latest_url is None:
        return (0, 0)
    latest_stamp = stamp_from_url(latest_url)
    if latest_stamp is None:
        return (0, 0)

    last = state.last_stamp
    if last is None:
        windows = [latest_stamp]
    else:
        windows = stamps_between(last, latest_stamp)
        if not windows:
            return (0, 0)
        if len(windows) >= MAX_BACKFILL_WINDOWS:
            logger.warning(
                "backfill gap exceeds %d windows; truncated to the newest %d",
                MAX_BACKFILL_WINDOWS,
                MAX_BACKFILL_WINDOWS,
            )

    published = skipped = 0
    for stamp in windows:
        url = export_url_for(stamp)
        try:
            rows = list(client.fetch_rows(url))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            attempts = misses[stamp] = misses.get(stamp, 0) + 1
            if stamp == latest_stamp:
                if attempts <= LATEST_WINDOW_LOG_ESCALATION_ATTEMPTS:
                    logger.info("window %s not yet published (attempt %d)", stamp, attempts)
                else:
                    logger.warning(
                        "window %s still missing after %d attempts", stamp, attempts
                    )
                break
            if attempts < UPSTREAM_GAP_SKIP_ATTEMPTS:
                logger.info(
                    "window %s missing upstream (attempt %d/%d); retrying next cycle",
                    stamp,
                    attempts,
                    UPSTREAM_GAP_SKIP_ATTEMPTS,
                )
                break
            logger.warning(
                "window %s skipped — missing upstream after %d attempts", stamp, attempts
            )
            state.save(stamp)
            _mark_freshness()
            WINDOWS_SKIPPED_UPSTREAM_TOTAL.inc()
            misses.pop(stamp, None)
            continue

        window_published = window_skipped = 0
        for row in rows:
            event = parse_event_row(row)
            if event is None:
                window_skipped += 1
                continue
            send(RAW_TOPIC, event.event_id, event.to_json())
            window_published += 1
        published += window_published
        skipped += window_skipped

        if flush is not None:
            undelivered = flush()
            if undelivered > 0:
                logger.error(
                    "%d messages undelivered; state not advanced — cycle will retry",
                    undelivered,
                )
                break

        state.save(stamp)
        WINDOWS_TOTAL.inc()
        EVENTS_PUBLISHED_TOTAL.inc(window_published)
        ROWS_SKIPPED_TOTAL.inc(window_skipped)
        _mark_freshness()
        misses.pop(stamp, None)
        logger.info(
            "window %s done published=%d skipped=%d", stamp, window_published, window_skipped
        )

    return (published, skipped)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    if settings.metrics_port > 0:
        start_metrics_server(settings.metrics_port)
    client = GdeltClient(httpx.Client(follow_redirects=True))
    state = IngestorState(Path(settings.state_path))
    producer = make_producer(settings.kafka_bootstrap)
    tracker = DeliveryTracker()
    misses: dict[str, int] = {}

    def send(topic: str, key: str, value: bytes) -> None:
        producer.produce(topic, key=key, value=value, on_delivery=tracker.on_delivery)

    def flush() -> int:
        undelivered = producer.flush(30) + tracker.failed
        tracker.reset()
        return undelivered

    while True:
        try:
            run_cycle(client, state, send, flush=flush, misses=misses)
        except Exception:
            logger.exception("cycle failed; retrying next interval")
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    main()
