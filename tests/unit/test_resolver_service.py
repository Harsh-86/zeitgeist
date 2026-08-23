"""Tests for the resolver service's cycle orchestration and retry wrapper."""

from types import SimpleNamespace

import pytest
from neo4j.exceptions import ServiceUnavailable

from zeitgeist.resolver import graph as resolver_graph
from zeitgeist.resolver.judge import GenericVerdict, PairVerdict
from zeitgeist.resolver.main import main, run_cycle, run_cycle_with_retry

# ---- fakes -----------------------------------------------------------------


class FakeResult:
    """Fake neo4j Result: iterable of dict-like records, plus .single()."""

    def __init__(self, records=None):
        self._records = [dict(r) for r in (records or [])]

    def __iter__(self):
        return iter(self._records)

    def single(self):
        return self._records[0] if self._records else None


class FakeSession:
    """Fake neo4j session keyed by exact Cypher text: each cypher gets its own
    FIFO queue of canned FakeResults, independent of other queries' call order.
    Any cypher with no (or exhausted) canned queue returns an empty FakeResult."""

    def __init__(self, canned=None):
        self.queries: list[tuple[str, dict]] = []
        self._canned = {k: list(v) for k, v in (canned or {}).items()}

    def run(self, cypher, **params):
        self.queries.append((cypher, params))
        queue = self._canned.get(cypher)
        if queue:
            return queue.pop(0)
        return FakeResult([])

    def close(self):
        pass


class FakeJudge:
    """Fake ErJudge: canned verdicts keyed by name (screen) or (a, b) pair."""

    def __init__(self, generic_verdicts=None, pair_verdicts=None):
        self._generic = dict(generic_verdicts or {})
        self._pairs = dict(pair_verdicts or {})
        self.screen_calls: list[tuple[str, list[str]]] = []
        self.pair_calls: list[tuple[str, str, list[str], list[str]]] = []

    def screen_generic(self, name, sample_relations):
        self.screen_calls.append((name, sample_relations))
        return self._generic.get(name, (None, {}))

    def judge_pair(self, a, b, a_relations, b_relations):
        self.pair_calls.append((a, b, a_relations, b_relations))
        return self._pairs.get((a, b), (None, {}))


class AlwaysAvailable:
    def __init__(self):
        self.calls = 0

    def try_spend(self) -> bool:
        self.calls += 1
        return True


class NeverAvailable:
    def __init__(self):
        self.calls = 0

    def try_spend(self) -> bool:
        self.calls += 1
        return False


