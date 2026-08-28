"""Deterministic eval fixture graph: ~150 events over ~20 entities with known,
gradable ground truth.

Every event is a literal row in _SPECS below — no randomness anywhere — and the
ground-truth summary returned by seed_graph is computed FROM those same literals
(single source of truth). Timestamps are derived from the caller's `now` with
fixed per-index offsets, so the same (session, now) always produces the exact
same graph.

Designed-in shape (relied on by evals/golden_questions.jsonl):
  - GERMANY: exactly 8 today / 5 week / 4 older participant events.
  - GERMANY<->FRANCE: exactly 4 linking events today (plus 1 this week).
  - UNITED STATES: the mega-hub (dominates "latest N of everything" results, so
    the all_records_mention grader catches null-filter bugs).
  - BHUTAN: quiet — exactly 1 older event, nothing else.
  - ECB: zero today events (recency-window discriminator).
  - NATO: rich in subject-only events (object=None) — a non-OPTIONAL ACTOR2
    match drops them and fails the golden minimum.
  - BRAZIL: exactly 6 events, never as object (unambiguous count question).
  - One ALIAS_OF pair: EU -> EUROPEAN UNION ("EU" is a substring of
    "EUROPEAN UNION", so one all_records_mention grader accepts both
    resolutions of an alias question).
"""

from datetime import datetime, timedelta

from zeitgeist.graph import writer
from zeitgeist.models import Claim
from zeitgeist.resolver import graph as resolver_graph

_T, _W, _O = "today", "week", "older"
_R, _L = "rules", "llm"

