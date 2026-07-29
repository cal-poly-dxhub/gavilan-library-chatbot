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

# One implicit tier holding both URLs: the handler resolves an invocation with no tier to the
# complete sweep, so this reproduces the pre-tiering behaviour every test below was written against.
BASE_ENV = {
    "SCRAPER_TIERS": json.dumps({"full": {"urls": ["https://x/a", "https://x/b"]}}),
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
    # Empty head = no stored fingerprint = every page reads as changed, which is the state of a
    # fresh bucket. Tests that exercise the gating set a fingerprint explicitly.
    s3.head_object.return_value = {}
    bedrock_agent.start_ingestion_job.return_value = {
        "ingestionJob": {"ingestionJobId": "job-1", "status": "STARTING"}
    }
    # No job history by default: nothing running to defer behind, nothing already ingested.
    bedrock_agent.list_ingestion_jobs.return_value = {"ingestionJobSummaries": []}
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


# --- Pruning and KB exclusion ----------------------------------------------------------------
#
# Before this the uploader could only ever put_object, so a page removed from seed_urls kept its
# document in the bucket and stayed indexed forever - de-seeding was a silent no-op. These pin the
# fix and, more importantly, the safety property: pruning keys off CONFIGURATION, not off what a
# given run managed to fetch.


def _paginated(keys):
    """A MagicMock paginator whose paginate() yields one page of the given object keys."""
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": k} for k in keys]}]
    return paginator


def test_expected_kb_keys_covers_the_seed_list_minus_exclusions():
    keys = lf.expected_kb_keys(["https://x/a", "https://x/b"], ["https://x/b"])
    a = lf.slugify_url("https://x/a")
    b = lf.slugify_url("https://x/b")
    assert keys == {f"{a}.md", f"{a}.md.metadata.json"}
    assert f"{b}.md" not in keys


def test_prune_deletes_only_what_configuration_no_longer_wants(monkeypatch):
    s3, _ = _wire(monkeypatch, [_ok()])
    s3.get_paginator.return_value = _paginated(
        ["keep.md", "keep.md.metadata.json", "stale.md", "stale.md.metadata.json"]
    )

    deleted = lf.prune_stale_objects(s3, "kb-bucket", {"keep.md", "keep.md.metadata.json"})

    assert sorted(deleted) == ["stale.md", "stale.md.metadata.json"]
    assert {c.kwargs["Key"] for c in s3.delete_object.call_args_list} == {
        "stale.md",
        "stale.md.metadata.json",
    }


def test_prune_never_raises_when_s3_refuses(monkeypatch):
    # Housekeeping must not break a scrape that already succeeded - e.g. if the role is missing
    # s3:ListBucket, the run should still upload and ingest.
    s3, _ = _wire(monkeypatch, [_ok()])
    s3.get_paginator.side_effect = RuntimeError("AccessDenied")

    assert lf.prune_stale_objects(s3, "kb-bucket", {"keep.md"}) == []


def test_a_failed_fetch_does_not_delete_that_page_from_the_knowledge_base(monkeypatch):
    # THE point of keying on config. Both pages are seeded; /b 404s this run. Pruning by what
    # uploaded successfully would delete /b's document and silently drop its answers from the bot
    # until someone noticed. Its last-good document must survive a transient failure.
    s3, _ = _wire(monkeypatch, [_ok(), _fail()])
    b_slug = lf.slugify_url("https://x/b")
    s3.get_paginator.return_value = _paginated(
        ["x-a-hash.md", "x-a-hash.md.metadata.json", f"{b_slug}.md", f"{b_slug}.md.metadata.json"]
    )

    lf.handler({}, None)

    deleted = {c.kwargs["Key"] for c in s3.delete_object.call_args_list}
    assert deleted == set()  # nothing pruned, despite /b producing no upload this run


def test_kb_excluded_page_is_scraped_but_never_uploaded(monkeypatch):
    # databases.php is the real case: regenerate_catalog needs its HTML, the knowledge base does
    # not need its text. It stays in SEED_URLS and is skipped at upload time.
    s3, _ = _wire(monkeypatch, [_ok()])
    monkeypatch.setenv("KB_EXCLUDE_URLS", json.dumps(["https://x/a"]))
    s3.get_paginator.return_value = _paginated(["x-a-hash.md", "x-a-hash.md.metadata.json"])

    out = lf.handler({}, None)

    s3.put_object.assert_not_called()
    assert out["uploaded"] == 0
    # ...and the prune retires the copy a previous run had already indexed.
    assert sorted(out["pruned"]) == ["x-a-hash.md", "x-a-hash.md.metadata.json"]


def test_ingestion_runs_for_a_prune_only_change(monkeypatch):
    # A run that only REMOVES pages still has to re-ingest, or the deleted document keeps its
    # vectors and the bot keeps answering from content that is no longer in the bucket.
    s3, bedrock_agent = _wire(monkeypatch, [_fail()])
    s3.get_paginator.return_value = _paginated(["gone.md"])

    lf.handler({}, None)

    bedrock_agent.start_ingestion_job.assert_called_once()


# --- Tiering: which URLs a run fetches, and what it is allowed to prune ------------------------

_TIER_ENV = json.dumps({
    "fast": {"schedule_cron": "cron(30 11 * * ? *)", "urls": ["https://x/hours"]},
    "full": {"schedule_cron": "cron(0 10 1,6,11,16,21,26 * ? *)", "urls": ["https://x/a", "https://x/b"]},
})


def _wire_tiers(monkeypatch, results):
    s3, bedrock_agent = _wire(monkeypatch, results)
    monkeypatch.setenv("SCRAPER_TIERS", _TIER_ENV)
    return s3, bedrock_agent


def _captured_scrape_list(monkeypatch, results):
    """Run the handler and return the URL list it actually handed to scrape_urls."""
    captured = {}

    def fake_scrape(urls, **kw):
        captured["urls"] = list(urls)
        return results

    monkeypatch.setattr(lf, "scrape_urls", fake_scrape)
    return captured


def test_fast_tier_scrapes_only_its_own_urls(monkeypatch):
    _wire_tiers(monkeypatch, [])
    captured = _captured_scrape_list(monkeypatch, [])
    lf.handler({"tier": "fast"}, None)
    assert captured["urls"] == ["https://x/hours"]


def test_full_tier_scrapes_every_configured_url(monkeypatch):
    _wire_tiers(monkeypatch, [])
    captured = _captured_scrape_list(monkeypatch, [])
    lf.handler({"tier": "full"}, None)
    assert captured["urls"] == ["https://x/hours", "https://x/a", "https://x/b"]


def test_deploy_trigger_event_without_a_tier_does_the_full_sweep(monkeypatch):
    # The one-shot install Trigger invokes with no tier. It must populate the WHOLE knowledge base.
    _wire_tiers(monkeypatch, [])
    captured = _captured_scrape_list(monkeypatch, [])
    lf.handler({}, None)
    assert captured["urls"] == ["https://x/hours", "https://x/a", "https://x/b"]


def test_a_fast_run_never_prunes_the_pages_it_did_not_fetch(monkeypatch):
    # THE tiering hazard. The prune deletes whatever configuration no longer calls for; if it
    # keyed off the three URLs a fast run fetched, the daily schedule would delete the rest of the
    # corpus from the knowledge base every single night.
    s3, _ = _wire_tiers(monkeypatch, [])
    a_slug, b_slug = lf.slugify_url("https://x/a"), lf.slugify_url("https://x/b")
    s3.get_paginator.return_value = _paginated([f"{a_slug}.md", f"{b_slug}.md", "genuinely-stale.md"])

    out = lf.handler({"tier": "fast"}, None)

    assert out["pruned"] == ["genuinely-stale.md"]
    assert {c.kwargs["Key"] for c in s3.delete_object.call_args_list} == {"genuinely-stale.md"}


def test_fast_tier_never_touches_the_catalog_enrichment(monkeypatch):
    # databases.php is a full-tier page, so a fast run cannot reach the model call at all. Pinned
    # by asserting the bedrock-runtime client is never even constructed.
    s3, _ = _wire_tiers(monkeypatch, [_ok()])

    def explode():
        raise AssertionError("fast tier must not reach the catalog enrichment model")

    monkeypatch.setattr(lf, "_bedrock_runtime_client", explode)

    out = lf.handler({"tier": "fast"}, None)

    assert out["catalog"] == "skipped (databases.php not in this tier)"
    assert out["summary"]["enrichment"] == {"ran": False, "reason": "not attempted"}


# --- Gate 1: upload only what changed ---------------------------------------------------------


def test_content_fingerprint_ignores_the_scrape_timestamp():
    # Verified against the live site: two consecutive scrapes produce byte-identical markdown and
    # differ ONLY in scrape_timestamp. If the fingerprint covered it, every page would look
    # changed on every run and the whole gate would be an expensive no-op.
    md = "# Page\n\nbody"
    base = {"source_url": "https://x/a", "title": "Page", "scrape_timestamp": "2026-07-07T00:00:00Z"}
    later = dict(base, scrape_timestamp="2026-07-29T21:16:07Z")
    assert lf.content_fingerprint(md, base) == lf.content_fingerprint(md, later)


def test_content_fingerprint_moves_on_body_or_title_change():
    md = "# Page\n\nbody"
    meta = {"source_url": "https://x/a", "title": "Page"}
    baseline = lf.content_fingerprint(md, meta)
    assert lf.content_fingerprint(md + " more", meta) != baseline
    assert lf.content_fingerprint(md, dict(meta, title="Renamed")) != baseline
    assert lf.content_fingerprint(md, dict(meta, source_url="https://x/moved")) != baseline


def test_changed_page_is_uploaded_and_stamped_with_its_fingerprint(monkeypatch):
    s3, bedrock_agent = _wire(monkeypatch, [_ok()])
    out = lf.handler({}, None)

    md_put = next(c.kwargs for c in s3.put_object.call_args_list if c.kwargs["Key"] == "x-a-hash.md")
    expected = lf.content_fingerprint(_ok().markdown, _ok().metadata)
    assert md_put["Metadata"] == {lf.CONTENT_HASH_METADATA_KEY: expected}
    assert out["uploaded"] == 1 and out["unchanged"] == 0
    bedrock_agent.start_ingestion_job.assert_called_once()


def test_unchanged_page_uploads_nothing_and_starts_no_ingestion(monkeypatch):
    s3, bedrock_agent = _wire(monkeypatch, [_ok()])
    stored = lf.content_fingerprint(_ok().markdown, _ok().metadata)
    s3.head_object.return_value = {"Metadata": {lf.CONTENT_HASH_METADATA_KEY: stored}}

    out = lf.handler({}, None)

    s3.put_object.assert_not_called()
    bedrock_agent.start_ingestion_job.assert_not_called()
    assert out["uploaded"] == 0
    assert out["unchanged"] == 1
    assert out["summary"]["pages_changed"] == 0
    assert out["ingestion"].startswith("skipped")


def test_a_second_consecutive_run_over_unchanged_content_reports_zero_changes(monkeypatch):
    # The acceptance property, end to end: run once against an empty bucket, feed run one's own
    # stamped fingerprint back as what S3 now holds, and run again.
    s3, bedrock_agent = _wire(monkeypatch, [_ok()])

    first = lf.handler({}, None)
    assert first["uploaded"] == 1

    md_put = next(c.kwargs for c in s3.put_object.call_args_list if c.kwargs["Key"] == "x-a-hash.md")
    s3.reset_mock()
    s3.head_object.return_value = {"Metadata": dict(md_put["Metadata"])}
    s3.get_paginator.return_value = _paginated(["x-a-hash.md", "x-a-hash.md.metadata.json"])
    bedrock_agent.reset_mock()
    bedrock_agent.list_ingestion_jobs.return_value = {"ingestionJobSummaries": []}

    second = lf.handler({}, None)

    assert second["summary"]["pages_changed"] == 0
    assert second["summary"]["pages_unchanged"] == 1
    assert second["pruned"] == []
    s3.put_object.assert_not_called()
    bedrock_agent.start_ingestion_job.assert_not_called()


def test_a_missing_fingerprint_is_treated_as_changed(monkeypatch):
    # Objects written before change gating existed carry no fingerprint; so does an object S3
    # refuses to HEAD. Both must re-upload (and thereby stamp themselves), never silently skip.
    s3, _ = _wire(monkeypatch, [_ok()])
    s3.head_object.side_effect = RuntimeError("AccessDenied")

    out = lf.handler({}, None)
    assert out["uploaded"] == 1


def test_an_unchanged_page_is_never_pruned_as_stale(monkeypatch):
    # The regression change gating introduces if you are not careful. The prune's belt-and-braces
    # union used to cover every live page for free, because every successful page was re-uploaded
    # on every run. Once unchanged pages stop uploading, a page can be live, correct, and absent
    # from that union - and a slug-derivation mismatch would then delete it. Pages found unchanged
    # must count as live.
    s3, _ = _wire(monkeypatch, [_ok()])
    stored = lf.content_fingerprint(_ok().markdown, _ok().metadata)
    s3.head_object.return_value = {"Metadata": {lf.CONTENT_HASH_METADATA_KEY: stored}}
    s3.get_paginator.return_value = _paginated(
        ["x-a-hash.md", "x-a-hash.md.metadata.json", "really-stale.md"]
    )

    out = lf.handler({}, None)

    assert out["unchanged"] == 1
    assert out["pruned"] == ["really-stale.md"]


# --- Gate 3: the catalog enrichment, the only model call in this path -------------------------


def _parsed_two():
    return _scraper.extract_database_catalog(_TWO_DB_TABLE)


def test_catalog_source_fingerprint_is_stable_and_row_sensitive():
    assert lf.catalog_source_fingerprint(_parsed_two()) == lf.catalog_source_fingerprint(_parsed_two())
    changed = _parsed_two()
    changed[0]["description"] = "rewritten"
    assert lf.catalog_source_fingerprint(changed) != lf.catalog_source_fingerprint(_parsed_two())


def _previous_catalog(s3, body):
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps(body).encode())}


