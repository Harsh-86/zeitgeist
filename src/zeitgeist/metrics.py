"""Prometheus metrics helpers shared by all services.

get_counter/get_gauge are idempotent against the default registry: the first
call registers the collector, subsequent calls (including a re-import of a
module that declares module-level metrics, as pytest sometimes triggers)
return the already-registered collector instead of raising. start_metrics_server
is fully guarded — a metrics endpoint must never be able to crash a service.
"""

import logging

from prometheus_client import REGISTRY, Counter, Gauge, start_http_server

logger = logging.getLogger("zeitgeist.metrics")

_collectors: dict[str, Counter | Gauge] = {}


def get_counter(name: str, description: str, labelnames: tuple[str, ...] = ()) -> Counter:
    if name in _collectors:
        return _collectors[name]  # type: ignore[return-value]
    try:
        counter = Counter(name, description, labelnames)
    except ValueError:
        counter = REGISTRY._names_to_collectors[name]  # type: ignore[assignment]
    _collectors[name] = counter
    return counter


def get_gauge(name: str, description: str) -> Gauge:
    if name in _collectors:
        return _collectors[name]  # type: ignore[return-value]
    try:
        gauge = Gauge(name, description)
    except ValueError:
        gauge = REGISTRY._names_to_collectors[name]  # type: ignore[assignment]
    _collectors[name] = gauge
    return gauge


def start_metrics_server(port: int) -> None:
    """Start the Prometheus HTTP exposition server. Never raises."""
    try:
        start_http_server(port)
    except Exception:
        logger.exception("failed to start metrics server on port %d", port)
