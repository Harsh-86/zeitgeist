"""Neo4j writer: merges claims into the accretive temporal graph."""

from dataclasses import asdict

from zeitgeist.models import Claim

SCHEMA_STATEMENTS = [
    (
        "CREATE CONSTRAINT event_id_unique IF NOT EXISTS "
        "FOR (ev:Event) REQUIRE ev.event_id IS UNIQUE"
    ),
    "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
    # /recent sorts by observed_at over the whole graph; without this index
    # Neo4j materializes and sorts every event per request.
    "CREATE INDEX event_observed_at IF NOT EXISTS FOR (ev:Event) ON (ev.observed_at)",
]

CLAIM_CYPHER = """
MERGE (ev:Event {event_id: $event_id})
ON CREATE SET
  ev.relation = $relation,
  ev.event_code = $event_code,
  ev.quad_class = $quad_class,
  ev.goldstein = $goldstein,
  ev.tone = $tone,
  ev.num_mentions = $num_mentions,
  ev.occurred_on = date($occurred_on),
  ev.observed_at = datetime($observed_at),
  ev.geo_name = $geo_name,
  ev.lat = $geo_lat,
  ev.lon = $geo_lon,
  ev.source_url = $source_url,
  ev.confidence = $confidence,
  ev.tier = $tier,
  ev.detail = $detail
MERGE (s:Entity {name: $subject})
MERGE (s)-[:ACTOR1_IN]->(ev)
FOREACH (_ IN CASE WHEN $object IS NULL THEN [] ELSE [1] END |
  MERGE (o:Entity {name: $object})
  MERGE (ev)-[:ACTOR2]->(o)
)
"""


def claim_to_params(claim: Claim) -> dict:
    return asdict(claim)


def ensure_schema(session) -> None:
    for statement in SCHEMA_STATEMENTS:
        session.run(statement)


def write_claim(session, claim: Claim) -> None:
    session.run(CLAIM_CYPHER, claim_to_params(claim))
