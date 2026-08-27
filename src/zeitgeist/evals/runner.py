"""Retrieval eval runner: real Cypher generation against a deterministic seeded
graph in a throwaway Neo4j container.

    python -m zeitgeist.evals.runner --suite retrieval [--golden PATH] [--limit N]

Requires ANTHROPIC_API_KEY (exit 2 when unset) and Docker for the Neo4j
testcontainer. Per question it mirrors production's /ask shape exactly:
generate_cypher -> validate_cypher -> execute_read -> one retry with error
feedback -> grade. Grading happens only over executed records, never by
string-matching the Cypher.
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


def run_question(agent, session, question_row: dict) -> QuestionResult:
    """One golden question through the production /ask shape:
    generate -> validate -> execute -> (one retry with error feedback) -> grade."""
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
        )

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
            )
        try:
            records = _execute(session, cypher)
        except Exception as retry_exc:  # noqa: BLE001 — final failure is a graded outcome
            logger.warning("%s: cypher retry failed (cypher=%s): %s", qid, cypher, retry_exc)
            return QuestionResult(
                qid, question, False, cypher, 0,
                (f"query execution failed after retry: {retry_exc}",), llm_calls,
            )

    grade = grade_retrieval(records, question_row["expect"])
    return QuestionResult(
        qid, question, grade.passed, cypher, len(records), grade.failures, llm_calls
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


def format_line(result: QuestionResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    line = (
        f"[{status}] {result.id} ({result.records_count} records, "
        f"{result.llm_calls} llm calls): {result.question}"
    )
    if result.failures:
        line += "\n" + "\n".join(f"       - {failure}" for failure in result.failures)
    return line


def write_results(summary: dict, results_dir: Path = RESULTS_DIR) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = results_dir / f"{summary['suite']}-{stamp}.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


def threshold_exit_code(pass_rate: float, thresholds_path: Path = THRESHOLDS_PATH) -> int:
    """Exit 1 when a committed retrieval_pass_rate floor exists and the run
    fell below it; 0 otherwise (printing that no threshold is set yet)."""
    if thresholds_path.exists():
        thresholds = json.loads(thresholds_path.read_text(encoding="utf-8"))
        floor = thresholds.get("retrieval_pass_rate")
        if floor is not None:
            if pass_rate < floor:
                print(f"FAIL: pass rate {pass_rate:.2%} is below the floor {floor:.2%}")
                return 1
            print(f"pass rate {pass_rate:.2%} meets the floor {floor:.2%}")
            return 0
    print("no threshold set (Task 4)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zeitgeist.evals.runner")
    parser.add_argument("--suite", required=True, choices=["retrieval"])
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ANTHROPIC_API_KEY is not set. The retrieval suite generates real "
            "Cypher through the API; set the key and re-run.",
            file=sys.stderr,
        )
        return 2

    questions = load_golden(args.golden)
    if args.limit is not None:
        questions = questions[: args.limit]

    import anthropic  # deferred with the other heavy deps

    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    agent = QueryAgent(anthropic.Anthropic(api_key=api_key), model=model)

    from neo4j import GraphDatabase
    from testcontainers.community.neo4j import Neo4jContainer

    results: list[QuestionResult] = []
    with Neo4jContainer("neo4j:5-community") as neo4j:
        driver = GraphDatabase.driver(
            neo4j.get_connection_url(), auth=(neo4j.username, neo4j.password)
        )
        try:
            with driver.session() as session:
                seed_graph(session, now=datetime.now(UTC))
                for question_row in questions:
                    try:
                        result = run_question(agent, session, question_row)
                    except Exception:
                        logger.warning(
                            "question %s raised unexpectedly", question_row.get("id"),
                            exc_info=True,
                        )
                        result = QuestionResult(
                            id=question_row.get("id", "?"),
                            question=question_row.get("question", ""),
                            passed=False,
                            cypher=None,
                            records_count=0,
                            failures=("unexpected exception during run_question",),
                            llm_calls=0,
                        )
                    results.append(result)
                    print(format_line(result))
        finally:
            driver.close()

    summary = summarize(results, suite=args.suite, model=model)
    results_path = write_results(summary)
    print(
        f"\n{summary['passed']}/{summary['total']} passed "
        f"(pass rate {summary['pass_rate']:.2%}), {summary['llm_calls']} llm calls"
    )
    print(f"results written to {results_path}")
    return threshold_exit_code(summary["pass_rate"])


if __name__ == "__main__":
    sys.exit(main())