def test_unchanged_databases_page_skips_the_model_and_the_write(monkeypatch):
    # The cost gate. Same parsed rows as last time -> no Sonnet call, no S3 write, catalog left
    # exactly as it is.
    _catalog_env(monkeypatch)
    s3 = MagicMock()
    _previous_catalog(s3, {
        "held": [{"name": "Alpha DB", "description": "alpha stuff", "subjects": ["alpha"], "aliases": []}],
        "source_sha256": lf.catalog_source_fingerprint(_parsed_two()),
    })

    def explode():
        raise AssertionError("enrichment must not run when the databases page is unchanged")

    monkeypatch.setattr(lf, "_bedrock_runtime_client", explode)

    status = lf.regenerate_catalog([_db_result(_TWO_DB_TABLE)], s3)

    assert status["catalog"] == "unchanged; enrichment skipped"
    assert status["enrichment"] == {"ran": False, "reason": "databases page unchanged"}
    s3.put_object.assert_not_called()


def test_changed_databases_page_reenriches_and_stores_the_new_fingerprint(monkeypatch):
    _catalog_env(monkeypatch)
    reply = json.dumps([{"name": "Beta Health Index", "subjects": ["health"], "aliases": ["BHI"]}])
    monkeypatch.setattr(lf, "_bedrock_runtime_client", lambda: _mock_bedrock(reply))
    s3 = MagicMock()
    # A stale fingerprint from some earlier version of the page.
    _previous_catalog(s3, {
        "held": [{"name": "Alpha DB", "description": "alpha stuff", "subjects": ["alpha"], "aliases": []}],
        "source_sha256": "stale-fingerprint",
    })

    status = lf.regenerate_catalog([_db_result(_TWO_DB_TABLE)], s3)

    assert status["catalog"] == "written"
    body = json.loads(s3.put_object.call_args.kwargs["Body"])
    assert body["source_sha256"] == lf.catalog_source_fingerprint(_parsed_two())


