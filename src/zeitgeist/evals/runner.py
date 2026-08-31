"""Eval runner: real Cypher generation against a deterministic seeded graph in
a throwaway Neo4j container, plus citation-faithfulness grading of the answers
synthesized from those retrievals, plus (once labeled golden data exists)
extraction precision/recall over evals/golden_extraction.jsonl.

    python -m zeitgeist.evals.runner --suite {retrieval,faithfulness,extraction,all}
                                     [--golden PATH] [--golden-extraction PATH] [--limit N]

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

The extraction leg needs NO Neo4j container — only the API key and the golden
file: `--suite extraction` alone never boots testcontainers, and when the
golden file is absent or empty it exits 0 with a clear notice (`--suite all`
prints the notice and simply skips the leg).
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
from zeitgeist.evals.extraction import ItemResult, run_extraction, score_items
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
DEFAULT_GOLDEN_EXTRACTION = Path("evals/golden_extraction.jsonl")
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


def load_extraction_rows(path: Path) -> list[dict] | None:
    """The labeled extraction golden rows, or None (with a printed notice)
    when the file is absent or empty — the extraction leg then doesn't run."""
    if not path.exists():
        print(f"extraction: no golden data ({path} missing)")
        return None
    rows = load_golden(path)
    if not rows:
        print(f"extraction: no golden data ({path} empty)")
        return None
    return rows


def summarize_extraction(items: list[ItemResult]) -> dict:
    """Aggregate the extraction leg: score_items' overall + per-relation
    metrics, plus per-item detail and the llm-call count (one extract call
    per item)."""
    return {
        **score_items(items),
        "llm_calls": len(items),
        "items": [asdict(item) for item in items],
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


def format_extraction_line(item: ItemResult) -> str:
    if item.error is not None:
        return f"[ERROR] {item.id}: {item.error}"
    status = "PASS" if item.fp == 0 and item.fn == 0 else "MISS"
    line = (
        f"[{status}] {item.id}: tp={item.tp} fp={item.fp} fn={item.fn} "
        f"(predicted {len(item.predicted)}, expected {len(item.expected)})"
    )
    for triple in item.unmatched_predicted:
        line += f"\n       - false positive: {triple}"
    for triple in item.unmatched_expected:
        line += f"\n       - missed: {triple}"
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


# --- EVALS.md report writer --------------------------------------------------

EVALS_MD_PATH = Path("EVALS.md")
# Approximate cost of one Haiku call at this suite's typical prompt/answer
# sizes, measured from real runs. A label, not an invoice.
APPROX_COST_PER_CALL_USD = 0.0029

THRESHOLD_RATIONALE = (
    "Floors sit at 0.85, set from MEASURED run-to-run variance (92.31%-100% across "
    "identical prompts): the current Messages API exposes no sampling controls (no "
    "temperature parameter), so query-shape variance is inherent and floors must "
    "tolerate it. 0.85 forgives up to three flaky questions per 26 while still "
    "catching any real regression — the bug this harness exists for cratered "
    "retrieval to 11.54%. Raising a floor is a deliberate commit."
)


def _md_escape(text: str) -> str:
    """Keep arbitrary question/failure text from breaking a markdown table."""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _pct(value: float | None) -> str:
    """Render a rate, or an em-dash for an undefined (zero-division) one."""
    return f"{value:.2%}" if value is not None else "—"


def parse_history_rows(markdown: str) -> list[str]:
    """Extract the data rows (verbatim `| ... |` lines, header and separator
    skipped) of the History table from an existing EVALS.md, so a new run can
    prepend its row without losing the record of prior runs."""
    rows: list[str] = []
    in_history = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            in_history = line.strip().lower() == "## history"
            continue
        if not in_history:
            continue
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0].lower() == "date":
            continue  # header
        if all(set(cell) <= set(":- ") for cell in cells):
            continue  # separator
        rows.append(stripped)
    return rows


def _history_row(summary: dict, notes: str) -> str:
    date = summary["ran_at"][:10]
    if "pass_rate" in summary:
        retrieval = f"{summary['pass_rate']:.2%} ({summary['passed']}/{summary['total']})"
    else:
        retrieval = "—"
    faith = summary.get("faithfulness")
    if faith is not None:
        faith_cell = (
            f"{faith['faithfulness_rate']:.2%} ({faith['supported']}/{faith['judged']})"
        )
    else:
        faith_cell = "—"
    return f"| {date} | {retrieval} | {faith_cell} | {_md_escape(notes)} |"


