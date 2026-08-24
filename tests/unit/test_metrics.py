import prometheus_client

from zeitgeist import metrics


def test_get_counter_is_idempotent():
    first = metrics.get_counter("zg_test_counter_a", "test counter a")
    second = metrics.get_counter("zg_test_counter_a", "test counter a")
    assert first is second
    first.inc()
    assert second._value.get() == 1


def test_get_counter_with_labels_is_idempotent():
    first = metrics.get_counter("zg_test_counter_b", "test counter b", labelnames=("kind",))
    second = metrics.get_counter("zg_test_counter_b", "test counter b", labelnames=("kind",))
    assert first is second
    first.labels(kind="x").inc()
    assert second.labels(kind="x")._value.get() == 1


def test_get_gauge_is_idempotent():
    first = metrics.get_gauge("zg_test_gauge_a", "test gauge a")
    second = metrics.get_gauge("zg_test_gauge_a", "test gauge a")
    assert first is second
    first.set(5)
    assert second._value.get() == 5


def test_get_counter_recovers_when_already_registered_outside_cache():
    """Simulates a collector already present in the default registry but not yet
    in our module-level cache (e.g. a module re-import registering it again).
    """
    name = "zg_test_counter_c"
    external = prometheus_client.Counter(name, "registered directly")
    found = metrics.get_counter(name, "registered directly")
    assert found is external


def test_get_gauge_recovers_when_already_registered_outside_cache():
    name = "zg_test_gauge_c"
    external = prometheus_client.Gauge(name, "registered directly")
    found = metrics.get_gauge(name, "registered directly")
    assert found is external


def test_start_metrics_server_starts_http_server(monkeypatch):
    calls = []
    monkeypatch.setattr(metrics, "start_http_server", lambda port: calls.append(port))
    metrics.start_metrics_server(9999)
    assert calls == [9999]


def test_start_metrics_server_never_raises_on_failure(monkeypatch, caplog):
    def boom(port):
        raise OSError("port in use")

    monkeypatch.setattr(metrics, "start_http_server", boom)
    metrics.start_metrics_server(9999)  # must not raise
