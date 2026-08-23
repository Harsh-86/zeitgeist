"""Graph layer for entity resolution: reads over the accretive graph plus
reversible writes (ALIAS_OF / ER_JUDGED edges and Entity metadata properties).

Binding invariants (see the Phase 2b plan's Global Constraints):
  - Never merge, delete, or rewrite existing nodes or edges.
  - The only writes this module performs are: ADD ALIAS_OF edges, ADD
    ER_JUDGED edges, and SET is_generic / generic_checked / generic_confidence
    properties on Entity (additive metadata, not history rewrite).
  - record_judgment uses ON CREATE SET only — a judgment, once written, is
    immutable.
  - write_alias enforces single-hop, single-parent (first judgment wins), and
    no-self-loop invariants (see its docstring for the exact approach and its
    accepted limitation).
"""

import logging

logger = logging.getLogger("zeitgeist.resolver.graph")

SCHEMA_STATEMENTS = [
    "CREATE INDEX entity_is_generic IF NOT EXISTS FOR (e:Entity) ON (e.is_generic)",
]

# Read-side idiom for resolving any Entity to its canonical form. Not run by
# this module directly; documented here as the pattern downstream readers
# (e.g. the API layer) should follow once ALIAS_OF edges exist.
CANONICAL_PATTERN = """
MATCH (e:Entity)
OPTIONAL MATCH (e)-[:ALIAS_OF]->(c)
RETURN coalesce(c, e) AS canonical
"""

FETCH_UNSCREENED_ENTITIES_CYPHER = """
MATCH (e:Entity)-[:ACTOR1_IN]->(ev:Event)
WITH e, count(ev) AS count
WHERE count >= $min_events AND e.generic_checked IS NULL
RETURN e.name AS name, count
ORDER BY count DESC
LIMIT $limit
"""

FETCH_ENTITIES_CYPHER = """
MATCH (e:Entity)-[:ACTOR1_IN]->(ev:Event)
WITH e, count(ev) AS count
WHERE count >= $min_events AND e.generic_checked = true AND e.is_generic = false
  AND NOT (e)-[:ALIAS_OF]->()
RETURN e.name AS name, count
ORDER BY count DESC
LIMIT $limit
"""

FETCH_SAMPLE_RELATIONS_CYPHER = """
MATCH (e:Entity {name: $name})-[:ACTOR1_IN]->(ev:Event)
RETURN ev.relation AS relation
LIMIT $limit
"""

FETCH_JUDGED_PAIRS_CYPHER = """
MATCH (a:Entity)-[:ER_JUDGED]->(b:Entity)
RETURN a.name AS a, b.name AS b
"""

MARK_GENERIC_CYPHER = """
MATCH (e:Entity {name: $name})
SET e.generic_checked = true,
    e.is_generic = $generic,
    e.generic_confidence = $confidence
"""

RECORD_JUDGMENT_CYPHER = """
MATCH (a:Entity {name: $a})
MATCH (b:Entity {name: $b})
MERGE (a)-[j:ER_JUDGED]->(b)
ON CREATE SET
  j.verdict = $verdict,
  j.confidence = $confidence,
  j.judged_at = datetime()
"""

RESOLVE_CANONICAL_CYPHER = """
MATCH (c:Entity {name: $canonical_name})
OPTIONAL MATCH (c)-[:ALIAS_OF]->(cc:Entity)
RETURN coalesce(cc.name, c.name) AS resolved
"""

CHECK_EXISTING_ALIAS_CYPHER = """
MATCH (alias:Entity {name: $alias_name})
OPTIONAL MATCH (alias)-[:ALIAS_OF]->(existing)
RETURN existing IS NOT NULL AS has_alias
"""

WRITE_ALIAS_CYPHER = """
MATCH (alias:Entity {name: $alias_name})
MATCH (resolved:Entity {name: $resolved_name})
MERGE (alias)-[:ALIAS_OF]->(resolved)
"""


def ensure_schema(session) -> None:
    for statement in SCHEMA_STATEMENTS:
        session.run(statement)


def fetch_unscreened_entities(
    session, min_events: int = 3, limit: int = 1000
) -> list[tuple[str, int]]:
    """Entities with at least min_events ACTOR1_IN events that have never
    been screened for genericness (generic_checked IS NULL).

    Used by Task 4's generic-screening step: these are the candidates fed to
    the judge for a generic/specific verdict.
    """
    result = session.run(FETCH_UNSCREENED_ENTITIES_CYPHER, min_events=min_events, limit=limit)
    return [(record["name"], record["count"]) for record in result]


def fetch_entities(session, min_events: int = 3, limit: int = 1000) -> list[tuple[str, int]]:
    """Entities with at least min_events ACTOR1_IN events that have already
    been screened AND confirmed non-generic (generic_checked = true AND
    is_generic = false).

    Also excludes entities that already have an outgoing ALIAS_OF edge (i.e.
    are already aliased to some canonical) — re-pairing an already-resolved
    entity would waste judge budget and could never change its parent under
    the first-judgment-wins rule (see write_alias).

    Used by Task 4's pair-candidate step: the safe universe to run
    candidate_pairs() over, since unscreened or generic names would produce
    spurious same-entity matches (e.g. "POLICE" matching many unrelated
    "... POLICE" names).
    """
    result = session.run(FETCH_ENTITIES_CYPHER, min_events=min_events, limit=limit)
    return [(record["name"], record["count"]) for record in result]


