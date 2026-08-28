"""Eval runner: real Cypher generation against a deterministic seeded graph in
a throwaway Neo4j container, plus citation-faithfulness grading of the answers
synthesized from those retrievals.

    python -m zeitgeist.evals.runner --suite {retrieval,faithfulness,all}
                                     [--golden PATH] [--limit N]

Requires ANTHROPIC_API_KEY (exit 2 when unset) and Docker for the Neo4j
testcontainer. Per question the retrieval leg mirrors production's /ask shape
exactly: generate_cypher -> validate_cypher -> execute_read -> one retry with
error feedback -> grade. Grading happens only over executed records, never by
string-matching the Cypher.

The faithfulness leg runs after retrieval in the same container/session and
reuses each retrieval question's executed records — only for questions whose
retrieval PASSED (grading answer quality on garbage records is noise): it calls
agent.synthesize, runs the deterministic citation/shape checks, then asks the
FaithfulnessJudge for a per-question verdict.
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from zeitgeist.agent.query import QueryAgent, validate_cypher
from zeitgeist.evals.faithfulness import (
    FaithfulnessJudge,
    extract_urls,
    grade_answer_shape,
    grade_citations,
)
from zeitgeist.evals.graders import grade_retrieval
from zeitgeist.evals.seed import seed_graph

logger = logging.getLogger("zeitgeist.evals.runner")

DEFAULT_GOLDEN = Path("evals/golden_questions.jsonl")
RESULTS_DIR = Path("evals/results")
THRESHOLDS_PATH = Path("evals/thresholds.json")
DEFAULT_MODEL = "claude-haiku-4-5"


@dataclass(frozen=True)
class QuestionResult:
    id: str
    question: str
    passed: bool
    cypher: str | None
    records_count: int
    failures: tuple[str, ...]
    llm_calls: int


@dataclass(frozen=True)
class FaithfulnessResult:
    """One retrieval-passed question's answer graded for faithfulness.

    supported is None when no verdict was obtained (synthesis or judge failed);
    such questions count as judge_errors, never as unsupported."""

    id: str
    question: str
    supported: bool | None
    unsupported_claims: tuple[str, ...]
    confidence: float | None
    citation_check: bool
    answer_shape_check: bool
    answer: str | None
    failures: tuple[str, ...]
    llm_calls: int


def load_golden(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _execute(session, cypher: str) -> list[dict]:
    # Mirrors production layer 2: execute_read makes Neo4j itself reject writes.
    return session.execute_read(lambda tx: [dict(record) for record in tx.run(cypher)])


def run_question(agent, session, question_row: dict) -> tuple[QuestionResult, list[dict]]:
    """One golden question through the production /ask shape:
    generate -> validate -> execute -> (one retry with error feedback) -> grade.

    Returns the graded result plus the executed records so the faithfulness
    suite can reuse them (empty when no query executed successfully)."""
    qid = question_row["id"]
    question = question_row["question"]
    llm_calls = 0

    raw, _usage = agent.generate_cypher(question)
    llm_calls += 1
    cypher = validate_cypher(raw) if raw is not None else None
    if cypher is None:
        return QuestionResult(
            qid, question, False, None, 0,
            ("could not generate a safe query (unsafe/unparseable)",), llm_calls,
        ), []

    try:
        records = _execute(session, cypher)
    except Exception as exc:  # noqa: BLE001 — mirror production: any driver error triggers retry
        failed_cypher = cypher
        logger.warning("%s: cypher failed, retrying once (cypher=%s): %s", qid, cypher, exc)
        raw, _usage = agent.generate_cypher(question, error_feedback=f"{failed_cypher}\n{exc}")
        llm_calls += 1
        cypher = validate_cypher(raw) if raw is not None else None
        if cypher is None:
            return QuestionResult(
                qid, question, False, None, 0,
                ("retry could not generate a safe query (unsafe/unparseable)",), llm_calls,
            ), []
        try:
            records = _execute(session, cypher)
        except Exception as retry_exc:  # noqa: BLE001 — final failure is a graded outcome
            logger.warning("%s: cypher retry failed (cypher=%s): %s", qid, cypher, retry_exc)
            return QuestionResult(
                qid, question, False, cypher, 0,
                (f"query execution failed after retry: {retry_exc}",), llm_calls,
            ), []

    grade = grade_retrieval(records, question_row["expect"])
    return QuestionResult(
        qid, question, grade.passed, cypher, len(records), grade.failures, llm_calls
    ), records


def run_faithfulness(
    agent, judge, retrieval_results: list[tuple[QuestionResult, list[dict]]]
) -> list[FaithfulnessResult]:
    """Grade answer faithfulness for every retrieval-PASSED question, reusing
    its executed records: synthesize -> deterministic checks -> judge verdict.

    Failed retrievals are skipped entirely — grading answer quality on garbage
    records is noise. A missing answer or judge verdict yields supported=None
    (a judge_error in the summary), never an unsupported count."""
    results: list[FaithfulnessResult] = []
    for question_result, records in retrieval_results:
        if not question_result.passed:
            continue
        # Same crash-guard discipline as the retrieval loop: one malformed
        # response must fail one question (as a judge_error), never abort the
        # run and lose everything already measured.
        try:
            results.append(_judge_one(agent, judge, question_result, records))
        except Exception:
            logger.warning(
                "faithfulness judging for %s raised unexpectedly",
                question_result.id,
                exc_info=True,
            )
            results.append(
                FaithfulnessResult(
                    id=question_result.id,
                    question=question_result.question,
                    supported=None,
                    unsupported_claims=(),
                    confidence=None,
                    citation_check=False,
                    answer_shape_check=False,
                    answer=None,
                    failures=("unexpected exception during faithfulness judging",),
                    llm_calls=0,
                )
            )
    return results


def _judge_one(agent, judge, question_result: QuestionResult, records) -> FaithfulnessResult:
    question = question_result.question
    llm_calls = 0

    answer, _usage = agent.synthesize(question, records)
    llm_calls += 1

    citation_grade = grade_citations(extract_urls(answer or ""), records)
    shape_grade = grade_answer_shape(answer, records)
    failures = citation_grade.failures + shape_grade.failures

    supported: bool | None = None
    unsupported_claims: tuple[str, ...] = ()
    confidence: float | None = None
    if answer is not None:
        verdict, _judge_usage = judge.judge(question, records, answer)
        llm_calls += 1
        if verdict is not None:
            supported = verdict.supported
            unsupported_claims = verdict.unsupported_claims
            confidence = verdict.confidence

    return FaithfulnessResult(
        id=question_result.id,
        question=question,
        supported=supported,
        unsupported_claims=unsupported_claims,
        confidence=confidence,
        citation_check=citation_grade.passed,
        answer_shape_check=shape_grade.passed,
        answer=answer,
        failures=failures,
        llm_calls=llm_calls,
    )


def summarize(results: list[QuestionResult], suite: str, model: str) -> dict:
    passed = sum(1 for result in results if result.passed)
    total = len(results)
    return {
        "suite": suite,
        "model": model,
        "ran_at": datetime.now(UTC).isoformat(),
        "total": total,
        "passed": passed,
        "pass_rate": passed / total if total else 0.0,
        "llm_calls": sum(result.llm_calls for result in results),
        "questions": [asdict(result) for result in results],
    }


def summarize_faithfulness(results: list[FaithfulnessResult]) -> dict:
    """Aggregate the faithfulness leg: rate = supported / judged. Questions
    with no verdict (supported=None) are judge_errors, excluded from the rate."""
    judged = [result for result in results if result.supported is not None]
    supported = sum(1 for result in judged if result.supported)
    return {
        "total": len(results),
        "judged": len(judged),
        "supported": supported,
        "judge_errors": len(results) - len(judged),
        "faithfulness_rate": supported / len(judged) if judged else 0.0,
        "llm_calls": sum(result.llm_calls for result in results),
        "questions": [asdict(result) for result in results],
    }


def format_line(result: QuestionResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    line = (
        f"[{status}] {result.id} ({result.records_count} records, "
        f"{result.llm_calls} llm calls): {result.question}"
    )
    if result.failures:
        line += "\n" + "\n".join(f"       - {failure}" for failure in result.failures)
    return line


def format_faithfulness_line(result: FaithfulnessResult) -> str:
    if result.supported is None:
        status = "JUDGE-ERROR"
    elif result.supported:
        status = "SUPPORTED"
    else:
        status = "UNSUPPORTED"
    confidence = f"{result.confidence:.2f}" if result.confidence is not None else "-"
    line = (
        f"[{status}] {result.id} (confidence {confidence}, "
        f"{result.llm_calls} llm calls): {result.question}"
    )
    for claim in result.unsupported_claims:
        line += f"\n       - unsupported: {claim}"
    for failure in result.failures:
        line += f"\n       - {failure}"
    return line


def write_results(summary: dict, results_dir: Path = RESULTS_DIR) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = results_dir / f"{summary['suite']}-{stamp}.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


def threshold_exit_code(rates: dict[str, float], thresholds_path: Path = THRESHOLDS_PATH) -> int:
    """Exit 1 when ANY rate this run produced has a committed floor in
    thresholds.json and fell below it; 0 otherwise (printing that no threshold
    is set yet). rates maps threshold keys (e.g. "retrieval_pass_rate",
    "faithfulness_rate") to the rates the run measured."""
    if not thresholds_path.exists():
        print("no threshold set (Task 4)")
        return 0
    thresholds = json.loads(thresholds_path.read_text(encoding="utf-8"))
    exit_code = 0
    checked_any = False
    for key, rate in rates.items():
        floor = thresholds.get(key)
        if floor is None:
            continue
        checked_any = True
        if rate < floor:
            print(f"FAIL: {key} {rate:.2%} is below the floor {floor:.2%}")
            exit_code = 1
        else:
            print(f"{key} {rate:.2%} meets the floor {floor:.2%}")
    if not checked_any:
        print("no threshold set (Task 4)")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zeitgeist.evals.runner")
    parser.add_argument("--suite", required=True, choices=["retrieval", "faithfulness", "all"])
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    # Faithfulness grades the answers synthesized from retrieval's executed
    # records, so retrieval always runs; --suite picks what is reported/gated.
    gate_retrieval = args.suite in ("retrieval", "all")
    run_faith = args.suite in ("faithfulness", "all")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ANTHROPIC_API_KEY is not set. The eval suites generate real "
            "Cypher/answers through the API; set the key and re-run.",
            file=sys.stderr,
        )
        return 2

    questions = load_golden(args.golden)
    if args.limit is not None:
        questions = questions[: args.limit]

    import anthropic  # deferred with the other heavy deps

    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    client = anthropic.Anthropic(api_key=api_key)
    agent = QueryAgent(client, model=model)
    judge = FaithfulnessJudge(client, model=model) if run_faith else None

    from neo4j import GraphDatabase
    from testcontainers.community.neo4j import Neo4jContainer

    retrieval_results: list[tuple[QuestionResult, list[dict]]] = []
    faithfulness_results: list[FaithfulnessResult] = []
    with Neo4jContainer("neo4j:5-community") as neo4j:
        driver = GraphDatabase.driver(
            neo4j.get_connection_url(), auth=(neo4j.username, neo4j.password)
        )
        try:
            with driver.session() as session:
                seed_graph(session, now=datetime.now(UTC))
                for question_row in questions:
                    try:
                        result, records = run_question(agent, session, question_row)
                    except Exception:
                        logger.warning(
                            "question %s raised unexpectedly", question_row.get("id"),
                            exc_info=True,
                        )
                        result, records = QuestionResult(
                            id=question_row.get("id", "?"),
                            question=question_row.get("question", ""),
                            passed=False,
                            cypher=None,
                            records_count=0,
                            failures=("unexpected exception during run_question",),
                            llm_calls=0,
                        ), []
                    retrieval_results.append((result, records))
                    print(format_line(result))
                if run_faith:
                    print("\nfaithfulness (retrieval-passed questions only):")
                    faithfulness_results = run_faithfulness(agent, judge, retrieval_results)
                    for faithfulness_result in faithfulness_results:
                        print(format_faithfulness_line(faithfulness_result))
        finally:
            driver.close()

    results = [result for result, _records in retrieval_results]
    summary = summarize(results, suite=args.suite, model=model)
    rates: dict[str, float] = {}
    if gate_retrieval:
        rates["retrieval_pass_rate"] = summary["pass_rate"]
    if run_faith:
        faith_summary = summarize_faithfulness(faithfulness_results)
        summary["faithfulness"] = faith_summary
        rates["faithfulness_rate"] = faith_summary["faithfulness_rate"]

    results_path = write_results(summary)
    print(
        f"\nretrieval: {summary['passed']}/{summary['total']} passed "
        f"(pass rate {summary['pass_rate']:.2%}), {summary['llm_calls']} llm calls"
    )
    if run_faith:
        print(
            f"faithfulness: {faith_summary['supported']}/{faith_summary['judged']} supported "
            f"(rate {faith_summary['faithfulness_rate']:.2%}), "
            f"{faith_summary['judge_errors']} judge errors, "
            f"{faith_summary['llm_calls']} llm calls"
        )
    print(f"results written to {results_path}")
    return threshold_exit_code(rates)


if __name__ == "__main__":
    sys.exit(main())
