"""Unit tests for the local scraper. No live network calls: HTTP is mocked via httpx.MockTransport,
and content extraction is exercised against a static in-repo HTML fixture."""

import json
import re

import httpx
import pytest

import scraper

# A realistic static page: boilerplate nav/header/footer/sidebar wrapping a real article body.
# trafilatura should keep the article and drop the boilerplate.
FIXTURE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head><title>Library Hours | Gavilan College</title></head>
<body>
  <header><nav><ul>
    <li><a href="/">Home</a></li>
    <li><a href="/library/">Library</a></li>
    <li><a href="/admissions/">Admissions</a></li>
  </ul></nav></header>
  <aside class="sidebar">
    <h3>Quick Links</h3>
    <ul><li><a href="/library/hours">Hours</a></li><li><a href="/library/contact">Contact</a></li></ul>
  </aside>
  <main>
    <article>
      <h1>Library Hours</h1>
      <p>The Gavilan College Library is open Monday through Thursday from 8:00 AM to 7:00 PM,
         and Friday from 8:00 AM to 2:00 PM. The library is closed on weekends and during
         campus holidays.</p>
      <p>During final exam weeks the library extends its hours until 9:00 PM on weekdays to
         support students preparing for exams. Check the college calendar for exact dates.</p>
      <p>For questions about hours or to request research help, contact the reference desk at
         the circulation counter on the first floor.</p>
    </article>
  </main>
  <footer><p>&copy; 2026 Gavilan College. All rights reserved. Privacy policy. Accessibility.</p></footer>
</body>
</html>
"""


# --- slugify -----------------------------------------------------------------------------------

def test_slugify_is_deterministic():
    url = "https://www.gavilan.edu/library/hours.php"
    assert scraper.slugify_url(url) == scraper.slugify_url(url)


def test_slugify_is_filesystem_safe_and_readable():
    slug = scraper.slugify_url("https://www.gavilan.edu/library/hours.php")
    # Only lowercase alphanumerics and hyphens; readable host/path retained.
    assert re.fullmatch(r"[a-z0-9-]+", slug)
    assert slug.startswith("www-gavilan-edu-library-hours-php-")


def test_slugify_distinct_urls_do_not_collide():
    # Same slugified path but different query strings must yield different filenames.
    a = scraper.slugify_url("https://www.gavilan.edu/library/search?q=hours")
    b = scraper.slugify_url("https://www.gavilan.edu/library/search?q=books")
    assert a != b


def test_slugify_root_path_becomes_index():
    slug = scraper.slugify_url("https://www.gavilan.edu/")
    assert slug.startswith("www-gavilan-edu-index-") or slug.startswith("www-gavilan-edu-")


# --- metadata ----------------------------------------------------------------------------------

def test_build_metadata_shape_and_injected_timestamp():
    md = scraper.build_metadata(
        "https://www.gavilan.edu/library/hours",
        "https://www.gavilan.edu/library/hours/",
        "Library Hours",
        "some markdown body",
        timestamp="2026-07-07T00:00:00Z",
    )
    assert md == {
        "source_url": "https://www.gavilan.edu/library/hours",
        "fetched_url": "https://www.gavilan.edu/library/hours/",
        "title": "Library Hours",
        "scrape_timestamp": "2026-07-07T00:00:00Z",
        "content_chars": len("some markdown body"),
        "scraper_version": scraper.SCRAPER_VERSION,
    }


def test_metadata_has_required_attribution_keys():
    md = scraper.build_metadata("u", "u", None, "x", timestamp="2026-07-07T00:00:00Z")
    for key in ("source_url", "title", "scrape_timestamp"):
        assert key in md


# --- extraction (static fixture, no network) ---------------------------------------------------

def test_extract_markdown_keeps_article_drops_boilerplate():
    title, markdown = scraper.extract_markdown(FIXTURE_HTML, url="https://www.gavilan.edu/library/hours")
    assert markdown, "expected non-empty markdown from the fixture"
    # Main content survives.
    assert "Monday through Thursday" in markdown
    assert "final exam weeks" in markdown
    # Boilerplate is stripped.
    assert "Admissions" not in markdown
    assert "Quick Links" not in markdown
    assert "All rights reserved" not in markdown
    # Title extracted (site suffix may or may not be trimmed by trafilatura).
    assert title and "Library Hours" in title


# --- fetch handling (mocked) -------------------------------------------------------------------

def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_scrape_url_success_via_mock():
    def handler(request):
        return httpx.Response(200, html=FIXTURE_HTML)

    with _client(handler) as client:
        result = scraper.scrape_url("https://www.gavilan.edu/library/hours", client)

    assert result.ok
    assert "Monday through Thursday" in result.markdown
    assert result.metadata["source_url"] == "https://www.gavilan.edu/library/hours"
    assert result.metadata["content_chars"] == len(result.markdown)


def test_scrape_url_404_is_graceful():
    def handler(request):
        return httpx.Response(404, text="Not Found")

    with _client(handler) as client:
        result = scraper.scrape_url("https://www.gavilan.edu/library/missing", client)

    assert result.ok is False
    assert result.error == "HTTP 404"
    assert result.markdown is None
    assert result.metadata is None


def test_scrape_url_network_error_is_graceful():
    def handler(request):
        raise httpx.ConnectError("dns failure", request=request)

    with _client(handler) as client:
        result = scraper.scrape_url("https://nope.invalid/", client)

    assert result.ok is False
    assert "ConnectError" in result.error
    assert result.markdown is None


def test_scrape_urls_continues_past_a_failure():
    good = "https://www.gavilan.edu/library/hours"
    bad = "https://www.gavilan.edu/library/missing"

    def handler(request):
        if str(request.url) == bad:
            return httpx.Response(404, text="Not Found")
        return httpx.Response(200, html=FIXTURE_HTML)

    with _client(handler) as client:
        results = scraper.scrape_urls([good, bad], client=client)

    assert len(results) == 2
    assert results[0].ok is True
    assert results[1].ok is False
    # One failure does not abort the run.


# --- write_result (tmp dir) --------------------------------------------------------------------

def test_write_result_creates_md_and_json(tmp_path):
    result = scraper.ScrapeResult(
        url="https://www.gavilan.edu/library/hours",
        slug="www-gavilan-edu-library-hours-deadbeef",
        ok=True,
        title="Library Hours",
        markdown="# Library Hours\n\nOpen weekdays.",
        metadata={"source_url": "https://www.gavilan.edu/library/hours", "title": "Library Hours"},
    )
    md_path = scraper.write_result(result, tmp_path)
    json_path = tmp_path / f"{result.slug}.json"

    assert md_path.exists() and md_path.read_text(encoding="utf-8").startswith("# Library Hours")
    assert json.loads(json_path.read_text(encoding="utf-8"))["title"] == "Library Hours"


def test_write_result_skips_failed(tmp_path):
    result = scraper.ScrapeResult(url="u", slug="s", ok=False, error="HTTP 404")
    assert scraper.write_result(result, tmp_path) is None
    assert list(tmp_path.iterdir()) == []
