"""Unit tests for the eval runner's pure parts (fakes only — no containers, no LLM),
plus schema and satisfiability tests that keep evals/golden_questions.jsonl honest."""

from datetime import UTC, datetime
from pathlib import Path

from tests.unit.test_eval_seed import FakeSession as SeedFakeSession
from zeitgeist.evals import runner, seed
from zeitgeist.evals.graders import KNOWN_EXPECT_KEYS

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "evals" / "golden_questions.jsonl"


class FakeTx:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)

    def run(self, cypher):
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSession:
    """execute_read-only fake: each call consumes the next outcome
    (a list of records, or an exception to raise)."""

    def __init__(self, outcomes):
        self._tx = FakeTx(outcomes)

    def execute_read(self, fn):
        return fn(self._tx)


class FakeAgent:
    def __init__(self, cyphers):
        self._cyphers = list(cyphers)
        self.calls: list[tuple[str, str | None]] = []

    def generate_cypher(self, question, error_feedback=None):
        self.calls.append((question, error_feedback))
        return self._cyphers.pop(0), {"input_tokens": 1, "output_tokens": 1}


def make_question(**overrides):
    base = {
        "id": "T01",
        "question": "What happened around GERMANY today?",
        "expect": {"min_records": 1, "all_records_mention": "GERMANY"},
        "note": "test",
    }
    base.update(overrides)
    return base


# --- run_question ---------------------------------------------------------------


def test_run_question_happy_path_passes():
    agent = FakeAgent(["MATCH (s:Entity) RETURN s.name LIMIT 5"])
    session = FakeSession([[{"s.name": "GERMANY"}, {"s.name": "GERMANY OFFICIALS"}]])
    result, records = runner.run_question(agent, session, make_question())
    assert result.passed is True
    assert result.records_count == 2
    assert result.llm_calls == 1
    assert result.failures == ()
    assert result.cypher == "MATCH (s:Entity) RETURN s.name LIMIT 5"
    # executed records come back for the faithfulness suite to reuse
    assert records == [{"s.name": "GERMANY"}, {"s.name": "GERMANY OFFICIALS"}]


def test_run_question_validator_reject_fails_without_retry():
    agent = FakeAgent(["MERGE (n:Entity {name: 'X'}) RETURN n"])
    session = FakeSession([])
    result, records = runner.run_question(agent, session, make_question())
    assert result.passed is False
    assert result.cypher is None
    assert result.records_count == 0
    assert result.llm_calls == 1
    assert records == []
    assert len(agent.calls) == 1  # production shape: no retry on unsafe generation
    assert any("unsafe" in f or "unparseable" in f for f in result.failures)


def test_run_question_generation_none_fails():
    class NoneAgent:
        def generate_cypher(self, question, error_feedback=None):
            return None, {}

    result, records = runner.run_question(NoneAgent(), FakeSession([]), make_question())
    assert result.passed is False
    assert result.cypher is None
    assert records == []


def test_run_question_execute_raises_then_retry_succeeds():
    agent = FakeAgent(
        ["MATCH (a) RETURN a.name LIMIT 5", "MATCH (b:Entity) RETURN b.name LIMIT 5"]
    )
    session = FakeSession([RuntimeError("boom: unknown function"), [{"b.name": "GERMANY"}]])
    result, records = runner.run_question(agent, session, make_question())
    assert result.passed is True
    assert result.llm_calls == 2
    assert result.cypher == "MATCH (b:Entity) RETURN b.name LIMIT 5"
    assert records == [{"b.name": "GERMANY"}]
    # retry carried error feedback with the failing cypher and the error text
    _, feedback = agent.calls[1]
    assert "boom" in feedback
    assert "MATCH (a) RETURN a.name LIMIT 5" in feedback


def test_run_question_retry_also_raises_fails():
    agent = FakeAgent(["MATCH (a) RETURN a LIMIT 5", "MATCH (b) RETURN b LIMIT 5"])
    session = FakeSession([RuntimeError("boom1"), RuntimeError("boom2")])
    result, records = runner.run_question(agent, session, make_question())
    assert result.passed is False
    assert result.llm_calls == 2
    assert records == []
    assert any("execution failed" in f for f in result.failures)