def test_first_ever_run_enriches_even_though_there_is_no_fingerprint(monkeypatch):
    _catalog_env(monkeypatch)
    reply = json.dumps([
        {"name": "Alpha DB", "subjects": ["general"], "aliases": []},
        {"name": "Beta Health Index", "subjects": ["health"], "aliases": []},
    ])
    monkeypatch.setattr(lf, "_bedrock_runtime_client", lambda: _mock_bedrock(reply))
    s3 = MagicMock()
    s3.get_object.side_effect = Exception("NoSuchKey")

    status = lf.regenerate_catalog([_db_result(_TWO_DB_TABLE)], s3)
    assert status["catalog"] == "written"
    s3.put_object.assert_called_once()


def test_guard_failure_does_not_record_a_fingerprint(monkeypatch):
    # Ordering matters: guard BEFORE fingerprint. If a broken page could record its fingerprint,
    # the next run would compare equal, skip, and freeze the catalog on the broken parse forever.
    _catalog_env(monkeypatch, CATALOG_MIN_DATABASES="30")
    monkeypatch.setattr(lf, "_bedrock_runtime_client", lambda: _mock_bedrock("[]"))
    s3 = MagicMock()
    _previous_catalog(s3, {"held": [{"name": "Alpha DB", "description": "d", "subjects": ["a"]}],
                          "source_sha256": "prior"})

    status = lf.regenerate_catalog([_db_result(_TWO_DB_TABLE)], s3)

    assert "guard failed" in status["catalog"]
    s3.put_object.assert_not_called()


