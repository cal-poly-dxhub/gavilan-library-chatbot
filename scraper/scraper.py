"""Local static-HTML scraper for Gavilan College Library pages.

Fetches a curated list of URLs over plain HTTP (the site is server-rendered HTML - no SPA, no
browser automation needed), extracts main-content markdown with trafilatura, and - via the CLI -
writes a markdown file plus a JSON metadata sidecar per page for manual inspection.

Design for painless Lambdafication (next step):
  - `scrape_urls(urls)` does fetch + extract only and returns `ScrapeResult` objects. It performs
    NO file or AWS I/O, so the future Lambda wrapper can call it and upload each result's markdown
    + metadata to the KB S3 source bucket instead of writing local files.
  - `write_result()` and `main()` are the local-only concerns (filesystem + CLI + config).

Scope discipline: only the given URLs are fetched. There is deliberately NO link-following /
recursive crawling here (the managed Web Crawler over-crawled the /library/ tree and hit
ingestion limits; we use an explicit curated list instead).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import trafilatura

LOG = logging.getLogger("scraper")

SCRAPER_VERSION = "1"
DEFAULT_TIMEOUT = 20.0
DEFAULT_USER_AGENT = "GavilanLibraryScraper/1.0 (+https://www.gavilan.edu/library/)"
DEFAULT_OUTPUT_DIR = "./scraper_output"

# "ï¿½" (U+00EF U+00BF U+00BD) - the Latin-1 view of a UTF-8-encoded U+FFFD. Baked into some
# source pages as the entities &#239;&#191;&#189;. See _scrub_replacement_chars.
_REPLACEMENT_SEQ = "ï¿½"

# Readable slug: everything that is not a lowercase letter or digit becomes a hyphen.
_SLUG_RE = re.compile(r"[^a-z0-9]+")
# Keep the readable portion bounded so filenames stay sane; uniqueness is guaranteed by the hash.
_SLUG_MAX_READABLE = 80
_HASH_LEN = 8


@dataclass
class ScrapeResult:
    """Outcome of scraping one URL. `ok` gates whether markdown/metadata are populated."""

    url: str
    slug: str
    ok: bool
    title: Optional[str] = None
    markdown: Optional[str] = None
    metadata: Optional[dict] = None
    error: Optional[str] = None


def slugify_url(url: str) -> str:
    """Map a URL to a deterministic, filesystem-safe filename stem.

    Readable part is `<host><path>` with non-alphanumerics collapsed to hyphens; a short sha256
    prefix of the FULL url (including any query string) is appended so distinct URLs that would
    otherwise slugify identically never collide. Same URL in -> same slug out, always.
    """
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.netloc}{parts.path}".lower()
    readable = _SLUG_RE.sub("-", base).strip("-")[:_SLUG_MAX_READABLE].strip("-")
    if not readable:
        readable = "index"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:_HASH_LEN]
    return f"{readable}-{digest}"


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 'Z' string (second precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_metadata(
    url: str,
    fetched_url: str,
    title: Optional[str],
    markdown: str,
    *,
    timestamp: Optional[str] = None,
) -> dict:
    """Metadata sidecar for one page. `timestamp` is injectable so tests stay deterministic.

    `source_url` is the requested URL (used for KB source attribution); `fetched_url` is the
    final URL after any redirects.
    """
    return {
        "source_url": url,
        "fetched_url": fetched_url,
        "title": title,
        "scrape_timestamp": timestamp or _now_iso(),
        "content_chars": len(markdown),
        "scraper_version": SCRAPER_VERSION,
    }


def _scrub_replacement_chars(text: Optional[str]) -> Optional[str]:
    """Remove U+FFFD replacement-char garbage baked into the source content.

    Several Gavilan library pages carry corrupted characters in their HTML - not from OUR fetch,
    but from an upstream cp1252->UTF-8 lossy mis-decode at authoring/CMS time. The corruption is
    stored as HTML entities `&#239;&#191;&#189;`, which decode to U+00EF U+00BF U+00BD ("ï¿½") -
    the Latin-1 view of a UTF-8-encoded U+FFFD. The original characters (an apostrophe in
    "world's", the ligature in "Encyclopædia") were replaced by U+FFFD at the source and are
    UNRECOVERABLE - U+FFFD carries no information about what it replaced. All we can do is strip
    the garbage so it does not pollute the knowledge base. We remove both the 3-char "ï¿½"
    sequence and any bare U+FFFD; neither occurs in legitimate English library content.
    """
    if not text:
        return text
    return text.replace(_REPLACEMENT_SEQ, "").replace("�", "")


def _flatten_markdown_tables(markdown: Optional[str]) -> Optional[str]:
    """Turn markdown-table rows into plain prose lines. We do NOT want tables in the knowledge
    base - the Gavilan pages use tables for LAYOUT (databases.php, contactus.php), so trafilatura
    emits mangled "| cell | |" rows with empty cells. This flattens that markup to flat text.

    Deliberately DUMB and robust (string ops on the OUTPUT markdown, never HTML table parsing, no
    column/header semantics): a line is treated as a table row only if it starts with "|". Its
    cells are split on "|"; empty cells and pure table-drawing cells (---, :, spaces - i.e. the
    |---|---| separator row) are dropped; each remaining cell becomes its own prose line. Non-table
    lines (including headings) pass through untouched.

    (include_tables stays True in extract_markdown on purpose: setting it False makes trafilatura
    drop heading markup and jam adjacent cells together with no separator - worse than this.)
    """
    if not markdown:
        return markdown
    out = []
    for line in markdown.split("\n"):
        if not line.strip().startswith("|"):
            out.append(line)
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # Keep content cells only: non-empty and not composed solely of table-drawing chars.
        out.extend(c for c in cells if c and (set(c) - set("-: ")))
    return "\n".join(out)


def _extract_title(html: str) -> Optional[str]:
    """Best-effort page title via trafilatura metadata; None if unavailable."""
    try:
        meta = trafilatura.extract_metadata(html)
    except Exception:  # trafilatura metadata extraction is best-effort; never fatal
        return None
    title = getattr(meta, "title", None) if meta is not None else None
    return title or None


def extract_markdown(html: str, url: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Extract (title, main-content markdown) from a page's HTML.

    Uses trafilatura, which strips nav/header/footer/sidebar boilerplate. Tables are kept (library
    hours are often tabular); comments are dropped. Returns (title, None) when no main content is
    found (e.g. a redirect stub or an empty page).

    Note on characters: the Gavilan pages are correctly served as UTF-8, so `html` from
    `response.text` decodes fine. Some pages, however, have replacement-char garbage baked into
    their source (see `_scrub_replacement_chars`), so both the title and markdown are scrubbed of
    it before returning.

    Note on tables: the pages use tables for layout, which trafilatura renders as mangled markdown
    tables. We flatten those to prose (see `_flatten_markdown_tables`) - the KB wants flat text.
    """
    markdown = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_comments=False,
    )
    markdown = _flatten_markdown_tables(_scrub_replacement_chars(markdown))
    if markdown is not None:
        markdown = markdown.strip() or None
    return _scrub_replacement_chars(_extract_title(html)), markdown


