"""Event scoring based on relevance signals."""

from zeitgeist.models import GdeltEvent


def score(event: GdeltEvent) -> float:
    """Calculate free-signal relevance score for an event.

    Score range: 0–2.5

    Components:
    - impact (0–1): abs(goldstein or 0) / 10
    - prominence (0–1): min(1.0, num_mentions / 20)
    - richness (0–0.5): 0.5 if both actors present else 0.0

    Args:
        event: A GdeltEvent to score.

    Returns:
        The combined relevance score (0–2.5).
    """
    # Impact: absolute value of goldstein normalized to 0-1
    impact = abs(event.goldstein or 0) / 10.0

    # Prominence: mentions capped at 1.0
    prominence = min(1.0, event.num_mentions / 20.0)

    # Richness: bonus if both actors are present
    richness = 0.5 if (event.actor1_name and event.actor2_name) else 0.0

    return impact + prominence + richness
