"""Parser for GDELT 2.0 event export rows (61 tab-separated columns)."""

from datetime import UTC, datetime

from zeitgeist.models import GdeltEvent

NUM_FIELDS = 61


def _or_none(value: str) -> str | None:
    return value if value else None


def _float_or_none(value: str) -> float | None:
    return float(value) if value else None


def _yyyymmdd_to_iso(value: str) -> str:
    dt = datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%d")


def _timestamp_to_iso(value: str) -> str:
    dt = datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_event_row(row: str) -> GdeltEvent | None:
    """Parse one export line; return None on any malformed input."""
    fields = row.rstrip("\n").split("\t")
    if len(fields) != NUM_FIELDS:
        return None
    try:
        return GdeltEvent(
            event_id=fields[0],
            occurred_on=_yyyymmdd_to_iso(fields[1]),
            actor1_code=_or_none(fields[5]),
            actor1_name=_or_none(fields[6]),
            actor2_code=_or_none(fields[15]),
            actor2_name=_or_none(fields[16]),
            event_code=fields[26],
            event_root_code=fields[28],
            quad_class=int(fields[29]),
            goldstein=_float_or_none(fields[30]),
            num_mentions=int(fields[31] or 0),
            avg_tone=_float_or_none(fields[34]),
            geo_name=_or_none(fields[52]),
            geo_lat=_float_or_none(fields[56]),
            geo_lon=_float_or_none(fields[57]),
            observed_at=_timestamp_to_iso(fields[59]),
            source_url=_or_none(fields[60]),
        )
    except ValueError:
        return None
