"""Event sampler: scoring and deduplication."""

from zeitgeist.sampler.dedup import RecentKeys
from zeitgeist.sampler.scoring import score

__all__ = ["RecentKeys", "score"]
