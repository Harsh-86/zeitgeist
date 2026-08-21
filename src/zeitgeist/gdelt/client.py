"""HTTP client for the GDELT 2.0 15-minute update feed."""

import io
import re
import zipfile
from collections.abc import Iterator
from datetime import datetime, timedelta

import httpx

LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
EXPORT_URL_TEMPLATE = "https://data.gdeltproject.org/gdeltv2/{stamp}.export.CSV.zip"

_STAMP_FORMAT = "%Y%m%d%H%M%S"
_STAMP_RE = re.compile(r"(\d{14})\.export\.CSV\.zip")
_WINDOW = timedelta(minutes=15)
MAX_BACKFILL_WINDOWS = 672  # 7 days of 15-minute windows


def stamp_from_url(url: str) -> str | None:
    """Extract the 14-digit GDELT window stamp from an export URL, else None."""
    match = _STAMP_RE.search(url)
    return match.group(1) if match else None


def export_url_for(stamp: str) -> str:
    return EXPORT_URL_TEMPLATE.format(stamp=stamp)


def stamps_between(last: str, latest: str) -> list[str]:
    """All 15-minute stamps AFTER last, up to and including latest, in order.

    Empty list when latest <= last. Capped at MAX_BACKFILL_WINDOWS (7 days),
    keeping the newest windows when the gap is larger.
    """
    # GDELT stamps are naive UTC timestamps with no timezone component to parse.
    last_dt = datetime.strptime(last, _STAMP_FORMAT)  # noqa: DTZ007
    latest_dt = datetime.strptime(latest, _STAMP_FORMAT)  # noqa: DTZ007
    if latest_dt <= last_dt:
        return []
    stamps = []
    current = last_dt + _WINDOW
    while current <= latest_dt:
        stamps.append(current.strftime(_STAMP_FORMAT))
        current += _WINDOW
    if len(stamps) > MAX_BACKFILL_WINDOWS:
        stamps = stamps[-MAX_BACKFILL_WINDOWS:]
    return stamps


class GdeltClient:
    def __init__(self, http: httpx.Client) -> None:
        self._http = http

    def latest_export_url(self) -> str | None:
        try:
            response = self._http.get(LASTUPDATE_URL, timeout=30)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        for line in response.text.splitlines():
            parts = line.split()
            if parts and parts[-1].endswith(".export.CSV.zip"):
                return parts[-1]
        return None

    def fetch_rows(self, url: str) -> Iterator[str]:
        response = self._http.get(url, timeout=120)
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            name = archive.namelist()[0]
            with archive.open(name) as member:
                for raw_line in io.TextIOWrapper(member, encoding="utf-8", errors="replace"):
                    line = raw_line.rstrip("\n")
                    if line:
                        yield line