def test_run_question_retry_generates_unsafe_fails():
    agent = FakeAgent(["MATCH (a) RETURN a LIMIT 5", "DELETE everything"])
    session = FakeSession([RuntimeError("boom")])
    result, records = runner.run_question(agent, session, make_question())
    assert result.passed is False
    assert result.llm_calls == 2
    assert records == []


def test_run_question_grade_failure_path():
    agent = FakeAgent(["MATCH (s:Entity) RETURN s.name LIMIT 5"])
    session = FakeSession([[{"s.name": "FRANCE"}]])
    result, records = runner.run_question(agent, session, make_question())
    assert result.passed is False
    assert result.records_count == 1
    assert records == [{"s.name": "FRANCE"}]
    assert any("GERMANY" in f for f in result.failures)


# --- aggregate / threshold helpers -----------------------------------------------


def test_summarize_results_pass_rate_and_llm_calls():
    results = [
        runner.QuestionResult("A", "q", True, "c", 3, (), 1),
        runner.QuestionResult("B", "q", False, "c", 0, ("nope",), 2),
    ]
    summary = runner.summarize(results, suite="retrieval", model="m")
    assert summary["pass_rate"] == 0.5
    assert summary["llm_calls"] == 3
    assert summary["passed"] == 1
    assert summary["total"] == 2


def test_threshold_exit_no_file(tmp_path):
    rates = {"retrieval_pass_rate": 0.5}
    assert runner.threshold_exit_code(rates, tmp_path / "missing.json") == 0


