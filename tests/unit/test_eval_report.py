"""Unit tests for the EVALS.md report writer (pure markdown builder + thin
file wrapper) and for ci.yml being parseable YAML."""

import json
from pathlib import Path

from zeitgeist.evals.runner import build_evals_md, parse_history_rows, write_evals_md


def make_summary(**overrides) -> dict:
    """Summary shaped exactly like summarize() + summarize_faithfulness()
    produce (and like evals/results/all-*.json stores)."""
    summary = {
        "suite": "all",
        "model": "claude-haiku-4-5",
        "ran_at": "2026-08-28T00:05:38.322764+00:00",
        "total": 26,
        "passed": 26,
        "pass_rate": 1.0,
        "llm_calls": 26,
        "questions": [
            {
                "id": "R01",
                "question": "What happened around GERMANY today?",
                "passed": True,
                "cypher": "MATCH ...",
                "records_count": 8,
                "failures": [],
                "llm_calls": 1,
            }
        ],
        "faithfulness": {
            "total": 26,
            "judged": 26,
            "supported": 26,
            "judge_errors": 0,
            "faithfulness_rate": 1.0,
            "llm_calls": 52,
            "questions": [
                {
                    "id": "R01",
                    "question": "What happened around GERMANY today?",
                    "supported": True,
                    "unsupported_claims": [],
                    "confidence": 0.95,
                    "citation_check": True,
                    "answer_shape_check": True,
                    "answer": "Stuff happened [1].",
                    "failures": [],
                    "llm_calls": 2,
                }
            ],
        },
    }
    summary.update(overrides)
    return summary


THRESHOLDS = {"retrieval_pass_rate": 0.95, "faithfulness_rate": 0.95}


# --- scores table ------------------------------------------------------------


def test_scores_table_has_retrieval_and_faithfulness_rows():
    md = build_evals_md(make_summary(), thresholds=THRESHOLDS)
    assert "| suite | score | questions | llm calls |" in md
    assert "| retrieval | 100.00% | 26/26 passed | 26 |" in md
    assert "| faithfulness | 100.00% | 26/26 supported | 52 |" in md


def test_scores_table_omits_faithfulness_row_when_suite_did_not_run_it():
    summary = make_summary()
    del summary["faithfulness"]
    summary["suite"] = "retrieval"
    md = build_evals_md(summary)
    assert "| retrieval | 100.00% | 26/26 passed | 26 |" in md
    # no faithfulness score row (the History table header's "faithfulness" column is fine)
    assert not any(line.startswith("| faithfulness |") for line in md.splitlines())
    assert "Judge errors:" not in md


def test_run_date_and_model_present():
    md = build_evals_md(make_summary())
    assert "2026-08-28" in md
    assert "claude-haiku-4-5" in md


def test_judge_errors_count_reported():
    faith = make_summary()["faithfulness"] | {"judge_errors": 3, "judged": 23, "supported": 23}
    md = build_evals_md(make_summary(faithfulness=faith))
    assert "Judge errors: 3" in md


# --- failure table -----------------------------------------------------------


def test_all_pass_renders_single_line_no_failure_table():
    md = build_evals_md(make_summary())
    assert "All questions passed." in md
    assert "| id | question | failures |" not in md


def test_retrieval_failures_render_table():
    summary = make_summary(
        passed=25,
        pass_rate=25 / 26,
        questions=[
            {
                "id": "R07",
                "question": "Which events involve ECB?",
                "passed": False,
                "cypher": "MATCH ...",
                "records_count": 50,
                "failures": ["all_records_mention: 42/50 records do not mention 'ECB'"],
                "llm_calls": 1,
            }
        ],
    )
    md = build_evals_md(summary)
    assert "| id | question | failures |" in md
    assert "R07" in md
    assert "42/50 records do not mention 'ECB'" in md
    assert "All questions passed." not in md


def test_unsupported_faithfulness_question_lands_in_failure_table():
    faith = make_summary()["faithfulness"]
    faith = faith | {
        "supported": 25,
        "faithfulness_rate": 25 / 26,
        "questions": [
            {
                "id": "R09",
                "question": "What did FRANCE do?",
                "supported": False,
                "unsupported_claims": ["FRANCE signed a treaty with MARS"],
                "confidence": 0.9,
                "citation_check": True,
                "answer_shape_check": True,
                "answer": "FRANCE signed a treaty with MARS [1].",
                "failures": [],
                "llm_calls": 2,
            }
        ],
    }
    md = build_evals_md(make_summary(faithfulness=faith))
    assert "| id | question | failures |" in md
    assert "R09" in md
    assert "FRANCE signed a treaty with MARS" in md


def test_pipes_in_failure_text_are_escaped():
    summary = make_summary(
        passed=25,
        pass_rate=25 / 26,
        questions=[
            {
                "id": "R05",
                "question": "a | b?",
                "passed": False,
                "cypher": None,
                "records_count": 0,
                "failures": ["expected x | got y"],
                "llm_calls": 1,
            }
        ],
    )
    md = build_evals_md(summary)
    assert "a \\| b?" in md
    assert "expected x \\| got y" in md