class ScriptedBudget:
    """Returns each value from `script` in order, then True forever after."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def try_spend(self) -> bool:
        self.calls += 1
        if self._script:
            return self._script.pop(0)
        return True


def make_cfg(er_min_events=3, er_min_confidence=0.8):
    return SimpleNamespace(er_min_events=er_min_events, er_min_confidence=er_min_confidence)


EMPTY_COUNTERS = {"screened": 0, "judged": 0, "aliased": 0, "skipped": 0, "budget_left": True}


# ---- screening step ----------------------------------------------------


def test_screening_marks_generic_entity_and_counts_screened():
    session = FakeSession(
        canned={
            resolver_graph.FETCH_UNSCREENED_ENTITIES_CYPHER: [
                FakeResult([{"name": "POLICE", "count": 5}])
            ]
        }
    )
    judge = FakeJudge(
        generic_verdicts={"POLICE": (GenericVerdict(generic=True, confidence=0.95), {})}
    )
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg())

    assert counters == {**EMPTY_COUNTERS, "screened": 1}
    assert budget.calls == 1
    mark_calls = [q for q in session.queries if q[0] == resolver_graph.MARK_GENERIC_CYPHER]
    assert mark_calls == [
        (
            resolver_graph.MARK_GENERIC_CYPHER,
            {"name": "POLICE", "generic": True, "confidence": 0.95},
        )
    ]


def test_screening_skips_without_marking_on_parse_none():
    session = FakeSession(
        canned={
            resolver_graph.FETCH_UNSCREENED_ENTITIES_CYPHER: [
                FakeResult([{"name": "POLICE", "count": 5}])
            ]
        }
    )
    judge = FakeJudge()  # no canned verdict -> screen_generic returns (None, {})
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg())

    assert counters == {**EMPTY_COUNTERS, "skipped": 1}
    assert budget.calls == 1  # honestly spent even though skipped
    mark_calls = [q for q in session.queries if q[0] == resolver_graph.MARK_GENERIC_CYPHER]
    assert mark_calls == []


def test_screening_multiple_entities_mixed_outcomes():
    session = FakeSession(
        canned={
            resolver_graph.FETCH_UNSCREENED_ENTITIES_CYPHER: [
                FakeResult([{"name": "POLICE", "count": 5}, {"name": "NATO", "count": 8}])
            ]
        }
    )
    judge = FakeJudge(
        generic_verdicts={
            "POLICE": (GenericVerdict(generic=True, confidence=0.9), {}),
            # NATO omitted -> parse-None
        }
    )
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg())

    assert counters == {**EMPTY_COUNTERS, "screened": 1, "skipped": 1}
    assert budget.calls == 2
    assert [c[0] for c in judge.screen_calls] == ["POLICE", "NATO"]


# ---- pair-judging step ---------------------------------------------------


ECB_PAIR_ENTITIES = [
    FakeResult(
        [
            {"name": "ECB", "count": 10},
            {"name": "EUROPEAN CENTRAL BANK", "count": 50},
        ]
    )
]


def test_pair_judging_skips_already_judged_pairs():
    session = FakeSession(
        canned={
            resolver_graph.FETCH_ENTITIES_CYPHER: ECB_PAIR_ENTITIES,
            resolver_graph.FETCH_JUDGED_PAIRS_CYPHER: [
                FakeResult([{"a": "ECB", "b": "EUROPEAN CENTRAL BANK"}])
            ],
        }
    )
    judge = FakeJudge()
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg())

    assert counters == EMPTY_COUNTERS
    assert judge.pair_calls == []
    assert budget.calls == 0


def test_pair_judging_records_judgment_and_writes_alias_when_same_and_confident():
    session = FakeSession(
        canned={
            resolver_graph.FETCH_ENTITIES_CYPHER: ECB_PAIR_ENTITIES,
            resolver_graph.RESOLVE_CANONICAL_CYPHER: [
                FakeResult([{"resolved": "EUROPEAN CENTRAL BANK"}])
            ],
            resolver_graph.CHECK_EXISTING_ALIAS_CYPHER: [
                FakeResult([{"has_outgoing": False, "has_incoming": False}])
            ],
        }
    )
    judge = FakeJudge(
        pair_verdicts={
            ("ECB", "EUROPEAN CENTRAL BANK"): (PairVerdict(verdict="SAME", confidence=0.9), {})
        }
    )
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg(er_min_confidence=0.8))

    assert counters == {**EMPTY_COUNTERS, "judged": 1, "aliased": 1}
    assert budget.calls == 1
    record_calls = [q for q in session.queries if q[0] == resolver_graph.RECORD_JUDGMENT_CYPHER]
    assert record_calls == [
        (
            resolver_graph.RECORD_JUDGMENT_CYPHER,
            {"a": "ECB", "b": "EUROPEAN CENTRAL BANK", "verdict": "SAME", "confidence": 0.9},
        )
    ]
    write_calls = [q for q in session.queries if q[0] == resolver_graph.WRITE_ALIAS_CYPHER]
    assert write_calls == [
        (
            resolver_graph.WRITE_ALIAS_CYPHER,
            {"alias_name": "ECB", "resolved_name": "EUROPEAN CENTRAL BANK"},
        )
    ]


def test_pair_judging_records_judgment_without_alias_below_confidence_threshold():
    session = FakeSession(canned={resolver_graph.FETCH_ENTITIES_CYPHER: ECB_PAIR_ENTITIES})
    judge = FakeJudge(
        pair_verdicts={
            ("ECB", "EUROPEAN CENTRAL BANK"): (PairVerdict(verdict="SAME", confidence=0.5), {})
        }
    )
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg(er_min_confidence=0.8))

    assert counters == {**EMPTY_COUNTERS, "judged": 1, "aliased": 0}
    write_calls = [q for q in session.queries if q[0] == resolver_graph.WRITE_ALIAS_CYPHER]
    assert write_calls == []


def test_pair_judging_records_judgment_without_alias_for_different_verdict():
    session = FakeSession(canned={resolver_graph.FETCH_ENTITIES_CYPHER: ECB_PAIR_ENTITIES})
    judge = FakeJudge(
        pair_verdicts={
            ("ECB", "EUROPEAN CENTRAL BANK"): (PairVerdict(verdict="DIFFERENT", confidence=0.9), {})
        }
    )
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg())

    assert counters == {**EMPTY_COUNTERS, "judged": 1, "aliased": 0}
    write_calls = [q for q in session.queries if q[0] == resolver_graph.WRITE_ALIAS_CYPHER]
    assert write_calls == []


def test_pair_judging_skips_recording_on_parse_none():
    session = FakeSession(canned={resolver_graph.FETCH_ENTITIES_CYPHER: ECB_PAIR_ENTITIES})
    judge = FakeJudge()  # no canned pair verdict -> parse-None
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg())

    assert counters == {**EMPTY_COUNTERS, "skipped": 1}
    assert budget.calls == 1  # honestly spent
    record_calls = [q for q in session.queries if q[0] == resolver_graph.RECORD_JUDGMENT_CYPHER]
    assert record_calls == []


def test_judgment_direction_uses_candidate_pairs_order_verbatim():
    """record_judgment/write_alias must use pair (lesser, greater) verbatim,
    never reversed, regardless of which side is 'a' in some other context."""
    session = FakeSession(canned={resolver_graph.FETCH_ENTITIES_CYPHER: ECB_PAIR_ENTITIES})
    judge = FakeJudge(
        pair_verdicts={
            ("ECB", "EUROPEAN CENTRAL BANK"): (PairVerdict(verdict="SAME", confidence=0.99), {})
        }
    )
    budget = AlwaysAvailable()

    run_cycle(session, judge, budget, make_cfg())

    _record_cypher, record_params = next(
        q for q in session.queries if q[0] == resolver_graph.RECORD_JUDGMENT_CYPHER
    )
    assert record_params["a"] == "ECB"
    assert record_params["b"] == "EUROPEAN CENTRAL BANK"


def test_pair_judging_same_verdict_above_threshold_but_alias_guard_hit_leaves_aliased_at_zero():
    """I1 fix: write_alias's own guard (e.g. the alias already having an
    outgoing ALIAS_OF edge) can refuse the write even when the verdict is
    SAME and confident. The `aliased` counter must reflect the actual write,
    not just that a SAME/confident verdict was judged."""
    session = FakeSession(
        canned={
            resolver_graph.FETCH_ENTITIES_CYPHER: ECB_PAIR_ENTITIES,
            resolver_graph.RESOLVE_CANONICAL_CYPHER: [
                FakeResult([{"resolved": "EUROPEAN CENTRAL BANK"}])
            ],
            resolver_graph.CHECK_EXISTING_ALIAS_CYPHER: [
                FakeResult([{"has_outgoing": True, "has_incoming": False}])
            ],
        }
    )
    judge = FakeJudge(
        pair_verdicts={
            ("ECB", "EUROPEAN CENTRAL BANK"): (PairVerdict(verdict="SAME", confidence=0.9), {})
        }
    )
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg(er_min_confidence=0.8))

    assert counters == {**EMPTY_COUNTERS, "judged": 1, "aliased": 0}
    write_calls = [q for q in session.queries if q[0] == resolver_graph.WRITE_ALIAS_CYPHER]
    assert write_calls == []


# ---- budget stops the cycle ----------------------------------------------


def test_budget_exhausted_before_first_entity_stops_instantly_and_skips_pairing():
    session = FakeSession(
        canned={
            resolver_graph.FETCH_UNSCREENED_ENTITIES_CYPHER: [
                FakeResult([{"name": "A", "count": 5}, {"name": "B", "count": 5}])
            ]
        }
    )
    judge = FakeJudge()
    budget = NeverAvailable()

    counters = run_cycle(session, judge, budget, make_cfg())

    assert counters == {**EMPTY_COUNTERS, "budget_left": False}
    assert budget.calls == 1
    assert judge.screen_calls == []
    fetch_entities_calls = [
        q for q in session.queries if q[0] == resolver_graph.FETCH_ENTITIES_CYPHER
    ]
    assert fetch_entities_calls == []
    fetch_judged_calls = [
        q for q in session.queries if q[0] == resolver_graph.FETCH_JUDGED_PAIRS_CYPHER
    ]
    assert fetch_judged_calls == []


def test_budget_exhausted_mid_screening_stops_before_second_entity():
    session = FakeSession(
        canned={
            resolver_graph.FETCH_UNSCREENED_ENTITIES_CYPHER: [
                FakeResult([{"name": "A", "count": 5}, {"name": "B", "count": 5}])
            ]
        }
    )
    judge = FakeJudge(generic_verdicts={"A": (GenericVerdict(generic=False, confidence=0.9), {})})
    budget = ScriptedBudget([True, False])

    counters = run_cycle(session, judge, budget, make_cfg())

    assert counters == {**EMPTY_COUNTERS, "screened": 1, "budget_left": False}
    assert [c[0] for c in judge.screen_calls] == ["A"]
    fetch_entities_calls = [
        q for q in session.queries if q[0] == resolver_graph.FETCH_ENTITIES_CYPHER
    ]
    assert fetch_entities_calls == []


def test_budget_exhausted_during_pairing_stops_further_pairs():
    session = FakeSession(
        canned={
            resolver_graph.FETCH_ENTITIES_CYPHER: [
                FakeResult(
                    [
                        {"name": "ECB", "count": 10},
                        {"name": "EUROPEAN CENTRAL BANK", "count": 50},
                        {"name": "IMF", "count": 5},
                        {"name": "INTERNATIONAL MONETARY FUND", "count": 40},
                    ]
                )
            ]
        }
    )
    judge = FakeJudge(
        pair_verdicts={
            ("ECB", "EUROPEAN CENTRAL BANK"): (PairVerdict(verdict="DIFFERENT", confidence=0.9), {})
        }
    )
    budget = ScriptedBudget([True, False])

    counters = run_cycle(session, judge, budget, make_cfg())

    # Only the higher-combined-count pair (ECB) gets processed before budget stops.
    assert counters == {**EMPTY_COUNTERS, "judged": 1, "budget_left": False}
    assert len(judge.pair_calls) == 1
    assert judge.pair_calls[0][0] == "ECB"
    assert judge.pair_calls[0][1] == "EUROPEAN CENTRAL BANK"


# ---- counters accurate across a combined cycle ---------------------------


def test_counters_accurate_across_screening_and_pairing():
    session = FakeSession(
        canned={
            resolver_graph.FETCH_UNSCREENED_ENTITIES_CYPHER: [
                FakeResult([{"name": "A", "count": 5}])
            ],
            resolver_graph.FETCH_ENTITIES_CYPHER: [
                FakeResult(
                    [
                        {"name": "ECB", "count": 10},
                        {"name": "EUROPEAN CENTRAL BANK", "count": 50},
                        {"name": "IMF", "count": 5},
                        {"name": "INTERNATIONAL MONETARY FUND", "count": 40},
                    ]
                )
            ],
            resolver_graph.FETCH_JUDGED_PAIRS_CYPHER: [
                FakeResult([{"a": "IMF", "b": "INTERNATIONAL MONETARY FUND"}])
            ],
            resolver_graph.RESOLVE_CANONICAL_CYPHER: [
                FakeResult([{"resolved": "EUROPEAN CENTRAL BANK"}])
            ],
            resolver_graph.CHECK_EXISTING_ALIAS_CYPHER: [
                FakeResult([{"has_outgoing": False, "has_incoming": False}])
            ],
        }
    )
    judge = FakeJudge(
        generic_verdicts={"A": (GenericVerdict(generic=True, confidence=0.8), {})},
        pair_verdicts={
            ("ECB", "EUROPEAN CENTRAL BANK"): (PairVerdict(verdict="SAME", confidence=0.9), {})
        },
    )
    budget = AlwaysAvailable()

    counters = run_cycle(session, judge, budget, make_cfg())

    assert counters == {
        "screened": 1,
        "judged": 1,
        "aliased": 1,
        "skipped": 0,
        "budget_left": True,
    }
    # 1 spend for screening A, 1 spend for the ECB pair; IMF pair was already judged (no spend).
    assert budget.calls == 2


def test_run_cycle_logs_one_info_summary(caplog):
    session = FakeSession()
    judge = FakeJudge()
    budget = AlwaysAvailable()

    with caplog.at_level("INFO"):
        run_cycle(session, judge, budget, make_cfg())

    resolver_records = [r for r in caplog.records if r.name == "zeitgeist.resolver"]
    assert len(resolver_records) == 1
    message = resolver_records[0].message
    assert "screened" in message
    assert "judged" in message
    assert "aliased" in message
    assert "skipped" in message
    assert "budget_left" in message


# ---- run_cycle_with_retry -------------------------------------------------


class RetrySession:
    def __init__(self, name, run):
        self.name = name
        self.closed = False
        self._run = run

    def run(self, cypher, **params):
        return self._run(cypher, **params)

    def close(self):
        self.closed = True


class RetryDriver:
    def __init__(self, run_factory):
        self.sessions_created = 0
        self._run_factory = run_factory

    def session(self):
        self.sessions_created += 1
        name = f"session-{self.sessions_created}"
        return RetrySession(name, self._run_factory(name))


def test_run_cycle_with_retry_retries_once_then_succeeds():
    calls = []

    def run_factory(name):
        def run(cypher, **params):
            calls.append(name)
            if len(calls) == 1:
                raise ServiceUnavailable("neo4j down")
            return FakeResult([])

        return run

    driver = RetryDriver(run_factory)
    old_session = RetrySession("session-0", run_factory("session-0"))
    sleeps = []
    judge = FakeJudge()
    budget = AlwaysAvailable()

    session, counters = run_cycle_with_retry(
        driver, old_session, judge, budget, make_cfg(), sleep=sleeps.append
    )

    assert calls[0] == "session-0"
    assert all(name == "session-1" for name in calls[1:])
    assert len(calls) > 1  # session-0's failing call, plus session-1's successful reads
    assert old_session.closed is True
    assert session.name == "session-1"
    assert sleeps == [2]
    assert counters == EMPTY_COUNTERS


def test_run_cycle_with_retry_propagates_non_retryable_exceptions():
    def run_factory(name):
        def run(cypher, **params):
            raise ValueError("boom")

        return run

    driver = RetryDriver(run_factory)
    session = RetrySession("session-0", run_factory("session-0"))
    judge = FakeJudge()
    budget = AlwaysAvailable()

    with pytest.raises(ValueError):
        run_cycle_with_retry(driver, session, judge, budget, make_cfg(), sleep=lambda s: None)


# ---- main(): missing ANTHROPIC_API_KEY -----------------------------------


def test_main_exits_nonzero_when_anthropic_api_key_missing(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code != 0
    assert any("ANTHROPIC_API_KEY" in record.message for record in caplog.records)