def test_threshold_exit_below_floor(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text('{"retrieval_pass_rate": 0.8}')
    assert runner.threshold_exit_code({"retrieval_pass_rate": 0.75}, path) == 1
    assert runner.threshold_exit_code({"retrieval_pass_rate": 0.8}, path) == 0


def test_threshold_exit_file_without_matching_key(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text('{"faithfulness_rate": 0.8}')
    assert runner.threshold_exit_code({"retrieval_pass_rate": 0.1}, path) == 0


def test_threshold_exit_faithfulness_floor(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text('{"faithfulness_rate": 0.9}')
    assert runner.threshold_exit_code({"faithfulness_rate": 0.85}, path) == 1
    assert runner.threshold_exit_code({"faithfulness_rate": 0.9}, path) == 0


def test_threshold_exit_either_breach_fails(tmp_path):
    path = tmp_path / "thresholds.json"
    path.write_text('{"retrieval_pass_rate": 0.8, "faithfulness_rate": 0.9}')
    both_ok = {"retrieval_pass_rate": 0.9, "faithfulness_rate": 0.95}
    faith_low = {"retrieval_pass_rate": 0.9, "faithfulness_rate": 0.5}
    retrieval_low = {"retrieval_pass_rate": 0.5, "faithfulness_rate": 0.95}
    assert runner.threshold_exit_code(both_ok, path) == 0
    assert runner.threshold_exit_code(faith_low, path) == 1
    assert runner.threshold_exit_code(retrieval_low, path) == 1


# --- golden data: schema-valid ----------------------------------------------------


def load_golden_rows():
    return runner.load_golden(GOLDEN_PATH)


def test_golden_file_parses_and_has_enough_questions():
    rows = load_golden_rows()
    assert len(rows) >= 20


def test_golden_rows_schema_valid():
    rows = load_golden_rows()
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids))
    for row in rows:
        assert set(row) <= {"id", "question", "expect", "note"}
        assert row["question"].strip()
        expect = row["expect"]
        assert expect, row["id"]
        assert set(expect) <= KNOWN_EXPECT_KEYS, row["id"]
        if "min_records" in expect:
            assert isinstance(expect["min_records"], int)
        if "max_records" in expect:
            assert isinstance(expect["max_records"], int)
        if "min_records" in expect and "max_records" in expect:
            assert expect["min_records"] <= expect["max_records"], row["id"]
        if "all_records_mention" in expect:
            assert isinstance(expect["all_records_mention"], str)
        if "any_record_mentions" in expect:
            assert isinstance(expect["any_record_mentions"], list)
            assert all(isinstance(n, str) for n in expect["any_record_mentions"])
        if "forbid_empty" in expect:
            assert isinstance(expect["forbid_empty"], bool)


# --- golden data: satisfiable against the seeded graph -----------------------------
# Pure arithmetic against seed_graph's ground-truth summary — no containers.


def seed_summary():
    return seed.seed_graph(SeedFakeSession(), datetime(2026, 8, 27, 12, 0, tzinfo=UTC))


def truth(summary, entity, buckets):
    return sum(summary[entity][b] for b in buckets)


def test_every_golden_expectation_is_satisfiable_against_the_seed():
    summary = seed_summary()
    rows = {row["id"]: row["expect"] for row in load_golden_rows()}

    # id -> (entity, buckets the question's time window covers); None = custom check
    windows = {
        "R01": ("GERMANY", ["today"]),
        "R02": ("UKRAINE", ["today", "week", "older"]),
        "R03": ("GERMANY", ["today", "week"]),
        "R04": ("FRANCE", ["today"]),
        "R05": None,
        "R06": ("ECB", ["today"]),
        "R07": ("ECB", ["today", "week"]),
        "R08": ("BHUTAN", ["today", "week", "older"]),
        "R09": ("BHUTAN", ["today"]),
        "R10": ("NATO", ["today", "week"]),
        "R11": ("UNITED STATES", ["today"]),
        "R12": None,
        "R13": ("GERMANY", ["today", "week", "older"]),
        "R14": ("UNITED STATES", ["today", "week", "older"]),
        "R15": None,
        "R16": ("RUSSIA", ["today"]),
        "R17": ("CHINA", ["today", "week"]),
        "R18": ("UKRAINE", ["today", "week"]),
        "R19": None,
        "R20": ("POLAND", ["today", "week", "older"]),
        "R21": ("GERMANY", ["today", "week", "older"]),
        "R22": ("EGYPT", ["today", "week", "older"]),
        "R23": ("JAPAN", ["today"]),
        "R24": ("UNITED STATES", ["today"]),
        "R25": ("NIGERIA", ["today", "week", "older"]),
        "R26": None,
    }
    assert set(windows) == set(rows), "every golden row needs a satisfiability check"

    for qid, spec in windows.items():
        expect = rows[qid]
        if spec is not None:
            entity, buckets = spec
            true_count = truth(summary, entity, buckets)
            if "min_records" in expect:
                assert expect["min_records"] <= true_count, qid
            if "max_records" in expect:
                assert true_count <= expect["max_records"], qid

    # R05: GERMANY-FRANCE connection — bounded by the seeded link counts
    assert rows["R05"]["min_records"] <= seed.GERMANY_FRANCE_LINKS_TODAY
    assert seed.GERMANY_FRANCE_LINKS_TOTAL <= rows["R05"]["max_records"]

    # R12: alias question — EU or EUROPEAN UNION events exist today, and grading
    # relies on the alias name being a substring of its canonical name
    assert rows["R12"]["all_records_mention"] == seed.ALIAS_NAME == "EU"
    assert seed.ALIAS_NAME in seed.ALIAS_CANONICAL
    assert rows["R12"]["min_records"] <= (
        summary["EU"]["today"] + summary["EUROPEAN UNION"]["today"]
    )

    # R15: count question — the expected exact count matches BRAZIL's true total
    assert rows["R15"]["count_equals"] == summary["BRAZIL"]["total"]

    # R26: count question — the expected exact count matches GERMANY's today count
    assert rows["R26"]["count_equals"] == summary["GERMANY"]["today"]


def test_run_faithfulness_survives_one_exploding_question():
    """One malformed response fails one question as a judge_error; the run
    and every other verdict survive."""
    from zeitgeist.evals.runner import QuestionResult, run_faithfulness

    class ExplodingOnFirstAgent:
        def __init__(self):
            self.calls = 0

        def synthesize(self, question, records):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("malformed response object")
            return "fine answer", {}

    class AlwaysSupportedJudge:
        def judge(self, question, records, answer):
            from zeitgeist.evals.faithfulness import FaithfulnessVerdict

            return FaithfulnessVerdict(True, (), 0.9), {}

    def qr(qid):
        return QuestionResult(
            id=qid, question=f"q {qid}", passed=True, cypher="MATCH", records_count=1,
            failures=(), llm_calls=1,
        )

    results = run_faithfulness(
        ExplodingOnFirstAgent(), AlwaysSupportedJudge(), [(qr("F1"), [{}]), (qr("F2"), [{}])]
    )

    assert len(results) == 2
    assert results[0].supported is None
    assert "unexpected exception" in results[0].failures[0]
    assert results[1].supported is True