# --- thresholds table --------------------------------------------------------


def test_thresholds_table_rendered_from_dict():
    md = build_evals_md(make_summary(), thresholds=THRESHOLDS)
    assert "| retrieval_pass_rate | 0.95 |" in md
    assert "| faithfulness_rate | 0.95 |" in md


def test_no_thresholds_renders_placeholder_not_table():
    md = build_evals_md(make_summary(), thresholds=None)
    assert "No thresholds committed yet" in md
    assert "| retrieval_pass_rate |" not in md


# --- cost line ---------------------------------------------------------------


def test_cost_line_sums_both_legs_and_multiplies():
    # 26 retrieval + 52 faithfulness = 78 calls; 78 * 0.0029 = 0.2262 -> $0.23
    md = build_evals_md(make_summary())
    assert "78 LLM calls" in md
    assert "$0.23" in md
    assert "approximate" in md


def test_cost_line_retrieval_only():
    summary = make_summary(suite="retrieval")
    del summary["faithfulness"]
    md = build_evals_md(summary)
    # 26 * 0.0029 = 0.0754 -> $0.08
    assert "26 LLM calls" in md
    assert "$0.08" in md


# --- history -----------------------------------------------------------------


EXISTING = """# zeitgeist Evals

## Scores

stale stuff

## History

| date | retrieval | faithfulness | notes |
|---|---|---|---|
| 2026-08-27 | 100.00% (26/26) | — | prompt fix 2 |
| 2026-08-27 | 11.54% (3/26) | — | baseline |

## Extraction evals

Pending.
"""


def test_parse_history_rows_skips_header_and_separator():
    rows = parse_history_rows(EXISTING)
    assert rows == [
        "| 2026-08-27 | 100.00% (26/26) | — | prompt fix 2 |",
        "| 2026-08-27 | 11.54% (3/26) | — | baseline |",
    ]


def test_parse_history_rows_no_history_section():
    assert parse_history_rows("# Title\n\nno table here\n") == []


def test_history_new_row_prepended_prior_rows_preserved():
    md = build_evals_md(make_summary(), existing=EXISTING, notes="full run")
    history = md[md.index("## History"):]
    new_row = history.index("| 2026-08-28 | 100.00% (26/26) | 100.00% (26/26) | full run |")
    fix2 = history.index("| 2026-08-27 | 100.00% (26/26) | — | prompt fix 2 |")
    baseline = history.index("| 2026-08-27 | 11.54% (3/26) | — | baseline |")
    assert new_row < fix2 < baseline


def test_history_without_existing_file_has_only_new_row():
    md = build_evals_md(make_summary(), notes="first run")
    assert "| 2026-08-28 | 100.00% (26/26) | 100.00% (26/26) | first run |" in md


def test_history_faithfulness_cell_dash_when_not_run():
    summary = make_summary(suite="retrieval")
    del summary["faithfulness"]
    md = build_evals_md(summary)
    assert "| 2026-08-28 | 100.00% (26/26) | — |" in md


# --- placeholder section ------------------------------------------------------


def test_extraction_placeholder_present():
    md = build_evals_md(make_summary())
    assert "Extraction evals" in md
    assert "pending golden data (Task 5)" in md


# --- file wrapper ------------------------------------------------------------


def test_write_evals_md_reads_existing_and_thresholds(tmp_path: Path):
    path = tmp_path / "EVALS.md"
    path.write_text(EXISTING, encoding="utf-8")
    thresholds_path = tmp_path / "thresholds.json"
    thresholds_path.write_text(json.dumps(THRESHOLDS), encoding="utf-8")

    write_evals_md(make_summary(), path=path, thresholds_path=thresholds_path)

    md = path.read_text(encoding="utf-8")
    assert "| retrieval_pass_rate | 0.95 |" in md
    assert "| 2026-08-27 | 11.54% (3/26) | — | baseline |" in md  # preserved
    assert "| 2026-08-28 |" in md  # prepended


def test_write_evals_md_fresh_file_no_thresholds(tmp_path: Path):
    path = tmp_path / "EVALS.md"
    write_evals_md(
        make_summary(), path=path, thresholds_path=tmp_path / "missing.json"
    )
    md = path.read_text(encoding="utf-8")
    assert "No thresholds committed yet" in md
    assert "| 2026-08-28 |" in md


# --- ci.yml parses ------------------------------------------------------------


def test_ci_yaml_parses_and_evals_job_wired():
    yaml = __import__("yaml")  # pyyaml is a transitive dep (uvicorn[standard])
    ci_path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    assert "evals" in workflow["jobs"]
    evals_job = workflow["jobs"]["evals"]
    assert evals_job["timeout-minutes"] == 15
    run_steps = [step.get("run", "") for step in evals_job["steps"]]
    assert any("zeitgeist.evals.runner --suite all" in run for run in run_steps)
    # deploy must not gate on evals, and must exclude the golden-data dir
    assert "evals" not in workflow["jobs"]["deploy"].get("needs", [])
    deploy_runs = " ".join(step.get("run", "") for step in workflow["jobs"]["deploy"]["steps"])
    assert "--exclude evals" in deploy_runs
