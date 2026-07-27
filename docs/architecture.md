# Gavilan Library Chatbot — Architecture

**What:** RAG chatbot for Gavilan College Library. Answers operational questions (hours, checkout, textbooks, "what does the library offer"), routes research questions and out-of-scope issues.
**Client:** Gavilan College Library via Cal Poly DxHub.
**Status:** deployed to the gavilan AWS account; validated end-to-end (query path, four tools, multi-turn, guardrails).
**Updated:** 2026-07-15

---

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Ingestion | Scraper Lambda -> S3 source bucket -> Bedrock KB (S3 data source) | Curated seed URLs scraped to clean markdown; KB re-ingests each run. Weekly EventBridge schedule + one-click deploy Trigger. |
| RAG engine | Bedrock Managed Knowledge Base | Managed chunk/embed/store. FIXED_SIZE chunking (300 tokens, 20% overlap). |
| Vector store | Amazon S3 Vectors | `s3vectors.CfnVectorBucket` + `CfnIndex`; KB `StorageConfiguration` type `S3_VECTORS`, referenced by `IndexArn`. 1024-dim, cosine, `float32`, semantic-only. |
| Embeddings | Titan Text Embeddings v2 (1024-dim) | Managed default. |
| Query path | Agentic `Converse` tool-use loop (Lambda) | `run_agent`: the model calls tools; the loop feeds each `toolResult` back until `end_turn` (iteration cap). Multi-turn `messages` seed; system prompt via the Converse `system` param. |
| Tools | `search_library_info`, `database_catalog`, `search_book_catalog`, `search_course_reserves` | KB retrieval for general questions; authoritative research-database availability + subject listing; live Primo general-catalog and course-reserves search. Routing via tool descriptions + system-prompt guidance + `toolChoice` auto. |
| Live catalog (external) | Ex Libris Primo discovery API | `search_book_catalog` + `search_course_reserves` call Primo directly (search + per-record availability/delivery call). Outbound HTTPS to a third party, not an AWS API (no IAM); query Lambda needs outbound internet. Timed out + soft-fail so a slow/broken Primo never kills `/query`. |
| LLM (generation) | Bedrock-hosted Claude Sonnet 4.6 via Converse | Invoked through a `us.`-prefixed cross-region inference profile. |
| Database catalog | Self-updating JSON in a dedicated S3 bucket | Scraper derives the held list from databases.php (HTML anchor parse + Sonnet enrichment for subjects/aliases); the hand-authored not-held list is a bundled seed merged at read time; the tool reads from S3 with per-container TTL caching. |
| Orchestration/API | Lambda + HTTP API (API Gateway v2) | `POST /query`, `GET /warm`. Stage-level throttling. CORS preflight locked to the `cors.allow_origins` allowlist in config (never `*`). |
| Guardrails | Bedrock Guardrails (content + PII) | Input screen via `ApplyGuardrail` on the bare query before the loop; output guardrail attached to every Converse call. |
| Widget | Custom vanilla-JS embed, Shadow DOM | Self-injecting single file, reads `data-api-url` from its script tag. Shadow DOM isolates from host-site CSS. |
| Widget hosting | S3 + CloudFront (OAC), same stack | One `cdk deploy` ships backend + widget. OAC (not OAI/S3Origin). Same stack because OAC has a cross-stack cyclical-dependency problem. |
| Config | `config.yaml` (declarative) | Model IDs, vector store, scraper, chunking, retrieval, catalog, and guardrail settings live in config, not code. |
| Deploy | CloudFormation / CDK (Python, L1 constructs) | One-click AWS install. |
| Logs/eval | CloudWatch (structured logs); Bedrock RAG eval harness | Guardrail assessment logged per request. |

---

## Data flow

**Ingest (scheduled + on deploy):** Scraper Lambda fetches the curated library seed URLs -> extracts clean markdown -> uploads to the KB source bucket -> triggers a KB ingestion job (FIXED_SIZE chunk -> Titan v2 embed -> S3 Vectors). In the same run it regenerates the database catalog from databases.php (deterministic HTML anchor parse for names/descriptions/URLs + a Sonnet call for subjects/aliases, constrained to the parsed names), validates it, and writes it to the catalog S3 bucket - keeping the last-good copy if validation fails, and never blocking the KB scrape. Weekly EventBridge schedule + one-click deploy Trigger.

**Query (runtime):** Widget -> API Gateway (HTTP API, POST /query) -> Lambda. The request carries a multi-turn `messages` array (legacy `{query}` still accepted); history is client-sent (the Lambda is stateless) and trimmed server-side to the last 10 messages before seeding the loop. An input guardrail (`ApplyGuardrail`, source=INPUT) screens the newest user turn. Then `run_agent` runs the Converse tool-use loop under the system prompt: the model decides when to call `search_library_info` (KB `Retrieve`), `database_catalog` (reads the catalog from S3), or the two live Primo tools `search_book_catalog` / `search_course_reserves` (each a timed-out, soft-failing HTTPS call to the external Ex Libris Primo API); the loop executes each `toolUse` and feeds the `toolResult` back, repeating until `end_turn` (or the iteration cap). The output guardrail is attached to every Converse call. Response `{answer, sources[]}` returns to the widget: `sources` accumulate from the loop's KB retrievals (deduped by uri) plus one synthetic source per non-KB tool that returned a result - the A-Z databases page for `database_catalog`, a per-query discovery-search URL for each Primo tool. On a guardrail block, the blocked message returns with empty sources.

**Widget delivery:** Browser loads the host library page -> its `<script>` tag fetches `widget.js` from CloudFront (served from private S3 via OAC) -> widget injects into the page -> widget calls the API Gateway `/query` (the query flow above).

---

## Decisions (resolved)

