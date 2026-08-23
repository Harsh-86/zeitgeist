import re

from zeitgeist.resolver import graph


def placeholders(cypher: str) -> set[str]:
    return set(re.findall(r"\$(\w+)", cypher))


class FakeResult:
    """Fake neo4j Result: iterable of dict-like records, plus .single()."""

    def __init__(self, records=None):
        self._records = [dict(r) for r in (records or [])]

    def __iter__(self):
        return iter(self._records)

    def single(self):
        return self._records[0] if self._records else None


class FakeSession:
    """Fake neo4j session: records every (cypher, params) call, returns
    canned FakeResults in call order."""

    def __init__(self, results=None):
        self.queries: list[tuple[str, dict]] = []
        self._results = list(results) if results is not None else []

    def run(self, cypher, **params):
        self.queries.append((cypher, params))
        if self._results:
            return self._results.pop(0)
        return FakeResult([])


# --- schema ---------------------------------------------------------------


def test_schema_statements_index_is_generic():
    assert any(
        "is_generic" in stmt and "IF NOT EXISTS" in stmt for stmt in graph.SCHEMA_STATEMENTS
    )


def test_ensure_schema_runs_every_statement():
    session = FakeSession()
    graph.ensure_schema(session)
    ran = [q for q, _ in session.queries]
    assert ran == graph.SCHEMA_STATEMENTS


# --- fetch_unscreened_entities ---------------------------------------------


def test_fetch_unscreened_entities_params_match_cypher_placeholders():
    session = FakeSession()
    graph.fetch_unscreened_entities(session, min_events=5, limit=10)
    cypher, params = session.queries[0]
    assert cypher == graph.FETCH_UNSCREENED_ENTITIES_CYPHER
    assert placeholders(cypher) == set(params)
    assert params == {"min_events": 5, "limit": 10}


def test_fetch_unscreened_entities_filters_on_generic_checked_is_null():
    where_clause = graph.FETCH_UNSCREENED_ENTITIES_CYPHER.split("WHERE")[1].split("RETURN")[0]
    assert "generic_checked IS NULL" in where_clause
    assert "is_generic" not in where_clause


def test_fetch_unscreened_entities_returns_name_count_tuples():
    session = FakeSession(
        results=[FakeResult([{"name": "NATO", "count": 9}, {"name": "OPEC", "count": 4}])]
    )
    result = graph.fetch_unscreened_entities(session)
    assert result == [("NATO", 9), ("OPEC", 4)]


# --- fetch_entities ---------------------------------------------------------


def test_fetch_entities_params_match_cypher_placeholders():
    session = FakeSession()
    graph.fetch_entities(session, min_events=7, limit=20)
    cypher, params = session.queries[0]
    assert cypher == graph.FETCH_ENTITIES_CYPHER
    assert placeholders(cypher) == set(params)
    assert params == {"min_events": 7, "limit": 20}


def test_fetch_entities_requires_screened_and_non_generic():
    where_clause = graph.FETCH_ENTITIES_CYPHER.split("WHERE")[1].split("RETURN")[0]
    assert "generic_checked = true" in where_clause
    assert "is_generic = false" in where_clause


def test_fetch_entities_excludes_already_aliased_entities():
    where_clause = graph.FETCH_ENTITIES_CYPHER.split("WHERE")[1].split("RETURN")[0]
    assert "NOT (e)-[:ALIAS_OF]->()" in where_clause


def test_fetch_entities_returns_name_count_tuples():
    session = FakeSession(results=[FakeResult([{"name": "ECB", "count": 12}])])
    result = graph.fetch_entities(session)
    assert result == [("ECB", 12)]


def test_fetch_entities_defaults():
    session = FakeSession()
    graph.fetch_entities(session)
    _, params = session.queries[0]
    assert params == {"min_events": 3, "limit": 1000}


# --- fetch_sample_relations -------------------------------------------------


def test_fetch_sample_relations_params_match_cypher_placeholders():
    session = FakeSession()
    graph.fetch_sample_relations(session, "NATO", limit=3)
    cypher, params = session.queries[0]
    assert cypher == graph.FETCH_SAMPLE_RELATIONS_CYPHER
    assert placeholders(cypher) == set(params)
    assert params == {"name": "NATO", "limit": 3}