# (subject, relation, object, bucket, tier) — one literal row per event.
_SPECS: list[tuple[str, str, str | None, str, str]] = [
    # --- GERMANY as subject (4 today / 3 week / 3 older) ---
    ("GERMANY", "SIGNED_AGREEMENT_WITH", "FRANCE", _T, _L),
    ("GERMANY", "HOSTED_TALKS_WITH", "FRANCE", _T, _R),
    ("GERMANY", "ANNOUNCED_POLICY", None, _T, _L),
    ("GERMANY", "MET_WITH", "UN", _T, _R),
    ("GERMANY", "CONSULTED", "POLAND", _W, _R),
    ("GERMANY", "EXPRESSED_INTENT_TO_COOPERATE_WITH", "ECB", _W, _L),
    ("GERMANY", "APPEALED_TO", "UN", _W, _R),
    ("GERMANY", "CRITICIZED", "RUSSIA", _O, _R),
    ("GERMANY", "SIGNED_TRADE_DEAL_WITH", "CHINA", _O, _L),
    ("GERMANY", "PLEDGED_AID_TO", "TURKEY", _O, _R),
    # --- GERMANY as object (4 today / 2 week / 1 older) ---
    ("FRANCE", "MET_WITH", "GERMANY", _T, _R),
    ("FRANCE", "PRAISED", "GERMANY", _T, _L),
    ("UNITED STATES", "CONSULTED", "GERMANY", _T, _R),
    ("CHINA", "SIGNED_TRADE_DEAL_WITH", "GERMANY", _T, _L),
    ("FRANCE", "COOPERATED_WITH", "GERMANY", _W, _R),
    ("ECB", "MET_WITH", "GERMANY", _W, _R),
    ("RUSSIA", "CRITICIZED", "GERMANY", _O, _R),
    # --- FRANCE (beyond the GERMANY links) ---
    ("FRANCE", "CONDEMNED", "RUSSIA", _T, _R),
    ("FRANCE", "PLEDGED_AID_TO", "UKRAINE", _W, _L),
    ("FRANCE", "MET_WITH", "UN", _W, _R),
    ("FRANCE", "MET_WITH", "EGYPT", _W, _R),
    ("FRANCE", "SIGNED_AGREEMENT_WITH", "CANADA", _O, _R),
    ("FRANCE", "HOSTED_TALKS_WITH", "EGYPT", _O, _L),
    ("FRANCE", "CONSULTED", "UN", _O, _R),
    # --- ECB (zero today anywhere in this table) ---
    ("ECB", "RAISED_INTEREST_RATES", None, _W, _L),
    ("ECB", "COORDINATED_WITH", "EUROPEAN UNION", _W, _R),
    ("ECB", "CONSULTED", "FRANCE", _W, _R),
    ("ECB", "MET_WITH", "UNITED STATES", _O, _R),
    ("ECB", "CONSULTED", "JAPAN", _O, _L),
    # --- UNITED STATES: the mega-hub (22 more today / 12 week / 9 older) ---
    ("UNITED STATES", "IMPOSED_SANCTIONS_ON", "RUSSIA", _T, _L),
    ("UNITED STATES", "MET_WITH", "CHINA", _T, _R),
    ("UNITED STATES", "PLEDGED_AID_TO", "UKRAINE", _T, _L),
    ("UNITED STATES", "ADDRESSED", "UN", _T, _R),
    ("UNITED STATES", "SIGNED_TRADE_DEAL_WITH", "JAPAN", _T, _R),
    ("UNITED STATES", "CONSULTED", "INDIA", _T, _R),
    ("UNITED STATES", "HOSTED_TALKS_WITH", "CANADA", _T, _L),
    ("UNITED STATES", "NEGOTIATED_WITH", "MEXICO", _T, _R),
    ("UNITED STATES", "EXPRESSED_INTENT_TO_COOPERATE_WITH", "AUSTRALIA", _T, _R),
    ("UNITED STATES", "ANNOUNCED_POLICY", None, _T, _L),
    ("UNITED STATES", "PLEDGED_AID_TO", "EGYPT", _T, _R),
    ("UNITED STATES", "INVESTED_IN", "NIGERIA", _T, _R),
    ("UNITED STATES", "EXPRESSED_INTENT_TO_COOPERATE_WITH", "UN", _T, _R),
    ("UNITED STATES", "MET_WITH", "UN", _T, _R),
    ("UNITED STATES", "CRITICIZED", "RUSSIA", _T, _R),
    ("UNITED STATES", "CONSULTED", "JAPAN", _T, _R),
    ("UNITED STATES", "NEGOTIATED_WITH", "CHINA", _T, _L),
    ("UNITED STATES", "EXPRESSED_INTENT_TO_MEET_WITH", "INDIA", _T, _R),
    ("UNITED STATES", "HOSTED_TALKS_WITH", "EGYPT", _T, _R),
    ("UNITED STATES", "SIGNED_AGREEMENT_WITH", "AUSTRALIA", _T, _L),
    ("UNITED STATES", "COOPERATED_WITH", "MEXICO", _T, _R),
    ("UNITED STATES", "ANNOUNCED_SANCTIONS_AGAINST", "RUSSIA", _T, _L),
    ("UNITED STATES", "MET_WITH", "JAPAN", _W, _R),
    ("UNITED STATES", "CRITICIZED", "CHINA", _W, _L),
    ("UNITED STATES", "CONSULTED", "NATO", _W, _R),
    ("UNITED STATES", "SIGNED_AGREEMENT_WITH", "INDIA", _W, _R),
    ("UNITED STATES", "THREATENED_SANCTIONS_ON", "RUSSIA", _W, _R),
    ("UNITED STATES", "APPEALED_TO", "UN", _W, _R),
    ("UNITED STATES", "HOSTED_TALKS_WITH", "EGYPT", _W, _L),
    ("UNITED STATES", "COOPERATED_WITH", "CANADA", _W, _R),
    ("UNITED STATES", "MET_WITH", "UN", _W, _R),
    ("UNITED STATES", "CONSULTED", "MEXICO", _W, _R),
    ("UNITED STATES", "CONSULTED", "TURKEY", _W, _R),
    ("UNITED STATES", "NEGOTIATED_WITH", "JAPAN", _W, _R),
    ("UNITED STATES", "IMPOSED_TARIFFS_ON", "CHINA", _O, _L),
    ("UNITED STATES", "MET_WITH", "MEXICO", _O, _R),
    ("UNITED STATES", "PLEDGED_AID_TO", "UKRAINE", _O, _R),
    ("UNITED STATES", "CONSULTED", "AUSTRALIA", _O, _R),
    ("UNITED STATES", "CRITICIZED", "RUSSIA", _O, _R),
    ("UNITED STATES", "SIGNED_AGREEMENT_WITH", "JAPAN", _O, _L),
    ("UNITED STATES", "HOSTED_TALKS_WITH", "INDIA", _O, _R),
    ("UNITED STATES", "MET_WITH", "CANADA", _O, _R),
    ("UNITED STATES", "CRITICIZED", "CHINA", _O, _L),
    # --- EUROPEAN UNION + the EU alias node ---
    ("EUROPEAN UNION", "IMPOSED_SANCTIONS_ON", "RUSSIA", _T, _L),
    ("EUROPEAN UNION", "MET_WITH", "UKRAINE", _T, _R),
    ("EUROPEAN UNION", "SIGNED_TRADE_DEAL_WITH", "JAPAN", _W, _R),
    ("EUROPEAN UNION", "CONSULTED", "UKRAINE", _W, _R),
    ("EUROPEAN UNION", "PLEDGED_AID_TO", "EGYPT", _O, _R),
    ("EUROPEAN UNION", "MET_WITH", "UN", _O, _L),
    ("EU", "EXPRESSED_INTENT_TO_COOPERATE_WITH", "UKRAINE", _T, _R),
    # --- RUSSIA ---
    ("RUSSIA", "CONDEMNED", "UNITED STATES", _T, _R),
    ("RUSSIA", "THREATENED", "UKRAINE", _T, _L),
    ("RUSSIA", "MET_WITH", "CHINA", _W, _R),
    ("RUSSIA", "CRITICIZED", "NATO", _W, _R),
    ("RUSSIA", "NEGOTIATED_WITH", "TURKEY", _W, _L),
    ("RUSSIA", "SIGNED_AGREEMENT_WITH", "INDIA", _O, _R),
    ("RUSSIA", "REDUCED_RELATIONS_WITH", "EUROPEAN UNION", _O, _L),
    ("RUSSIA", "NEGOTIATED_WITH", "CHINA", _O, _R),
    # --- CHINA ---
    ("CHINA", "CRITICIZED", "UNITED STATES", _T, _R),
    ("CHINA", "EXPRESSED_INTENT_TO_COOPERATE_WITH", "RUSSIA", _T, _R),
    ("CHINA", "INVESTED_IN", "NIGERIA", _W, _R),
    ("CHINA", "CONSULTED", "INDIA", _W, _L),
    ("CHINA", "MET_WITH", "JAPAN", _W, _R),
    ("CHINA", "HOSTED_TALKS_WITH", "TURKEY", _O, _R),
    ("CHINA", "INVESTED_IN", "EGYPT", _O, _R),
    ("CHINA", "SIGNED_TRADE_DEAL_WITH", "AUSTRALIA", _O, _L),
    # --- UKRAINE ---
    ("UKRAINE", "APPEALED_TO", "NATO", _T, _L),
    ("UKRAINE", "MET_WITH", "EUROPEAN UNION", _T, _R),
    ("UKRAINE", "APPEALED_FOR_SUPPORT", None, _T, _R),
    ("UKRAINE", "CONSULTED", "UNITED STATES", _W, _R),
    ("UKRAINE", "CONDEMNED", "RUSSIA", _W, _R),
    ("UKRAINE", "MET_WITH", "CANADA", _W, _L),
    ("UKRAINE", "SIGNED_AGREEMENT_WITH", "POLAND", _O, _L),
    ("UKRAINE", "CONSULTED", "EUROPEAN UNION", _O, _R),
    # --- NATO (subject-only heavy) ---
    ("NATO", "HELD_EXERCISES", None, _T, _R),
    ("NATO", "ANNOUNCED_EXPANSION_PLANS", None, _T, _L),
    ("NATO", "ISSUED_STATEMENT", None, _T, _R),
    ("NATO", "PLEDGED_SUPPORT_TO", "UKRAINE", _T, _R),
    ("NATO", "CONVENED_SUMMIT", None, _W, _L),
    ("NATO", "CONSULTED", "POLAND", _W, _R),
    ("NATO", "REINFORCED_EASTERN_FLANK", None, _W, _R),
    ("NATO", "PUBLISHED_STRATEGY_REVIEW", None, _O, _L),
    # --- UN as subject ---
    ("UN", "CALLED_FOR_CEASEFIRE", None, _T, _L),
    ("UN", "APPEALED_TO", "RUSSIA", _W, _R),
    ("UN", "MET_WITH", "EGYPT", _W, _R),
    ("UN", "CONSULTED", "TURKEY", _O, _R),
    ("UN", "CONDEMNED", "RUSSIA", _O, _R),
    # --- BHUTAN: the quiet entity (exactly one older event) ---
    ("BHUTAN", "SIGNED_AGREEMENT_WITH", "INDIA", _O, _R),
    # --- BRAZIL: exactly 6 events, never as object ---
    ("BRAZIL", "HOSTED_TALKS_WITH", "MEXICO", _T, _R),
    ("BRAZIL", "SIGNED_TRADE_DEAL_WITH", "CHINA", _T, _L),
    ("BRAZIL", "MET_WITH", "INDIA", _W, _R),
    ("BRAZIL", "ANNOUNCED_POLICY", None, _W, _R),
    ("BRAZIL", "CONSULTED", "UNITED STATES", _O, _R),
    ("BRAZIL", "PLEDGED_AID_TO", "NIGERIA", _O, _L),
    # --- fillers ---
    ("INDIA", "MET_WITH", "JAPAN", _T, _R),
    ("INDIA", "SIGNED_TRADE_DEAL_WITH", "AUSTRALIA", _W, _L),
    ("INDIA", "MET_WITH", "NIGERIA", _W, _R),
    ("INDIA", "CONSULTED", "RUSSIA", _O, _R),
    ("JAPAN", "MET_WITH", "CANADA", _T, _R),
    ("JAPAN", "MET_WITH", "AUSTRALIA", _W, _R),
    ("JAPAN", "SIGNED_AGREEMENT_WITH", "INDIA", _O, _L),
    ("JAPAN", "CONSULTED", "CANADA", _O, _R),
    ("TURKEY", "HOSTED_TALKS_WITH", "EGYPT", _T, _L),
    ("TURKEY", "CONSULTED", "UN", _W, _R),
    ("TURKEY", "CRITICIZED", "FRANCE", _O, _R),
    ("EGYPT", "MET_WITH", "UN", _W, _R),
    ("EGYPT", "SIGNED_AGREEMENT_WITH", "TURKEY", _O, _R),
    ("NIGERIA", "MET_WITH", "EGYPT", _T, _R),
    ("NIGERIA", "CONSULTED", "UN", _O, _L),
    ("CANADA", "MET_WITH", "UNITED STATES", _T, _R),
    ("CANADA", "PLEDGED_AID_TO", "UKRAINE", _W, _R),
    ("AUSTRALIA", "SIGNED_TRADE_DEAL_WITH", "JAPAN", _W, _R),
    ("AUSTRALIA", "MET_WITH", "INDIA", _O, _R),
    ("POLAND", "MET_WITH", "UKRAINE", _T, _R),
    ("POLAND", "CONSULTED", "UKRAINE", _W, _L),
    ("MEXICO", "MET_WITH", "CANADA", _W, _R),
    ("MEXICO", "SIGNED_TRADE_DEAL_WITH", "CANADA", _O, _R),
]

