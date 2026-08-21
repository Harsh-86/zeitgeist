import io
import zipfile
from datetime import datetime, timedelta

import httpx

from zeitgeist.gdelt.client import (
    LASTUPDATE_URL,
    GdeltClient,
    export_url_for,
    stamp_from_url,
    stamps_between,
)

EXPORT_URL = "http://data.gdeltproject.org/gdeltv2/20260818143000.export.CSV.zip"

LASTUPDATE_BODY = (
    f"120000 abc123 {EXPORT_URL}\n"
    "45000 def456 http://data.gdeltproject.org/gdeltv2/20260818143000.mentions.CSV.zip\n"
    "900000 ghi789 http://data.gdeltproject.org/gdeltv2/20260818143000.gkg.csv.zip\n"
)


def make_zip(content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("20260818143000.export.CSV", content)
    return buf.getvalue()


def make_client(handler) -> GdeltClient:
    return GdeltClient(httpx.Client(transport=httpx.MockTransport(handler)))


def test_latest_export_url_picks_export_line():
    def handler(request):
        assert str(request.url) == LASTUPDATE_URL
        return httpx.Response(200, text=LASTUPDATE_BODY)

    assert make_client(handler).latest_export_url() == EXPORT_URL


def test_latest_export_url_returns_none_on_http_error():
    def handler(request):
        return httpx.Response(503)

    assert make_client(handler).latest_export_url() is None


def test_latest_export_url_returns_none_on_malformed_body():
    def handler(request):
        return httpx.Response(200, text="no urls here\n")

    assert make_client(handler).latest_export_url() is None


def test_fetch_rows_yields_lines_from_zip():
    def handler(request):
        return httpx.Response(200, content=make_zip("row1\tcol\nrow2\tcol\n"))

    rows = list(make_client(handler).fetch_rows(EXPORT_URL))
    assert rows == ["row1\tcol", "row2\tcol"]


def test_stamp_from_url_extracts_the_14_digit_stamp():
    assert stamp_from_url(EXPORT_URL) == "20260818143000"


def test_stamp_from_url_returns_none_for_garbage():
    assert stamp_from_url("not a url at all") is None
    assert stamp_from_url("https://data.gdeltproject.org/gdeltv2/lastupdate.txt") is None


def test_export_url_for_round_trips_with_stamp_from_url():
    stamp = "20260818143000"
    url = export_url_for(stamp)
    assert url == "https://data.gdeltproject.org/gdeltv2/20260818143000.export.CSV.zip"
    assert stamp_from_url(url) == stamp


def test_stamps_between_normal_gap():
    assert stamps_between("20260818140000", "20260818150000") == [
        "20260818141500",
        "20260818143000",
        "20260818144500",
        "20260818150000",
    ]


def test_stamps_between_empty_when_latest_equal_to_last():
    assert stamps_between("20260818140000", "20260818140000") == []


def test_stamps_between_empty_when_latest_before_last():
    assert stamps_between("20260818150000", "20260818140000") == []


def test_stamps_between_caps_at_672_keeping_newest():
    last = "20260101000000"
    last_dt = datetime.strptime(last, "%Y%m%d%H%M%S")  # noqa: DTZ007
    latest_dt = last_dt + timedelta(minutes=15 * 1000)  # far beyond the 7-day cap
    latest = latest_dt.strftime("%Y%m%d%H%M%S")

    result = stamps_between(last, latest)

    assert len(result) == 672
    assert result[-1] == latest
    expected_first = (latest_dt - timedelta(minutes=15 * 671)).strftime("%Y%m%d%H%M%S")
    assert result[0] == expected_first