def _extraction_section(extraction: dict) -> list[str]:
    """The EVALS.md Extraction section rendered from a summarize_extraction
    dict — replaces the pending-golden-data placeholder once data exists."""
    lines = [
        "",
        "## Extraction evals",
        "",
        (
            f"{extraction['total_items']} golden items · {extraction['errors']} errors · "
            f"tp {extraction['tp']} / fp {extraction['fp']} / fn {extraction['fn']}"
        ),
        "",
        "| metric | value |",
        "|---|---|",
        f"| precision | {_pct(extraction['precision'])} |",
        f"| recall | {_pct(extraction['recall'])} |",
        f"| F1 | {_pct(extraction['f1'])} |",
        "",
        (
            "Matching is exact on canonicalized (subject, relation, object): relation "
            "synonyms (ANNOUNCED vs STATED) count as misses by design — track the trend, "
            "not the absolute value. — means undefined (zero-division guarded)."
        ),
    ]
    per_relation = extraction.get("per_relation") or {}
    if per_relation:
        lines += [
            "",
            "| relation | tp | fp | fn | precision | recall | f1 |",
            "|---|---|---|---|---|---|---|",
        ]
        for relation, stats in sorted(per_relation.items()):
            lines.append(
                f"| {_md_escape(relation)} | {stats['tp']} | {stats['fp']} | {stats['fn']} "
                f"| {_pct(stats['precision'])} | {_pct(stats['recall'])} "
                f"| {_pct(stats['f1'])} |"
            )
    return lines


def _failure_rows(summary: dict) -> list[str]:
    rows: list[str] = []
    for question in summary.get("questions", []):
        if question["passed"]:
            continue
        failures = "; ".join(question["failures"]) or "failed"
        rows.append(
            f"| {_md_escape(question['id'])} | {_md_escape(question['question'])} "
            f"| {_md_escape(failures)} |"
        )
    faith = summary.get("faithfulness")
    for question in (faith or {}).get("questions", []):
        problems = list(question["failures"])
        if question["supported"] is None:
            problems.append("judge error (no verdict)")
        elif not question["supported"]:
            problems.extend(f"unsupported: {claim}" for claim in question["unsupported_claims"])
            problems = problems or ["judge verdict: unsupported"]
        if not problems:
            continue
        rows.append(
            f"| {_md_escape(question['id'])} (faithfulness) "
            f"| {_md_escape(question['question'])} | {_md_escape('; '.join(problems))} |"
        )
    return rows


def build_evals_md(
    summary: dict,
    thresholds: dict | None = None,
    existing: str | None = None,
    notes: str = "",
) -> str:
    """Render EVALS.md from one run's summary dict (the results-JSON shape).

    Pure: file reading/writing lives in write_evals_md. `existing` is the
    previous EVALS.md content (its History rows are preserved, the new run
    prepended); `thresholds` is the parsed evals/thresholds.json when present."""
    ran_at = summary["ran_at"][:16].replace("T", " ")
    faith = summary.get("faithfulness")
    extraction = summary.get("extraction")

    lines: list[str] = [
        "# zeitgeist Evals",
        "",
        (
            "Generated by `python -m zeitgeist.evals.runner` — regenerated on every run; "
            "edit only History notes by hand."
        ),
        "",
        (
            f"**Last run:** {ran_at} UTC · **Model:** {summary['model']} · "
            f"**Suite:** {summary['suite']}"
        ),
        "",
        "## Scores",
        "",
        "| suite | score | questions | llm calls |",
        "|---|---|---|---|",
    ]
    if "pass_rate" in summary:
        lines.append(
            f"| retrieval | {summary['pass_rate']:.2%} | {summary['passed']}/{summary['total']} "
            f"passed | {summary['llm_calls']} |"
        )
    if faith is not None:
        lines.append(
            f"| faithfulness | {faith['faithfulness_rate']:.2%} | "
            f"{faith['supported']}/{faith['judged']} supported | {faith['llm_calls']} |"
        )
    if extraction is not None:
        lines.append(
            f"| extraction | F1 {_pct(extraction['f1'])} | {extraction['total_items']} items "
            f"| {extraction['llm_calls']} |"
        )
    if faith is not None:
        lines += ["", f"Judge errors: {faith['judge_errors']}"]

    lines += ["", "## Thresholds", ""]
    if thresholds:
        lines += ["| metric | floor |", "|---|---|"]
        lines += [f"| {key} | {value} |" for key, value in thresholds.items()]
        lines += ["", THRESHOLD_RATIONALE]
    else:
        lines.append("No thresholds committed yet (see evals/thresholds.json).")

    lines += ["", "## Failures", ""]
    failure_rows = _failure_rows(summary)
    if failure_rows:
        lines += ["| id | question | failures |", "|---|---|---|", *failure_rows]
    else:
        lines.append("All questions passed.")

    total_calls = (
        summary.get("llm_calls", 0)
        + (faith["llm_calls"] if faith else 0)
        + (extraction["llm_calls"] if extraction else 0)
    )
    cost = total_calls * APPROX_COST_PER_CALL_USD
    lines += [
        "",
        "## Cost",
        "",
        (
            f"{total_calls} LLM calls × ~${APPROX_COST_PER_CALL_USD}/call ≈ ${cost:.2f} "
            "(approximate — flat per-call estimate from measured runs, not token-metered)."
        ),
        "",
        "## History",
        "",
        "| date | retrieval | faithfulness | notes |",
        "|---|---|---|---|",
        _history_row(summary, notes),
        *parse_history_rows(existing or ""),
    ]
    if extraction is not None:
        lines += _extraction_section(extraction)
    else:
        lines += [
            "",
            "## Extraction evals",
            "",
            (
                "Extraction evals: pending golden data (Task 5) — the Task 1 archiver is "
                "accumulating extraction inputs; per-relation precision/recall lands once "
                "labeled pairs exist."
            ),
        ]
    lines.append("")
    return "\n".join(lines)


