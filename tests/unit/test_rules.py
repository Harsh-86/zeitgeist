from tests.unit.test_models import make_event
from zeitgeist.extractor.rules import event_to_claims


def test_two_actor_event_yields_subject_relation_object():
    claims = event_to_claims(make_event())
    assert len(claims) == 1
    claim = claims[0]
    assert claim.subject == "UNITED STATES"
    assert claim.relation == "CONSULTED"  # root code 04
    assert claim.object == "EUROPEAN CENTRAL BANK"
    assert claim.event_id == "1234567890"
    assert claim.confidence == 1.0  # 12 mentions capped at 1.0


def test_actor1_only_event_has_null_object():
    claims = event_to_claims(make_event(actor2_name=None, actor2_code=None))
    assert claims[0].object is None
    assert claims[0].subject == "UNITED STATES"


def test_actor2_only_event_promotes_actor2_to_subject():
    claims = event_to_claims(make_event(actor1_name=None, actor1_code=None))
    assert claims[0].subject == "EUROPEAN CENTRAL BANK"
    assert claims[0].object is None


def test_no_actor_event_yields_nothing():
    assert event_to_claims(make_event(actor1_name=None, actor2_name=None)) == []


def test_unknown_root_code_falls_back_to_interacted_with():
    claims = event_to_claims(make_event(event_root_code="99"))
    assert claims[0].relation == "INTERACTED_WITH"


def test_confidence_scales_with_mentions():
    claims = event_to_claims(make_event(num_mentions=3))
    assert claims[0].confidence == 0.3