ALIAS_NAME = "EU"
ALIAS_CANONICAL = "EUROPEAN UNION"

# Ground-truth link counts for the two-entity golden questions, derived from
# the same literals that build the graph.
GERMANY_FRANCE_LINKS_TODAY = sum(
    1 for s, _, o, bucket, _ in _SPECS if {s, o} == {"GERMANY", "FRANCE"} and bucket == _T
)
GERMANY_FRANCE_LINKS_TOTAL = sum(
    1 for s, _, o, _, _ in _SPECS if {s, o} == {"GERMANY", "FRANCE"}
)


def _offset(bucket: str, index: int) -> timedelta:
    """Fixed, index-derived offset before `now`, safely inside its bucket:
    today: 2h10m..~18h, week: 3d..3d12h, older: 30d..30d12h (strict bounds,
    so window comparisons like P1D/P7D/P30D never sit on a boundary)."""
    if bucket == _T:
        return timedelta(minutes=130 + (index * 7) % 950)
    if bucket == _W:
        return timedelta(days=3, minutes=30 + (index * 11) % 660)
    return timedelta(days=30, minutes=30 + (index * 13) % 660)


def _detail(subject: str, relation: str, obj: str | None, occurred_on: str) -> str:
    words = relation.replace("_", " ").lower()
    if obj is None:
        return f"On {occurred_on}, {subject} {words}, according to the wire report."
    return f"On {occurred_on}, {subject} {words} {obj}, according to the wire report."


