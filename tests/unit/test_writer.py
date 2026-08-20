from tests.unit.test_models import make_event
from zeitgeist.extractor.rules import event_to_claims
from zeitgeist.graph.writer import CLAIM_CYPHER, claim_to_params


def make_claim(**event_overrides):
    return event_to_claims(make_event(**event_overrides))[0]


def test_params_match_cypher_placeholders():
    params = claim_to_params(make_claim())
    import re

    placeholders = set(re.findall(r"\$(\w+)", CLAIM_CYPHER))
    assert placeholders == set(params)


def test_params_carry_temporal_fields():
    params = claim_to_params(make_claim())
    assert params["occurred_on"] == "2026-08-18"
    assert params["observed_at"] == "2026-08-18T14:30:00Z"


def test_params_null_object_passes_through():
    params = claim_to_params(make_claim(actor2_name=None, actor2_code=None))
    assert params["object"] is None


def test_cypher_is_idempotent_on_event_id():
    assert "MERGE (ev:Event {event_id: $event_id})" in CLAIM_CYPHER
