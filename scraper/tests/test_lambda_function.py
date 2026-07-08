"""Tests for the scraper Lambda handler. No live network/AWS: scrape_urls and the S3 /
bedrock-agent clients are monkeypatched, and boto3 is stubbed (it's provided by the Lambda runtime,
not installed in the scraper venv)."""

import json
import sys
import types
from unittest.mock import MagicMock

# Stub boto3 BEFORE importing the handler (the handler does `import boto3` at module load). The
# tests replace the client getters, so boto3 itself is never called.
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

import lambda_function as lf  # noqa: E402
from scraper import ScrapeResult  # noqa: E402

BASE_ENV = {
    "SEED_URLS": json.dumps(["https://x/a", "https://x/b"]),
    "SCRAPE_TIMEOUT_SECONDS": "15",
    "SCRAPER_USER_AGENT": "TestAgent/1.0",
    "SOURCE_BUCKET": "kb-bucket",
    "KNOWLEDGE_BASE_ID": "kb-123",
    "DATA_SOURCE_ID": "ds-456",
}


def _ok(slug="x-a-hash"):
    return ScrapeResult(
        url="https://x/a", slug=slug, ok=True, title="Page A",
        markdown="# Page A\n\nbody text",
        metadata={
            "source_url": "https://x/a", "fetched_url": "https://x/a", "title": "Page A",
            "scrape_timestamp": "2026-07-07T00:00:00Z", "content_chars": 11, "scraper_version": "1",
        },
    )


def _fail():
    return ScrapeResult(url="https://x/b", slug="x-b", ok=False, error="HTTP 404")


def _wire(monkeypatch, results):
    for k, v in BASE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(lf, "scrape_urls", lambda urls, **kw: results)
    s3, bedrock_agent = MagicMock(), MagicMock()
    bedrock_agent.start_ingestion_job.return_value = {
        "ingestionJob": {"ingestionJobId": "job-1", "status": "STARTING"}
    }
    monkeypatch.setattr(lf, "_s3_client", lambda: s3)
    monkeypatch.setattr(lf, "_bedrock_agent_client", lambda: bedrock_agent)
    return s3, bedrock_agent


def test_uploads_markdown_and_metadata_and_triggers_ingestion(monkeypatch):
    s3, bedrock_agent = _wire(monkeypatch, [_ok()])
    out = lf.handler({}, None)

    puts = {c.kwargs["Key"]: c.kwargs for c in s3.put_object.call_args_list}
    # Two objects per page: the markdown and a Bedrock metadata sidecar (NOT `<slug>.json`).
    assert set(puts) == {"x-a-hash.md", "x-a-hash.md.metadata.json"}
    assert puts["x-a-hash.md"]["Body"] == b"# Page A\n\nbody text"
    assert all(p["Bucket"] == "kb-bucket" for p in puts.values())
    meta = json.loads(puts["x-a-hash.md.metadata.json"]["Body"])
    assert meta["metadataAttributes"]["source_url"] == "https://x/a"
    assert meta["metadataAttributes"]["title"] == "Page A"

    bedrock_agent.start_ingestion_job.assert_called_once()
    ck = bedrock_agent.start_ingestion_job.call_args.kwargs
    assert ck["knowledgeBaseId"] == "kb-123"
    assert ck["dataSourceId"] == "ds-456"

    assert out["uploaded"] == 1
    assert out["failed"] == []
    assert out["ingestionJobId"] == "job-1"
    # No CATALOG_BUCKET in this env -> catalog regeneration is skipped, not attempted.
    assert out["catalog"].startswith("skipped")


def test_partial_failure_uploads_survivors_and_still_ingests(monkeypatch):
    s3, bedrock_agent = _wire(monkeypatch, [_ok(), _fail()])
    out = lf.handler({}, None)
    # Only the successful page (md + metadata = 2 puts); the failure is reported; ingestion runs.
    assert s3.put_object.call_count == 2
    assert out["uploaded"] == 1
    assert out["failed"] == [{"url": "https://x/b", "error": "HTTP 404"}]
    bedrock_agent.start_ingestion_job.assert_called_once()


def test_no_successes_skips_ingestion(monkeypatch):
    s3, bedrock_agent = _wire(monkeypatch, [_fail()])
    out = lf.handler({}, None)
    s3.put_object.assert_not_called()
    bedrock_agent.start_ingestion_job.assert_not_called()
    assert out["uploaded"] == 0
    assert out["ingestionJobId"] is None


def test_passes_seed_urls_and_config_to_scrape_urls(monkeypatch):
    captured = {}
    for k, v in BASE_ENV.items():
        monkeypatch.setenv(k, v)

    def fake_scrape(urls, **kw):
        captured["urls"] = urls
        captured["kw"] = kw
        return []

    monkeypatch.setattr(lf, "scrape_urls", fake_scrape)
    monkeypatch.setattr(lf, "_s3_client", lambda: MagicMock())
    monkeypatch.setattr(lf, "_bedrock_agent_client", lambda: MagicMock())

    lf.handler({}, None)
    assert captured["urls"] == ["https://x/a", "https://x/b"]
    assert captured["kw"]["timeout"] == 15.0
    assert captured["kw"]["user_agent"] == "TestAgent/1.0"


# --- Phase 2b: catalog enrichment + regeneration + S3 write --------------------------------

import scraper as _scraper  # noqa: E402


def _mock_bedrock(json_text):
    m = MagicMock()
    m.converse.return_value = {"output": {"message": {"content": [{"text": json_text}]}}}
    return m


def _db_result(html):
    return ScrapeResult(url="https://www.gavilan.edu/library/databases.php", slug="databases-php",
                        ok=True, title="Databases", markdown="md", metadata={}, html=html)


