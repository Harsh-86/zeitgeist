import httpx

from zeitgeist.articles.fetch import fetch_article_text

# Fixture HTML with realistic page structure: nav, article, footer
FIXTURE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Test Article</title>
</head>
<body>
<nav>
    <a href="/">Home</a>
    <a href="/about">About</a>
    <a href="/contact">Contact</a>
</nav>

<article>
    <h1>Breaking News: Scientists Discover New Particle</h1>
    <p>This is the beginning of the article body with important information.</p>
    <p>The researchers conducted experiments over three months and found remarkable results.</p>
    <p>This discovery could revolutionize the field of physics for decades to come.</p>
</article>

<footer>
    <p>Copyright 2026</p>
    <p>Privacy Policy</p>
</footer>
</body>
</html>
"""


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_article_text_happy_path():
    """Happy path: extract article text from HTML, exclude nav/footer."""
    def handler(request):
        assert request.url == "https://example.com/article"
        return httpx.Response(200, text=FIXTURE_HTML, headers={"content-type": "text/html"})

    text = fetch_article_text(make_client(handler), "https://example.com/article")

    # Should contain article text
    assert text is not None
    assert "Breaking News" in text
    assert "Scientists Discover New Particle" in text
    assert "researchers conducted experiments" in text or "experiments" in text

    # Should NOT contain nav or footer text
    assert "Home" not in text
    assert "About" not in text
    assert "Copyright" not in text


def test_fetch_article_text_returns_none_on_404():
    """404 error → None."""
    def handler(request):
        return httpx.Response(404)

    result = fetch_article_text(make_client(handler), "https://example.com/missing")
    assert result is None


def test_fetch_article_text_returns_none_on_timeout():
    """Timeout exception → None."""
    def handler(request):
        raise httpx.TimeoutException("timeout")

    result = fetch_article_text(make_client(handler), "https://example.com/article")
    assert result is None


def test_fetch_article_text_returns_none_on_oversized_body():
    """Body exceeds max_bytes → None."""
    def handler(request):
        # Return a body larger than max_bytes (1000 in test)
        return httpx.Response(200, text="x" * 1001, headers={"content-type": "text/html"})

    result = fetch_article_text(make_client(handler), "https://example.com/article", max_bytes=1000)
    assert result is None


def test_fetch_article_text_returns_none_on_non_html_content_type():
    """Non-HTML content-type → None."""
    def handler(request):
        return httpx.Response(200, content=b"\x89PNG...", headers={"content-type": "image/png"})

    result = fetch_article_text(make_client(handler), "https://example.com/image.png")
    assert result is None


def test_fetch_article_text_accepts_html_variants():
    """Accept various HTML content-type headers (case-insensitive, with charset)."""
    test_cases = [
        "text/html",
        "text/html; charset=utf-8",
        "TEXT/HTML",
        "text/html; charset=UTF-8",
    ]

    for content_type in test_cases:

        def handler(request, ct=content_type):
            return httpx.Response(200, text=FIXTURE_HTML, headers={"content-type": ct})

        result = fetch_article_text(make_client(handler), "https://example.com/article")
        assert result is not None, f"Failed for content-type: {content_type}"


def test_fetch_article_text_truncates_at_max_chars():
    """Text exceeding max_chars is truncated."""
    html_with_long_text = FIXTURE_HTML + "<p>" + ("word " * 100) + "</p>"

    def handler(request):
        return httpx.Response(200, text=html_with_long_text, headers={"content-type": "text/html"})

    result = fetch_article_text(make_client(handler), "https://example.com/article", max_chars=100)
    assert result is not None
    assert len(result) <= 100


def test_fetch_article_text_returns_none_on_extraction_failure():
    """trafilatura returns None for non-extractable content → None."""
    # Empty HTML with no article content
    def handler(request):
        empty_html = "<html><body></body></html>"
        return httpx.Response(200, text=empty_html, headers={"content-type": "text/html"})

    result = fetch_article_text(make_client(handler), "https://example.com/article")
    # Empty/no content extraction should return None
    assert result is None


def test_fetch_article_text_uses_20s_timeout():
    """Verify 20s timeout is set in request."""
    timeout_used = None

    def handler(request):
        nonlocal timeout_used
        timeout_used = request.extensions.get("timeout", None)
        return httpx.Response(
            200, text=FIXTURE_HTML, headers={"content-type": "text/html"}
        )

    fetch_article_text(make_client(handler), "https://example.com/article")
    # Assert timeout was passed; httpx represents it as a dict or Timeout object
    assert timeout_used is not None
    # Timeout can be a dict with keys like "connect", "read", "write", "pool"
    # or a Timeout object; both should contain/equal 20.0 for the overall timeout
    if isinstance(timeout_used, dict):
        # Dict representation: assert at least one timeout value is 20.0
        assert any(v == 20.0 for v in timeout_used.values() if isinstance(v, (int, float)))
    else:
        # Timeout object: check the timeout equals 20
        assert float(timeout_used) == 20.0


def test_fetch_article_text_sets_user_agent():
    """Verify desktop User-Agent is set."""
    user_agent_set = None

    def handler(request):
        nonlocal user_agent_set
        user_agent_set = request.headers.get("user-agent", "")
        return httpx.Response(200, text=FIXTURE_HTML, headers={"content-type": "text/html"})

    fetch_article_text(make_client(handler), "https://example.com/article")
    assert user_agent_set is not None
    assert len(user_agent_set) > 0
    # Desktop user agent should not contain "Mobile"
    assert "Chrome" in user_agent_set or "Mozilla" in user_agent_set


def test_fetch_article_text_returns_none_when_extraction_raises():
    """trafilatura.extract raising an exception → None (never raises)."""
    import trafilatura

    def handler(request):
        return httpx.Response(200, text=FIXTURE_HTML, headers={"content-type": "text/html"})

    # Monkeypatch trafilatura.extract to raise RecursionError
    original_extract = trafilatura.extract

    def raising_extract(html):
        raise RecursionError("deeply nested HTML")

    trafilatura.extract = raising_extract
    try:
        result = fetch_article_text(make_client(handler), "https://example.com/article")
        assert result is None
    finally:
        trafilatura.extract = original_extract
