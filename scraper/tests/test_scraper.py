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

# Real Gavilan pages are served CORRECTLY as UTF-8, but some have replacement-char garbage baked
# into their SOURCE: the entities &#239;&#191;&#189; (which decode to U+00EF U+00BF U+00BD = the
# 3-char sequence "ï¿½") stand where an apostrophe or accented letter was lost to an upstream
# cp1252->UTF-8 mis-decode at authoring time. This fixture reproduces that exactly - a clean UTF-8
# page whose content carries the baked-in garbage. The scraper cannot recover the lost chars
# (U+FFFD is information-free), only strip the garbage.
FIXTURE_MOJIBAKE_HTML = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Databases</title></head>"
    "<body><main><article><h1>Databases</h1>"
    "<p>Access the world&#239;&#191;&#189;s largest research collection and the "
    "Encyclop&#239;&#191;&#189;dia Britannica online for library research assistance here.</p>"
    "</article></main></body></html>"
)

# The Gavilan pages use tables for LAYOUT (databases.php, contactus.php). trafilatura renders them
# as mangled markdown tables ("| cell | |" with empty cells). We want flat prose, not tables. This
# fixture reproduces that: a one-content-cell row (empty 2nd cell) and a two-cell contact row.
FIXTURE_TABLE_HTML = (
    "<!DOCTYPE html><html><head><title>Databases</title></head><body><main><article>"
    "<h1>Databases</h1>"
    "<table>"
    "<tr><td>Films on Demand -- Thousands of videos from the world's top producers.</td><td></td></tr>"
    "<tr><td>Gale Databases -- Articles and journals for research and coursework.</td><td></td></tr>"
    "</table>"
    "<h2>Contact</h2>"
    "<table>"
    "<tr><td>Phone: (408) 848-4806</td><td>Email: library@gavilan.edu</td></tr>"
    "<tr><td>Address: 5055 Santa Teresa Blvd</td><td>Gilroy, CA 95020</td></tr>"
    "</table>"
    "</article></main></body></html>"
)


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


# --- replacement-char scrubbing (baked-in source mojibake, no network) -------------------------

def test_scrub_replacement_chars():
    assert scraper._scrub_replacement_chars("worldï¿½s") == "worlds"  # "ï¿½" triple
    assert scraper._scrub_replacement_chars("a�b") == "ab"                       # bare U+FFFD
    assert scraper._scrub_replacement_chars("clean text") == "clean text"
    assert scraper._scrub_replacement_chars(None) is None
    assert scraper._scrub_replacement_chars("") == ""


def test_extract_markdown_scrubs_baked_in_mojibake():
    # The baked-in "ï¿½" garbage (from source HTML entities) must not reach the output. The
    # original apostrophe/ligature are unrecoverable, so the garbage is simply removed.
    _title, markdown = scraper.extract_markdown(
        FIXTURE_MOJIBAKE_HTML, url="https://www.gavilan.edu/library/databases.php"
    )
    assert markdown
    assert "ï¿½" not in markdown, "baked-in 'ï¿½' garbage still present"
    assert "�" not in markdown, "bare U+FFFD still present"
    assert "worlds largest" in markdown          # "world[ï¿½]s" -> garbage stripped
    assert "Encyclopdia Britannica" in markdown   # "Encyclop[ï¿½]dia" -> garbage stripped


# --- table flattening (layout tables -> flat prose, no network) --------------------------------

def test_flatten_markdown_tables_unit():
    src = (
        "# Title\n\n"
        "| Films on Demand -- videos. | |\n"
        "| --- | --- |\n"
        "| Phone: (408) 848-4806 | Email: library@gavilan.edu |\n\n"
        "Regular paragraph.\n"
    )
    out = scraper._flatten_markdown_tables(src)
    lines = out.split("\n")
    # No markdown-table markup survives.
    assert "| |" not in out
    assert not any(ln.strip().startswith("|") for ln in lines)
    # Heading and non-table prose pass through untouched.
    assert "# Title" in lines
    assert "Regular paragraph." in lines
    # Separator row dropped; content cells become their own prose lines.
    assert "Films on Demand -- videos." in lines
    assert "Phone: (408) 848-4806" in lines
    assert "Email: library@gavilan.edu" in lines


