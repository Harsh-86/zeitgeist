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


class _FakeTx:
    def __init__(self, session):
        self._session = session

    def run(self, query, **kwargs):
        self._session.read_queries.append(query)
        return self._session.ask_records


class FakeSession:
    def __init__(self, ask_records=None, execute_read_failures=0):
        self.calls = []
        self.read_queries = []
        self.execute_read_calls = 0
        self.ask_records = ask_records or []
        self._execute_read_failures = execute_read_failures

    def run(self, query, **kwargs):
        self.calls.append((query, kwargs))
        if "ACTOR1_IN" in query:
            return FakeRecords(RECENT_CLAIM_RECORDS)
        return FakeResult(42 if "Entity" in query else 7)

    def execute_read(self, fn):
        self.execute_read_calls += 1
        if self._execute_read_failures > 0:
            self._execute_read_failures -= 1
            raise RuntimeError("neo4j syntax error")
        return fn(_FakeTx(self))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeDriver:
    def __init__(self, session=None):
        self.session_instance = session if session is not None else FakeSession()

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


# -- POST /ask -------------------------------------------------------------------


ASK_TOKEN = "test-ask-token"
SAFE_CYPHER = (
    "MATCH (s:Entity)-[:ACTOR1_IN]->(ev:Event) "
    "RETURN s.name AS subject, ev.source_url AS source_url LIMIT 5"
)
ASK_RECORDS = [
    {"subject": "GERMANY", "source_url": "https://a.example/1"},
    {"subject": "FRANCE", "source_url": "https://b.example/2"},
    {"subject": "GERMANY", "source_url": "https://a.example/1"},
    {"subject": "POLAND", "source_url": None},
]


class FakeQueryAgent:
    """Scripted stand-in for agent.query.QueryAgent; records every call."""

    def __init__(self, cyphers=None, answer="THE ANSWER"):
        self.generate_calls = []
        self.synthesize_calls = []
        self._cyphers = list(cyphers or [])
        self._answer = answer

    def generate_cypher(self, question, error_feedback=None):
        self.generate_calls.append((question, error_feedback))
        if self._cyphers:
            return self._cyphers.pop(0), {}
        return None, {}

    def synthesize(self, question, records):
        self.synthesize_calls.append((question, records))
        return self._answer, {}


class FakeBudget:
    """try_spend returns scripted results (then False); None means always True."""

    def __init__(self, results=None):
        self._results = list(results) if results is not None else None
        self.calls = 0

    def try_spend(self):
        self.calls += 1
        if self._results is None:
            return True
        return self._results.pop(0) if self._results else False


def make_ask_client(monkeypatch, agent=None, budget=None, session=None, token=ASK_TOKEN):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if token is None:
        monkeypatch.delenv("ASK_TOKEN", raising=False)
    else:
        monkeypatch.setenv("ASK_TOKEN", token)
    driver = FakeDriver(session=session)
    app = create_app(driver=driver, start_consumer=False, agent=agent, ask_budget=budget)
    return TestClient(app), driver


def ask(client, question="What happened around GERMANY?", token=ASK_TOKEN, body=...):
    headers = {} if token is None else {"X-Ask-Token": token}
    if body is ...:
        body = {"question": question}
    if body is None:
        return client.post("/ask", headers=headers)
    return client.post("/ask", json=body, headers=headers)


def error_body(message):
    return {
        "answer": None,
        "cypher": None,
        "citations": [],
        "records_count": 0,
        "error": message,
    }


def test_ask_403_when_token_unset(monkeypatch):
    client, _ = make_ask_client(monkeypatch, agent=FakeQueryAgent(), token=None)
    response = ask(client)
    assert response.status_code == 403
    assert response.json() == {"error": "ask endpoint disabled"}


def test_ask_403_when_token_wrong(monkeypatch):
    client, _ = make_ask_client(monkeypatch, agent=FakeQueryAgent())
    response = ask(client, token="wrong-token")
    assert response.status_code == 403
    assert response.json() == {"error": "invalid token"}


def test_ask_403_when_token_header_missing(monkeypatch):
    client, _ = make_ask_client(monkeypatch, agent=FakeQueryAgent())
    response = ask(client, token=None)
    assert response.status_code == 403
    assert response.json() == {"error": "invalid token"}


def test_ask_403_not_500_on_non_ascii_token_header(monkeypatch):
    # compare_digest raises TypeError on non-ASCII str; the gate must compare
    # bytes so an attacker-controlled header can never crash the endpoint.
    client, _ = make_ask_client(monkeypatch, agent=FakeQueryAgent())
    # Send as latin-1 bytes: that's how a raw client smuggles non-ASCII past
    # httpx's own str-header validation, and how the server actually sees it.
    response = client.post(
        "/ask",
        json={"question": "q"},
        headers={b"X-Ask-Token": "ééé".encode("latin-1")},
    )
    assert response.status_code == 403
    assert response.json() == {"error": "invalid token"}


def test_ask_400_when_body_too_large(monkeypatch):
    client, _ = make_ask_client(monkeypatch, agent=FakeQueryAgent())
    response = ask(client, body={"question": "q", "padding": "x" * 20_000})
    assert response.status_code == 400
    assert response.json() == {"error": "request body too large"}


def test_ask_400_when_no_body(monkeypatch):
    client, _ = make_ask_client(monkeypatch, agent=FakeQueryAgent())
    response = ask(client, body=None)
    assert response.status_code == 400
    assert response.json()["error"]


