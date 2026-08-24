"""Fetch and extract article text from URLs."""

import httpx
import trafilatura


def fetch_article_text(
    http: httpx.Client,
    url: str,
    max_bytes: int = 2_000_000,
    max_chars: int = 12_000,
) -> str | None:
    """Fetch HTML from a URL and extract article text.

    GET with 20s timeout and desktop browser User-Agent.

    Args:
        http: httpx.Client instance.
        url: Article URL to fetch.
        max_bytes: Maximum response body size in bytes. Default 2MB.
        max_chars: Maximum extracted text length in characters. Default 12K.

    Returns:
        Extracted article text truncated to max_chars, or None on any error:
        - HTTP error (status code)
        - Non-HTML content-type
        - Response body exceeds max_bytes
        - trafilatura extraction fails (returns None/empty string)
    """
    # Desktop browser User-Agent
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    try:
        response = http.get(
            url,
            timeout=20,
            headers={"User-Agent": user_agent},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return None

    # Check content-type (case-insensitive, accept anything containing "html")
    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type:
        return None

    # Check response body size
    if len(response.content) > max_bytes:
        return None

    # Extract article text; wrap in try/except to catch parser exceptions
    try:
        text = trafilatura.extract(response.text)
    except Exception:  # noqa: BLE001
        return None

    # Return None if extraction failed
    if not text:
        return None

    # Truncate to max_chars
    return text[:max_chars]
