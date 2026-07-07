# Scraper

Standalone local tool that fetches curated Gavilan College Library pages, extracts clean
main-content markdown (via [trafilatura](https://trafilatura.readthedocs.io/)), and writes a
markdown file + JSON metadata sidecar per page. It replaces the removed managed Bedrock Web
Crawler. **Local only for now** - no AWS/Lambda/S3 (that wrapper is the next step).

The site is server-rendered static HTML, so this uses a plain HTTP fetch (`httpx`) - no browser
automation.

## Setup

```bash
cd scraper
python -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt pytest
```

## Run

Uses the `scraper:` block in the repo-root `config.yaml` (`seed_urls`, `output_dir`, `timeout_seconds`,
`user_agent`):

```bash
python scraper.py                                   # scrape config seed_urls -> output_dir
python scraper.py https://www.gavilan.edu/library/  # or pass explicit URLs (override config)
python scraper.py --output-dir /tmp/out URL1 URL2
```

Per successful page it writes `<slug>.md` and `<slug>.json` into `output_dir` (default
`./scraper_output`). The slug is `<host><path>` slugified plus a short hash of the full URL
(collision-safe, deterministic). Failed fetches (404s, network errors) are logged and skipped;
the run continues. Exit code: `0` all ok, `1` some URLs failed, `2` nothing to scrape.

## Importable API

```python
from scraper import scrape_urls        # scrape_urls(urls) -> list[ScrapeResult]
```

`scrape_urls` does fetch + extract only (no file/AWS I/O), so the Lambda wrapper can call it and
upload each result's `.markdown` / `.metadata` to S3 instead of writing local files.

## Tests

```bash
python -m pytest tests -v   # no network; HTTP mocked, extraction run on a static fixture
```