def test_ask_400_when_question_missing(monkeypatch):
    client, _ = make_ask_client(monkeypatch, agent=FakeQueryAgent())
    response = ask(client, body={})
    assert response.status_code == 400
    assert response.json()["error"]


def test_ask_400_when_question_empty(monkeypatch):
    client, _ = make_ask_client(monkeypatch, agent=FakeQueryAgent())
    response = ask(client, body={"question": "  "})
    assert response.status_code == 400
    assert response.json()["error"]


def test_ask_400_when_question_too_long(monkeypatch):
    client, _ = make_ask_client(monkeypatch, agent=FakeQueryAgent())
    response = ask(client, question="x" * 501)
    assert response.status_code == 400
    assert response.json()["error"]


def test_ask_reports_agent_not_configured_when_agent_is_none(monkeypatch):
    client, _ = make_ask_client(monkeypatch, agent=None)
    response = ask(client)
    assert response.status_code == 200
    assert response.json() == error_body("ask agent not configured")


def test_ask_happy_path_answers_with_deduped_citations(monkeypatch):
    agent = FakeQueryAgent(cyphers=[SAFE_CYPHER])
    budget = FakeBudget()
    session = FakeSession(ask_records=ASK_RECORDS)
    client, _ = make_ask_client(monkeypatch, agent=agent, budget=budget, session=session)
    questions_before = api_main.ASK_QUESTIONS._value.get()

    response = ask(client)

    assert response.status_code == 200
    assert response.json() == {
        "answer": "THE ANSWER",
        "cypher": SAFE_CYPHER,
        "citations": ["https://a.example/1", "https://b.example/2"],
        "records_count": 4,
        "error": None,
    }
    assert session.read_queries == [SAFE_CYPHER]
    assert agent.generate_calls == [("What happened around GERMANY?", None)]
    assert agent.synthesize_calls == [("What happened around GERMANY?", ASK_RECORDS)]
    assert budget.calls == 2
    assert api_main.ASK_QUESTIONS._value.get() == questions_before + 1


def test_ask_rejects_write_cypher_without_executing(monkeypatch):
    agent = FakeQueryAgent(cyphers=["CREATE (n:Entity {name: 'X'}) RETURN n"])
    session = FakeSession(ask_records=ASK_RECORDS)
    client, _ = make_ask_client(monkeypatch, agent=agent, budget=FakeBudget(), session=session)
    failed_before = api_main.ASK_FAILED._value.get()

    response = ask(client)

    assert response.status_code == 200
    assert response.json() == error_body("could not generate a safe query")
    assert session.execute_read_calls == 0
    assert api_main.ASK_FAILED._value.get() == failed_before + 1


def test_ask_retries_once_with_error_feedback_then_succeeds(monkeypatch):
    retry_cypher = "MATCH (ev:Event) RETURN ev.source_url AS source_url LIMIT 3"
    agent = FakeQueryAgent(cyphers=[SAFE_CYPHER, retry_cypher])
    budget = FakeBudget()
    session = FakeSession(ask_records=ASK_RECORDS, execute_read_failures=1)
    client, _ = make_ask_client(monkeypatch, agent=agent, budget=budget, session=session)

    response = ask(client)

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert body["cypher"] == retry_cypher
    assert len(agent.generate_calls) == 2
    feedback = agent.generate_calls[1][1]
    assert SAFE_CYPHER in feedback
    assert "neo4j syntax error" in feedback
    assert budget.calls == 3
    assert session.read_queries == [retry_cypher]


def test_ask_reports_query_execution_failed_after_two_failures(monkeypatch):
    agent = FakeQueryAgent(cyphers=[SAFE_CYPHER, SAFE_CYPHER])
    session = FakeSession(ask_records=ASK_RECORDS, execute_read_failures=2)
    client, _ = make_ask_client(monkeypatch, agent=agent, budget=FakeBudget(), session=session)

    response = ask(client)

    assert response.status_code == 200
    assert response.json() == error_body("query execution failed")
    assert len(agent.generate_calls) == 2


def test_ask_denies_when_budget_exhausted(monkeypatch):
    agent = FakeQueryAgent(cyphers=[SAFE_CYPHER])
    budget = FakeBudget(results=[False])
    client, _ = make_ask_client(monkeypatch, agent=agent, budget=budget)
    denied_before = api_main.ASK_DENIED._value.get()

    response = ask(client)

    assert response.status_code == 200
    assert response.json() == error_body("daily ask budget exhausted")
    assert agent.generate_calls == []
    assert api_main.ASK_DENIED._value.get() == denied_before + 1


def test_ask_reports_synthesis_failure_when_answer_is_none(monkeypatch):
    agent = FakeQueryAgent(cyphers=[SAFE_CYPHER], answer=None)
    session = FakeSession(ask_records=ASK_RECORDS)
    client, _ = make_ask_client(monkeypatch, agent=agent, budget=FakeBudget(), session=session)

    response = ask(client)

    assert response.status_code == 200
    assert response.json() == error_body("could not synthesize an answer")


def test_ask_never_500s_on_unexpected_exception(monkeypatch, caplog):
    class ExplodingAgent:
        def generate_cypher(self, question, error_feedback=None):
            raise ValueError("boom")

    client, _ = make_ask_client(monkeypatch, agent=ExplodingAgent(), budget=FakeBudget())

    with caplog.at_level("WARNING"):
        response = ask(client)

    assert response.status_code == 200
    assert response.json() == error_body("internal error")


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
