"""THROWAWAY discovery crawler - enumerate URLs under the Gavilan library site.

This is NOT part of the production scraper (scraper/scraper.py) or the CDK. Its only job is to
list candidate pages so a human can hand-pick real content URLs for the production scraper's
`scraper.seed_urls`. It does NOT extract, save, or ingest page content - just URL, <title>, and
an approximate content length for eyeballing which pages are substantive.

Bounded, polite crawl: scope-limited to the /library/ path prefix, max depth 2, hard page cap,
a delay between requests, and the same identifying User-Agent the production scraper uses.

Run:  python discover.py            (from the scraper/ dir)
      python discover.py --max-pages 50 --max-depth 1 https://www.gavilan.edu/library/
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from lxml import html as lxml_html

# Reuse the production scraper's User-Agent so discovery traffic is identified the same way.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scraper import DEFAULT_USER_AGENT  # noqa: E402

LOG = logging.getLogger("discover")

START_URL = "https://www.gavilan.edu/library/"
SCOPE_HOST = "www.gavilan.edu"
SCOPE_PREFIX = "/library/"          # follow only URLs whose path starts with this
MAX_DEPTH = 2                       # start page is depth 0
MAX_PAGES = 200                     # hard cap on fetches - a bug can't hammer the site
DELAY_SECONDS = 0.5                 # politeness delay between requests
TIMEOUT = 20.0
OUTPUT_FILE = Path(__file__).resolve().parent / "discovered_urls.csv"

# Non-HTML resources: never follow or record these.
NON_HTML_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".rtf",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".ico", ".tif", ".tiff",
    ".mp3", ".mp4", ".mov", ".avi", ".wmv", ".wav", ".m4a",
    ".zip", ".rar", ".7z", ".gz", ".tar",
    ".css", ".js", ".json", ".xml", ".rss", ".woff", ".woff2", ".ttf", ".eot",
}
BAD_SCHEME_PREFIXES = ("mailto:", "tel:", "javascript:", "data:")


def normalize(url: str) -> str:
    """Dedup key: drop the #fragment. Kept deliberately simple for a throwaway tool."""
    return urldefrag(url)[0]


def in_scope(url: str) -> bool:
    """In scope = http(s), the library host, and a path under /library/ (NOT the whole host)."""
    p = urlsplit(url)
    return (
        p.scheme in ("http", "https")
        and p.netloc == SCOPE_HOST
        and p.path.startswith(SCOPE_PREFIX)
    )


def followable(url: str) -> bool:
    """Reject bad schemes and non-HTML file extensions before we ever enqueue a link."""
    low = url.lower()
    if low.startswith(BAD_SCHEME_PREFIXES):
        return False
    ext = os.path.splitext(urlsplit(url).path)[1].lower()
    return ext not in NON_HTML_EXT


def extract_links(base_url: str, doc) -> list[str]:
    """Absolute, de-fragmented links from <a href>. Pure anchors (#...) collapse to base and are
    dropped by the seen-set. Malformed hrefs are skipped."""
    links = []
    for anchor in doc.xpath("//a[@href]"):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(BAD_SCHEME_PREFIXES):
            continue
        try:
            absolute = normalize(urljoin(base_url, href))
        except ValueError:
            continue
        links.append(absolute)
    return links


def page_info(doc) -> tuple[str, int]:
    """(title, approx visible-text length). text_content() includes nav/footer, but it's a fine
    coarse signal for 'substantive page' vs 'near-empty landing page'."""
    title = " ".join((doc.findtext(".//title") or "").split())
    text_len = len(" ".join(doc.text_content().split()))
    return title, text_len


