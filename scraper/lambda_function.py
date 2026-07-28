"""Scraper Lambda handler: config -> scrape_urls -> S3 upload -> start_ingestion_job.

Thin wrapper over scraper.py (the fetch + extract logic is NOT reimplemented here). On each
invocation it scrapes the configured seed URLs, uploads the markdown + a metadata sidecar for
every successful page to the KB's S3 source bucket, and triggers a Bedrock ingestion job so the
knowledge base picks up the fresh content. Partial failures are tolerated: failed URLs are logged
and skipped, and ingestion still runs as long as at least one page was uploaded.

It also PRUNES: objects the configuration no longer calls for are deleted from the source
bucket before ingestion, so removing a page from seed_urls actually removes it from the
knowledge base instead of leaving it indexed forever (see prune_stale_objects).

Runtime wiring (all from stack-set env vars; boto3 from the Lambda runtime, deps from the layer):
  SEED_URLS               JSON array of URLs to scrape
  KB_EXCLUDE_URLS         JSON array of seed URLs to scrape but NOT index (see handler)
  SCRAPE_TIMEOUT_SECONDS  per-request HTTP timeout
  SCRAPER_USER_AGENT      identifying User-Agent
  SOURCE_BUCKET           KB S3 source bucket to upload into
  KNOWLEDGE_BASE_ID       KB to start ingestion on
  DATA_SOURCE_ID          the KB's S3 data source id
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import boto3

from scraper import extract_database_catalog, scrape_urls, slugify_url, validate_held_list

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)


def _s3_client():
    return boto3.client("s3")


def _bedrock_agent_client():
    return boto3.client("bedrock-agent")


def _bedrock_runtime_client():
    return boto3.client("bedrock-runtime")


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


def enrich_held(entries, bedrock_runtime, model_id):
    """Ask the model for {subjects, aliases} per database. `entries` is [{name, description}].
    Returns {normalized_name: {"subjects": [...], "aliases": [...]}} for names it returned; names
    the model omits simply won't appear (the caller defaults them). Never raises for a bad model
    reply - returns {} so the caller can proceed with empty enrichment rather than fail the scrape."""
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


def regenerate_held(html, previous_held, bedrock_runtime, model_id, min_databases):
    """Build the fresh held list from databases.php HTML: parse -> reuse prior enrichment for
    unchanged databases + enrich only new ones -> validate. Returns the held list, or None if the
    parse/validation guard fails (caller keeps the last-good catalog).

    `previous_held` is the held list from the current S3 catalog (or []); its subjects/aliases are
    reused for databases whose names are unchanged, so wording stays stable and the model is only
    called for newly-added databases (often zero)."""
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
    new_enrichment = enrich_held(to_enrich, bedrock_runtime, model_id) if to_enrich else {}

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


def _read_previous_held(s3, bucket, key):
    """The held list from the catalog currently in S3, or [] if absent/unreadable (first run)."""
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        data = json.loads(obj["Body"].read())
        held = data.get("held")
        return held if isinstance(held, list) else []
    except Exception as exc:  # noqa: BLE001 - no prior catalog yet, or transient read error
        LOG.info("no readable previous catalog at s3://%s/%s (%s); enriching all", bucket, key, exc)
        return []


def regenerate_catalog(results, s3, *, timestamp=None):
    """Find the databases.php scrape result, regenerate the held catalog, and write it to S3 -
    unless the robustness guard fails, in which case the last-good catalog is left in place.

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

    previous_held = _read_previous_held(s3, bucket, key)
    held = regenerate_held(
        db_result.html, previous_held, _bedrock_runtime_client(), model_id, min_databases
    )
    if held is None:
        return {"catalog": "guard failed; last-good kept"}

    body = {
        "generated_at": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_url": db_result.url,
        "held": held,
    }
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    LOG.info("catalog written: s3://%s/%s (%d databases)", bucket, key, len(held))
    return {"catalog": "written", "databases": len(held)}


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


