"""Shared persistent daily spend budget (sampler and llm-extractor both draw against it)."""

import json
import logging
import math
import os
import tempfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger("zeitgeist.budget")

_LOG_EVERY = 100

# When pacing is on, this fraction of the daily limit is always spendable
# right from midnight so quiet hours never starve a service completely.
_BURST_FLOOR_FRACTION = 0.05

_SECONDS_PER_DAY = 86400


class DailyBudget:
    """Persistent daily counter capping some resource (LLM calls, samples, etc).

    State is a JSON file shaped `{"date": "...", "count": N}`. The counter resets
    whenever the clock reports a date different from the one on disk. `today` is
    injectable so tests can control "now" without mocking the clock module.

    When `now` is provided, spending is additionally *paced* across the day:
    the allowance so far is `min(limit, max(burst_floor, floor(limit * frac)))`
    where `frac` is the fraction of the day elapsed, so a burst at midnight
    can't drain the whole day's budget. Unused allowance accumulates (the
    allowance is cumulative-linear). With `now=None` (the default) behavior is
    exactly the unpaced original. If both `now` and `today` are given, `now`
    wins for date derivation so the two clocks can't disagree.
    """

    def __init__(
        self,
        path: Path,
        limit: int,
        today: Callable[[], date] = date.today,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(path)
        self._limit = limit
        self._today = today
        self._now = now
        self._exhausted_count = 0
        self._paced_denied_count = 0
        self._date, self._count = self._load()

    def _load(self) -> tuple[str | None, int]:
        if not self._path.exists():
            return None, 0
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            logger.exception(
                "budget state file unreadable/corrupt, treating as fresh (path=%s)",
                self._path,
            )
            return None, 0
        return data.get("date"), data.get("count", 0)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"date": self._date, "count": self._count})

        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _allowance(self, now: datetime) -> int:
        """Cumulative spend allowed by this time of day (pacing enabled only).

        Paces against the injected clock's naive local midnight — i.e. the
        container's timezone (UTC in production). A non-UTC host would pace
        (and reset) against its own local midnight.
        """
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        frac = (now - midnight).total_seconds() / _SECONDS_PER_DAY
        burst_floor = max(1, math.ceil(self._limit * _BURST_FLOOR_FRACTION))
        return min(self._limit, max(burst_floor, math.floor(self._limit * frac)))

    def try_spend(self) -> bool:
        """Increment and return True while under today's limit; False once exhausted.

        With pacing enabled (`now` provided), also returns False while today's
        spend has caught up with the clock-proportional allowance.
        """
        now = self._now() if self._now is not None else None
        today_str = now.date().isoformat() if now is not None else self._today().isoformat()
        if today_str != self._date:
            self._date = today_str
            self._count = 0
            self._exhausted_count = 0
            self._paced_denied_count = 0

        if self._count >= self._limit:
            self._exhausted_count += 1
            if self._exhausted_count == 1 or self._exhausted_count % _LOG_EVERY == 0:
                logger.info(
                    "daily budget exhausted (path=%s, limit=%d, date=%s)",
                    self._path,
                    self._limit,
                    self._date,
                )
            return False

        if now is not None:
            allowance = self._allowance(now)
            if self._count >= allowance:
                self._paced_denied_count += 1
                if (
                    self._paced_denied_count == 1
                    or self._paced_denied_count % _LOG_EVERY == 0
                ):
                    logger.info(
                        "paced budget: %d/%d spent, allowance so far today is %d (path=%s)",
                        self._count,
                        self._limit,
                        allowance,
                        self._path,
                    )
                return False

        self._count += 1
        self._save()
        return True
