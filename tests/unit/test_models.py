from zeitgeist.models import Claim, GdeltEvent


def make_event(**overrides) -> GdeltEvent:
    base = {
        "event_id": "1234567890",
        "occurred_on": "2026-08-18",
        "actor1_code": "USAGOV",
        "actor1_name": "UNITED STATES",
        "actor2_code": "ECB",
        "actor2_name": "EUROPEAN CENTRAL BANK",
        "event_code": "042",
        "event_root_code": "04",
        "quad_class": 1,
        "goldstein": 1.9,
        "num_mentions": 12,
        "avg_tone": 2.5,
        "geo_name": "Frankfurt, Hessen, Germany",
        "geo_lat": 50.11,
        "geo_lon": 8.68,
        "observed_at": "2026-08-18T14:30:00Z",
        "source_url": "https://example.com/article",
    }
    base.update(overrides)
    return GdeltEvent(**base)


def test_gdelt_event_json_round_trip():
    ev = make_event()
    assert GdeltEvent.from_json(ev.to_json()) == ev


def test_gdelt_event_round_trip_preserves_nones():
    ev = make_event(actor2_code=None, actor2_name=None, goldstein=None, source_url=None)
    assert GdeltEvent.from_json(ev.to_json()) == ev


def test_claim_json_round_trip():
    claim = Claim(
        subject="UNITED STATES",
        relation="CONSULTED",
        object="EUROPEAN CENTRAL BANK",
        event_id="1234567890",
        event_code="042",
        quad_class=1,
        goldstein=1.9,
        tone=2.5,
        num_mentions=12,
        occurred_on="2026-08-18",
        observed_at="2026-08-18T14:30:00Z",
        geo_name="Frankfurt, Hessen, Germany",
        geo_lat=50.11,
        geo_lon=8.68,
        source_url="https://example.com/article",
        confidence=1.0,
    )
    assert Claim.from_json(claim.to_json()) == claim
