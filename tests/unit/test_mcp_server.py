import asyncio
import json
import re

from zeitgeist import mcp_server


def placeholders(cypher: str) -> set[str]:
    return set(re.findall(r"\$(\w+)", cypher))


class _FakeTx:
    def __init__(self, session):
        self._session = session

    def run(self, query, **params):
        self._session.read_queries.append((query, params))
        return self._session.records


class FakeSession:
    """Fake neo4j session exposing ONLY execute_read: a tool that tried
    session.run() would fail here, mirroring the layer-2 read enforcement."""

    def __init__(self, records=None):
        self.read_queries: list[tuple[str, dict]] = []
        self.records = records if records is not None else []

    def execute_read(self, fn):
        return fn(_FakeTx(self))


class FakeTemporal:
    """Stand-in for a neo4j.time temporal: not JSON-serializable, str()-able."""

    def __str__(self):
        return "2026-08-26T10:00:00+00:00"


# --- search_entities --------------------------------------------------------


def test_search_entities_query_and_params():
    session = FakeSession()
    mcp_server.search_entities(session, "germ")
    query, params = session.read_queries[0]
    assert query == mcp_server.SEARCH_ENTITIES_CYPHER
    assert placeholders(query) == set(params)
    assert params == {"name_fragment": "germ", "limit": 10}
    assert "toUpper(e.name) CONTAINS toUpper($name_fragment)" in query
    assert "ALIAS_OF" in query
    assert "coalesce" in query
    assert "ORDER BY event_count DESC" in query


def test_search_entities_returns_rows():
    records = [{"name": "GERMANY", "event_count": 12, "canonical": "GERMANY"}]
    session = FakeSession(records=records)
    assert mcp_server.search_entities(session, "germ") == records


def test_search_entities_clamps_limit():
    session = FakeSession()
    mcp_server.search_entities(session, "x", limit=0)
    mcp_server.search_entities(session, "x", limit=999)
    assert session.read_queries[0][1]["limit"] == 1
    assert session.read_queries[1][1]["limit"] == 50


# --- entity_timeline --------------------------------------------------------


def test_entity_timeline_defaults_have_no_time_filters():
    session = FakeSession()
    mcp_server.entity_timeline(session, "GERMANY")
    query, params = session.read_queries[0]
    assert params == {"name": "GERMANY", "limit": 25}
    assert placeholders(query) == set(params)
    assert "$since" not in query
    assert "$until" not in query
    assert "ACTOR1_IN" in query
    assert "ACTOR2" in query
    assert "observed_at DESC" in query


def test_entity_timeline_since_fragment_only_when_provided():
    session = FakeSession()
    mcp_server.entity_timeline(session, "GERMANY", since="2026-08-01T00:00:00Z")
    query, params = session.read_queries[0]
    assert "ev.observed_at >= datetime($since)" in query
    assert "$until" not in query
    assert params == {"name": "GERMANY", "since": "2026-08-01T00:00:00Z", "limit": 25}
    assert placeholders(query) == set(params)


def test_entity_timeline_until_fragment_only_when_provided():
    session = FakeSession()
    mcp_server.entity_timeline(session, "GERMANY", until="2026-08-20T00:00:00Z")
    query, params = session.read_queries[0]
    assert "ev.observed_at <= datetime($until)" in query
    assert "$since" not in query
    assert params == {"name": "GERMANY", "until": "2026-08-20T00:00:00Z", "limit": 25}
    assert placeholders(query) == set(params)


def test_entity_timeline_with_both_since_and_until():
    session = FakeSession()
    mcp_server.entity_timeline(
        session, "GERMANY", since="2026-08-01T00:00:00Z", until="2026-08-20T00:00:00Z"
    )
    query, params = session.read_queries[0]
    assert "ev.observed_at >= datetime($since)" in query
    assert "ev.observed_at <= datetime($until)" in query
    assert set(params) == {"name", "since", "until", "limit"}
    assert placeholders(query) == set(params)


def test_entity_timeline_clamps_limit():
    session = FakeSession()
    mcp_server.entity_timeline(session, "GERMANY", limit=-3)
    mcp_server.entity_timeline(session, "GERMANY", limit=51)
    assert session.read_queries[0][1]["limit"] == 1
    assert session.read_queries[1][1]["limit"] == 50


def test_entity_timeline_converts_temporals_to_json_safe_strings():
    records = [
        {
            "relation": "MET_WITH",
            "other": "FRANCE",
            "role": "subject",
            "observed_at": FakeTemporal(),
            "tier": "llm",
            "detail": "Chancellor met the president.",
            "source_url": "https://news.example/1",
        }
    ]
    session = FakeSession(records=records)
    rows = mcp_server.entity_timeline(session, "GERMANY")
    assert rows[0]["observed_at"] == "2026-08-26T10:00:00+00:00"
    json.dumps(rows)  # must not raise


