"""Fire-and-forget JSONL archive of every successful LLM extraction.

Each record captures the full extraction context (event, article text, claims,
usage) — the substrate for the future golden extraction dataset (Phase 4 Task 5).
Archiving must never affect the pipeline: any failure is logged and swallowed.
"""

import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from zeitgeist.llm.extract import LlmClaim
from zeitgeist.models import GdeltEvent

logger = logging.getLogger("zeitgeist.llm.archive")

SCHEMA_VERSION = 1


class ExtractionArchive:
    """Appends one JSON line per extraction to `<dir_path>/<YYYY-MM-DD>.jsonl` (UTC date).

    An empty `dir_path` makes the archive inert: record() returns immediately
    and never touches the filesystem.
    """

    def __init__(self, dir_path: str) -> None:
        self._dir_path = dir_path

    def record(
        self,
        event: GdeltEvent,
        article_text: str,
        llm_claims: list[LlmClaim],
        usage: dict,
    ) -> None:
        """Append one archive line. Fire-and-forget: warns on failure, never raises."""
        if not self._dir_path:
            return
        try:
            now = datetime.now(UTC)
            line = json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "archived_at": now.isoformat(),
                    "event": asdict(event),
                    "article_text": article_text,
                    "claims": [asdict(claim) for claim in llm_claims],
                    "usage": usage,
                }
            )
            directory = Path(self._dir_path)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{now.date().isoformat()}.jsonl"
            # Plain append is enough here: llm-extractor is a single-writer
            # service and archive lines are only read offline, so a torn tail
            # line is tolerable — no need for budget.py's tempfile+rename dance.
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            logger.warning(
                "failed to archive extraction (dir=%s)", self._dir_path, exc_info=True
            )
