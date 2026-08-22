"""Rules tier: map GDELT's structured CAMEO fields straight to graph claims. Zero LLM cost."""

from zeitgeist.models import Claim, GdeltEvent

CAMEO_ROOT_RELATIONS = {
    "01": "MADE_STATEMENT_ABOUT",
    "02": "APPEALED_TO",
    "03": "EXPRESSED_INTENT_TO_COOPERATE_WITH",
    "04": "CONSULTED",
    "05": "COOPERATED_DIPLOMATICALLY_WITH",
    "06": "COOPERATED_MATERIALLY_WITH",
    "07": "PROVIDED_AID_TO",
    "08": "YIELDED_TO",
    "09": "INVESTIGATED",
    "10": "DEMANDED_FROM",
    "11": "DISAPPROVED_OF",
    "12": "REJECTED",
    "13": "THREATENED",
    "14": "PROTESTED_AGAINST",
    "15": "EXHIBITED_FORCE_POSTURE_TOWARD",
    "16": "REDUCED_RELATIONS_WITH",
    "17": "COERCED",
    "18": "ASSAULTED",
    "19": "FOUGHT_WITH",
    "20": "USED_MASS_VIOLENCE_AGAINST",
}

FALLBACK_RELATION = "INTERACTED_WITH"


def event_to_claims(event: GdeltEvent) -> list[Claim]:
    subject = event.actor1_name or event.actor2_name
    if subject is None:
        return []
    obj = event.actor2_name if event.actor1_name else None
    return [
        Claim(
            subject=subject,
            relation=CAMEO_ROOT_RELATIONS.get(event.event_root_code, FALLBACK_RELATION),
            object=obj,
            event_id=event.event_id,
            event_code=event.event_code,
            quad_class=event.quad_class,
            goldstein=event.goldstein,
            tone=event.avg_tone,
            num_mentions=event.num_mentions,
            occurred_on=event.occurred_on,
            observed_at=event.observed_at,
            geo_name=event.geo_name,
            geo_lat=event.geo_lat,
            geo_lon=event.geo_lon,
            source_url=event.source_url,
            confidence=min(1.0, event.num_mentions / 10),
            tier="rules",
        )
    ]
