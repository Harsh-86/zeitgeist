import io
import zipfile

import httpx

from zeitgeist.gdelt.client import LASTUPDATE_URL, GdeltClient

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
