"""Unit tests for the pure retrieval graders (no LLM, no containers)."""

from zeitgeist.evals.graders import (
    KNOWN_EXPECT_KEYS,
    GradeResult,
    grade_retrieval,
    record_mentions,
)

# --- record_mentions --------------------------------------------------------


def test_record_mentions_exact_value():
    assert record_mentions({"s.name": "GERMANY"}, "GERMANY")


def test_record_mentions_case_insensitive_both_ways():
    assert record_mentions({"s.name": "germany"}, "GERMANY")
    assert record_mentions({"s.name": "GERMANY"}, "germany")


def test_record_mentions_substring_inside_value():
    assert record_mentions({"detail": "On 2026-08-27, GERMANY met with FRANCE."}, "FRANCE")


def test_record_mentions_stringifies_non_str_values():
    assert record_mentions({"count": 42}, "42")


def test_record_mentions_none_value_no_crash_no_match():
    assert not record_mentions({"o.name": None}, "GERMANY")


def test_record_mentions_no_match():
    assert not record_mentions({"s.name": "FRANCE", "relation": "MET_WITH"}, "GERMANY")


def test_record_mentions_checks_values_not_keys():
    assert not record_mentions({"GERMANY": "FRANCE"}, "GERMANY")


# --- grade_retrieval: shape -------------------------------------------------


def test_grade_result_is_frozen_with_tuple_failures():
    result = grade_retrieval([], {})
    assert isinstance(result, GradeResult)
    assert result.passed is True
    assert result.failures == ()


def test_known_expect_keys_cover_the_schema():
    assert KNOWN_EXPECT_KEYS == frozenset(
        {
            "min_records",
            "max_records",
            "all_records_mention",
            "any_record_mentions",
            "count_equals",
            "forbid_empty",
        }
    )


# --- min_records / max_records ----------------------------------------------


def test_min_records_pass():
    assert grade_retrieval([{"a": 1}, {"a": 2}], {"min_records": 2}).passed


def test_min_records_fail():
    result = grade_retrieval([{"a": 1}], {"min_records": 3})
    assert not result.passed
    assert any("min_records" in f for f in result.failures)


def test_min_records_fail_on_empty():
    result = grade_retrieval([], {"min_records": 1})
    assert not result.passed


def test_max_records_pass():
    assert grade_retrieval([{"a": 1}], {"max_records": 1}).passed


def test_max_records_fail():
    result = grade_retrieval([{"a": 1}, {"a": 2}], {"max_records": 1})
    assert not result.passed
    assert any("max_records" in f for f in result.failures)


def test_max_records_zero_requires_empty():
    assert grade_retrieval([], {"max_records": 0}).passed
    assert not grade_retrieval([{"a": 1}], {"max_records": 0}).passed


# --- all_records_mention ------------------------------------------------------


def test_all_records_mention_pass():
    records = [
        {"s.name": "GERMANY", "o.name": "FRANCE"},
        {"detail": "germany signed an agreement"},
    ]
    assert grade_retrieval(records, {"all_records_mention": "GERMANY"}).passed


def test_all_records_mention_latest_25_of_everything_must_fail():
    # Simulates production bug #2: WHERE bound to an OPTIONAL MATCH no-ops the
    # filter and the query returns the latest 25 events of the whole graph.
    records = [{"s.name": name, "relation": "MET_WITH"} for name in ["UNITED STATES"] * 20] + [
        {"s.name": "RUSSIA"},
        {"s.name": "CHINA"},
        {"s.name": "JAPAN"},
        {"s.name": "GERMANY"},  # one genuine hit must not rescue the batch
        {"s.name": "BRAZIL"},
    ]
    result = grade_retrieval(records, {"all_records_mention": "GERMANY"})
    assert not result.passed
    assert any("GERMANY" in f for f in result.failures)


def test_all_records_mention_vacuously_passes_on_empty_records():
    # Pair with min_records/forbid_empty in golden data to reject empty results.
    assert grade_retrieval([], {"all_records_mention": "GERMANY"}).passed


# --- any_record_mentions -------------------------------------------------------


def test_any_record_mentions_pass_when_each_name_found_somewhere():
    records = [{"s.name": "GERMANY"}, {"o.name": "FRANCE"}]
    assert grade_retrieval(records, {"any_record_mentions": ["GERMANY", "FRANCE"]}).passed


def test_any_record_mentions_fails_when_one_name_missing():
    records = [{"s.name": "GERMANY"}]
    result = grade_retrieval(records, {"any_record_mentions": ["GERMANY", "FRANCE"]})
    assert not result.passed
    assert any("FRANCE" in f for f in result.failures)


def test_any_record_mentions_fails_on_empty_records():
    result = grade_retrieval([], {"any_record_mentions": ["GERMANY"]})
    assert not result.passed


# --- forbid_empty -------------------------------------------------------------


def test_forbid_empty_true_fails_on_empty():
    result = grade_retrieval([], {"forbid_empty": True})
    assert not result.passed
    assert any("empty" in f.lower() for f in result.failures)


def test_forbid_empty_true_passes_when_records_exist():
    assert grade_retrieval([{"a": 1}], {"forbid_empty": True}).passed


def test_forbid_empty_false_passes_on_empty():
    assert grade_retrieval([], {"forbid_empty": False}).passed


# --- unknown keys (typo guard) -------------------------------------------------


def test_unknown_expect_key_fails():
    result = grade_retrieval([{"a": 1}], {"min_record": 1})  # typo: min_record
    assert not result.passed
    assert any("min_record" in f and "unknown" in f.lower() for f in result.failures)


def test_multiple_failures_accumulate():
    records = [{"s.name": "FRANCE"}]
    result = grade_retrieval(
        records, {"min_records": 2, "all_records_mention": "GERMANY", "bogus_key": 1}
    )
    assert not result.passed
    assert len(result.failures) == 3


# ---- count_equals ----


def test_count_equals_passes_on_exact_aggregate_row():
    result = grade_retrieval([{"n": 6}], {"count_equals": 6})
    assert result.passed


def test_count_equals_passes_on_string_aggregate_field():
    result = grade_retrieval([{"count": "6"}], {"count_equals": 6})
    assert result.passed


def test_count_equals_rejects_digit_superstring():
    # The whole point of exact matching: 16 must NOT pass for 6.
    result = grade_retrieval([{"n": 16}], {"count_equals": 6})
    assert not result.passed
    assert any("count_equals" in f for f in result.failures)


def test_count_equals_passes_on_exactly_n_raw_rows():
    rows = [{"subject": "BRAZIL", "relation": "MET_WITH"} for _ in range(6)]
    assert grade_retrieval(rows, {"count_equals": 6}).passed


def test_count_equals_rejects_wrong_raw_row_count():
    rows = [{"subject": "BRAZIL"} for _ in range(5)]
    assert not grade_retrieval(rows, {"count_equals": 6}).passed


def test_count_equals_rejects_empty_records():
    assert not grade_retrieval([], {"count_equals": 6}).passed


def test_count_equals_multiple_rows_do_not_count_as_aggregate():
    # Two rows where one happens to contain the number is not an aggregate answer.
    rows = [{"n": 6}, {"n": 99}]
    assert not grade_retrieval(rows, {"count_equals": 6}).passed
