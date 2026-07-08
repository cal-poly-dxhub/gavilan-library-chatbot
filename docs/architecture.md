# Gavilan Library Chatbot — Architecture

**What:** RAG chatbot for Gavilan College Library. Answers operational questions (hours, checkout, textbooks, "what does the library offer"), routes research questions and out-of-scope issues.
**Client:** Gavilan College Library via Cal Poly DxHub.
**Status:** built.
**Updated:** 2026-07-08

---

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Ingestion | Scraper Lambda -> S3 source bucket -> Bedrock KB (S3 data source) | Curated seed URLs scraped to clean markdown; KB re-ingests each run. Weekly EventBridge schedule + one-click deploy Trigger. |
| RAG engine | Bedrock Managed Knowledge Base | Managed chunk/embed/store. FIXED_SIZE chunking (300 tokens, 20% overlap). |
| Vector store | Amazon S3 Vectors | `s3vectors.CfnVectorBucket` + `CfnIndex`; KB `StorageConfiguration` type `S3_VECTORS`, referenced by `IndexArn`. 1024-dim, cosine, `float32`, semantic-only. |
| Embeddings | Titan Text Embeddings v2 (1024-dim) | Managed default. |
| Query path | Agentic `Converse` tool-use loop (Lambda) | `run_agent`: the model calls tools; the loop feeds each `toolResult` back until `end_turn` (iteration cap). System prompt via the Converse `system` param. |
| Tools | `search_library_info`, `database_catalog` | KB retrieval for general library questions; authoritative database availability + subject listing. Routing via tool descriptions + system-prompt guidance + `toolChoice` auto. |
| LLM (generation) | Bedrock-hosted Claude Sonnet 4.6 via Converse | Invoked through a `us.`-prefixed cross-region inference profile. |
| Database catalog | Self-updating JSON in a dedicated S3 bucket | Scraper derives the held list from databases.php (HTML anchor parse + Sonnet enrichment for subjects/aliases); the hand-authored not-held list is a bundled seed merged at read time; the tool reads from S3 with per-container TTL caching. |
| Orchestration/API | Lambda + HTTP API (API Gateway v2) | `POST /query`, `GET /warm`. Stage-level throttling. |
| Guardrails | Bedrock Guardrails (content + PII) | Input screen via `ApplyGuardrail` on the bare query before the loop; output guardrail attached to every Converse call. |
| Widget | Custom vanilla-JS embed, Shadow DOM | Self-injecting single file, reads `data-api-url` from its script tag. Shadow DOM isolates from host-site CSS. |
| Widget hosting | S3 + CloudFront (OAC), same stack | One `cdk deploy` ships backend + widget. OAC (not OAI/S3Origin). Same stack because OAC has a cross-stack cyclical-dependency problem. |
| Config | `config.yaml` (declarative) | Model IDs, vector store, scraper, chunking, retrieval, catalog, and guardrail settings live in config, not code. |
| Deploy | CloudFormation / CDK (Python, L1 constructs) | One-click AWS install. |
| Logs/eval | CloudWatch (structured logs); Bedrock RAG eval harness | Guardrail assessment logged per request. |

---

## Data flow

**Ingest (scheduled + on deploy):** Scraper Lambda fetches the curated library seed URLs -> extracts clean markdown -> uploads to the KB source bucket -> triggers a KB ingestion job (FIXED_SIZE chunk -> Titan v2 embed -> S3 Vectors). In the same run it regenerates the database catalog from databases.php (deterministic HTML anchor parse for names/descriptions/URLs + a Sonnet call for subjects/aliases, constrained to the parsed names), validates it, and writes it to the catalog S3 bucket - keeping the last-good copy if validation fails, and never blocking the KB scrape. Weekly EventBridge schedule + one-click deploy Trigger.

**Query (runtime):** Widget -> API Gateway (HTTP API, POST /query) -> Lambda. An input guardrail (`ApplyGuardrail`, source=INPUT) screens the bare query. Then `run_agent` runs the Converse tool-use loop under the system prompt: the model decides when to call `search_library_info` (KB `Retrieve`) or `database_catalog` (reads the catalog from S3), the loop executes each `toolUse` and feeds the `toolResult` back, repeating until `end_turn` (or the iteration cap). The output guardrail is attached to every Converse call. Response `{answer, sources[]}` returns to the widget: `sources` accumulate from the loop's KB retrievals (deduped by uri) plus the A-Z databases page when the catalog tool was used. On a guardrail block, the blocked message returns with empty sources.

