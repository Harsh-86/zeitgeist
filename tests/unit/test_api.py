import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient

from zeitgeist.api import main as api_main
from zeitgeist.api.main import create_app


class FakeResult:
    def __init__(self, value):
        self._value = value

    def single(self):
        return {"n": self._value}


RECENT_CLAIM_RECORDS = [
    {"subject": "ECB", "relation": "criticizes", "object": "Italy"},
    {"subject": "Fed", "relation": "raises", "object": "Rates"},
]


class FakeRecords:
    def __init__(self, records):
        self._records = records

    def __iter__(self):
        return iter(self._records)


class FakeSession:
    def __init__(self):
        self.calls = []

    def run(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if "ACTOR1_IN" in query:
            return FakeRecords(RECENT_CLAIM_RECORDS)
        return FakeResult(42 if "Entity" in query else 7)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeDriver:
    def __init__(self):
        self.session_instance = FakeSession()

    def session(self):
        return self.session_instance


class FailingSession:
    def run(self, query, **kwargs):
        raise RuntimeError("neo4j down")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FailingDriver:
    def session(self):
        return FailingSession()


def make_client():
    return TestClient(create_app(driver=FakeDriver(), start_consumer=False))


def test_healthz():
    response = make_client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_stats_counts_entities_and_events():
    response = make_client().get("/stats")
    assert response.json() == {"entities": 42, "events": 7}


# -- /recent preload ------------------------------------------------------------


def test_recent_returns_claims_from_cypher():
    driver = FakeDriver()
    client = TestClient(create_app(driver=driver, start_consumer=False))

    response = client.get("/recent")

    assert response.status_code == 200
    assert response.json() == {"claims": RECENT_CLAIM_RECORDS}
    query, kwargs = driver.session_instance.calls[-1]
    assert "ACTOR1_IN" in query
    assert kwargs["limit"] == 500


def test_recent_limit_is_capped_at_1000():
    driver = FakeDriver()
    client = TestClient(create_app(driver=driver, start_consumer=False))

    response = client.get("/recent?limit=5000")

    assert response.status_code == 200
    assert driver.session_instance.calls[-1][1]["limit"] == 1000


def test_recent_limit_is_floored_at_1_for_zero():
    driver = FakeDriver()
    client = TestClient(create_app(driver=driver, start_consumer=False))

    response = client.get("/recent?limit=0")

    assert response.status_code == 200
    assert driver.session_instance.calls[-1][1]["limit"] == 1


def test_recent_limit_is_floored_at_1_for_negative():
    driver = FakeDriver()
    client = TestClient(create_app(driver=driver, start_consumer=False))

    response = client.get("/recent?limit=-5")

    assert response.status_code == 200
    assert driver.session_instance.calls[-1][1]["limit"] == 1


def test_recent_survives_neo4j_failure_and_returns_empty_claims(caplog):
    client = TestClient(create_app(driver=FailingDriver(), start_consumer=False))

    with caplog.at_level("WARNING"):
        response = client.get("/recent")

    assert response.status_code == 200
    assert response.json() == {"claims": []}
    assert any("recent" in r.message.lower() for r in caplog.records)


def test_websocket_receives_broadcast():
    client = make_client()
    with client, client.websocket_connect("/ws/claims") as ws:
        broadcaster = client.app.state.broadcaster
        client.portal.call(broadcaster.broadcast, '{"subject": "ECB"}')
        assert ws.receive_text() == '{"subject": "ECB"}'


# -- metrics instrumentation ---------------------------------------------------


def test_metrics_endpoint_returns_prometheus_text_and_refreshes_gauges():
    response = make_client().get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "zeitgeist_graph_entities 42.0" in body
    assert "zeitgeist_graph_events 7.0" in body
    assert api_main.GRAPH_ENTITIES._value.get() == 42
    assert api_main.GRAPH_EVENTS._value.get() == 7


def test_metrics_endpoint_survives_neo4j_failure_and_still_serves_metrics(caplog):
    client = TestClient(create_app(driver=FailingDriver(), start_consumer=False))

    with caplog.at_level("WARNING"):
        response = client.get("/metrics")

    assert response.status_code == 200
    assert b"zeitgeist_" in response.content
    assert any("metrics" in r.message.lower() for r in caplog.records)


# -- main(): metrics server wiring ---------------------------------------------


def test_main_starts_metrics_server_when_port_configured(monkeypatch):
    monkeypatch.setenv("METRICS_PORT", "9306")
    calls = []
    monkeypatch.setattr(api_main, "start_metrics_server", lambda port: calls.append(port))
    monkeypatch.setattr(api_main, "create_app", lambda **kwargs: object())
    fake_uvicorn = SimpleNamespace(run=lambda app, **kwargs: None)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    api_main.main()

    assert calls == [9306]


def test_main_does_not_start_metrics_server_when_port_is_zero(monkeypatch):
    monkeypatch.delenv("METRICS_PORT", raising=False)
    calls = []
    monkeypatch.setattr(api_main, "start_metrics_server", lambda port: calls.append(port))
    monkeypatch.setattr(api_main, "create_app", lambda **kwargs: object())
    fake_uvicorn = SimpleNamespace(run=lambda app, **kwargs: None)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    api_main.main()

    assert calls == []
