"""Extraction eval: run the production LlmExtractor over human-labeled golden
articles (evals/golden_extraction.jsonl) and score predicted claims against
expected claims.

Matching is deliberately STRICT: a predicted claim counts only when its
canonicalized (subject, relation, object) equals an expected claim's exactly.
Relation synonymy (ANNOUNCED vs STATED) is counted as a miss BY DESIGN — the
metric's absolute value matters less than its trend, and a fuzzy matcher would
be a second model to distrust. Detail and confidence are ignored for matching.
"""

import logging
from dataclasses import dataclass

from zeitgeist.models import GdeltEvent

logger = logging.getLogger("zeitgeist.evals.extraction")

# A canonicalized (subject, relation, object) triple; object may be None.
Triple = tuple[str, str, str | None]


def canonical_claim(subject: str, relation: str, object: str | None) -> Triple:
    """Canonical form used for matching: uppercase + strip each part.

    A None object stays None (subject-only claims match only subject-only
    expectations)."""
    return (
        subject.strip().upper(),
        relation.strip().upper(),
        object.strip().upper() if object is not None else None,
    )


def _triple(claim: object) -> Triple:
    """Canonical triple from either a dict (golden expected_claims rows) or an
    attribute-style claim (LlmClaim)."""
    if isinstance(claim, dict):
        return canonical_claim(claim["subject"], claim["relation"], claim.get("object"))
    return canonical_claim(claim.subject, claim.relation, claim.object)


@dataclass(frozen=True)
class MatchResult:
    """Greedy 1:1 matching outcome for one item's predicted-vs-expected claims."""

    true_positives: int
    false_positives: int
    false_negatives: int
    matched: tuple[Triple, ...]
    unmatched_predicted: tuple[Triple, ...]
    unmatched_expected: tuple[Triple, ...]


@dataclass(frozen=True)
class ItemResult:
    """One golden row's graded extraction. error is set (and counts zeroed)
    when the row crashed — a bad row fails one item, never the run."""

    id: str
    predicted: tuple[Triple, ...]
    expected: tuple[Triple, ...]
    matched: tuple[Triple, ...]
    unmatched_predicted: tuple[Triple, ...]
    unmatched_expected: tuple[Triple, ...]
    tp: int
    fp: int
    fn: int
    error: str | None = None


def match_claims(predicted: list, expected: list) -> MatchResult:
    """Greedily 1:1-match predicted claims against expected claims on EXACT
    canonical equality (subject, relation, and object must all be equal after
    uppercasing/stripping; None object matches only None).

    1:1 means each expected claim is consumed by at most one predicted claim —
    duplicate predictions cannot double-match a single expectation (the extra
    duplicate is a false positive). Relation synonymy (ANNOUNCED vs STATED) is
    a miss by design; see the module docstring.
    """
    remaining = [_triple(claim) for claim in expected]
    matched: list[Triple] = []
    unmatched_predicted: list[Triple] = []
    for triple in (_triple(claim) for claim in predicted):
        if triple in remaining:
            remaining.remove(triple)
            matched.append(triple)
        else:
            unmatched_predicted.append(triple)
    return MatchResult(
        true_positives=len(matched),
        false_positives=len(unmatched_predicted),
        false_negatives=len(remaining),
        matched=tuple(matched),
        unmatched_predicted=tuple(unmatched_predicted),
        unmatched_expected=tuple(remaining),
    )


def _rates(tp: int, fp: int, fn: int) -> tuple[float | None, float | None, float | None]:
    """(precision, recall, f1) with zero-division reported as None (rendered
    as "—"): 0 predicted → precision undefined; 0 expected → recall undefined;
    f1 undefined unless both are defined."""
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def score_items(items: list[ItemResult]) -> dict:
    """Aggregate item results: overall precision/recall/F1 + a per-relation
    breakdown + counts. Errored items are counted in `errors` and excluded
    from the metrics (their tp/fp/fn are zero anyway).

    Relation attribution: a TP/FP carries its PREDICTED claim's relation, an
    FN its EXPECTED claim's relation."""
    scored = [item for item in items if item.error is None]
    tp = sum(item.tp for item in scored)
    fp = sum(item.fp for item in scored)
    fn = sum(item.fn for item in scored)
    precision, recall, f1 = _rates(tp, fp, fn)

    tallies: dict[str, dict[str, int]] = {}

    def tally(relation: str, key: str) -> None:
        entry = tallies.setdefault(relation, {"tp": 0, "fp": 0, "fn": 0})
        entry[key] += 1

    for item in scored:
        for _, relation, _ in item.matched:
            tally(relation, "tp")
        for _, relation, _ in item.unmatched_predicted:
            tally(relation, "fp")
        for _, relation, _ in item.unmatched_expected:
            tally(relation, "fn")

    per_relation = {}
    for relation in sorted(tallies):
        entry = tallies[relation]
        rel_precision, rel_recall, rel_f1 = _rates(entry["tp"], entry["fp"], entry["fn"])
        per_relation[relation] = {
            **entry,
            "precision": rel_precision,
            "recall": rel_recall,
            "f1": rel_f1,
        }

    return {
        "total_items": len(items),
        "errors": len(items) - len(scored),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "per_relation": per_relation,
    }


def run_extraction(extractor, golden_rows: list[dict]) -> list[ItemResult]:
    """Run the extractor over every golden row and match against its
    expected_claims: rebuild the GdeltEvent from the archived dict (the archive
    stores asdict(event), so field names align with GdeltEvent exactly), call
    extractor.extract(event, article_text), grade with match_claims.

    Per-item crash guard: any exception (malformed row, API blowup) fails that
    ONE item (error set, counts zeroed) and the run survives."""
    results: list[ItemResult] = []
    for row in golden_rows:
        row_id = str(row.get("id", "?"))
        try:
            source = row["source"]
            event = GdeltEvent(**source["event"])
            predicted, _usage = extractor.extract(event, source["article_text"])
            match = match_claims(predicted, row["expected_claims"])
            results.append(
                ItemResult(
                    id=row_id,
                    predicted=tuple(_triple(claim) for claim in predicted),
                    expected=tuple(_triple(claim) for claim in row["expected_claims"]),
                    matched=match.matched,
                    unmatched_predicted=match.unmatched_predicted,
                    unmatched_expected=match.unmatched_expected,
                    tp=match.true_positives,
                    fp=match.false_positives,
                    fn=match.false_negatives,
                )
            )
        except Exception as exc:  # one bad row = one failed item, run survives
            logger.warning("extraction eval item %s failed", row_id, exc_info=True)
            results.append(
                ItemResult(
                    id=row_id,
                    predicted=(),
                    expected=(),
                    matched=(),
                    unmatched_predicted=(),
                    unmatched_expected=(),
                    tp=0,
                    fp=0,
                    fn=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    return results
