"""Pure, deterministic graders for retrieval eval results. No LLM involved.

Grading happens ONLY over executed query results (records), never by
string-matching the generated Cypher — queries may be legitimately shaped
many ways; what matters is what they return.
"""

from dataclasses import dataclass

# The full expectation schema golden_questions.jsonl rows may use. Anything
# else is treated as a failure (typo guard), so a misspelled key can never
# silently grade as a pass.
KNOWN_EXPECT_KEYS = frozenset(
    {
        "min_records",
        "max_records",
        "all_records_mention",
        "any_record_mentions",
        "count_equals",
        "forbid_empty",
    }
)


@dataclass(frozen=True)
class GradeResult:
    passed: bool
    failures: tuple[str, ...]


def record_mentions(record: dict, name: str) -> bool:
    """True when any value of the record contains `name`, case-insensitively.

    Values are stringified first, so numbers, dates, and Neo4j temporal types
    all participate; keys are not searched.
    """
    needle = name.lower()
    return any(needle in str(value).lower() for value in record.values())


def grade_retrieval(records: list[dict], expect: dict) -> GradeResult:
    """Grade executed query records against one golden expectation dict.

    Supported keys (see KNOWN_EXPECT_KEYS):
      - min_records: at least this many rows came back
      - max_records: at most this many rows came back
      - all_records_mention: EVERY record mentions this name in some value
        (the null-filter / geo-tangent catcher); vacuously true on zero
        records — pair with min_records or forbid_empty
      - any_record_mentions: for EACH listed name, at least one record
        mentions it
      - count_equals: the answer to a "how many" question must equal this
        integer EXACTLY, via either legitimate shape: one aggregate row where
        some field's whole value equals the number (16 does NOT pass for 6),
        or exactly that many raw rows returned
      - forbid_empty: when true, zero records is a failure

    Unknown keys fail the question outright.
    """
    failures: list[str] = []

    for key in sorted(set(expect) - KNOWN_EXPECT_KEYS):
        failures.append(f"unknown expectation key: {key!r}")

    if "min_records" in expect and len(records) < expect["min_records"]:
        failures.append(
            f"min_records: expected at least {expect['min_records']} records, "
            f"got {len(records)}"
        )

    if "max_records" in expect and len(records) > expect["max_records"]:
        failures.append(
            f"max_records: expected at most {expect['max_records']} records, "
            f"got {len(records)}"
        )

    if "all_records_mention" in expect:
        name = expect["all_records_mention"]
        misses = [i for i, record in enumerate(records) if not record_mentions(record, name)]
        if misses:
            failures.append(
                f"all_records_mention: {len(misses)}/{len(records)} records do not "
                f"mention {name!r} (first offender: record {misses[0]})"
            )

    if "any_record_mentions" in expect:
        for name in expect["any_record_mentions"]:
            if not any(record_mentions(record, name) for record in records):
                failures.append(f"any_record_mentions: no record mentions {name!r}")

    if "count_equals" in expect:
        expected_n = expect["count_equals"]
        aggregate_hit = len(records) == 1 and any(
            str(value).strip() == str(expected_n) for value in records[0].values()
        )
        raw_rows_hit = len(records) == expected_n and expected_n > 0
        if not (aggregate_hit or raw_rows_hit):
            failures.append(
                f"count_equals: expected an aggregate field exactly equal to "
                f"{expected_n} or exactly {expected_n} raw rows, got "
                f"{len(records)} record(s)"
            )

    if expect.get("forbid_empty") and not records:
        failures.append("forbid_empty: query returned an empty result")

    return GradeResult(passed=not failures, failures=tuple(failures))