# --- connections ------------------------------------------------------------


def test_connections_query_and_params():
    session = FakeSession()
    mcp_server.connections(session, "GERMANY", "FRANCE")
    query, params = session.read_queries[0]
    assert query == mcp_server.CONNECTIONS_CYPHER
    assert placeholders(query) == set(params)
    assert params == {"a": "GERMANY", "b": "FRANCE", "limit": 25}
    # both directions: a as subject with b as object, and the reverse
    assert "s.name = $a AND o.name = $b" in query
    assert "s.name = $b AND o.name = $a" in query
    assert "observed_at DESC" in query


def test_connections_clamps_limit():
    session = FakeSession()
    mcp_server.connections(session, "A", "B", limit=0)
    mcp_server.connections(session, "A", "B", limit=1000)
    assert session.read_queries[0][1]["limit"] == 1
    assert session.read_queries[1][1]["limit"] == 50


# --- recent_events ----------------------------------------------------------


def test_recent_events_default_has_no_tier_filter():
    session = FakeSession()
    mcp_server.recent_events(session)
    query, params = session.read_queries[0]
    assert params == {"limit": 25}
    assert placeholders(query) == set(params)
    assert "$tier" not in query
    assert "ACTOR2" in query  # full-pair claims only
    assert "observed_at DESC" in query


def test_recent_events_applies_valid_tier_filter():
    session = FakeSession()
    mcp_server.recent_events(session, tier="llm")
    mcp_server.recent_events(session, tier="rules")
    for query, params in session.read_queries:
        assert "ev.tier = $tier" in query
        assert placeholders(query) == set(params)
    assert session.read_queries[0][1]["tier"] == "llm"
    assert session.read_queries[1][1]["tier"] == "rules"


def test_recent_events_ignores_bogus_tier():
    session = FakeSession()
    mcp_server.recent_events(session, tier="bogus; DROP")
    query, params = session.read_queries[0]
    assert "$tier" not in query
    assert params == {"limit": 25}


def test_recent_events_clamps_limit():
    session = FakeSession()
    mcp_server.recent_events(session, limit=0)
    mcp_server.recent_events(session, limit=999)
    assert session.read_queries[0][1]["limit"] == 1
    assert session.read_queries[1][1]["limit"] == 50


# --- graph_stats ------------------------------------------------------------


def test_graph_stats_shape():
    session = FakeSession(
        records=[{"entities": 42, "events": 317000, "llm_events": 900, "alias_edges": 17}]
    )
    stats = mcp_server.graph_stats(session)
    assert stats == {"entities": 42, "events": 317000, "llm_events": 900, "alias_edges": 17}
    query, params = session.read_queries[0]
    assert query == mcp_server.GRAPH_STATS_CYPHER
    assert params == {}


# --- run_cypher -------------------------------------------------------------


def test_run_cypher_rejects_write_query_without_executing():
    session = FakeSession()
    result = mcp_server.run_cypher(session, "CREATE (n)")
    assert result == {
        "error": "query rejected: read-only Cypher only (no writes, procedures, or semicolons)"
    }
    assert session.read_queries == []


def test_run_cypher_executes_sanitized_query():
    session = FakeSession(records=[{"name": "GERMANY"}])
    result = mcp_server.run_cypher(session, "MATCH (e:Entity) RETURN e.name AS name")
    query, params = session.read_queries[0]
    # validate_cypher appends LIMIT 50 when the query has none
    assert query == "MATCH (e:Entity) RETURN e.name AS name LIMIT 50"
    assert params == {}
    assert result == {"rows": [{"name": "GERMANY"}], "row_count": 1}


def test_run_cypher_caps_rows_at_50_regardless_of_query_limit():
    session = FakeSession(records=[{"i": i} for i in range(60)])
    result = mcp_server.run_cypher(session, "MATCH (e:Entity) RETURN e.name AS i LIMIT 60")
    assert result["row_count"] == 50
    assert len(result["rows"]) == 50


def test_run_cypher_converts_temporals_to_json_safe_strings():
    session = FakeSession(records=[{"observed_at": FakeTemporal()}])
    result = mcp_server.run_cypher(session, "MATCH (ev:Event) RETURN ev.observed_at LIMIT 1")
    assert result["rows"][0]["observed_at"] == "2026-08-26T10:00:00+00:00"
    json.dumps(result)  # must not raise


# --- server registration ----------------------------------------------------


def test_build_server_exposes_exactly_the_six_tools():
    server = mcp_server.build_server(driver=object())
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "search_entities",
        "entity_timeline",
        "connections",
        "recent_events",
        "graph_stats",
        "run_cypher",
    }
    # docstrings are the MCP descriptions a consuming LLM sees
    assert all(tool.description for tool in tools)