# --- Measuring the enrichment call ------------------------------------------------------------


def test_enrichment_records_the_real_token_counts():
    # The cost of this call was previously an estimate. It is now whatever Bedrock reported.
    bedrock = _mock_bedrock(json.dumps([{"name": "Alpha DB", "subjects": ["x"], "aliases": []}]))
    bedrock.converse.return_value["usage"] = {
        "inputTokens": 1234, "outputTokens": 567, "totalTokens": 1801
    }
    usage = {}
    lf.enrich_held([{"name": "Alpha DB", "description": "d"}], bedrock, "model-x", usage=usage)

    assert usage["ran"] is True
    assert usage["input_tokens"] == 1234
    assert usage["output_tokens"] == 567
    assert usage["total_tokens"] == 1801
    assert usage["model_id"] == "model-x"
    assert usage["databases_enriched"] == 1
    assert usage["at"].endswith("Z")


def test_enrichment_usage_survives_a_response_with_no_usage_block():
    # A metrics detail must never be able to break the catalog.
    bedrock = _mock_bedrock(json.dumps([{"name": "Alpha DB", "subjects": ["x"], "aliases": []}]))
    usage = {}
    out = lf.enrich_held([{"name": "Alpha DB", "description": "d"}], bedrock, "m", usage=usage)
    assert out  # enrichment still worked
    assert usage["input_tokens"] == 0 and usage["output_tokens"] == 0


