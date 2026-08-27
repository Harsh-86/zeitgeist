"""Read-only MCP server over the zeitgeist news graph (stdio transport).

Exposes LLM-free graph tools — entity search, timelines, connections, recent
events, stats, and a validated read-only Cypher escape hatch. The visitor's
own LLM (Claude Desktop, Claude Code, any MCP client) does the reasoning, so
no API key is needed here.

Run it with:

    uv run python -m zeitgeist.mcp_server

Bolt access to the graph's Neo4j is required: start the local stack
(`make up`) or tunnel to production (`ssh -L 7687:127.0.0.1:7687 <host>`).
Connection settings come from NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD
(see Settings.from_env for defaults).

Read-only enforcement is two-layer: every tool runs module-level
parameterized Cypher constants via session.execute_read (Neo4j rejects
writes server-side), and run_cypher additionally gates arbitrary queries
through agent.query.validate_cypher.
"""


import logging

from mcp.server.mcpserver import MCPServer

from zeitgeist.agent.query import validate_cypher
from zeitgeist.config import Settings

_MAX_LIMIT = 50
_MAX_ROWS = 50

_VALID_TIERS = {"rules", "llm"}

RUN_CYPHER_REJECTED = (
    "query rejected: read-only Cypher only (no writes, procedures, or semicolons)"
)

SEARCH_ENTITIES_CYPHER = """
MATCH (e:Entity)
WHERE toUpper(e.name) CONTAINS toUpper($name_fragment)
OPTIONAL MATCH (e)-[:ACTOR1_IN|ACTOR2]-(ev:Event)
WITH e, count(ev) AS event_count
OPTIONAL MATCH (e)-[:ALIAS_OF]->(c)
RETURN e.name AS name, event_count, coalesce(c.name, e.name) AS canonical
ORDER BY event_count DESC
LIMIT $limit
"""

# entity_timeline is composed from these fragments: MATCH, then a WHERE built
# only from the clauses whose parameters were actually provided, then the
# shared tail. Everything stays fully parameterized — the fragments are
# constants, never interpolated values.
ENTITY_TIMELINE_MATCH = """
MATCH (e:Entity {name: $name})-[rel:ACTOR1_IN|ACTOR2]-(ev:Event)
"""
ENTITY_TIMELINE_SINCE_CLAUSE = "ev.observed_at >= datetime($since)"
ENTITY_TIMELINE_UNTIL_CLAUSE = "ev.observed_at <= datetime($until)"
ENTITY_TIMELINE_RETURN = """
OPTIONAL MATCH (subj:Entity)-[:ACTOR1_IN]->(ev)
OPTIONAL MATCH (ev)-[:ACTOR2]->(obj:Entity)
WITH ev,
     CASE WHEN type(rel) = 'ACTOR1_IN' THEN 'subject' ELSE 'object' END AS role,
     CASE WHEN type(rel) = 'ACTOR1_IN' THEN obj.name ELSE subj.name END AS other
RETURN ev.relation AS relation, other, role,
       ev.observed_at AS observed_at, ev.tier AS tier, ev.detail AS detail,
       ev.source_url AS source_url
ORDER BY observed_at DESC
LIMIT $limit
"""

CONNECTIONS_CYPHER = """
MATCH (s:Entity)-[:ACTOR1_IN]->(ev:Event)-[:ACTOR2]->(o:Entity)
WHERE (s.name = $a AND o.name = $b) OR (s.name = $b AND o.name = $a)
RETURN s.name AS subject, ev.relation AS relation, o.name AS object,
       ev.observed_at AS observed_at, ev.tier AS tier, ev.source_url AS source_url
ORDER BY observed_at DESC
LIMIT $limit
"""

# recent_events is composed like entity_timeline: the tier WHERE joins the
# query only when a valid tier was requested.
RECENT_EVENTS_MATCH = """
MATCH (s:Entity)-[:ACTOR1_IN]->(ev:Event)-[:ACTOR2]->(o:Entity)
"""
RECENT_EVENTS_TIER_CLAUSE = "WHERE ev.tier = $tier"
RECENT_EVENTS_RETURN = """
RETURN s.name AS subject, ev.relation AS relation, o.name AS object,
       ev.observed_at AS observed_at, ev.tier AS tier, ev.source_url AS source_url
ORDER BY observed_at DESC
LIMIT $limit
"""

