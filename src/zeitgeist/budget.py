"""Shared persistent daily spend budget (sampler and llm-extractor both draw against it)."""

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

logger = logging.getLogger("zeitgeist.budget")

_LOG_EVERY = 100


class DailyBudget:
    """Persistent daily counter capping some resource (LLM calls, samples, etc).

    State is a JSON file shaped `{"date": "...", "count": N}`. The counter resets
    whenever `today()` reports a date different from the one on disk. `today` is
    injectable so tests can control "now" without mocking the clock module.
    """

    def __init__(
        self,
        path: Path,
        limit: int,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._path = Path(path)
        self._limit = limit
        self._today = today
        self._exhausted_count = 0
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

    def try_spend(self) -> bool:
        """Increment and return True while under today's limit; False once exhausted."""
        today_str = self._today().isoformat()
        if today_str != self._date:
            self._date = today_str
            self._count = 0
            self._exhausted_count = 0

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

        self._count += 1
        self._save()
        return True