def test_fetch_sample_relations_returns_relation_strings():
    session = FakeSession(
        results=[FakeResult([{"relation": "condemns"}, {"relation": "meets with"}])]
    )
    result = graph.fetch_sample_relations(session, "NATO")
    assert result == ["condemns", "meets with"]


def test_fetch_sample_relations_default_limit_is_5():
    session = FakeSession()
    graph.fetch_sample_relations(session, "NATO")
    _, params = session.queries[0]
    assert params["limit"] == 5


# --- fetch_judged_pairs ------------------------------------------------------


def test_fetch_judged_pairs_has_no_placeholders():
    assert placeholders(graph.FETCH_JUDGED_PAIRS_CYPHER) == set()


def test_fetch_judged_pairs_returns_frozensets():
    session = FakeSession(
        results=[FakeResult([{"a": "NATO", "b": "North Atlantic Treaty Organization"}])]
    )
    result = graph.fetch_judged_pairs(session)
    assert result == {frozenset({"NATO", "North Atlantic Treaty Organization"})}


def test_fetch_judged_pairs_empty_when_no_edges():
    session = FakeSession(results=[FakeResult([])])
    assert graph.fetch_judged_pairs(session) == set()


# --- mark_generic ------------------------------------------------------------


def test_mark_generic_params_match_cypher_placeholders():
    session = FakeSession()
    graph.mark_generic(session, "POLICE", generic=True, confidence=0.95)
    cypher, params = session.queries[0]
    assert cypher == graph.MARK_GENERIC_CYPHER
    assert placeholders(cypher) == set(params)
    assert params == {"name": "POLICE", "generic": True, "confidence": 0.95}


def test_mark_generic_only_sets_properties_never_deletes():
    assert "SET" in graph.MARK_GENERIC_CYPHER
    assert "DELETE" not in graph.MARK_GENERIC_CYPHER
    assert "REMOVE" not in graph.MARK_GENERIC_CYPHER


# --- record_judgment ---------------------------------------------------------


def test_record_judgment_params_match_cypher_placeholders():
    session = FakeSession()
    graph.record_judgment(session, "NATO", "North Atlantic Treaty Organization", "SAME", 0.9)
    cypher, params = session.queries[0]
    assert cypher == graph.RECORD_JUDGMENT_CYPHER
    assert placeholders(cypher) == set(params)
    assert params == {
        "a": "NATO",
        "b": "North Atlantic Treaty Organization",
        "verdict": "SAME",
        "confidence": 0.9,
    }


def test_record_judgment_is_on_create_set_only():
    """Idempotency shape: a judgment, once written, is immutable."""
    assert "MERGE" in graph.RECORD_JUDGMENT_CYPHER
    assert "ON CREATE SET" in graph.RECORD_JUDGMENT_CYPHER
    assert "ON MATCH SET" not in graph.RECORD_JUDGMENT_CYPHER


def test_record_judgment_does_not_merge_entity_nodes():
    """Entities are MATCHed, not MERGEd — only the ER_JUDGED edge is created."""
    before_merge = graph.RECORD_JUDGMENT_CYPHER.split("MERGE")[0]
    assert "MATCH (a:Entity" in before_merge
    assert "MATCH (b:Entity" in before_merge


# --- write_alias --------------------------------------------------------------
#
# Query sequence on the happy path: [0] resolve canonical, [1] check whether
# alias_name already has an outgoing ALIAS_OF edge, [2] write the edge (only
# reached if neither guard short-circuits).


def not_aliased_result():
    """Canned result for the existing-alias check: alias has no parent yet."""
    return FakeResult([{"has_alias": False}])


def test_write_alias_resolves_canonical_with_no_existing_alias():
    session = FakeSession(
        results=[FakeResult([{"resolved": "European Central Bank"}]), not_aliased_result()]
    )
    graph.write_alias(session, "ECB", "European Central Bank")

    resolve_cypher, resolve_params = session.queries[0]
    assert resolve_cypher == graph.RESOLVE_CANONICAL_CYPHER
    assert placeholders(resolve_cypher) == set(resolve_params)
    assert resolve_params == {"canonical_name": "European Central Bank"}

    check_cypher, check_params = session.queries[1]
    assert check_cypher == graph.CHECK_EXISTING_ALIAS_CYPHER
    assert placeholders(check_cypher) == set(check_params)
    assert check_params == {"alias_name": "ECB"}

    write_cypher, write_params = session.queries[2]
    assert write_cypher == graph.WRITE_ALIAS_CYPHER
    assert placeholders(write_cypher) == set(write_params)
    assert write_params == {"alias_name": "ECB", "resolved_name": "European Central Bank"}
    assert len(session.queries) == 3


