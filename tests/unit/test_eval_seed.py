"""Unit tests for the deterministic eval seed graph (fake session, no containers)."""

from datetime import UTC, datetime, timedelta

from zeitgeist.evals import seed
from zeitgeist.graph import writer
from zeitgeist.resolver import graph as resolver_graph

NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)


class FakeResult:
    def __init__(self, records=None):
        self._records = [dict(r) for r in (records or [])]

    def __iter__(self):
        return iter(self._records)

    def single(self):
        return self._records[0] if self._records else None


class FakeSession:
    """Records every (query, params) call; answers write_alias's read queries
    so the alias write proceeds."""

    def __init__(self):
        self.queries: list[tuple[str, dict]] = []

    def run(self, query, parameters=None, **kwargs):
        params = {**(parameters or {}), **kwargs}
        self.queries.append((query, params))
        if "coalesce(cc.name, c.name)" in query:
            return FakeResult([{"resolved": params["canonical_name"]}])
        if "has_outgoing" in query:
            return FakeResult([{"has_outgoing": False, "has_incoming": False}])
        return FakeResult([])


def build(now=NOW):
    session = FakeSession()
    summary = seed.seed_graph(session, now)
    claims = [params for query, params in session.queries if query == writer.CLAIM_CYPHER]
    return session, summary, claims


def bucket_of(now, observed_at: str) -> str:
    delta = now - datetime.fromisoformat(observed_at)
    if delta < timedelta(days=1):
        return "today"
    if delta < timedelta(days=7):
        return "week"
    return "older"


# --- volume and identity -----------------------------------------------------


def test_total_event_count_is_around_150_and_matches_summary_totals():
    _, summary, claims = build()
    assert 140 <= len(claims) <= 165
    # every participant slot in every claim is accounted for in the summary
    participant_slots = sum(1 for c in claims for name in (c["subject"], c["object"]) if name)
    assert sum(counts["total"] for counts in summary.values()) == participant_slots


def test_event_ids_unique_and_eval_prefixed():
    _, _, claims = build()
    ids = [c["event_id"] for c in claims]
    assert len(ids) == len(set(ids))
    assert all(event_id.startswith("eval-") for event_id in ids)


# --- summary consistency (single source of truth) ------------------------------


def test_summary_bucket_counts_sum_to_totals():
    _, summary, _ = build()
    for entity, counts in summary.items():
        assert counts["today"] + counts["week"] + counts["older"] == counts["total"], entity


def test_summary_matches_the_claims_actually_written():
    _, summary, claims = build()
    recomputed: dict[str, dict[str, int]] = {}
    for claim in claims:
        bucket = bucket_of(NOW, claim["observed_at"])
        for name in (claim["subject"], claim["object"]):
            if name is None:
                continue
            counts = recomputed.setdefault(name, {"today": 0, "week": 0, "older": 0, "total": 0})
            counts[bucket] += 1
            counts["total"] += 1
    assert recomputed == summary


# --- determinism ----------------------------------------------------------------


def test_seeding_twice_with_same_now_is_identical():
    session_a, summary_a, _ = build()
    session_b, summary_b, _ = build()
    assert session_a.queries == session_b.queries
    assert summary_a == summary_b


# --- schema + alias order --------------------------------------------------------


def test_writer_schema_runs_before_any_claim():
    session, _, _ = build()
    queries = [query for query, _ in session.queries]
    first_claim = queries.index(writer.CLAIM_CYPHER)
    for statement in writer.SCHEMA_STATEMENTS:
        assert queries.index(statement) < first_claim


def test_alias_written_after_resolver_schema():
    session, _, _ = build()
    queries = [query for query, _ in session.queries]
    resolver_schema_idx = queries.index(resolver_graph.SCHEMA_STATEMENTS[0])
    alias_idx = queries.index(resolver_graph.WRITE_ALIAS_CYPHER)
    assert resolver_schema_idx < alias_idx


def test_alias_pair_is_eu_to_european_union():
    session, _, _ = build()
    alias_calls = [
        params for query, params in session.queries if query == resolver_graph.WRITE_ALIAS_CYPHER
    ]
    assert alias_calls == [{"alias_name": "EU", "resolved_name": "EUROPEAN UNION"}]


# --- tiers, details, urls --------------------------------------------------------


def test_roughly_30_percent_llm_tier_with_details():
    _, _, claims = build()
    llm = [c for c in claims if c["tier"] == "llm"]
    ratio = len(llm) / len(claims)
    assert 0.25 <= ratio <= 0.35
    assert all(c["detail"] for c in llm)
    assert all(c["detail"] is None for c in claims if c["tier"] == "rules")


def test_source_urls_distinct_and_example_org():
    _, _, claims = build()
    urls = [c["source_url"] for c in claims]
    assert len(urls) == len(set(urls))
    assert all(url.startswith("https://example.org/eval-") for url in urls)


# --- shape guarantees the golden questions rely on --------------------------------


def test_subject_only_events_around_10():
    _, _, claims = build()
    subject_only = [c for c in claims if c["object"] is None]
    assert 8 <= len(subject_only) <= 15


def test_germany_counts_exact():
    _, summary, _ = build()
    assert summary["GERMANY"] == {"today": 8, "week": 5, "older": 4, "total": 17}


def test_germany_france_linked_events_today():
    _, _, claims = build()
    links_today = [
        c
        for c in claims
        if {c["subject"], c["object"]} == {"GERMANY", "FRANCE"}
        and bucket_of(NOW, c["observed_at"]) == "today"
    ]
    assert len(links_today) == seed.GERMANY_FRANCE_LINKS_TODAY == 4


def test_bhutan_is_quiet_one_older_event_only():
    _, summary, _ = build()
    assert summary["BHUTAN"] == {"today": 0, "week": 0, "older": 1, "total": 1}


def test_ecb_has_zero_today_events():
    _, summary, _ = build()
    assert summary["ECB"]["today"] == 0
    assert summary["ECB"]["week"] >= 3


def test_united_states_is_the_mega_hub():
    _, summary, _ = build()
    assert summary["UNITED STATES"]["today"] >= 20
    assert summary["UNITED STATES"]["total"] >= 40
    assert summary["UNITED STATES"]["total"] == max(c["total"] for c in summary.values())


def test_brazil_never_appears_as_object():
    _, summary, claims = build()
    assert all(c["object"] != "BRAZIL" for c in claims)
    assert summary["BRAZIL"]["total"] == sum(1 for c in claims if c["subject"] == "BRAZIL") == 6


def test_time_buckets_have_safe_offsets():
    _, _, claims = build()
    for claim in claims:
        observed = datetime.fromisoformat(claim["observed_at"])
        delta = NOW - observed
        assert (
            timedelta(hours=2) <= delta <= timedelta(hours=20)
            or timedelta(days=3) <= delta < timedelta(days=4)
            or timedelta(days=30) < delta < timedelta(days=31)
        ), claim["event_id"]
        assert claim["occurred_on"] == observed.date().isoformat()


def test_around_20_entities():
    _, summary, _ = build()
    assert 18 <= len(summary) <= 25
