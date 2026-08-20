from tests.unit.test_models import make_event
from zeitgeist.extractor.main import process_message
from zeitgeist.models import Claim


def test_process_message_produces_claim_bytes():
    payloads = process_message(make_event().to_json())
    assert len(payloads) == 1
    claim = Claim.from_json(payloads[0])
    assert claim.subject == "UNITED STATES"
    assert claim.relation == "CONSULTED"


def test_process_message_actorless_event_produces_nothing():
    event = make_event(actor1_name=None, actor2_name=None)
    assert process_message(event.to_json()) == []


def test_process_message_garbage_input_produces_nothing():
    assert process_message(b"not json at all") == []