GRAPH_STATS_CYPHER = """
RETURN COUNT { MATCH (e:Entity) } AS entities,
       COUNT { MATCH (ev:Event) } AS events,
       COUNT { MATCH (lv:Event {tier: 'llm'}) } AS llm_events,
       COUNT { MATCH (:Entity)-[:ALIAS_OF]->(:Entity) } AS alias_edges
"""


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, _MAX_LIMIT))


def _plain(value):
    """Recursively convert a Neo4j record value to a JSON-serializable one.

    Primitives pass through; lists and dicts recurse; anything else (Neo4j
    temporals, spatial points, ...) becomes str(value).
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return str(value)


def _read_rows(session, query: str, **params) -> list[dict]:
    """Run a parameterized read query via execute_read; rows as plain dicts."""
    records = session.execute_read(lambda tx: [dict(r) for r in tx.run(query, **params)])
    return [{key: _plain(value) for key, value in record.items()} for record in records]


def search_entities(session, name_fragment: str, limit: int = 10) -> list[dict]:
    """Case-insensitive substring search over entity names, busiest first.

    Returns [{name, event_count, canonical}] where canonical is the entity's
    ALIAS_OF target when one exists (the name to use in other tools).
    """
    return _read_rows(
        session, SEARCH_ENTITIES_CYPHER, name_fragment=name_fragment, limit=_clamp_limit(limit)
    )


def entity_timeline(
    session,
    name: str,
    since: str | None = None,
    until: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """Events where the entity appears as subject or object, newest first.

    `name` is matched exactly and entity names are UPPERCASE in the graph
    (e.g. "GERMANY"). since/until are ISO-8601 datetime strings; each WHERE
    clause joins the query only when its value was provided.
    """
    clauses = []
    params: dict = {"name": name, "limit": _clamp_limit(limit)}
    if since is not None:
        clauses.append(ENTITY_TIMELINE_SINCE_CLAUSE)
        params["since"] = since
    if until is not None:
        clauses.append(ENTITY_TIMELINE_UNTIL_CLAUSE)
        params["until"] = until
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = ENTITY_TIMELINE_MATCH + where + ENTITY_TIMELINE_RETURN
    return _read_rows(session, query, **params)


def connections(session, a: str, b: str, limit: int = 25) -> list[dict]:
    """Events directly linking entities a and b, in either direction."""
    return _read_rows(session, CONNECTIONS_CYPHER, a=a, b=b, limit=_clamp_limit(limit))


def recent_events(session, limit: int = 25, tier: str | None = None) -> list[dict]:
    """The latest full-pair claims, optionally filtered to one tier.

    tier must be "rules" or "llm"; anything else is ignored (no filter).
    """
    params: dict = {"limit": _clamp_limit(limit)}
    where = ""
    if tier in _VALID_TIERS:
        where = RECENT_EVENTS_TIER_CLAUSE
        params["tier"] = tier
    query = RECENT_EVENTS_MATCH + where + RECENT_EVENTS_RETURN
    return _read_rows(session, query, **params)


def graph_stats(session) -> dict:
    """Whole-graph counts: {entities, events, llm_events, alias_edges}."""
    rows = _read_rows(session, GRAPH_STATS_CYPHER)
    return rows[0]


def run_cypher(session, query: str) -> dict:
    """Validated read-only Cypher: {"rows", "row_count"} or {"error"}.

    The query must pass agent.query.validate_cypher (no writes, procedures,
    or semicolons; a LIMIT 50 is appended when missing). Results are capped
    at 50 rows regardless of the query's own LIMIT.
    """
    sanitized = validate_cypher(query)
    if sanitized is None:
        return {"error": RUN_CYPHER_REJECTED}
    rows = _read_rows(session, sanitized)[:_MAX_ROWS]
    return {"rows": rows, "row_count": len(rows)}


def build_server(driver) -> MCPServer:
    """Register the six graph tools on an MCPServer bound to this driver.

    Each MCP tool is a thin wrapper that opens a session and delegates to the
    plain function above; the wrapper docstrings are the tool descriptions an
    MCP client's LLM sees.
    """
    server = MCPServer("zeitgeist")

    @server.tool()
    def search_entities(name_fragment: str, limit: int = 10) -> list[dict]:
        """Find entities in the zeitgeist news graph by name substring
        (case-insensitive). Use this FIRST to discover the exact entity name
        for the other tools. Returns up to `limit` (max 50) rows of
        {name, event_count, canonical}, busiest entities first; `canonical`
        is the resolved alias target — prefer it in follow-up calls.
        Example: name_fragment "german" finds "GERMANY"."""
        with driver.session() as session:
            return _search_entities(session, name_fragment, limit)

    @server.tool()
    def entity_timeline(
        name: str, since: str | None = None, until: str | None = None, limit: int = 25
    ) -> list[dict]:
        """News events involving one entity (as subject or object), newest
        first. `name` must match exactly and entity names are UPPERCASE
        (e.g. "GERMANY", "UNITED STATES") — use search_entities when unsure.
        Optional since/until are ISO-8601 datetimes (e.g.
        "2026-08-01T00:00:00Z") bounding the observation window. Returns up
        to `limit` (max 50) rows of {relation, other, role, observed_at,
        tier, detail, source_url}; `detail` is a one-sentence fact on
        tier "llm" events, `source_url` is the citation."""
        with driver.session() as session:
            return _entity_timeline(session, name, since=since, until=until, limit=limit)

    @server.tool()
    def connections(a: str, b: str, limit: int = 25) -> list[dict]:
        """News events directly linking two entities, in either direction,
        newest first. Both names must match exactly and are UPPERCASE
        (e.g. a="RUSSIA", b="UKRAINE"). Returns up to `limit` (max 50) rows
        of {subject, relation, object, observed_at, tier, source_url}."""
        with driver.session() as session:
            return _connections(session, a, b, limit)

    @server.tool()
    def recent_events(limit: int = 25, tier: str | None = None) -> list[dict]:
        """The latest news claims in the graph (subject-relation-object
        pairs), newest first. Optional tier filter: "rules" (high-volume
        pattern-extracted) or "llm" (richer, LLM-extracted with a `detail`
        sentence); any other value is ignored. Returns up to `limit` (max
        50) rows of {subject, relation, object, observed_at, tier,
        source_url}."""
        with driver.session() as session:
            return _recent_events(session, limit=limit, tier=tier)

    @server.tool()
    def graph_stats() -> dict:
        """Size of the zeitgeist news graph right now: {entities, events,
        llm_events, alias_edges}. Cheap; good first call to see what you
        are working with."""
        with driver.session() as session:
            return _graph_stats(session)

    @server.tool()
    def run_cypher(query: str) -> dict:
        """Run an arbitrary READ-ONLY Cypher query against the news graph.
        Schema: (s:Entity)-[:ACTOR1_IN]->(ev:Event)-[:ACTOR2]->(o:Entity),
        plus (alias:Entity)-[:ALIAS_OF]->(canonical:Entity). Entity has
        `name` (UPPERCASE); Event has relation, observed_at (DATETIME),
        occurred_on (DATE), tier, detail, source_url, goldstein, tone,
        geo_name, lat, lon. Writes, procedure CALLs, and semicolons are
        rejected. Returns {rows, row_count}, capped at 50 rows, or
        {error} when the query is rejected."""
        with driver.session() as session:
            return _run_cypher(session, query)

    return server


# The wrappers above intentionally shadow the plain functions inside
# build_server's scope so the MCP tool names match the module API; these
# aliases let them delegate to the module-level implementations.
_search_entities = search_entities
_entity_timeline = entity_timeline
_connections = connections
_recent_events = recent_events
_graph_stats = graph_stats
_run_cypher = run_cypher


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    from neo4j import GraphDatabase

    settings = Settings.from_env()
    driver = GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        build_server(driver).run()  # stdio transport (the default)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