_TWO_DB_TABLE = """
<table>
  <tr><td>Alphabetical List</td></tr>
  <tr><td></td><td><a href="http://ez/a">Alpha DB</a> -- alpha stuff</td></tr>
  <tr><td></td><td><a href="http://ez/b">Beta Health Index</a> -- beta health stuff</td></tr>
</table>
"""


def test_enrich_held_constrains_to_given_names():
    # The model returns one valid name + one hallucinated name; only the valid one is kept.
    reply = json.dumps([
        {"name": "Alpha DB", "subjects": ["general", "alpha"], "aliases": ["ADB"]},
        {"name": "Hallucinated DB", "subjects": ["nope"], "aliases": []},
    ])
    out = lf.enrich_held([{"name": "Alpha DB", "description": "x"}], _mock_bedrock(reply), "m")
    assert set(out) == {lf._norm("Alpha DB")}
    assert out[lf._norm("Alpha DB")] == {"subjects": ["general", "alpha"], "aliases": ["ADB"]}


def test_enrich_held_bad_reply_returns_empty():
    assert lf.enrich_held([{"name": "X", "description": "y"}], _mock_bedrock("not json"), "m") == {}


def test_enrich_held_empty_input_skips_model():
    bedrock = _mock_bedrock("[]")
    assert lf.enrich_held([], bedrock, "m") == {}
    bedrock.converse.assert_not_called()


def test_regenerate_held_reuses_prev_and_enriches_only_new():
    # Alpha DB already enriched previously (reused, no model call for it); Beta Health Index is new.
    prev = [{"name": "Alpha DB", "description": "alpha stuff", "subjects": ["alpha"], "aliases": []}]
    reply = json.dumps([{"name": "Beta Health Index", "subjects": ["health"], "aliases": ["BHI"]}])
    bedrock = _mock_bedrock(reply)
    held = lf.regenerate_held(_TWO_DB_TABLE, prev, bedrock, "m", min_databases=1)

    by = {h["name"]: h for h in held}
    assert by["Alpha DB"]["subjects"] == ["alpha"]          # reused from prev
    assert by["Beta Health Index"]["subjects"] == ["health"]  # freshly enriched
    assert by["Beta Health Index"]["aliases"] == ["BHI"]
    # The model was called with ONLY the new database, not the reused one.
    sent = bedrock.converse.call_args.kwargs["messages"][0]["content"][0]["text"]
    assert "Beta Health Index" in sent and "Alpha DB" not in sent


def test_regenerate_held_guard_rejects_too_few():
    # Only 2 parsed, min 30 -> None, and the model is never called (guard runs before enrichment).
    bedrock = _mock_bedrock("[]")
    assert lf.regenerate_held(_TWO_DB_TABLE, [], bedrock, "m", min_databases=30) is None
    bedrock.converse.assert_not_called()


def _catalog_env(monkeypatch, **over):
    env = {"CATALOG_BUCKET": "cat-bucket", "CATALOG_KEY": "database_catalog.json",
           "CATALOG_ENRICHMENT_MODEL_ID": "us.amazon.nova-pro-v1:0", "CATALOG_MIN_DATABASES": "1"}
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_regenerate_catalog_writes_when_valid(monkeypatch):
    _catalog_env(monkeypatch)
    reply = json.dumps([
        {"name": "Alpha DB", "subjects": ["general"], "aliases": []},
        {"name": "Beta Health Index", "subjects": ["health"], "aliases": []},
    ])
    monkeypatch.setattr(lf, "_bedrock_runtime_client", lambda: _mock_bedrock(reply))
    s3 = MagicMock()
    s3.get_object.side_effect = Exception("NoSuchKey")  # no previous catalog

    status = lf.regenerate_catalog([_db_result(_TWO_DB_TABLE)], s3, timestamp="2026-07-08T00:00:00Z")

    assert status["catalog"] == "written" and status["databases"] == 2
    put = s3.put_object.call_args.kwargs
    assert put["Bucket"] == "cat-bucket" and put["Key"] == "database_catalog.json"
    body = json.loads(put["Body"])
    assert body["generated_at"] == "2026-07-08T00:00:00Z"
    names = {h["name"] for h in body["held"]}
    assert names == {"Alpha DB", "Beta Health Index"}
    assert all("subjects" in h and "aliases" in h and "url" in h for h in body["held"])


def test_regenerate_catalog_keeps_last_good_on_guard_fail(monkeypatch):
    # min 30 but only 2 parse -> guard fails -> NO write (last-good kept).
    _catalog_env(monkeypatch, CATALOG_MIN_DATABASES="30")
    monkeypatch.setattr(lf, "_bedrock_runtime_client", lambda: _mock_bedrock("[]"))
    s3 = MagicMock()
    s3.get_object.side_effect = Exception("NoSuchKey")

    status = lf.regenerate_catalog([_db_result(_TWO_DB_TABLE)], s3)
    assert "guard failed" in status["catalog"]
    s3.put_object.assert_not_called()


def test_regenerate_catalog_no_databases_result(monkeypatch):
    _catalog_env(monkeypatch)
    s3 = MagicMock()
    # A scrape with no databases.php page at all.
    other = ScrapeResult(url="https://x/other", slug="o", ok=True, markdown="m", metadata={}, html="<p/>")
    status = lf.regenerate_catalog([other], s3)
    assert "no databases.php" in status["catalog"]
    s3.put_object.assert_not_called()


def test_regenerate_catalog_skipped_without_bucket(monkeypatch):
    monkeypatch.delenv("CATALOG_BUCKET", raising=False)
    s3 = MagicMock()
    status = lf.regenerate_catalog([_db_result(_TWO_DB_TABLE)], s3)
    assert status["catalog"].startswith("skipped")
    s3.put_object.assert_not_called()