def test_a_run_with_nothing_new_to_enrich_reports_that_it_did_not_call_the_model(monkeypatch):
    _catalog_env(monkeypatch)
    bedrock = _mock_bedrock("[]")
    monkeypatch.setattr(lf, "_bedrock_runtime_client", lambda: bedrock)
    s3 = MagicMock()
    # Both databases already enriched, but the page content moved (different fingerprint), so the
    # catalog IS rewritten - without any model call, because nothing needs enriching.
    _previous_catalog(s3, {
        "held": [
            {"name": "Alpha DB", "description": "alpha stuff", "subjects": ["alpha"], "aliases": []},
            {"name": "Beta Health Index", "description": "b", "subjects": ["health"], "aliases": []},
        ],
        "source_sha256": "different",
    })

    status = lf.regenerate_catalog([_db_result(_TWO_DB_TABLE)], s3)

    assert status["catalog"] == "written"
    bedrock.converse.assert_not_called()
    assert status["enrichment"] == {"ran": False, "reason": "no new databases to enrich"}


# --- Gate 2 + concurrency: one ingestion job at a time, and never lose a change ----------------


def _job(job_id, status, started_at):
    return {"ingestionJobId": job_id, "status": status, "startedAt": started_at}


def test_overlapping_run_defers_instead_of_failing(monkeypatch):
    # Bedrock allows one ingestion job per data source. An overlap must skip cleanly.
    s3, bedrock_agent = _wire(monkeypatch, [_ok()])
    bedrock_agent.list_ingestion_jobs.return_value = {
        "ingestionJobSummaries": [_job("running-job", "IN_PROGRESS", 100)]
    }

    out = lf.handler({}, None)

    bedrock_agent.start_ingestion_job.assert_not_called()
    assert out["ingestionJobId"] is None
    assert out["ingestion"] == "deferred (job running-job in progress)"
    # The upload still happened - only the indexing was deferred.
    assert out["uploaded"] == 1


