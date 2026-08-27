"""Tests for the fire-and-forget extraction-input archiver (ExtractionArchive)."""

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

from tests.unit.test_models import make_event
from zeitgeist.llm import archive as archive_module
from zeitgeist.llm.archive import ExtractionArchive
from zeitgeist.llm.extract import LlmClaim

TWO_CLAIMS = [
    LlmClaim(
        subject="EUROPEAN CENTRAL BANK",
        relation="RAISED_INTEREST_RATES",
        object=None,
        detail="The ECB raised rates by 25 basis points on Thursday.",
        confidence=1.0,
    ),
    LlmClaim(
        subject="UNITED STATES",
        relation="CRITICIZED",
        object="EUROPEAN CENTRAL BANK",
        detail="A US official called the hike premature.",
        confidence=0.7,
    ),
]
USAGE = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 90}

FROZEN_NOW = datetime(2026, 3, 1, 23, 30, 45, tzinfo=UTC)


class _FrozenDatetime:
    """Stands in for archive.datetime so the daily filename is deterministic."""

    @classmethod
    def now(cls, tz=None):
        assert tz is UTC, "archive must ask for UTC time explicitly"
        return FROZEN_NOW


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


# ---- happy path ----


def test_record_writes_one_json_line_with_all_fields(tmp_path):
    event = make_event()
    archive = ExtractionArchive(str(tmp_path / "archive"))

    archive.record(event, "some article text", TWO_CLAIMS, USAGE)

    files = list((tmp_path / "archive").glob("*.jsonl"))
    assert len(files) == 1
    lines = _read_lines(files[0])
    assert len(lines) == 1
    row = lines[0]
    assert set(row.keys()) == {
        "schema_version",
        "archived_at",
        "event",
        "article_text",
        "claims",
        "usage",
    }
    assert row["schema_version"] == 1
    # archived_at is ISO-8601 UTC and parseable back to an aware datetime.
    archived_at = datetime.fromisoformat(row["archived_at"])
    assert archived_at.utcoffset() is not None
    assert row["event"] == dataclasses.asdict(event)
    assert row["article_text"] == "some article text"
    assert row["claims"] == [dataclasses.asdict(claim) for claim in TWO_CLAIMS]
    assert row["usage"] == USAGE


def test_two_records_append_two_lines_to_the_same_daily_file(tmp_path):
    event = make_event()
    archive = ExtractionArchive(str(tmp_path))

    archive.record(event, "first", TWO_CLAIMS, USAGE)
    archive.record(event, "second", [], {})

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = _read_lines(files[0])
    assert len(lines) == 2
    assert lines[0]["article_text"] == "first"
    assert lines[1]["article_text"] == "second"
    assert lines[1]["claims"] == []


def test_filename_is_utc_date_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_module, "datetime", _FrozenDatetime)
    archive = ExtractionArchive(str(tmp_path))

    archive.record(make_event(), "text", TWO_CLAIMS, USAGE)

    assert (tmp_path / "2026-03-01.jsonl").exists()
    row = _read_lines(tmp_path / "2026-03-01.jsonl")[0]
    assert row["archived_at"] == FROZEN_NOW.isoformat()


def test_directory_is_auto_created_including_parents(tmp_path):
    target = tmp_path / "does" / "not" / "exist" / "yet"
    archive = ExtractionArchive(str(target))

    archive.record(make_event(), "text", TWO_CLAIMS, USAGE)

    assert target.is_dir()
    assert len(list(target.glob("*.jsonl"))) == 1


# ---- inert mode ----


def test_empty_dir_path_is_inert_and_touches_no_filesystem(tmp_path, monkeypatch, caplog):
    def _explode(*args, **kwargs):
        raise AssertionError("inert archive must not touch the filesystem")

    # Any fs access would go through Path.mkdir or Path.open; both are booby-trapped.
    monkeypatch.setattr(Path, "mkdir", _explode)
    monkeypatch.setattr(Path, "open", _explode)
    archive = ExtractionArchive("")

    with caplog.at_level("WARNING"):
        archive.record(make_event(), "text", TWO_CLAIMS, USAGE)

    # No warning either: record() must return before its try block, not swallow a trap.
    assert caplog.records == []
    assert list(tmp_path.iterdir()) == []


def test_inert_archive_creates_nothing_under_a_nonexistent_parent(tmp_path):
    missing_parent = tmp_path / "no-such-parent"
    archive = ExtractionArchive("")

    archive.record(make_event(), "text", TWO_CLAIMS, USAGE)

    assert not missing_parent.exists()
    assert list(tmp_path.iterdir()) == []


# ---- fire-and-forget ----


def test_write_failure_logs_warning_and_does_not_raise(tmp_path, monkeypatch, caplog):
    archive = ExtractionArchive(str(tmp_path))

    def _explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", _explode)

    with caplog.at_level("WARNING"):
        archive.record(make_event(), "text", TWO_CLAIMS, USAGE)  # must not raise

    assert any(record.levelname == "WARNING" for record in caplog.records)
    assert list(tmp_path.glob("*.jsonl")) == []
