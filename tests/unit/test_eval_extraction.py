"""Unit tests for the extraction eval: claim matching/metrics (pure), the
golden-file runner leg's graceful no-data path, the EVALS.md extraction
section, and the stratified sampling script (scripts/label_extraction.py)."""

import importlib.util
import json
from pathlib import Path

from zeitgeist.evals import runner
from zeitgeist.evals.extraction import (
    ItemResult,
    canonical_claim,
    match_claims,
    run_extraction,
    score_items,
)
from zeitgeist.llm.extract import LlmClaim
from zeitgeist.models import GdeltEvent


def make_llm_claim(subject="GERMANY", relation="ANNOUNCED", object="FRANCE", **overrides):
    base = {
        "subject": subject,
        "relation": relation,
        "object": object,
        "detail": "a detail",
        "confidence": 1.0,
    }
    base.update(overrides)
    return LlmClaim(**base)


def expected(subject, relation, object=None):
    return {"subject": subject, "relation": relation, "object": object}


# Field names copied EXACTLY from zeitgeist.models.GdeltEvent — the archive
# stores asdict(event), so reconstruction is GdeltEvent(**event_dict).
ARCHIVED_EVENT = {
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


def make_golden_row(**overrides):
    base = {
        "id": "X01",
        "source": {"event": dict(ARCHIVED_EVENT), "article_text": "Some article."},
        "expected_claims": [expected("UNITED STATES", "MET_WITH", "EUROPEAN CENTRAL BANK")],
        "labeled_at": "2026-08-31T00:00:00+00:00",
        "labeler": "harsh",
    }
    base.update(overrides)
    return base


# --- canonical_claim ---------------------------------------------------------


def test_canonical_claim_uppercases_and_strips():
    assert canonical_claim(" germany ", "announced_sanctions", " France") == (
        "GERMANY",
        "ANNOUNCED_SANCTIONS",
        "FRANCE",
    )


def test_canonical_claim_none_object_stays_none():
    assert canonical_claim("GERMANY", "PROTESTED", None) == ("GERMANY", "PROTESTED", None)


# --- match_claims ------------------------------------------------------------


def test_match_exact_claim_is_true_positive():
    result = match_claims(
        [make_llm_claim("GERMANY", "ANNOUNCED", "FRANCE")],
        [expected("GERMANY", "ANNOUNCED", "FRANCE")],
    )
    assert result.true_positives == 1
    assert result.false_positives == 0
    assert result.false_negatives == 0
    assert result.matched == (("GERMANY", "ANNOUNCED", "FRANCE"),)
    assert result.unmatched_predicted == ()
    assert result.unmatched_expected == ()


def test_match_is_case_and_whitespace_insensitive():
    result = match_claims(
        [make_llm_claim(" germany", "Announced ", "france ")],
        [expected("GERMANY", "ANNOUNCED", "FRANCE")],
    )
    assert result.true_positives == 1
    assert result.false_positives == 0
    assert result.false_negatives == 0


def test_match_none_object_matches_none_only():
    result = match_claims(
        [make_llm_claim("GERMANY", "PROTESTED", None)],
        [expected("GERMANY", "PROTESTED", None)],
    )
    assert result.true_positives == 1

    result = match_claims(
        [make_llm_claim("GERMANY", "PROTESTED", None)],
        [expected("GERMANY", "PROTESTED", "FRANCE")],
    )
    assert result.true_positives == 0
    assert result.false_positives == 1
    assert result.false_negatives == 1


def test_match_relation_synonym_is_a_miss_by_design():
    result = match_claims(
        [make_llm_claim("GERMANY", "STATED", "FRANCE")],
        [expected("GERMANY", "ANNOUNCED", "FRANCE")],
    )
    assert result.true_positives == 0
    assert result.false_positives == 1
    assert result.false_negatives == 1


def test_match_is_greedy_one_to_one_duplicates_cannot_double_match():
    result = match_claims(
        [
            make_llm_claim("GERMANY", "ANNOUNCED", "FRANCE"),
            make_llm_claim("GERMANY", "ANNOUNCED", "FRANCE"),
        ],
        [expected("GERMANY", "ANNOUNCED", "FRANCE")],
    )
    assert result.true_positives == 1
    assert result.false_positives == 1
    assert result.false_negatives == 0


def test_match_fp_and_fn_accounting():
    result = match_claims(
        [
            make_llm_claim("GERMANY", "ANNOUNCED", "FRANCE"),
            make_llm_claim("ECB", "RAISED_RATES", None),
        ],
        [
            expected("GERMANY", "ANNOUNCED", "FRANCE"),
            expected("POLAND", "SIGNED_DEAL_WITH", "UKRAINE"),
            expected("NATO", "EXPANDED", None),
        ],
    )
    assert result.true_positives == 1
    assert result.false_positives == 1
    assert result.false_negatives == 2
    assert result.unmatched_predicted == (("ECB", "RAISED_RATES", None),)
    assert set(result.unmatched_expected) == {
        ("POLAND", "SIGNED_DEAL_WITH", "UKRAINE"),
        ("NATO", "EXPANDED", None),
    }


def test_match_empty_both_sides_is_all_zero():
    result = match_claims([], [])
    assert (result.true_positives, result.false_positives, result.false_negatives) == (0, 0, 0)


# --- score_items ---------------------------------------------------------------


def make_item(tp=0, fp=0, fn=0, matched=(), unmatched_predicted=(), unmatched_expected=(),
              item_id="X01", error=None):
    return ItemResult(
        id=item_id,
        predicted=tuple(matched) + tuple(unmatched_predicted),
        expected=tuple(matched) + tuple(unmatched_expected),
        matched=tuple(matched),
        unmatched_predicted=tuple(unmatched_predicted),
        unmatched_expected=tuple(unmatched_expected),
        tp=tp,
        fp=fp,
        fn=fn,
        error=error,
    )


def test_score_items_precision_recall_f1_arithmetic():
    items = [
        make_item(tp=2, fp=1, matched=[("A", "R1", "B"), ("C", "R1", None)],
                  unmatched_predicted=[("D", "R2", "E")]),
        make_item(tp=1, fn=1, matched=[("F", "R2", "G")],
                  unmatched_expected=[("H", "R1", "I")], item_id="X02"),
    ]
    scores = score_items(items)
    assert scores["tp"] == 3
    assert scores["fp"] == 1
    assert scores["fn"] == 1
    assert scores["precision"] == 3 / 4
    assert scores["recall"] == 3 / 4
    assert scores["f1"] == 3 / 4
    assert scores["total_items"] == 2
    assert scores["errors"] == 0


def test_score_items_zero_predicted_precision_is_none():
    items = [make_item(fn=2, unmatched_expected=[("A", "R1", "B"), ("C", "R1", None)])]
    scores = score_items(items)
    assert scores["precision"] is None
    assert scores["recall"] == 0.0
    assert scores["f1"] is None


def test_score_items_zero_expected_recall_is_none():
    items = [make_item(fp=1, unmatched_predicted=[("A", "R1", "B")])]
    scores = score_items(items)
    assert scores["precision"] == 0.0
    assert scores["recall"] is None
    assert scores["f1"] is None


def test_score_items_nothing_predicted_or_expected_all_none():
    scores = score_items([make_item()])
    assert scores["precision"] is None
    assert scores["recall"] is None
    assert scores["f1"] is None


def test_score_items_all_wrong_f1_zero_not_division_error():
    items = [make_item(fp=1, fn=1, unmatched_predicted=[("A", "R1", "B")],
                       unmatched_expected=[("C", "R2", "D")])]
    scores = score_items(items)
    assert scores["precision"] == 0.0
    assert scores["recall"] == 0.0
    assert scores["f1"] == 0.0


def test_score_items_per_relation_breakdown():
    items = [
        make_item(tp=1, fp=1, fn=1,
                  matched=[("A", "ANNOUNCED", "B")],
                  unmatched_predicted=[("C", "ANNOUNCED", "D")],
                  unmatched_expected=[("E", "RAISED_RATES", None)]),
        make_item(tp=1, matched=[("F", "RAISED_RATES", "G")], item_id="X02"),
    ]
    per_relation = score_items(items)["per_relation"]
    # relation attribution: predicted's relation for TP/FP, expected's for FN
    assert per_relation["ANNOUNCED"]["tp"] == 1
    assert per_relation["ANNOUNCED"]["fp"] == 1
    assert per_relation["ANNOUNCED"]["fn"] == 0
    assert per_relation["ANNOUNCED"]["precision"] == 0.5
    assert per_relation["ANNOUNCED"]["recall"] == 1.0
    assert per_relation["RAISED_RATES"]["tp"] == 1
    assert per_relation["RAISED_RATES"]["fn"] == 1
    assert per_relation["RAISED_RATES"]["recall"] == 0.5
    assert per_relation["RAISED_RATES"]["precision"] == 1.0


def test_score_items_excludes_errored_items_from_metrics():
    items = [
        make_item(tp=1, matched=[("A", "R1", "B")]),
        make_item(item_id="X02", error="boom"),
    ]
    scores = score_items(items)
    assert scores["errors"] == 1
    assert scores["total_items"] == 2
    assert scores["tp"] == 1
    assert scores["precision"] == 1.0


# --- run_extraction ---------------------------------------------------------------


class FakeExtractor:
    """Returns queued (claims, usage) tuples, or raises a queued exception."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[tuple[GdeltEvent, str]] = []

    def extract(self, event, article_text):
        self.calls.append((event, article_text))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {"input_tokens": 1, "output_tokens": 1}


def test_run_extraction_reconstructs_event_and_matches():
    extractor = FakeExtractor([[make_llm_claim("UNITED STATES", "MET_WITH",
                                               "EUROPEAN CENTRAL BANK")]])
    items = run_extraction(extractor, [make_golden_row()])
    assert len(items) == 1
    item = items[0]
    assert item.id == "X01"
    assert item.error is None
    assert (item.tp, item.fp, item.fn) == (1, 0, 0)
    # the extractor received a real GdeltEvent rebuilt from the archived dict
    event, article_text = extractor.calls[0]
    assert isinstance(event, GdeltEvent)
    assert event.event_root_code == "04"
    assert event.actor1_name == "UNITED STATES"
    assert article_text == "Some article."


def test_run_extraction_genuine_negative_row():
    extractor = FakeExtractor([[]])
    items = run_extraction(extractor, [make_golden_row(expected_claims=[])])
    assert (items[0].tp, items[0].fp, items[0].fn) == (0, 0, 0)
    assert items[0].error is None


def test_run_extraction_crash_guard_one_bad_row_run_survives():
    good = [make_llm_claim("UNITED STATES", "MET_WITH", "EUROPEAN CENTRAL BANK")]
    extractor = FakeExtractor([good, RuntimeError("api melted"), good])
    rows = [
        make_golden_row(id="X01"),
        make_golden_row(id="X02"),
        make_golden_row(id="X03"),
    ]
    items = run_extraction(extractor, rows)
    assert len(items) == 3
    assert items[0].error is None and items[0].tp == 1
    assert items[1].error is not None and "api melted" in items[1].error
    assert (items[1].tp, items[1].fp, items[1].fn) == (0, 0, 0)
    assert items[2].error is None and items[2].tp == 1


def test_run_extraction_malformed_event_dict_is_one_failed_item():
    row = make_golden_row()
    row["source"]["event"] = {"event_id": "1", "bogus_field": True}
    extractor = FakeExtractor([])  # never reached
    items = run_extraction(extractor, [row])
    assert items[0].error is not None
    assert extractor.calls == []


# --- runner: graceful no-golden-data path ------------------------------------------


def test_runner_extraction_suite_missing_file_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    missing = tmp_path / "golden_extraction.jsonl"
    code = runner.main(["--suite", "extraction", "--golden-extraction", str(missing)])
    assert code == 0
    out = capsys.readouterr().out
    assert "extraction: no golden data" in out
    assert "missing" in out


def test_runner_extraction_suite_empty_file_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    empty = tmp_path / "golden_extraction.jsonl"
    empty.write_text("\n", encoding="utf-8")
    code = runner.main(["--suite", "extraction", "--golden-extraction", str(empty)])
    assert code == 0
    assert "extraction: no golden data" in capsys.readouterr().out


def test_summarize_extraction_counts_one_llm_call_per_item():
    items = [make_item(tp=1, matched=[("A", "R1", "B")]),
             make_item(item_id="X02", error="boom")]
    summary = runner.summarize_extraction(items)
    assert summary["llm_calls"] == 2
    assert len(summary["items"]) == 2
    assert summary["items"][0]["id"] == "X01"


# --- EVALS.md: extraction section ---------------------------------------------------


def extraction_summary():
    return {
        "total_items": 2,
        "errors": 0,
        "tp": 3,
        "fp": 1,
        "fn": 1,
        "precision": 0.75,
        "recall": 0.75,
        "f1": 0.75,
        "per_relation": {
            "ANNOUNCED": {"tp": 2, "fp": 1, "fn": 0,
                          "precision": 2 / 3, "recall": 1.0, "f1": 0.8},
            "RAISED_RATES": {"tp": 1, "fp": 0, "fn": 1,
                             "precision": 1.0, "recall": 0.5, "f1": 2 / 3},
        },
        "llm_calls": 2,
        "items": [],
    }


def test_evals_md_extraction_section_replaces_placeholder():
    from tests.unit.test_eval_report import make_summary

    md = runner.build_evals_md(make_summary(extraction=extraction_summary()))
    assert "pending golden data" not in md
    assert "| precision | 75.00% |" in md
    assert "| recall | 75.00% |" in md
    assert "| F1 | 75.00% |" in md
    assert "| relation | tp | fp | fn | precision | recall | f1 |" in md
    assert "| ANNOUNCED | 2 | 1 | 0 | 66.67% | 100.00% | 80.00% |" in md
    # extraction llm calls join the cost line: 26 + 52 + 2 = 80
    assert "80 LLM calls" in md
    # and the scores table gains an extraction row
    assert any(line.startswith("| extraction |") for line in md.splitlines())


def test_evals_md_extraction_none_rates_render_as_dash():
    from tests.unit.test_eval_report import make_summary

    summary_ext = extraction_summary() | {
        "precision": None, "recall": None, "f1": None, "per_relation": {},
    }
    md = runner.build_evals_md(make_summary(extraction=summary_ext))
    assert "| precision | — |" in md
    assert "| F1 | — |" in md


def test_evals_md_extraction_only_summary_renders_without_retrieval():
    summary = {
        "suite": "extraction",
        "model": "claude-haiku-4-5",
        "ran_at": "2026-08-31T10:00:00+00:00",
        "extraction": extraction_summary(),
    }
    md = runner.build_evals_md(summary)
    assert "| F1 | 75.00% |" in md
    assert not any(line.startswith("| retrieval |") for line in md.splitlines())
    # history row still renders, retrieval cell dashed
    assert "| 2026-08-31 | — | — |" in md


# --- scripts/label_extraction.py ----------------------------------------------------


def load_sampler():
    script = Path(__file__).resolve().parents[2] / "scripts" / "label_extraction.py"
    spec = importlib.util.spec_from_file_location("label_extraction", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive_row(archived_at, relations):
    claims = [
        {"subject": "S", "relation": relation, "object": "O", "detail": "d", "confidence": 1.0}
        for relation in relations
    ]
    return {
        "schema_version": 1,
        "archived_at": archived_at,
        "event": dict(ARCHIVED_EVENT),
        "article_text": f"article at {archived_at}",
        "claims": claims,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def write_archive(tmp_path):
    """Two daily files, rows deliberately out of time order inside the files."""
    day1 = [
        archive_row("2026-08-28T10:00:00+00:00", ["ANNOUNCED"]),
        archive_row("2026-08-28T09:00:00+00:00", []),
        archive_row("2026-08-28T11:00:00+00:00", ["RAISED_RATES", "ANNOUNCED"]),
    ]
    day2 = [
        archive_row("2026-08-29T08:00:00+00:00", ["ANNOUNCED"]),
        archive_row("2026-08-29T09:00:00+00:00", ["ANNOUNCED"]),
        archive_row("2026-08-29T10:00:00+00:00", []),
    ]
    (tmp_path / "2026-08-28.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in day1), encoding="utf-8"
    )
    (tmp_path / "2026-08-29.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in day2), encoding="utf-8"
    )
    return tmp_path


def test_sampler_bucket_of_primary_relation_and_zero_claims():
    sampler = load_sampler()
    assert sampler.bucket_of(archive_row("t", ["RAISED_RATES", "ANNOUNCED"])) == "RAISED_RATES"
    assert sampler.bucket_of(archive_row("t", [])) == "ZERO_CLAIMS"


def test_sampler_round_robin_stratified_and_deterministic(tmp_path):
    sampler = load_sampler()
    archive_dir = write_archive(tmp_path)
    rows = sampler.load_archive(archive_dir)
    assert [row["archived_at"] for row in rows] == sorted(row["archived_at"] for row in rows)

    buckets = sampler.stratify(rows)
    assert set(buckets) == {"ANNOUNCED", "RAISED_RATES", "ZERO_CLAIMS"}

    picked = sampler.round_robin_sample(buckets, 3)
    # round 1 covers every bucket exactly once (hash visit order — the order
    # itself is unspecified, the COVERAGE is the contract), earliest-archived
    # row first within each bucket
    first_round = {sampler.bucket_of(row) for row in picked}
    assert first_round == {"ANNOUNCED", "RAISED_RATES", "ZERO_CLAIMS"}
    for row in picked:
        bucket_rows = buckets[sampler.bucket_of(row)]
        assert row["archived_at"] == min(r["archived_at"] for r in bucket_rows)

    # deterministic: a second pass over the same inputs picks the same rows
    again = sampler.round_robin_sample(sampler.stratify(sampler.load_archive(archive_dir)), 3)
    assert picked == again


def test_sampler_round_robin_exhausted_buckets_are_skipped(tmp_path):
    sampler = load_sampler()
    buckets = sampler.stratify(sampler.load_archive(write_archive(tmp_path)))
    picked = sampler.round_robin_sample(buckets, 5)
    assert len(picked) == 5
    relations = [sampler.bucket_of(row) for row in picked]
    # round 1 hits all three buckets once; round 2 draws only from the two
    # buckets still non-empty (RAISED_RATES has a single row)
    assert set(relations[:3]) == {"ANNOUNCED", "RAISED_RATES", "ZERO_CLAIMS"}
    assert sorted(relations[3:]) == ["ANNOUNCED", "ZERO_CLAIMS"]


def test_sampler_n_larger_than_archive_returns_everything(tmp_path):
    sampler = load_sampler()
    buckets = sampler.stratify(sampler.load_archive(write_archive(tmp_path)))
    assert len(sampler.round_robin_sample(buckets, 100)) == 6


def test_sampler_sample_command_writes_ids_and_raw_rows(tmp_path):
    sampler = load_sampler()
    (tmp_path / "archive").mkdir()
    archive_dir = write_archive(tmp_path / "archive")
    out = tmp_path / "sampled.jsonl"
    code = sampler.main(
        ["sample", "--archive-dir", str(archive_dir), "--n", "4", "--out", str(out)]
    )
    assert code == 0
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [row["id"] for row in lines] == ["X01", "X02", "X03", "X04"]
    for row in lines:
        assert row["schema_version"] == 1
        assert set(row) == {"id", "schema_version", "archived_at", "event",
                            "article_text", "claims", "usage"}


def test_sampler_stats_command_prints_bucket_counts(tmp_path, capsys):
    sampler = load_sampler()
    archive_dir = write_archive(tmp_path)
    assert sampler.main(["stats", "--archive-dir", str(archive_dir)]) == 0
    out = capsys.readouterr().out
    assert "ANNOUNCED" in out
    assert "ZERO_CLAIMS" in out
    assert "3" in out  # ANNOUNCED bucket count