def scrape_url(url: str, client: httpx.Client) -> ScrapeResult:
    """Fetch + extract one URL. Never raises: fetch/extract failures become ok=False results."""
    slug = slugify_url(url)
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        LOG.warning("fetch failed (HTTP %s): %s", code, url)
        return ScrapeResult(url=url, slug=slug, ok=False, error=f"HTTP {code}")
    except httpx.RequestError as exc:
        LOG.warning("fetch error (%s): %s", exc.__class__.__name__, url)
        return ScrapeResult(
            url=url, slug=slug, ok=False, error=f"{exc.__class__.__name__}: {exc}"
        )

    # response.text honors the page's declared charset (the site serves UTF-8, header + <meta>).
    # extract_markdown scrubs any replacement-char garbage baked into the source content.
    title, markdown = extract_markdown(response.text, url=url)
    if not markdown:
        LOG.warning("no main content extracted: %s", url)
        return ScrapeResult(
            url=url, slug=slug, ok=False, title=title, error="no content extracted"
        )

    metadata = build_metadata(url, str(response.url), title, markdown)
    return ScrapeResult(
        url=url, slug=slug, ok=True, title=title, markdown=markdown, metadata=metadata
    )


def scrape_urls(
    urls: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = DEFAULT_USER_AGENT,
    client: Optional[httpx.Client] = None,
) -> list[ScrapeResult]:
    """Scrape each URL in order, continuing past failures. Returns one result per input URL.

    Importable core with no filesystem/AWS coupling - the Lambda wrapper calls this directly.
    Pass `client` to inject a preconfigured/mock httpx.Client (used in tests); otherwise one is
    created with the given timeout + User-Agent and closed on exit. Redirects are followed.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
    try:
        return [scrape_url(url, client) for url in urls]
    finally:
        if owns_client:
            client.close()


def write_result(result: ScrapeResult, output_dir) -> Optional[Path]:
    """Write `<slug>.md` + `<slug>.json` for a successful result. Returns the markdown path.

    No-op (returns None) for failed results. Local-only; the Lambda path uploads to S3 instead.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if not result.ok:
        return None
    md_path = output_dir / f"{result.slug}.md"
    json_path = output_dir / f"{result.slug}.json"
    md_path.write_text(result.markdown, encoding="utf-8")
    json_path.write_text(
        json.dumps(result.metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return md_path


def load_scraper_config(config_path=None) -> dict:
    """Read the `scraper:` block from the repo-root config.yaml (single source of truth)."""
    import yaml

    if config_path is None:
        config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with open(config_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg.get("scraper", {}) or {}


def main(argv=None) -> int:
    """CLI: scrape config seed_urls (or URLs passed on the command line) to `output_dir`.

    Exit codes: 0 all URLs succeeded; 1 the run completed but some URLs failed; 2 nothing to do.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Scrape Gavilan Library pages to clean markdown + metadata sidecars."
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to config.yaml.")
    parser.add_argument(
        "--output-dir", type=Path, default=None, help="Override scraper.output_dir."
    )
    parser.add_argument(
        "urls", nargs="*", help="Explicit URLs to scrape (override config seed_urls)."
    )
    args = parser.parse_args(argv)

    cfg = load_scraper_config(args.config)
    urls = args.urls or cfg.get("seed_urls") or []
    output_dir = args.output_dir or Path(cfg.get("output_dir", DEFAULT_OUTPUT_DIR))
    timeout = float(cfg.get("timeout_seconds", DEFAULT_TIMEOUT))
    user_agent = cfg.get("user_agent", DEFAULT_USER_AGENT)

    if not urls:
        LOG.error("nothing to scrape: scraper.seed_urls is empty and no URLs were passed")
        return 2

    LOG.info("scraping %d URL(s) -> %s", len(urls), output_dir)
    results = scrape_urls(urls, timeout=timeout, user_agent=user_agent)

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    for result in ok:
        path = write_result(result, output_dir)
        LOG.info("wrote %s (%d chars) <- %s", path, result.metadata["content_chars"], result.url)
    for result in failed:
        LOG.warning("FAILED %s: %s", result.url, result.error)

    LOG.info("done: %d ok, %d failed", len(ok), len(failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