def test_extract_markdown_flattens_layout_tables_to_prose():
    _title, markdown = scraper.extract_markdown(
        FIXTURE_TABLE_HTML, url="https://www.gavilan.edu/library/databases.php"
    )
    assert markdown
    # No table markup: no empty-cell junk, and no line is a table row.
    assert "| |" not in markdown
    assert not any(ln.strip().startswith("|") for ln in markdown.split("\n"))
    # Cell TEXT is preserved as flat prose.
    assert "Films on Demand -- Thousands of videos from the world's top producers." in markdown
    assert "Gale Databases -- Articles and journals for research and coursework." in markdown
    assert "Phone: (408) 848-4806" in markdown
    assert "Email: library@gavilan.edu" in markdown
    assert "Gilroy, CA 95020" in markdown
    # Headings survive (include_tables stays True; we only strip pipes).
    assert "# Databases" in markdown


# --- fetch handling (mocked) -------------------------------------------------------------------

def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_scrape_url_scrubs_mojibake_end_to_end():
    # Server sends the page correctly as UTF-8 (as the real site does); the baked-in "ï¿½" garbage
    # must be scrubbed out of the result markdown end to end.
    def handler(request):
        return httpx.Response(
            200,
            content=FIXTURE_MOJIBAKE_HTML.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    with _client(handler) as client:
        result = scraper.scrape_url("https://www.gavilan.edu/library/databases.php", client)

    assert result.ok
    assert "ï¿½" not in result.markdown
    assert "�" not in result.markdown
    assert "worlds largest" in result.markdown


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


# --- Phase 2b: structured database-catalog extraction --------------------------------------

from pathlib import Path

_DB_FIXTURE = Path(__file__).resolve().parent / "fixtures_databases.html"

# Focused synthetic table exercising the tricky patterns the flattened-markdown parse got wrong.
_SYNTH_TABLE = """
<table>
  <tr><td>Alphabetical List</td></tr>
  <tr><td></td><td><a href="http://ez/login?url=foo">Foo DB</a> -- a great database</td></tr>
  <tr><td></td><td><a href="http://ez/login?url=bar">Bar Index with Full Text</a> covers everything about bars and more</td></tr>
  <tr><td></td><td><a href="http://ez/login?url=hs">Health Source: Consumer Edition</a> -- consumer health mags</td></tr>
  <tr><td></td><td><a id="bazanchor"></a> <a href="http://ez/login?url=baz">Baz</a>: some stuff</td></tr>
  <tr><td></td><td><p>Qux Series -- <br/> <a href="http://ez/login?url=q1">Q One</a>, <a href="http://ez/login?url=q2">Q Two</a></p></td></tr>
</table>
"""


def test_extract_catalog_synthetic_patterns():
    held = scraper.extract_database_catalog(_SYNTH_TABLE)
    by_name = {h["name"]: h for h in held}
    # Normal delimiter row.
    assert by_name["Foo DB"]["description"] == "a great database"
    assert by_name["Foo DB"]["url"] == "http://ez/login?url=foo"
    # NO-delimiter row: the anchor boundary isolates the name (the markdown parse garbled this).
    assert "Bar Index with Full Text" in by_name
    assert by_name["Bar Index with Full Text"]["description"].startswith("covers everything")
    # Colon INSIDE the name is preserved (split on ' -- ', not ':').
    assert "Health Source: Consumer Edition" in by_name
    # Empty id-only anchor is skipped; the real link is the name.
    assert by_name["Baz"]["description"] == "some stuff"
    # Series row: name is the leading plain text, not the first sub-link.
    assert "Qux Series" in by_name and "Q One" not in by_name


def test_extract_catalog_against_real_page_fixture():
    held = scraper.extract_database_catalog(_DB_FIXTURE.read_text(encoding="utf-8"))
    names = {h["name"] for h in held}
    # A healthy count from the real page.
    assert len(held) >= 40
    assert scraper.validate_held_list(held, 30) is True
    # Known databases, including the no-delimiter ones that garbled under the markdown parse.
    for expected in (
        "CQ Researcher",
        "Opposing Viewpoints In Context",
        "Criminal Justice Abstracts with Full Text",  # no delimiter on the page
        "Statista.com",                                # no delimiter on the page
        "Business Source Complete",
    ):
        assert expected in names, expected
    # No garbled names (a garble folds the description into the name -> very long), and every
    # entry has a non-empty description + url.
    assert all(len(h["name"].split()) <= 9 for h in held), [h["name"] for h in held if len(h["name"].split()) > 9]
    assert all(h["description"] for h in held)
    assert all(h["url"] for h in held)
    # Replacement-char garbage is scrubbed from descriptions.
    assert all("�" not in h["description"] for h in held)


def test_validate_held_list_guard():
    good = [{"name": f"DB {i}", "description": "d"} for i in range(30)]
    assert scraper.validate_held_list(good, 30) is True
    assert scraper.validate_held_list(good, 40) is False          # too few
    assert scraper.validate_held_list([], 1) is False             # empty
    assert scraper.validate_held_list("nonsense", 1) is False     # not a list
    assert scraper.validate_held_list([{"name": "", "description": "d"}], 1) is False   # blank name
    assert scraper.validate_held_list([{"name": "X", "description": 5}], 1) is False    # bad desc type


# --- Freshness tiers -------------------------------------------------------------------------
#
# Cadence and membership live entirely in config.yaml; these pin the only interpretation of that
# block. The property that matters most is the LAST one: a fast run must never be able to shrink
# the set of URLs the prune considers legitimate.

TIERS = {
    "fast": {"schedule_cron": "cron(30 11 * * ? *)", "urls": ["https://x/hours"]},
    "full": {"schedule_cron": "cron(0 10 1,6,11,16,21,26 * ? *)", "urls": ["https://x/a", "https://x/b"]},
}


def test_named_tier_fetches_only_its_own_urls():
    # The whole point of the fast tier: three pages a day, not the whole site.
    assert scraper.urls_for_tier(TIERS, "fast") == ["https://x/hours"]


def test_full_tier_fetches_every_url_in_every_tier():
    # "full" is the COMPLETE sweep, not the complement of fast - it re-fetches the fast pages too,
    # so a fast tier that has been quietly failing gets healed by the next full run.
    assert scraper.urls_for_tier(TIERS, "full") == ["https://x/hours", "https://x/a", "https://x/b"]


@pytest.mark.parametrize("tier", [None, "", "typo-tier"])
def test_unrecognised_tier_falls_back_to_the_complete_sweep(tier):
    # No tier at all is what the one-shot deploy Trigger and a console test invoke send. Defaulting
    # UP to everything means an unrecognised tier costs a few extra fetches instead of silently
    # refreshing a third of the corpus.
    assert scraper.urls_for_tier(TIERS, tier) == ["https://x/hours", "https://x/a", "https://x/b"]


def test_all_seed_urls_preserves_order_and_deduplicates():
    tiers = {
        "fast": {"urls": ["https://x/hours"]},
        "full": {"urls": ["https://x/a", "https://x/hours"]},
    }
    assert scraper.all_seed_urls(tiers) == ["https://x/hours", "https://x/a"]


def test_tier_helpers_survive_a_missing_or_empty_tier_block():
    assert scraper.all_seed_urls({}) == []
    assert scraper.urls_for_tier({}, "fast") == []
    assert scraper.urls_for_tier({"fast": {}}, "fast") == []
