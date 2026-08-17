"""Scraper Lambda handler: tier -> scrape_urls -> gated S3 upload -> gated start_ingestion_job.

Thin wrapper over scraper.py (the fetch + extract logic is NOT reimplemented here). On each
invocation it scrapes ONE freshness tier's URLs, uploads the markdown + a metadata sidecar for
every page whose content actually CHANGED, and triggers a Bedrock ingestion job only when the
source bucket actually moved. Partial failures are tolerated: failed URLs are logged and skipped,
and the run continues.

TIERS. Cadence and membership are declared entirely in config.yaml (`scraper.tiers`) and arrive
here as the SCRAPER_TIERS env var; there is one EventBridge rule per tier, each passing
{"tier": "<name>"} as its event. A named tier fetches only its own URLs; the `full` tier - and an
invocation with no tier at all, which is what the one-shot deploy Trigger sends - fetches
everything. See scraper.urls_for_tier.

CHANGE GATING, three gates, because the site changes far less often than we now look at it:
  1. Upload only what changed. Each markdown object carries a `content-sha256` of exactly what
     this scraper produced for that page; a page whose fingerprint matches what is already in the
     bucket is not re-uploaded. (Bedrock's sync is incremental on its own, but re-PUTting an
     object still moves its LastModified and re-parses it, so gating here is what makes an
     unchanged run genuinely free.)
  2. Ingest only when something moved. No uploads and no prunes -> no ingestion job.
  3. Enrich only when the databases page changed. The Sonnet catalog call is the one meaningful
     per-run cost in this whole path, so it is gated on a fingerprint of the PARSED database rows
     stored in the catalog object itself. See regenerate_catalog.

It also PRUNES: objects the configuration no longer calls for are deleted from the source
bucket before ingestion, so removing a page from a tier actually removes it from the
knowledge base instead of leaving it indexed forever (see prune_stale_objects).

NO NEW STORE for any of this. Change detection reads S3 object metadata (the per-object
fingerprint), S3 object LastModified, and Bedrock's own ingestion-job history. There is no
DynamoDB table, no state file, and nothing to back up or clean up.

Runtime wiring (all from stack-set env vars; boto3 from the Lambda runtime, deps from the layer):
  SCRAPER_TIERS           JSON {tier: {"schedule_cron": ..., "urls": [...]}} - all tiers
  KB_EXCLUDE_URLS         JSON array of seed URLs to scrape but NOT index (see handler)
  SCRAPE_TIMEOUT_SECONDS  per-request HTTP timeout
  SCRAPER_USER_AGENT      identifying User-Agent
  SOURCE_BUCKET           KB S3 source bucket to upload into
  KNOWLEDGE_BASE_ID       KB to start ingestion on
  DATA_SOURCE_ID          the KB's S3 data source id
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import boto3

from scraper import (
    TIER_FULL,
    all_seed_urls,
    extract_database_catalog,
    scrape_urls,
    slugify_url,
    urls_for_tier,
    validate_held_list,
)

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

# S3 user-metadata key holding the fingerprint of what this scraper produced for a page. Written
# on every markdown PUT, read back to decide whether the next run needs to PUT at all. boto3
# lowercases user-metadata keys on the way out of head_object, so keep this spelling lowercase.
CONTENT_HASH_METADATA_KEY = "content-sha256"

# Ingestion-job statuses that mean the data source is still occupied. Bedrock allows exactly one
# job at a time, so seeing any of these means we defer rather than call and fail. STOPPING counts:
# the job has not released the data source yet. (The full enum is STARTING, IN_PROGRESS, COMPLETE,
# FAILED, STOPPING, STOPPED - verified against the botocore service model.)
ACTIVE_INGESTION_STATUSES = ("STARTING", "IN_PROGRESS", "STOPPING")


def _s3_client():
    return boto3.client("s3")


def _bedrock_agent_client():
    return boto3.client("bedrock-agent")


def _bedrock_runtime_client():
    return boto3.client("bedrock-runtime")


# --- Change detection ----------------------------------------------------------------------
#
# The whole tiered schedule rests on one property: a page whose content has not changed must
# produce the SAME fingerprint run after run. Verified against the live site on 2026-07-29 by
# scraping all 19 seed URLs twice - every extracted markdown body was byte-identical. The only
# field that moved was the sidecar's scrape_timestamp, which is why the fingerprint below covers
# the document body and the two metadata fields Bedrock actually consumes, and deliberately NOT
# the timestamp. Hashing the timestamp would mark every page changed on every run and turn the
# gating into an expensive no-op.


def content_fingerprint(markdown, metadata) -> str:
    """Stable sha256 over everything a run would upload for one page.

    Covers the markdown body plus the sidecar fields that reach the knowledge base (source_url
    and title, which drive citation attribution), so a title change re-uploads even when the body
    is untouched. Excludes scrape_timestamp, which changes on every run by definition.
    """
    metadata = metadata or {}
    payload = "\x00".join(
        [
            markdown or "",
            metadata.get("source_url") or "",
            metadata.get("title") or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stored_content_fingerprint(s3, bucket, key):
    """The fingerprint recorded on the markdown object already in the bucket, or None.

    None means "treat this page as changed": the object is absent (first run), or it predates
    change gating and carries no fingerprint, or S3 could not be read. Every one of those wants
    an upload, so the failure direction is a redundant PUT rather than a missed update.
    """
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - absent object is the normal case, not an error
        LOG.debug("no stored fingerprint for %s (%s)", key, exc)
        return None
    value = (head.get("Metadata") or {}).get(CONTENT_HASH_METADATA_KEY)
    return value if isinstance(value, str) else None


def latest_object_modified(s3, bucket):
    """The newest LastModified across the source bucket, or None if unreadable/empty.

    Paired with the last ingestion job's start time this answers "does the bucket hold content
    the knowledge base has not indexed yet?" without storing anything of our own - which is what
    makes a deferred ingestion self-healing. See should_start_ingestion.
    """
    latest = None
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents") or []:
                modified = obj.get("LastModified")
                if modified is not None and (latest is None or modified > latest):
                    latest = modified
    except Exception as exc:  # noqa: BLE001 - falls back to the this-run-changed-something rule
        LOG.warning("could not read bucket modification times (%s); ignoring", exc)
        return None
    return latest


# --- Database-catalog regeneration (Phase 2b) ----------------------------------------------
#
# The held portion of the database catalog is DERIVED from databases.php on every scrape:
#   1. extract_database_catalog() parses the raw HTML into {name, description, url} deterministically;
#   2. a Bedrock model enriches each database with subjects + aliases (judgment the page lacks) -
#      ONLY for databases not already enriched in the previous catalog (stability + ~zero cost on
#      unchanged weeks), and CONSTRAINED to the parsed names (the model can't add/rename databases);
#   3. validate_held_list() guards the result; on failure we keep the last-good catalog (no write).
# The hand-authored NOT-HELD list is NOT produced here - it lives in the query Lambda bundle and is
# merged in at read time (you can't scrape absence).


def _norm(text):
    return " ".join("".join(c if c.isalnum() else " " for c in (text or "").lower()).split())


_ENRICH_INSTRUCTIONS = (
    "You are enriching a library's research-database catalog. For EACH database given (by name and "
    "description), return its subject tags and common aliases. Rules: use ONLY the exact database "
    "names provided - do not add, remove, rename, or invent databases. `subjects` is a list of "
    "lowercase topic keywords a student might search (broad and specific, e.g. \"business\", "
    "\"nursing\", \"psychology\", \"history\", \"criminal justice\"), inferred from the name and "
    "description. `aliases` is a list of alternate names, abbreviations, or short forms people use "
    "for that database (e.g. \"EBSCO\" for EbscoHost Core Search); [] if none. Return ONLY a JSON "
    'array of objects: [{"name": <exact name>, "subjects": [...], "aliases": [...]}].'
)


def _record_enrichment_usage(usage, response, model_id, database_count):
    """Copy Bedrock's reported token counts for the enrichment call into `usage`.

    Deliberately reads Converse's own `usage` block rather than estimating from payload length:
    the point of logging this is to REPLACE an estimate with a measurement. Missing/odd fields
    degrade to 0 rather than raising - a metrics detail must never break the catalog.
    """
    reported = (response or {}).get("usage") or {}

    def _count(field):
        try:
            return int(reported.get(field) or 0)
        except (TypeError, ValueError):
            return 0

    # Replace rather than merge: the caller seeds this dict with a did-not-run reason, and leaving
    # that behind next to ran=True produces a status line that contradicts itself in the logs.
    usage.clear()
    usage.update(
        {
            "ran": True,
            "at": _utc_now(),
            "model_id": model_id,
            "databases_enriched": database_count,
            "input_tokens": _count("inputTokens"),
            "output_tokens": _count("outputTokens"),
            "total_tokens": _count("totalTokens"),
        }
    )


def _utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_json_array(text):
    """Pull the first JSON array out of a model response (tolerates prose/code fences around it)."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) else None