1. **`Retrieve` via a tool, not `RetrieveAndGenerate`.** Full system-prompt control over out-of-scope and textbook behavior; the model calls Retrieve through `search_library_info`.
2. **Four tools, model-routed.** `search_library_info` handles hours/services/policies/how-to/borrowing/contact; `database_catalog` is authoritative for research-database availability (confirms when a named database is not held and suggests held alternatives) and subject listings; `search_book_catalog` and `search_course_reserves` search the live Primo general catalog and course reserves. Routing is via the tool descriptions + system-prompt guidance + `toolChoice` auto, not Lambda branches.
3. **Self-updating catalog with a robustness guard.** The held list is derived from the site on each scrape; the hand-authored not-held list is a bundled seed merged at read time; a minimum-count/required-field guard keeps the last-good catalog rather than overwrite it with garbage.
4. **L1 `Cfn*` constructs.** L1 core covers KB, DataSource, S3 Vectors, and guardrails; the `generative-ai-cdk-constructs` Bedrock L2s are excluded (deprecated).
5. **Same-stack widget hosting, OAC not OAI.** `S3BucketOrigin.with_origin_access_control`; one `cdk deploy`; cross-stack OAC hits a cyclical dependency.
6. **Contextual grounding excluded from guardrails.** Not supported for conversational chatbot use (needs fragile `guardContent` tagging); the system prompt handles grounding. The output guardrail is content-filters-only.
7. **WAF excluded.** HTTP API v2 cannot take WAF directly; API Gateway throttling is the cost-abuse control.
   **CORS is locked, not permissive** (pre-launch hardening): `cors.allow_origins` in config.yaml lists `https://www.gavilan.edu` (the host-only Origin the browser sends for the widget's page, `https://www.gavilan.edu/library/` - the path is irrelevant to CORS) plus a dev-only `http://localhost:8000` for `frontend/demo-live.html`, safe to drop at final launch. `infra/config.py` rejects `*` at synth. This is a spend-hygiene control, not a security boundary: CORS is browser-enforced only, so throttling remains the actual cost cap. `AllowCredentials` stays off (no cookies or auth headers are sent).
8. **Live-catalog tools return evidence, not a verdict.** Unlike `database_catalog` (authoritative from a curated list), Primo relevance scores are query-relative - a held item can score below not-held noise - so `search_book_catalog`/`search_course_reserves` return the top candidate records for the model to judge; `total == 0` is the only clean not-held / not-on-reserve signal and the handler applies no score threshold. Availability comes from a per-record delivery call and is reported as what the catalog SHOWS, not a guarantee. Reliability posture: per-call timeout + total availability budget + soft-fail to a "catalog unavailable" `toolResult`, plus defensive parsing of Primo's `$$`-encoded fields, so a slow/broken external call never kills the loop.
9. **Single-session multi-turn, client-carried.** The client sends the full `messages` history on each request; the Lambda is stateless (no DynamoDB, no server-side conversation store) and trims to the last 10 messages before seeding the Converse loop. Legacy `{query}` is treated as a single-message conversation.

---

## Verified (2026-07)

- **Bedrock Managed KB:** managed chunk/embed/store; S3 data source; citations via `x-amz-bedrock-kb-source-uri`.
- **S3 Vectors:** `CfnVectorBucket` + `CfnIndex`; KB `StorageConfiguration` type `S3_VECTORS` is a `oneOf` referenced by `IndexArn` alone (adding `IndexName`/`VectorBucketArn` is rejected at validation). 1024-dim, cosine, `float32`; `non_filterable_metadata_keys` set at index creation for the Bedrock-internal keys, or ingestion fails on the filterable-metadata limit. Semantic search only.
- **Agentic Converse tool-use:** request carries `toolConfig` (`toolSpec` = name/description/`inputSchema.json`); the model returns `stopReason: tool_use` with a `toolUse` block; reply with a user message carrying a `toolResult`; loop until `end_turn`. `toolChoice` auto.
- **Bedrock Guardrails:** content filters + PII. Input screen via the standalone `ApplyGuardrail` API; output backstop via `guardrailConfig` on Converse (blocked -> `stopReason: guardrail_intervened`). Needs `bedrock:ApplyGuardrail` alongside `InvokeModel`. Contextual grounding not supported for chatbot use.
- **Bedrock native RAG eval (BYOI):** LLM-as-judge. `create_evaluation_job` with `precomputedRagSourceConfig`; retrieve-only metrics ContextCoverage/ContextRelevance; R&G metrics Correctness/Completeness/Faithfulness/Helpfulness/Harmfulness + citation metrics. Uses `referenceResponses`.
- **CloudFront + S3 (CDK):** OAC via `S3BucketOrigin.with_origin_access_control` (auto-creates the OAC + bucket policy). Bucket needs bucket-owner-enforced ownership. Slow to create/destroy (~15-30 min).
- **Scraper:** `httpx` + `trafilatura` for page markdown; `extract_database_catalog` parses the databases.php HTML table by link anchor (name = anchor text, description = the rest of the cell), which stays reliable even where the page has no name/description delimiter.
- **Primo discovery API (live catalog tools):** verified against the public Ex Libris endpoint - `primaws/rest/pub/pnxs` search (`q=any,contains,...`; `scope=MyInstitution`/`tab=LibraryCatalog` for the general catalog, `scope=CourseReserves` for reserves) plus a per-record `/L/{recordid}?getDelivery=true` call for availability. No auth for discovery. `info.total` is the not-held / not-on-reserve signal; relevance is query-relative (not an absolute threshold). Fields are `$$`-encoded (creator; `crsinfo` carries course-code linkage, and a reserve record can serve multiple courses). Availability is a holding-level rollup ("catalog shows available"), not item-level truth - real-time due dates/copy counts would need the authenticated Alma API.