def write_evals_md(
    summary: dict,
    path: Path = EVALS_MD_PATH,
    thresholds_path: Path = THRESHOLDS_PATH,
    notes: str = "",
) -> None:
    """Thin file wrapper around build_evals_md: preserve the existing History,
    pull committed thresholds when present, write the report."""
    existing = path.read_text(encoding="utf-8") if path.exists() else None
    thresholds = (
        json.loads(thresholds_path.read_text(encoding="utf-8"))
        if thresholds_path.exists()
        else None
    )
    path.write_text(
        build_evals_md(summary, thresholds=thresholds, existing=existing, notes=notes),
        encoding="utf-8",
    )


def _run_retrieval_leg(
    agent, judge, questions: list[dict], run_faith: bool
) -> tuple[list[tuple[QuestionResult, list[dict]]], list[FaithfulnessResult]]:
    """Retrieval (and optionally faithfulness) against a throwaway seeded
    Neo4j container. Heavy deps imported here so an extraction-only run never
    touches testcontainers."""
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
    return retrieval_results, faithfulness_results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zeitgeist.evals.runner")
    parser.add_argument(
        "--suite", required=True, choices=["retrieval", "faithfulness", "extraction", "all"]
    )
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--golden-extraction", type=Path, default=DEFAULT_GOLDEN_EXTRACTION)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    # Faithfulness grades the answers synthesized from retrieval's executed
    # records, so retrieval runs for both legs; --suite picks what is
    # reported/gated. The extraction leg is container-free and independent.
    gate_retrieval = args.suite in ("retrieval", "all")
    run_retrieval = args.suite in ("retrieval", "faithfulness", "all")
    run_faith = args.suite in ("faithfulness", "all")
    run_extract = args.suite in ("extraction", "all")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ANTHROPIC_API_KEY is not set. The eval suites generate real "
            "Cypher/answers through the API; set the key and re-run.",
            file=sys.stderr,
        )
        return 2

    extraction_rows: list[dict] | None = None
    if run_extract:
        extraction_rows = load_extraction_rows(args.golden_extraction)
        if extraction_rows is None:
            if args.suite == "extraction":
                return 0  # graceful: notice printed, nothing to gate
            run_extract = False  # --suite all continues without the leg
        elif args.limit is not None:
            extraction_rows = extraction_rows[: args.limit]

    import anthropic  # deferred with the other heavy deps

    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    client = anthropic.Anthropic(api_key=api_key)

    retrieval_results: list[tuple[QuestionResult, list[dict]]] = []
    faithfulness_results: list[FaithfulnessResult] = []
    if run_retrieval:
        questions = load_golden(args.golden)
        if args.limit is not None:
            questions = questions[: args.limit]
        agent = QueryAgent(client, model=model)
        judge = FaithfulnessJudge(client, model=model) if run_faith else None
        retrieval_results, faithfulness_results = _run_retrieval_leg(
            agent, judge, questions, run_faith
        )

    extraction_summary: dict | None = None
    if run_extract:
        from zeitgeist.llm.extract import LlmExtractor

        print("\nextraction (golden labeled articles):")
        extractor = LlmExtractor(client, model=model)
        items = run_extraction(extractor, extraction_rows)
        for item in items:
            print(format_extraction_line(item))
        extraction_summary = summarize_extraction(items)

    rates: dict[str, float] = {}
    if run_retrieval:
        results = [result for result, _records in retrieval_results]
        summary = summarize(results, suite=args.suite, model=model)
        if gate_retrieval:
            rates["retrieval_pass_rate"] = summary["pass_rate"]
        if run_faith:
            faith_summary = summarize_faithfulness(faithfulness_results)
            summary["faithfulness"] = faith_summary
            rates["faithfulness_rate"] = faith_summary["faithfulness_rate"]
    else:
        summary = {
            "suite": args.suite,
            "model": model,
            "ran_at": datetime.now(UTC).isoformat(),
        }
    if extraction_summary is not None:
        summary["extraction"] = extraction_summary
        if extraction_summary["f1"] is not None:
            rates["extraction_f1"] = extraction_summary["f1"]

    results_path = write_results(summary)
    if run_retrieval:
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
    if extraction_summary is not None:
        print(
            f"extraction: {extraction_summary['total_items']} items "
            f"(tp {extraction_summary['tp']} / fp {extraction_summary['fp']} "
            f"/ fn {extraction_summary['fn']}), "
            f"precision {_pct(extraction_summary['precision'])}, "
            f"recall {_pct(extraction_summary['recall'])}, "
            f"F1 {_pct(extraction_summary['f1'])}, "
            f"{extraction_summary['errors']} errors, "
            f"{extraction_summary['llm_calls']} llm calls"
        )
    print(f"results written to {results_path}")
    write_evals_md(summary, notes=f"--suite {args.suite} run")
    print(f"report written to {EVALS_MD_PATH}")
    return threshold_exit_code(rates)


if __name__ == "__main__":
    sys.exit(main())
