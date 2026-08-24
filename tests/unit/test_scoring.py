"""Tests for event scoring."""

from tests.unit.test_models import make_event
from zeitgeist.sampler.scoring import score


def test_score_all_components_present():
    """Test scoring with goldstein=-9, mentions=40, both actors present."""
    event = make_event(goldstein=-9.0, num_mentions=40)
    result = score(event)
    # impact: abs(-9) / 10 = 0.9
    # prominence: min(1.0, 40 / 20) = 1.0
    # richness: 0.5 (both actors present)
    # total: 0.9 + 1.0 + 0.5 = 2.4
    assert result == 2.4


def test_score_missing_goldstein():
    """Test scoring with missing goldstein (None) means impact = 0."""
    event = make_event(goldstein=None, num_mentions=12)
    result = score(event)
    # impact: abs(0) / 10 = 0.0
    # prominence: min(1.0, 12 / 20) = 0.6
    # richness: 0.5 (both actors present)
    # total: 0.0 + 0.6 + 0.5 = 1.1
    assert result == 1.1


def test_score_single_actor_no_richness():
    """Test scoring with single actor (actor2_name=None) means no richness bonus."""
    event = make_event(actor2_name=None, goldstein=1.9, num_mentions=12)
    result = score(event)
    # impact: abs(1.9) / 10 = 0.19
    # prominence: min(1.0, 12 / 20) = 0.6
    # richness: 0.0 (only one actor)
    # total: 0.19 + 0.6 + 0.0 = 0.79
    assert result == 0.79


def test_score_high_mentions_capped():
    """Test that prominence is capped at 1.0."""
    event = make_event(goldstein=2.0, num_mentions=100)
    result = score(event)
    # impact: abs(2.0) / 10 = 0.2
    # prominence: min(1.0, 100 / 20) = 1.0
    # richness: 0.5 (both actors present)
    # total: 0.2 + 1.0 + 0.5 = 1.7
    assert result == 1.7


def test_score_zero_goldstein():
    """Test scoring with goldstein=0."""
    event = make_event(goldstein=0.0, num_mentions=0)
    result = score(event)
    # impact: abs(0) / 10 = 0.0
    # prominence: min(1.0, 0 / 20) = 0.0
    # richness: 0.5 (both actors present)
    # total: 0.0 + 0.0 + 0.5 = 0.5
    assert result == 0.5


def test_score_negative_goldstein():
    """Test that absolute value is used for goldstein impact."""
    event = make_event(goldstein=-5.0, num_mentions=10)
    result = score(event)
    # impact: abs(-5.0) / 10 = 0.5
    # prominence: min(1.0, 10 / 20) = 0.5
    # richness: 0.5 (both actors present)
    # total: 0.5 + 0.5 + 0.5 = 1.5
    assert result == 1.5
