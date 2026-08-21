"""HTTP client for the GDELT 2.0 15-minute update feed."""

import io
import zipfile
from collections.abc import Iterator

import httpx

LASTUPDATE_URL = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"


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