**Widget delivery:** Browser loads the host library page -> its `<script>` tag fetches `widget.js` from CloudFront (served from private S3 via OAC) -> widget injects into the page -> widget calls the API Gateway `/query` (the query flow above).

---

## Decisions (resolved)

1. **`Retrieve` via a tool, not `RetrieveAndGenerate`.** Full system-prompt control over out-of-scope and textbook behavior; the model calls Retrieve through `search_library_info`.
2. **Two tools, model-routed.** `database_catalog` is authoritative for database availability (confirms when a named database is not held and suggests held alternatives) and subject listings; `search_library_info` handles hours/services/policies/how-to/borrowing/contact. Routing is via the tool descriptions + system-prompt guidance + `toolChoice` auto, not Lambda branches.
3. **Self-updating catalog with a robustness guard.** The held list is derived from the site on each scrape; the hand-authored not-held list is a bundled seed merged at read time; a minimum-count/required-field guard keeps the last-good catalog rather than overwrite it with garbage.
4. **L1 `Cfn*` constructs.** L1 core covers KB, DataSource, S3 Vectors, and guardrails; the `generative-ai-cdk-constructs` Bedrock L2s are excluded (deprecated).
5. **Same-stack widget hosting, OAC not OAI.** `S3BucketOrigin.with_origin_access_control`; one `cdk deploy`; cross-stack OAC hits a cyclical dependency.
6. **Contextual grounding excluded from guardrails.** Not supported for conversational chatbot use (needs fragile `guardContent` tagging); the system prompt handles grounding. The output guardrail is content-filters-only.
7. **WAF excluded.** HTTP API v2 cannot take WAF directly; API Gateway throttling is the cost-abuse control.

---

## Open decisions

1. **LLM model** — Sonnet 4.6 in use; confirm any DxHub/sponsor preference.
2. **Refresh cadence** — weekly scrape baseline.
3. **CORS** — `allow_origins` currently permissive; lock to the widget domain before launch.
4. **Log store** — DynamoDB vs CloudWatch for conversation logging (deferred with multi-turn).

---

## Verified (2026-07)

- **Bedrock Managed KB:** managed chunk/embed/store; S3 data source; citations via `x-amz-bedrock-kb-source-uri`.
- **S3 Vectors:** `CfnVectorBucket` + `CfnIndex`; KB `StorageConfiguration` type `S3_VECTORS` is a `oneOf` referenced by `IndexArn` alone (adding `IndexName`/`VectorBucketArn` is rejected at validation). 1024-dim, cosine, `float32`; `non_filterable_metadata_keys` set at index creation for the Bedrock-internal keys, or ingestion fails on the filterable-metadata limit. Semantic search only.
- **Agentic Converse tool-use:** request carries `toolConfig` (`toolSpec` = name/description/`inputSchema.json`); the model returns `stopReason: tool_use` with a `toolUse` block; reply with a user message carrying a `toolResult`; loop until `end_turn`. `toolChoice` auto.
- **Bedrock Guardrails:** content filters + PII. Input screen via the standalone `ApplyGuardrail` API; output backstop via `guardrailConfig` on Converse (blocked -> `stopReason: guardrail_intervened`). Needs `bedrock:ApplyGuardrail` alongside `InvokeModel`. Contextual grounding not supported for chatbot use.
- **Bedrock native RAG eval (BYOI):** LLM-as-judge. `create_evaluation_job` with `precomputedRagSourceConfig`; retrieve-only metrics ContextCoverage/ContextRelevance; R&G metrics Correctness/Completeness/Faithfulness/Helpfulness/Harmfulness + citation metrics. Uses `referenceResponses`.
- **CloudFront + S3 (CDK):** OAC via `S3BucketOrigin.with_origin_access_control` (auto-creates the OAC + bucket policy). Bucket needs bucket-owner-enforced ownership. Slow to create/destroy (~15-30 min).
- **Scraper:** `httpx` + `trafilatura` for page markdown; `extract_database_catalog` parses the databases.php HTML table by link anchor (name = anchor text, description = the rest of the cell), which stays reliable even where the page has no name/description delimiter.