def crawl(start_url: str, *, max_depth: int, max_pages: int, delay: float) -> tuple[list[dict], dict]:
    """BFS crawl within scope. Returns (rows, stats). Never raises on a bad link/page."""
    client = httpx.Client(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )
    seen = {normalize(start_url)}       # pre-fetch dedup (enqueued URLs)
    recorded_finals: set[str] = set()   # post-redirect dedup (final URLs actually landed on)
    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    rows: list[dict] = []
    stats = {"fetched": 0, "http_errors": 0, "net_errors": 0, "non_html": 0,
             "parse_errors": 0, "redirected_out": 0, "duplicates": 0, "capped": False}

    try:
        while queue:
            if stats["fetched"] >= max_pages:
                LOG.warning("hit max-pages cap (%d); stopping", max_pages)
                stats["capped"] = True
                break
            url, depth = queue.popleft()

            if stats["fetched"] > 0:
                time.sleep(delay)  # politeness

            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                LOG.warning("HTTP %s  %s", exc.response.status_code, url)
                stats["http_errors"] += 1
                continue
            except httpx.RequestError as exc:
                LOG.warning("%s  %s", exc.__class__.__name__, url)
                stats["net_errors"] += 1
                continue

            stats["fetched"] += 1
            final_url = str(resp.url)

            # A redirect may have carried us out of scope; don't record or follow those.
            if not in_scope(final_url):
                LOG.info("redirected out of scope: %s -> %s", url, final_url)
                stats["redirected_out"] += 1
                continue
            if "html" not in resp.headers.get("content-type", "").lower():
                LOG.info("skip non-html (%s): %s", resp.headers.get("content-type", "?"), final_url)
                stats["non_html"] += 1
                continue

            # Distinct source URLs can redirect/alias to the same page (e.g. /archive, /archive/,
            # /archive/index.php). Dedup on the FINAL url so each page is recorded/followed once.
            norm_final = normalize(final_url)
            if norm_final in recorded_finals:
                stats["duplicates"] += 1
                continue
            recorded_finals.add(norm_final)
            seen.add(norm_final)

            try:
                doc = lxml_html.fromstring(resp.content)
            except Exception as exc:  # malformed HTML - record the URL, don't follow
                LOG.warning("parse error (%s): %s", exc.__class__.__name__, final_url)
                stats["parse_errors"] += 1
                rows.append({"url": final_url, "depth": depth, "title": "(parse error)",
                             "text_len": 0, "bytes": len(resp.content)})
                continue

            title, text_len = page_info(doc)
            rows.append({"url": final_url, "depth": depth, "title": title,
                         "text_len": text_len, "bytes": len(resp.content)})

            if depth < max_depth:
                for link in extract_links(final_url, doc):
                    if link in seen or not in_scope(link) or not followable(link):
                        continue
                    seen.add(link)
                    queue.append((link, depth + 1))
    finally:
        client.close()

    return rows, stats


def write_csv(rows: list[dict], output: Path) -> None:
    # Sort meatiest-first so substantive content pages float to the top for curation.
    rows_sorted = sorted(rows, key=lambda r: r["text_len"], reverse=True)
    with open(output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["url", "depth", "title", "text_len", "bytes"])
        writer.writeheader()
        writer.writerows(rows_sorted)


def print_summary(rows: list[dict], stats: dict, output: Path, top: int = 25) -> None:
    by_depth: dict[int, int] = {}
    for r in rows:
        by_depth[r["depth"]] = by_depth.get(r["depth"], 0) + 1

    print("\n" + "=" * 78)
    print(f"Discovered {len(rows)} in-scope page(s)  |  fetched {stats['fetched']} URL(s)")
    print(f"  by depth: " + ", ".join(f"d{d}={by_depth.get(d, 0)}" for d in sorted(by_depth)))
    print(f"  skipped: {stats['http_errors']} http-err, {stats['net_errors']} net-err, "
          f"{stats['non_html']} non-html, {stats['parse_errors']} parse-err, "
          f"{stats['redirected_out']} redirected-out, {stats['duplicates']} dup-redirect"
          + ("  [PAGE CAP HIT]" if stats["capped"] else ""))
    print(f"  full list (sorted by text length): {output}")
    print("=" * 78)
    print(f"\nTop {min(top, len(rows))} by approx content length (substantive pages first):\n")
    rows_sorted = sorted(rows, key=lambda r: r["text_len"], reverse=True)
    for r in rows_sorted[:top]:
        title = (r["title"][:55] + "...") if len(r["title"]) > 58 else r["title"]
        print(f"  {r['text_len']:>7}  d{r['depth']}  {title:<60}  {r['url']}")
    print()


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx logs every request at INFO; quiet it so our own crawl log stays readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description="Throwaway URL-discovery crawler for the Gavilan library site.")
    parser.add_argument("start_url", nargs="?", default=START_URL, help=f"Start URL (default {START_URL}).")
    parser.add_argument("--max-depth", type=int, default=MAX_DEPTH)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--delay", type=float, default=DELAY_SECONDS)
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    args = parser.parse_args(argv)

    if not in_scope(args.start_url):
        LOG.error("start URL is out of scope (must be %s under %s): %s",
                  SCOPE_HOST, SCOPE_PREFIX, args.start_url)
        return 2

    LOG.info("crawling %s  (depth<=%d, cap=%d, delay=%.1fs)",
             args.start_url, args.max_depth, args.max_pages, args.delay)
    rows, stats = crawl(args.start_url, max_depth=args.max_depth,
                        max_pages=args.max_pages, delay=args.delay)
    write_csv(rows, args.output)
    print_summary(rows, stats, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