def _build_claims(now: datetime) -> list[Claim]:
    claims: list[Claim] = []
    for index, (subject, relation, obj, bucket, tier) in enumerate(_SPECS):
        moment = now - _offset(bucket, index)
        occurred_on = moment.date().isoformat()
        event_id = f"eval-{index}"
        claims.append(
            Claim(
                subject=subject,
                relation=relation,
                object=obj,
                event_id=event_id,
                event_code="042",
                quad_class=1,
                goldstein=1.9,
                tone=2.5,
                num_mentions=5,
                occurred_on=occurred_on,
                observed_at=moment.isoformat(),
                geo_name=None,
                geo_lat=None,
                geo_lon=None,
                source_url=f"https://example.org/{event_id}",
                confidence=0.8 if tier == _L else 0.9,
                tier=tier,
                detail=_detail(subject, relation, obj, occurred_on) if tier == _L else None,
            )
        )
    return claims


def _summary() -> dict[str, dict[str, int]]:
    """Per-entity participant counts per time bucket, computed from _SPECS."""
    counts: dict[str, dict[str, int]] = {}
    for subject, _, obj, bucket, _ in _SPECS:
        for name in (subject, obj):
            if name is None:
                continue
            entity = counts.setdefault(name, {_T: 0, _W: 0, _O: 0, "total": 0})
            entity[bucket] += 1
            entity["total"] += 1
    return counts


def seed_graph(session, now: datetime) -> dict:
    """Write the deterministic fixture graph and return its ground truth.

    Same (session, now) -> identical graph: schema first (writer, then
    resolver), then every claim in _SPECS order, then the single ALIAS_OF
    pair. Returns {entity: {"today": n, "week": n, "older": n, "total": n}}.
    """
    writer.ensure_schema(session)
    resolver_graph.ensure_schema(session)
    for claim in _build_claims(now):
        writer.write_claim(session, claim)
    resolver_graph.write_alias(session, ALIAS_NAME, ALIAS_CANONICAL)
    return _summary()