def enrich_held(entries, bedrock_runtime, model_id, usage=None):
    """Ask the model for {subjects, aliases} per database. `entries` is [{name, description}].
    Returns {normalized_name: {"subjects": [...], "aliases": [...]}} for names it returned; names
    the model omits simply won't appear (the caller defaults them). Never raises for a bad model
    reply - returns {} so the caller can proceed with empty enrichment rather than fail the scrape.

    Pass `usage` (a dict) to have the call's REAL billable token counts written into it. This is
    the only model call in the ingestion path and it was previously invisible - its cost was an
    estimate - so the run summary reports what Bedrock actually charged rather than a guess."""
    if not entries:
        return {}
    payload = json.dumps([{"name": e["name"], "description": e.get("description", "")} for e in entries])
    try:
        response = bedrock_runtime.converse(
            modelId=model_id,
            system=[{"text": _ENRICH_INSTRUCTIONS}],
            messages=[{"role": "user", "content": [{"text": payload}]}],
            inferenceConfig={"maxTokens": 4000, "temperature": 0.0},
        )
        if usage is not None:
            _record_enrichment_usage(usage, response, model_id, len(entries))
        text = ""
        for block in response.get("output", {}).get("message", {}).get("content", []) or []:
            if isinstance(block, dict) and "text" in block:
                text += block["text"]
        rows = _parse_json_array(text) or []
    except Exception as exc:  # noqa: BLE001 - enrichment is best-effort; never break the scrape
        LOG.warning("catalog enrichment call failed (%s); proceeding without new subjects", exc)
        return {}

    given = {_norm(e["name"]) for e in entries}
    enriched = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _norm(row.get("name"))
        if key not in given:  # constrain to the deterministic name set; drop hallucinated names
            continue
        subjects = [str(s).strip().lower() for s in (row.get("subjects") or []) if str(s).strip()]
        aliases = [str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()]
        enriched[key] = {"subjects": subjects, "aliases": aliases}
    return enriched


