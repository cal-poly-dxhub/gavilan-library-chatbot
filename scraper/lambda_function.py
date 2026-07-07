"""Scraper Lambda handler: config -> scrape_urls -> S3 upload -> start_ingestion_job.

Thin wrapper over scraper.py (the fetch + extract logic is NOT reimplemented here). On each
invocation it scrapes the configured seed URLs, uploads the markdown + a metadata sidecar for
every successful page to the KB's S3 source bucket, and triggers a Bedrock ingestion job so the
knowledge base picks up the fresh content. Partial failures are tolerated: failed URLs are logged
and skipped, and ingestion still runs as long as at least one page was uploaded.

Runtime wiring (all from stack-set env vars; boto3 from the Lambda runtime, deps from the layer):
  SEED_URLS               JSON array of URLs to scrape
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

import boto3

from scraper import scrape_urls

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)


def _s3_client():
    return boto3.client("s3")


def _bedrock_agent_client():
    return boto3.client("bedrock-agent")


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


def handler(event, context):
    seed_urls = json.loads(os.environ["SEED_URLS"])
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
    failures: list[dict] = []
    for result in results:
        if not result.ok:
            LOG.warning("scrape failed: %s (%s)", result.url, result.error)
            failures.append({"url": result.url, "error": result.error})
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
        LOG.info("uploaded %s (<- %s)", md_key, result.url)

    LOG.info(
        "scrape complete: %d uploaded, %d failed", len(uploaded_keys), len(failures)
    )

    ingestion_job_id = None
    if uploaded_keys:
        response = _bedrock_agent_client().start_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id,
            description="Automated scraper re-sync",
        )
        ingestion_job_id = response["ingestionJob"]["ingestionJobId"]
        LOG.info("started ingestion job %s", ingestion_job_id)
    else:
        LOG.warning("no pages uploaded - skipping ingestion job")

    return {
        "uploaded": len(uploaded_keys),
        "failed": failures,
        "ingestionJobId": ingestion_job_id,
    }
