# Scraper

Fetches curated Gavilan College Library pages, extracts clean main-content markdown (via
[trafilatura](https://trafilatura.readthedocs.io/)), and writes a markdown file plus a JSON
metadata sidecar per page. The site is server-rendered static HTML, so this is a plain `httpx`
fetch - no browser automation.

Two entry points over the same code. `scraper.py` is the CLI below, writing local files: pure
fetch and extract, no AWS. `lambda_function.py` is the deployed Lambda: same `scrape_urls`, then
upload to the KB source bucket, prune stale objects, start a Bedrock ingestion job and regenerate
the database catalog - every step gated on whether content actually changed.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Reads the `scraper:` block in the repo-root `config.yaml`. URLs are grouped into freshness tiers,
each with its own schedule; `--tier` picks one, and the default `full` means every URL in every
tier:

```bash
python scraper.py                                   # every configured URL -> output_dir
python scraper.py --tier fast                       # just the hours/closures pages
python scraper.py https://www.gavilan.edu/library/  # explicit URLs override the config
python scraper.py --output-dir /tmp/out URL1 URL2
```

Per successful page it writes `<slug>.md` and `<slug>.json` into `output_dir` (default
`./scraper_output`). The slug is `<host><path>` slugified plus a short hash of the full URL, so it
is collision-safe and deterministic. Failed fetches are logged and skipped and the run continues.
Exit code: `0` all ok, `1` some URLs failed, `2` nothing to scrape.

## Importable API

`scrape_urls(urls) -> list[ScrapeResult]` does fetch and extract only - no file or AWS I/O -
which is how `lambda_function.py` uploads each result's `.markdown` / `.metadata` to S3 instead
of writing local files.

## Tests

```bash
python -m pytest tests -v   # no network; HTTP mocked, extraction run on a static fixture
```
