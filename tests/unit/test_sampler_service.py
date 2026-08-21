"""Routing-table tests for the sampler's score/dedup/budget gate."""

from tests.unit.test_models import make_event
from zeitgeist.budget import DailyBudget
from zeitgeist.models import GdeltEvent
from zeitgeist.sampler.dedup import RecentKeys
from zeitgeist.sampler.main import process_message

MIN_SCORE = 1.2


class AlwaysUnderBudget:
    """Fake budget that never blocks — used when a test isn't exercising budget logic."""

    def try_spend(self) -> bool:
        return True


class AlwaysExhausted:
    """Fake budget that always reports exhausted."""

    def try_spend(self) -> bool:
        return False


def high_scoring_event(**overrides) -> GdeltEvent:
    # goldstein -9 -> impact 0.9, num_mentions 40 -> prominence 1.0, both actors -> +0.5 => 2.4
    base = {"goldstein": -9.0, "num_mentions": 40}
    base.update(overrides)
    return make_event(**base)


def test_undecodable_message_returns_none(caplog):
    result = process_message(
        b"not json", RecentKeys(), AlwaysUnderBudget(), min_score=MIN_SCORE
    )
    assert result is None


def test_low_score_event_returns_none():
    event = make_event(goldstein=0.0, num_mentions=0, actor2_name=None)  # score 0.0
    result = process_message(
        event.to_json(), RecentKeys(), AlwaysUnderBudget(), min_score=MIN_SCORE
    )
    assert result is None


def test_duplicate_source_url_returns_none():
    recent = RecentKeys()
    event1 = high_scoring_event(event_id="1", source_url="https://example.com/dup")
    event2 = high_scoring_event(event_id="2", source_url="https://example.com/dup")

    first = process_message(event1.to_json(), recent, AlwaysUnderBudget(), min_score=MIN_SCORE)
    second = process_message(event2.to_json(), recent, AlwaysUnderBudget(), min_score=MIN_SCORE)

    assert first == event1
    assert second is None


def test_duplicate_actor_pair_returns_none():
    recent = RecentKeys()
    event1 = high_scoring_event(
        event_id="1",
        actor1_name="UNITED STATES",
        actor2_name="EUROPEAN CENTRAL BANK",
        event_root_code="04",
        source_url="https://example.com/a",
    )
    event2 = high_scoring_event(
        event_id="2",
        actor1_name="UNITED STATES",
        actor2_name="EUROPEAN CENTRAL BANK",
        event_root_code="04",
        source_url="https://example.com/b",
    )

    first = process_message(event1.to_json(), recent, AlwaysUnderBudget(), min_score=MIN_SCORE)
    second = process_message(event2.to_json(), recent, AlwaysUnderBudget(), min_score=MIN_SCORE)

    assert first == event1
    assert second is None


def test_budget_exhausted_returns_none():
    event = high_scoring_event()
    result = process_message(
        event.to_json(), RecentKeys(), AlwaysExhausted(), min_score=MIN_SCORE
    )
    assert result is None


def test_happy_path_returns_event():
    event = high_scoring_event()
    result = process_message(
        event.to_json(), RecentKeys(), AlwaysUnderBudget(), min_score=MIN_SCORE
    )
    assert result == event


def test_real_budget_integrates_with_process_message(tmp_path):
    from datetime import date

    budget = DailyBudget(tmp_path / "budget.json", limit=1, today=lambda: date(2026, 1, 1))
    event1 = high_scoring_event(event_id="1", source_url="https://example.com/x")
    event2 = high_scoring_event(event_id="2", source_url="https://example.com/y")

    first = process_message(event1.to_json(), RecentKeys(), budget, min_score=MIN_SCORE)
    second = process_message(event2.to_json(), RecentKeys(), budget, min_score=MIN_SCORE)

    assert first == event1
    assert second is None