def test_write_alias_single_hop_follows_existing_alias_of_edge():
    """canonical_name is itself already an alias: resolve to ITS target."""
    session = FakeSession(
        results=[FakeResult([{"resolved": "European Central Bank"}]), not_aliased_result()]
    )
    graph.write_alias(session, "Central Bank of Europe", "ECB")

    _, write_params = session.queries[2]
    assert write_params == {
        "alias_name": "Central Bank of Europe",
        "resolved_name": "European Central Bank",
    }


def test_write_alias_falls_back_to_canonical_name_when_not_found():
    session = FakeSession(results=[FakeResult([]), not_aliased_result()])  # canonical missing
    graph.write_alias(session, "ECB", "European Central Bank")

    _, write_params = session.queries[2]
    assert write_params == {"alias_name": "ECB", "resolved_name": "European Central Bank"}


def test_write_alias_logs_warning_when_canonical_name_matches_no_entity(caplog):
    session = FakeSession(results=[FakeResult([]), not_aliased_result()])
    with caplog.at_level("WARNING", logger="zeitgeist.resolver.graph"):
        graph.write_alias(session, "ECB", "European Central Bank")
    assert any(
        "European Central Bank" in message and "no Entity node" in message
        for message in caplog.messages
    )


def test_write_alias_merges_edge_never_merges_nodes():
    assert "MERGE (alias)-[:ALIAS_OF]->(resolved)" in graph.WRITE_ALIAS_CYPHER
    assert "MERGE (alias:Entity" not in graph.WRITE_ALIAS_CYPHER
    assert "MERGE (resolved:Entity" not in graph.WRITE_ALIAS_CYPHER


def test_write_alias_self_loop_is_a_noop(caplog):
    """Resolution collapsing to the alias itself must not MERGE anything."""
    session = FakeSession(results=[FakeResult([{"resolved": "ECB"}])])
    with caplog.at_level("INFO", logger="zeitgeist.resolver.graph"):
        graph.write_alias(session, "ECB", "ECB")

    # Only the resolve query ran; no existing-alias check, no MERGE write.
    assert len(session.queries) == 1
    assert session.queries[0][0] == graph.RESOLVE_CANONICAL_CYPHER
    assert any("self-loop" in message for message in caplog.messages)


def test_write_alias_multi_parent_is_a_noop_first_judgment_wins(caplog):
    """alias_name already has an outgoing ALIAS_OF edge: no new edge written."""
    session = FakeSession(
        results=[
            FakeResult([{"resolved": "European Central Bank"}]),
            FakeResult([{"has_alias": True}]),
        ]
    )
    with caplog.at_level("INFO", logger="zeitgeist.resolver.graph"):
        graph.write_alias(session, "ECB", "European Central Bank")

    # Resolve + existing-alias check ran, but no MERGE write followed.
    assert len(session.queries) == 2
    assert session.queries[1][0] == graph.CHECK_EXISTING_ALIAS_CYPHER
    assert any(
        "already aliased" in message and "first judgment wins" in message
        for message in caplog.messages
    )


def test_check_existing_alias_params_match_cypher_placeholders():
    session = FakeSession(
        results=[FakeResult([{"resolved": "European Central Bank"}]), not_aliased_result()]
    )
    graph.write_alias(session, "ECB", "European Central Bank")
    cypher, params = session.queries[1]
    assert placeholders(cypher) == set(params)


# --- CANONICAL_PATTERN doc-constant -------------------------------------------


def test_canonical_pattern_uses_coalesce_of_alias_target():
    assert "OPTIONAL MATCH (e)-[:ALIAS_OF]->(c)" in graph.CANONICAL_PATTERN
    assert "coalesce(c, e)" in graph.CANONICAL_PATTERN