def fetch_sample_relations(session, name: str, limit: int = 5) -> list[str]:
    """Up to `limit` ev.relation strings for events where `name` is the
    ACTOR1_IN subject, used as context for the judge's LLM prompt."""
    result = session.run(FETCH_SAMPLE_RELATIONS_CYPHER, name=name, limit=limit)
    return [record["relation"] for record in result]


def fetch_judged_pairs(session) -> set[frozenset[str]]:
    """Endpoints of every existing ER_JUDGED edge, as unordered pairs, so
    callers can skip pairs already judged (SAME or DIFFERENT)."""
    result = session.run(FETCH_JUDGED_PAIRS_CYPHER)
    return {frozenset({record["a"], record["b"]}) for record in result}


def mark_generic(session, name: str, generic: bool, confidence: float) -> None:
    """Record a genericness verdict as additive metadata on the Entity node.

    Overwrites any prior verdict for this name (re-screening is allowed and
    expected to converge); does not touch any edge.
    """
    session.run(MARK_GENERIC_CYPHER, name=name, generic=generic, confidence=confidence)


def record_judgment(session, a: str, b: str, verdict: str, confidence: float) -> None:
    """Record a pair judgment as an ER_JUDGED edge from a to b.

    Uses ON CREATE SET only: a judgment, once written for this ordered pair,
    is immutable — calling this again with different verdict/confidence for
    the same (a, b) is a no-op on the existing edge's properties.
    """
    session.run(
        RECORD_JUDGMENT_CYPHER,
        a=a,
        b=b,
        verdict=verdict,
        confidence=confidence,
    )


def write_alias(session, alias_name: str, canonical_name: str) -> None:
    """Write an ALIAS_OF edge from alias_name to canonical_name, enforcing
    three invariants: single-hop, single-parent (first judgment wins), and
    no self-loops.

    Approach:
      1. Resolve canonical_name's own canonical. If the node named
         canonical_name already has an outgoing ALIAS_OF edge (i.e. it is
         itself an alias), follow that edge once and target its destination
         instead — this is what makes the result single-hop. If
         canonical_name has no such edge, resolution is canonical_name
         itself. If no Entity node named canonical_name exists at all, the
         resolve query returns no row; this is logged at WARNING (the
         judgment referenced a name that isn't in the graph) and resolution
         falls back to canonical_name, which then simply fails to MATCH in
         step 3 below — a cheap, safe no-op.
      2. No-self-loop guard: if the resolved name equals alias_name (the
         judgment would alias a node to itself, e.g. after resolution
         collapses through an existing edge), log INFO and return without
         writing anything.
      3. Single-parent guard ("first judgment wins"): if alias_name already
         has ANY outgoing ALIAS_OF edge, log INFO and return without writing
         anything — the first judgment that aliased this node stands, and is
         never overwritten. This is a pre-check-then-skip, which is race-free
         for this resolver's single-threaded, single-writer usage.
      4. Only if both guards pass: MERGE the ALIAS_OF edge from alias_name to
         the resolved name. Both alias_name and the resolved name are
         MATCHed, never MERGEd as nodes — write_alias never creates an
         Entity node (both are expected to already exist from Phase 1
         ingestion).

    Together, single-hop + single-parent-first-wins mean every Entity has at
    most one outgoing ALIAS_OF edge and that edge always points at a node
    with zero outgoing ALIAS_OF edges of its own. CANONICAL_PATTERN's reader
    idiom (one OPTIONAL MATCH hop, coalesce to self) can therefore rely on at
    most one ALIAS_OF hop ever needing to be followed.

    Accepted limitation: this only prevents chains/re-parenting from *this*
    write. It does not retroactively re-point edges that already point at
    alias_name (existing incoming ALIAS_OF edges are left as-is) if some
    other node was already aliased to alias_name before alias_name itself
    became an alias — that would require rewriting an existing edge, which
    the additions-only invariant forbids. In normal operation this is
    unreachable: fetch_entities excludes already-aliased entities from the
    pairing step, so an entity is only ever offered as a pairing candidate
    (and thus a `canonical_name`/aliasing target) while it has no outgoing
    ALIAS_OF edge of its own to later invalidate.
    """
    resolve_result = session.run(RESOLVE_CANONICAL_CYPHER, canonical_name=canonical_name)
    resolve_record = resolve_result.single()
    if resolve_record is None:
        logger.warning(
            "write_alias: canonical_name %r matches no Entity node; skipping", canonical_name
        )
        resolved_name = canonical_name
    else:
        resolved_name = resolve_record["resolved"]

    if resolved_name == alias_name:
        logger.info("write_alias: self-loop skipped (%r resolves to itself)", alias_name)
        return

    check_result = session.run(CHECK_EXISTING_ALIAS_CYPHER, alias_name=alias_name)
    check_record = check_result.single()
    if check_record is not None and check_record["has_alias"]:
        logger.info("write_alias: %r already aliased, first judgment wins", alias_name)
        return

    session.run(WRITE_ALIAS_CYPHER, alias_name=alias_name, resolved_name=resolved_name)