def test_a_deferred_change_is_picked_up_by_the_next_run(monkeypatch):
    # THE reason deferring is safe. This run changes nothing, so "did I upload?" says no ingestion
    # is needed - but the bucket holds an object newer than the last job, which is exactly what a
    # previously deferred run leaves behind. Without this the deferred change would sit unindexed
    # forever, because no later run would ever see it as changed either.
    s3, bedrock_agent = _wire(monkeypatch, [_ok()])
    stored = lf.content_fingerprint(_ok().markdown, _ok().metadata)
    s3.head_object.return_value = {"Metadata": {lf.CONTENT_HASH_METADATA_KEY: stored}}
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "x-a-hash.md", "LastModified": 200}]}
    ]
    bedrock_agent.list_ingestion_jobs.return_value = {
        "ingestionJobSummaries": [_job("older-job", "COMPLETE", 100)]
    }

    out = lf.handler({}, None)

    assert out["uploaded"] == 0
    bedrock_agent.start_ingestion_job.assert_called_once()
    assert "newer than the last ingestion job" in out["ingestion"]


def test_a_fully_indexed_unchanged_bucket_starts_nothing(monkeypatch):
    s3, bedrock_agent = _wire(monkeypatch, [_ok()])
    stored = lf.content_fingerprint(_ok().markdown, _ok().metadata)
    s3.head_object.return_value = {"Metadata": {lf.CONTENT_HASH_METADATA_KEY: stored}}
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "x-a-hash.md", "LastModified": 100}]}
    ]
    bedrock_agent.list_ingestion_jobs.return_value = {
        "ingestionJobSummaries": [_job("recent-job", "COMPLETE", 200)]
    }

    out = lf.handler({}, None)
    bedrock_agent.start_ingestion_job.assert_not_called()
    assert out["ingestion"] == "skipped (nothing changed)"


def test_a_race_on_start_ingestion_defers_rather_than_raising(monkeypatch):
    # Another run can win between our list and our start, and StartIngestionJob is rate-limited to
    # one per ten seconds. Either way the scrape has already succeeded and must not fail.
    s3, bedrock_agent = _wire(monkeypatch, [_ok()])

    class ConflictException(Exception):
        pass

    bedrock_agent.start_ingestion_job.side_effect = ConflictException("ongoing ingestion job")

    out = lf.handler({}, None)

    assert out["ingestionJobId"] is None
    assert out["ingestion"] == "deferred (ConflictException)"
    assert out["uploaded"] == 1  # the upload is still reported as done


def test_losing_job_history_falls_back_to_the_old_rule(monkeypatch):
    # If ListIngestionJobs is denied or throttled, we must still ingest what we just changed.
    s3, bedrock_agent = _wire(monkeypatch, [_ok()])
    bedrock_agent.list_ingestion_jobs.side_effect = RuntimeError("AccessDenied")

    out = lf.handler({}, None)

    bedrock_agent.start_ingestion_job.assert_called_once()
    assert out["ingestion"] == "started (content changed this run)"