def catalog_source_fingerprint(parsed) -> str:
    """Fingerprint of the databases-page CONTENT the catalog is derived from.

    Hashes the PARSED rows (name / description / url), not the raw HTML. Enrichment only ever
    sees those three fields, so markup churn - a reordered nav, a rotating banner, a cache-busting
    query string on an asset - must not be able to look like a catalog change and bill a Sonnet
    call. sort_keys makes the encoding stable; row ORDER is significant and is preserved, since a
    reordered A-Z table is a real change to what the catalog publishes.
    """
    return hashlib.sha256(
        json.dumps(parsed or [], sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def regenerate_held(html, previous_held, bedrock_runtime, model_id, min_databases, usage=None):
    """Build the fresh held list from databases.php HTML: parse -> reuse prior enrichment for
    unchanged databases + enrich only new ones -> validate. Returns the held list, or None if the
    parse/validation guard fails (caller keeps the last-good catalog).

    `previous_held` is the held list from the current S3 catalog (or []); its subjects/aliases are
    reused for databases whose names are unchanged, so wording stays stable and the model is only
    called for newly-added databases (often zero). `usage` is the optional token-count sink passed
    through to enrich_held."""
    parsed = extract_database_catalog(html)
    if not validate_held_list(parsed, min_databases):
        LOG.error(
            "catalog extraction produced only %d entries (< min %d) or malformed rows; "
            "KEEPING LAST-GOOD catalog, not overwriting",
            len(parsed) if isinstance(parsed, list) else -1, min_databases,
        )
        return None

    prior = {_norm(e.get("name")): e for e in (previous_held or [])}
    to_enrich = [e for e in parsed if not prior.get(_norm(e["name"]), {}).get("subjects")]
    LOG.info("catalog: %d databases parsed, %d need enrichment (rest reused)", len(parsed), len(to_enrich))
    new_enrichment = (
        enrich_held(to_enrich, bedrock_runtime, model_id, usage=usage) if to_enrich else {}
    )

    held = []
    for e in parsed:
        key = _norm(e["name"])
        prev = prior.get(key, {})
        enr = new_enrichment.get(key, {})
        held.append({
            "name": e["name"],
            "description": e.get("description", ""),
            "url": e.get("url", ""),
            "subjects": enr.get("subjects") or prev.get("subjects") or [],
            "aliases": enr.get("aliases") or prev.get("aliases") or [],
        })

    if not validate_held_list(held, min_databases):
        LOG.error("catalog held list failed validation AFTER enrichment; KEEPING LAST-GOOD")
        return None
    return held


def _read_previous_catalog(s3, bucket, key):
    """The catalog object currently in S3 as a dict, or {} if absent/unreadable (first run).

    Two things are read back out of it: the held list (whose subjects/aliases are reused so the
    model is only asked about genuinely new databases) and `source_sha256` (the fingerprint of the
    databases-page rows that produced it, which is how the next run knows whether to do anything
    at all). Storing the fingerprint INSIDE the catalog it describes is what keeps this
    store-free - there is no side table to keep in sync with the object.
    """
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = json.loads(obj["Body"].read())
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - no prior catalog yet, or transient read error
        LOG.info("no readable previous catalog at s3://%s/%s (%s); enriching all", bucket, key, exc)
        return {}


def regenerate_catalog(results, s3, *, timestamp=None):
    """Find the databases.php scrape result, regenerate the held catalog, and write it to S3 -
    unless nothing changed, or the robustness guard fails, in which cases the catalog already in
    S3 is left exactly as it is.

    THE ENRICHMENT GATE lives here. The Sonnet call this function can make is the only meaningful
    per-run cost in the ingestion path, so before doing anything expensive the parsed rows are
    fingerprinted and compared against the fingerprint stored in the previous catalog. Identical
    rows -> return immediately: no model call, no S3 write, catalog untouched. Skipping the write
    matters as much as skipping the call, because the body carries a `generated_at` timestamp and
    rewriting it every run would churn the object forever for no content change.

    ORDER IS DELIBERATE: parse, then the min-count/required-field guard, then the fingerprint
    comparison. A broken or restructured page must be rejected by the guard BEFORE its fingerprint
    can be recorded, or one bad scrape would poison the comparison and freeze the catalog.

    Reads its config from env (set by the stack): CATALOG_BUCKET (required to do anything),
    CATALOG_KEY, CATALOG_ENRICHMENT_MODEL_ID, CATALOG_MIN_DATABASES. Returns a small status dict
    for logging. Callers wrap this so a catalog failure never breaks the KB scrape."""
    bucket = os.environ.get("CATALOG_BUCKET")
    if not bucket:
        return {"catalog": "skipped (no CATALOG_BUCKET)"}
    key = os.environ.get("CATALOG_KEY", "database_catalog.json")
    model_id = os.environ.get("CATALOG_ENRICHMENT_MODEL_ID", "")
    min_databases = int(os.environ.get("CATALOG_MIN_DATABASES", "30"))

    db_result = next(
        (r for r in results if r.ok and "databases.php" in r.url and r.html), None
    )
    if db_result is None:
        LOG.error("no successful databases.php scrape with HTML; KEEPING LAST-GOOD catalog")
        return {"catalog": "no databases.php html"}

    # Guard first: an extraction this thin is a broken page, not a catalog that shrank.
    parsed = extract_database_catalog(db_result.html)
    if not validate_held_list(parsed, min_databases):
        LOG.error(
            "catalog extraction produced only %d entries (< min %d) or malformed rows; "
            "KEEPING LAST-GOOD catalog, not overwriting",
            len(parsed) if isinstance(parsed, list) else -1, min_databases,
        )
        return {"catalog": "guard failed; last-good kept"}

    previous = _read_previous_catalog(s3, bucket, key)
    previous_held = previous.get("held") if isinstance(previous.get("held"), list) else []
    fingerprint = catalog_source_fingerprint(parsed)
    if previous_held and previous.get("source_sha256") == fingerprint:
        LOG.info(
            "catalog source unchanged (%d databases, %s); skipping enrichment and S3 write",
            len(parsed), fingerprint[:12],
        )
        return {
            "catalog": "unchanged; enrichment skipped",
            "databases": len(previous_held),
            "enrichment": {"ran": False, "reason": "databases page unchanged"},
        }

    usage = {"ran": False, "reason": "no new databases to enrich"}
    held = regenerate_held(
        db_result.html,
        previous_held,
        _bedrock_runtime_client(),
        model_id,
        min_databases,
        usage=usage,
    )
    if held is None:
        return {"catalog": "guard failed; last-good kept", "enrichment": usage}

    body = {
        "generated_at": timestamp or _utc_now(),
        "source_url": db_result.url,
        # The gate for the next run. Written alongside the data it describes so the two can
        # never disagree: if this object is rolled back or deleted, its fingerprint goes with it.
        "source_sha256": fingerprint,
        "held": held,
    }
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    LOG.info("catalog written: s3://%s/%s (%d databases)", bucket, key, len(held))
    return {"catalog": "written", "databases": len(held), "enrichment": usage}


def _metadata_body(metadata: dict) -> bytes:
    """Bedrock S3 metadata sidecar body.

    Uploaded as `<slug>.md.metadata.json`, NOT `<slug>.json`: Bedrock treats `*.metadata.json` as
    METADATA for the sibling document and does NOT ingest it as its own document. A plain
    `<slug>.json` (the local scraper's inspection filename) WOULD be ingested as a JSON document,
    polluting the KB. The wrapper is Bedrock's documented `metadataAttributes` shape; the fields
    drive source-URL attribution on retrieved chunks.
    """
    attributes = {
        "source_url": metadata.get("source_url", ""),
        "title": metadata.get("title") or "",
        "scrape_timestamp": metadata.get("scrape_timestamp", ""),
    }
    return json.dumps({"metadataAttributes": attributes}).encode("utf-8")


def expected_kb_keys(seed_urls, kb_exclude_urls):
    """Every object key the KB source bucket SHOULD hold after a healthy run.

    Derived from configuration (the seed list minus the KB-excluded pages), NOT from what this
    run happened to upload - see prune_stale_objects for why that distinction is load-bearing."""
    excluded = set(kb_exclude_urls or [])
    keys = set()
    for url in seed_urls:
        if url in excluded:
            continue
        slug = slugify_url(url)
        keys.add(f"{slug}.md")
        keys.add(f"{slug}.md.metadata.json")
    return keys


def prune_stale_objects(s3, bucket, expected_keys):
    """Delete KB source objects that configuration no longer calls for. Returns the deleted keys.

    WHY THIS EXISTS: the uploader only ever put_objects, so before this a page removed from
    seed_urls simply stopped being refreshed while its document stayed in the bucket and stayed
    indexed forever. De-seeding was a silent no-op.

    WHY IT PRUNES AGAINST CONFIG, NOT AGAINST THIS RUN'S UPLOADS: pruning by what succeeded would
    make one transient fetch failure delete that page from the knowledge base - a 404 on
    howdoi.php for a single week would drop every checkout answer the bot has until someone
    noticed. Keying on the configured seed list means a failed fetch leaves the last-good document
    in place, the same posture regenerate_catalog already takes with the database catalog.

    Never raises: a prune failure must not break a scrape that has already succeeded."""
    deleted: list[str] = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if key in expected_keys:
                    continue
                s3.delete_object(Bucket=bucket, Key=key)
                deleted.append(key)
                LOG.info("pruned stale KB object: %s", key)
    except Exception as exc:  # noqa: BLE001 - pruning is housekeeping, never fatal
        LOG.exception("prune failed (ignored): %s", exc)
    return deleted


# --- Ingestion: one job at a time, and never lose a change ---------------------------------
#
# Two schedules can in principle fire close together, and Bedrock allows exactly one ingestion job
# per data source (StartIngestionJob is also rate-limited to one per ten seconds). So an overlap
# has to SKIP cleanly rather than throw.
#
# Skipping is only safe if the skipped change is picked up later, and "this run uploaded
# something" cannot provide that: the next run finds the page unchanged, uploads nothing, and
# would start no job - leaving content sitting in the bucket unindexed indefinitely. The fix is a
# second, store-free signal: compare the newest object in the bucket against the start time of the
# last ingestion job Bedrock ran. Anything newer is unindexed, whoever put it there and whenever.
# A deferred run therefore heals itself on the next tier run, which is at most a day away.


def _ingestion_job_summaries(bedrock_agent, kb_id, data_source_id, max_results=20):
    """Recent ingestion jobs for this data source, newest first. [] if unreadable.

    Soft-fails on purpose: losing visibility of job history must degrade the decision below to
    "start when this run changed something" (the old behaviour), never block ingestion outright.
    """
    try:
        response = bedrock_agent.list_ingestion_jobs(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id,
            sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
            maxResults=max_results,
        )
        summaries = response.get("ingestionJobSummaries")
        return list(summaries) if summaries else []
    except Exception as exc:  # noqa: BLE001
        LOG.warning("could not list ingestion jobs (%s); proceeding without job history", exc)
        return []


def active_ingestion_job(summaries):
    """The id of a job currently STARTING/IN_PROGRESS, or None."""
    for job in summaries:
        if job.get("status") in ACTIVE_INGESTION_STATUSES:
            return job.get("ingestionJobId")
    return None


def last_ingestion_started_at(summaries):
    """When the most recent job STARTED, or None if there is no history.

    Uses started-at rather than completed-at because a job indexes the bucket as of roughly when
    it began; comparing against the start time can only over-trigger (a harmless extra incremental
    sync), while comparing against completion could skip an object written mid-job.
    """
    started = None
    for job in summaries:
        at = job.get("startedAt")
        if at is not None and (started is None or at > started):
            started = at
    return started


def should_start_ingestion(changed_this_run, bucket_latest, last_started):
    """Whether to start an ingestion job, and the human-readable reason. Never raises."""
    if changed_this_run:
        return True, "content changed this run"
    if bucket_latest is not None and last_started is not None and bucket_latest > last_started:
        # Something got into the bucket that no ingestion job has covered - almost always a
        # previous run that deferred because a job was already running.
        return True, "bucket holds objects newer than the last ingestion job"
    return False, "nothing changed"


def start_ingestion(bedrock_agent, kb_id, data_source_id, summaries, changed_this_run, bucket_latest):
    """Start an ingestion job if one is warranted and none is running. Returns (job_id, status).

    Never raises: an ingestion problem must not fail a scrape whose uploads already landed, and
    the bucket-newer-than-last-job rule above means the next run retries on its own.
    """
    running = active_ingestion_job(summaries)
    if running:
        LOG.info("ingestion job %s already running; deferring to the next scheduled run", running)
        return None, f"deferred (job {running} in progress)"

    wanted, reason = should_start_ingestion(
        changed_this_run, bucket_latest, last_ingestion_started_at(summaries)
    )
    if not wanted:
        LOG.info("no ingestion needed: %s", reason)
        return None, f"skipped ({reason})"

    try:
        response = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id,
            description="Automated scraper re-sync",
        )
        job_id = response["ingestionJob"]["ingestionJobId"]
        LOG.info("started ingestion job %s (%s)", job_id, reason)
        return job_id, f"started ({reason})"
    except Exception as exc:  # noqa: BLE001
        # A job that raced us between the list and the start, or a throttle on the one-per-ten-
        # seconds limit, lands here. Both are the overlap case and both self-heal next run.
        LOG.warning("could not start ingestion job (%s: %s); deferring", type(exc).__name__, exc)
        return None, f"deferred ({type(exc).__name__})"


def _requested_tier(event):
    """Which freshness tier this invocation is for.

    EventBridge sends {"tier": "<name>"} (one rule per tier, the input configured by the stack).
    Anything else - the one-shot deploy Trigger, a console test invoke, a scheduled rule someone
    added by hand - has no tier, and falls back to the complete sweep.
    """
    if isinstance(event, dict):
        tier = event.get("tier")
        if isinstance(tier, str) and tier.strip():
            return tier.strip()
    return TIER_FULL


def handler(event, context):
    # Which tier, and therefore which URLs. `tiers` is the WHOLE map, not just this tier's slice:
    # the prune below has to reason about the corpus configuration defines, not about the third of
    # it a fast run happened to fetch.
    tiers = json.loads(os.environ["SCRAPER_TIERS"])
    tier = _requested_tier(event)
    scrape_list = urls_for_tier(tiers, tier)
    seed_urls = all_seed_urls(tiers)
    # Pages fetched for their side effects but deliberately kept OUT of the knowledge base.
    # databases.php is the case this exists for: regenerate_catalog parses its HTML to rebuild the
    # held database_catalog, so it must stay in the seed list - but its content is redundant with
    # that catalog and it is the largest remaining document in the corpus, so it is not indexed.
    kb_exclude_urls = json.loads(os.environ.get("KB_EXCLUDE_URLS", "[]"))
    timeout = float(os.environ.get("SCRAPE_TIMEOUT_SECONDS", "20"))
    user_agent = os.environ.get("SCRAPER_USER_AGENT") or None
    bucket = os.environ["SOURCE_BUCKET"]
    kb_id = os.environ["KNOWLEDGE_BASE_ID"]
    data_source_id = os.environ["DATA_SOURCE_ID"]

    LOG.info("tier=%s scraping %d of %d configured URL(s)", tier, len(scrape_list), len(seed_urls))
    kwargs = {"timeout": timeout}
    if user_agent:
        kwargs["user_agent"] = user_agent
    results = scrape_urls(scrape_list, **kwargs)

    s3 = _s3_client()
    uploaded_keys: list[str] = []
    # Every object key this run confirmed is LIVE (markdown AND sidecars), whether it was
    # re-uploaded or found unchanged. The prune guard below unions these in, so a run can never
    # delete a page it just looked at. Change gating made the "found unchanged" half necessary:
    # before it, every successful page was re-uploaded on every run, so the uploaded set happened
    # to cover the whole corpus and the distinction did not exist.
    live_object_keys: list[str] = []
    unchanged_keys: list[str] = []
    failures: list[dict] = []
    excluded = set(kb_exclude_urls)
    for result in results:
        if not result.ok:
            LOG.warning("scrape failed: %s (%s)", result.url, result.error)
            failures.append({"url": result.url, "error": result.error})
            continue
        if result.url in excluded:
            # Still scraped (regenerate_catalog needs the HTML); just never indexed.
            LOG.info("kb-excluded, not uploaded: %s", result.url)
            continue
        md_key = f"{result.slug}.md"
        meta_key = f"{result.slug}.md.metadata.json"

        # GATE 1: upload only what changed. The sidecar rides with the markdown rather than being
        # gated separately - it carries a scrape_timestamp that moves every run, so gating it on
        # its own bytes would re-upload all of them forever and re-trigger ingestion each time.
        fingerprint = content_fingerprint(result.markdown, result.metadata)
        if stored_content_fingerprint(s3, bucket, md_key) == fingerprint:
            unchanged_keys.append(md_key)
            live_object_keys.extend((md_key, meta_key))
            LOG.info("unchanged, not uploaded: %s (<- %s)", md_key, result.url)
            continue

        s3.put_object(
            Bucket=bucket,
            Key=md_key,
            Body=result.markdown.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
            Metadata={CONTENT_HASH_METADATA_KEY: fingerprint},
        )
        s3.put_object(
            Bucket=bucket,
            Key=meta_key,
            Body=_metadata_body(result.metadata),
            ContentType="application/json",
        )
        uploaded_keys.append(md_key)
        live_object_keys.extend((md_key, meta_key))
        LOG.info("uploaded %s (<- %s)", md_key, result.url)

    # Drop anything configuration no longer calls for, BEFORE ingestion, so the same job that
    # indexes the new content also retires the removed content.
    #
    # KEYED ON THE FULL SEED LIST ACROSS ALL TIERS, never on this run's scrape list: a daily fast
    # run fetches three pages, and pruning against those would delete the rest of the corpus.
    #
    # The live keys are unioned in as a belt-and-braces guard: expected_kb_keys derives slugs
    # from the seed URL while the uploader uses result.slug, and although both slugify the same
    # input today, any future divergence (a redirect used for slugging, a slug scheme change)
    # would otherwise make the prune delete the very objects this run just confirmed. Whatever
    # this run saw as a live page is by definition wanted - including the ones it left alone
    # because they had not changed.
    expected = expected_kb_keys(seed_urls, kb_exclude_urls) | set(live_object_keys)
    pruned_keys = prune_stale_objects(s3, bucket, expected)

    LOG.info(
        "scrape complete [tier=%s]: %d uploaded, %d unchanged, %d failed, %d pruned",
        tier,
        len(uploaded_keys),
        len(unchanged_keys),
        len(failures),
        len(pruned_keys),
    )

    # GATE 2: ingest only when the bucket actually moved (or still holds unindexed content).
    bedrock_agent = _bedrock_agent_client()
    summaries = _ingestion_job_summaries(bedrock_agent, kb_id, data_source_id)
    changed_this_run = bool(uploaded_keys or pruned_keys)
    # Only worth asking when this run changed nothing: that is the case where the answer decides
    # between "genuinely nothing to do" and "a previous run deferred and left content unindexed".
    bucket_latest = None if changed_this_run else latest_object_modified(s3, bucket)
    ingestion_job_id, ingestion_status = start_ingestion(
        bedrock_agent,
        kb_id,
        data_source_id,
        summaries,
        changed_this_run=changed_this_run,
        bucket_latest=bucket_latest,
    )

    # GATE 3: the database catalog, and with it the only model call in this path.
    #
    # Attempted ONLY when this run actually fetched databases.php - which the fast tier never
    # does, so a daily run cannot reach the enrichment at all. Wrapped so ANY catalog failure is
    # logged but never breaks the KB scrape/ingestion above; the robustness guard inside also
    # keeps the last-good catalog rather than writing garbage.
    catalog_status = {"catalog": "skipped (databases.php not in this tier)"}
    if any("databases.php" in r.url for r in results):
        try:
            catalog_status = regenerate_catalog(results, s3)
        except Exception as exc:  # noqa: BLE001 - catalog is best-effort relative to the scrape
            LOG.exception("catalog regeneration failed (ignored): %s", exc)
            catalog_status = {"catalog": f"error: {type(exc).__name__}"}

    # One structured line per run, so "how fresh is the bot, and what did that cost?" is a log
    # query rather than an estimate. enrichment carries the REAL token counts Bedrock reported
    # when the model was called, and {"ran": false} plus a reason when it was not.
    summary = {
        "tier": tier,
        "pages_fetched": len(results),
        "pages_changed": len(uploaded_keys),
        "pages_unchanged": len(unchanged_keys),
        "pages_failed": len(failures),
        "objects_pruned": len(pruned_keys),
        "ingestion": ingestion_status,
        "ingestion_job_id": ingestion_job_id,
        "catalog": catalog_status.get("catalog"),
        "enrichment": catalog_status.get("enrichment", {"ran": False, "reason": "not attempted"}),
    }
    LOG.info("scrape run summary: %s", json.dumps(summary, sort_keys=True, default=str))

    return {
        "uploaded": len(uploaded_keys),
        "unchanged": len(unchanged_keys),
        "pruned": pruned_keys,
        "failed": failures,
        "ingestionJobId": ingestion_job_id,
        "ingestion": ingestion_status,
        "summary": summary,
        **catalog_status,
    }
