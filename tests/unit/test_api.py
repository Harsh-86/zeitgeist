from fastapi.testclient import TestClient

from zeitgeist.api.main import create_app


class FakeResult:
    def __init__(self, value):
        self._value = value

    def single(self):
        return {"n": self._value}


class FakeSession:
    def run(self, query, **kwargs):
        return FakeResult(42 if "Entity" in query else 7)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeDriver:
    def session(self):
        return FakeSession()


def make_client():
    return TestClient(create_app(driver=FakeDriver(), start_consumer=False))


def test_healthz():
    response = make_client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_stats_counts_entities_and_events():
    response = make_client().get("/stats")
    assert response.json() == {"entities": 42, "events": 7}


def test_websocket_receives_broadcast():
    client = make_client()
    with client, client.websocket_connect("/ws/claims") as ws:
        broadcaster = client.app.state.broadcaster
        client.portal.call(broadcaster.broadcast, '{"subject": "ECB"}')
        assert ws.receive_text() == '{"subject": "ECB"}'