def handler(event, context):
    seed_urls = json.loads(os.environ["SEED_URLS"])
    # Pages fetched for their side effects but deliberately kept OUT of the knowledge base.
    # databases.php is the case this exists for: regenerate_catalog parses its HTML to rebuild the
    # held database_catalog, so it must stay in seed_urls - but its content is redundant with that
    # catalog and it is the largest remaining document in the corpus, so it should not be indexed.
    kb_exclude_urls = json.loads(os.environ.get("KB_EXCLUDE_URLS", "[]"))
    timeout = float(os.environ.get("SCRAPE_TIMEOUT_SECONDS", "20"))
    user_agent = os.environ.get("SCRAPER_USER_AGENT") or None
    bucket = os.environ["SOURCE_BUCKET"]
    kb_id = os.environ["KNOWLEDGE_BASE_ID"]
    data_source_id = os.environ["DATA_SOURCE_ID"]

    kwargs = {"timeout": timeout}
    if user_agent:
        kwargs["user_agent"] = user_agent
    results = scrape_urls(seed_urls, **kwargs)

    s3 = _s3_client()
    uploaded_keys: list[str] = []
    # Every object key written this run (markdown AND sidecars) - the prune guard below unions
    # these in so a run can never delete what it just uploaded.
    uploaded_object_keys: list[str] = []
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
        s3.put_object(
            Bucket=bucket,
            Key=md_key,
            Body=result.markdown.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
        s3.put_object(
            Bucket=bucket,
            Key=meta_key,
            Body=_metadata_body(result.metadata),
            ContentType="application/json",
        )
        uploaded_keys.append(md_key)
        uploaded_object_keys.extend((md_key, meta_key))
        LOG.info("uploaded %s (<- %s)", md_key, result.url)

    # Drop anything configuration no longer calls for, BEFORE ingestion, so the same job that
    # indexes the new content also retires the removed content.
    #
    # The uploaded keys are unioned in as a belt-and-braces guard: expected_kb_keys derives slugs
    # from the seed URL while the uploader uses result.slug, and although both slugify the same
    # input today, any future divergence (a redirect used for slugging, a slug scheme change)
    # would otherwise make the prune delete the very objects this run just wrote. Whatever we
    # uploaded is by definition wanted.
    expected = expected_kb_keys(seed_urls, kb_exclude_urls) | set(uploaded_object_keys)
    pruned_keys = prune_stale_objects(s3, bucket, expected)

    LOG.info(
        "scrape complete: %d uploaded, %d failed, %d pruned",
        len(uploaded_keys),
        len(failures),
        len(pruned_keys),
    )

    ingestion_job_id = None
    if uploaded_keys or pruned_keys:
        response = _bedrock_agent_client().start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id,
            description="Automated scraper re-sync",
        )
        ingestion_job_id = response["ingestionJob"]["ingestionJobId"]
        LOG.info("started ingestion job %s", ingestion_job_id)
    else:
        LOG.warning("no pages uploaded - skipping ingestion job")

    # Regenerate the database catalog from the databases.php scrape (Phase 2b). Wrapped so ANY
    # catalog failure is logged but never breaks the KB scrape/ingestion above - the robustness
    # guard inside also keeps the last-good catalog rather than writing garbage.
    catalog_status = {"catalog": "not attempted"}
    try:
        catalog_status = regenerate_catalog(results, s3)
    except Exception as exc:  # noqa: BLE001 - catalog is best-effort relative to the KB scrape
        LOG.exception("catalog regeneration failed (ignored): %s", exc)
        catalog_status = {"catalog": f"error: {type(exc).__name__}"}

    return {
        "uploaded": len(uploaded_keys),
        "pruned": pruned_keys,
        "failed": failures,
        "ingestionJobId": ingestion_job_id,
        **catalog_status,
    }
