# CLAUDE.md

## What this is

RAG chatbot for Gavilan College Library. Answers operational student questions (hours, checkout, textbooks, "what does the library offer"). Not an AI research librarian; research questions get pointed to a human librarian. AWS-native.

## Stack

- **RAG:** Amazon Bedrock Managed Knowledge Base (S3 data source) over an Amazon S3 Vectors store (Titan Embed v2, 1024-dim, cosine, semantic-only)
- **Query path:** an agentic Bedrock `Converse` tool-use loop (`run_agent`); the model calls tools and the loop feeds results back until `end_turn`. Five tools: `search_library_info` (KB retrieval), `database_catalog` (research-database availability + subject lookup, authoritative), `search_book_catalog` (Primo general catalog), `search_course_reserves` (Primo course reserves), `library_links` (curated canonical Gavilan URLs, static). System prompt via the Converse `system` param. Requests carry a multi-turn `messages` array (see `/query` contract).
- **Live catalog (Primo):** `search_book_catalog` + `search_course_reserves` call the Ex Libris Primo discovery API directly - outbound HTTPS to a third party inside the agent loop, NOT an AWS API (no IAM). The query Lambda therefore needs outbound internet to reach Primo; a VPC without a NAT path would silently break these two tools. Every call is timed out and soft-fails, so a slow/broken Primo never kills `/query`.
- **Ingestion:** a scraper Lambda pulls the library site into the KB source bucket (KB re-ingests) and regenerates the database catalog to S3, on a weekly schedule + on deploy
- **Backend:** Lambda + HTTP API (API Gateway v2), Python
- **Infra:** AWS CDK (Python), L1 `Cfn*` constructs; everything as code
- **Guardrails:** Bedrock Guardrails (content + PII)
- **Config:** `config.yaml` at repo root; single source of truth for changeable knobs
- **Frontend:** vanilla JS widget (Shadow DOM, dependency-free)

## Read the docs before design work

The architecture is in `docs/architecture.md`. When a task touches architecture, read it first so you match what actually runs.

## Commands

CDK app lives in `infra/`. All infra commands run from `infra/` with the venv active.

First-time setup (from `infra/`):
- `source .venv/bin/activate`
- `python -m pip install -r requirements.txt -r requirements-dev.txt`

Then (from `infra/`, venv active):
- Synth (offline, no creds): `cdk synth`
- Tests: `python -m pytest`
- Deploy: `cdk deploy` (needs AWS creds; account pending)
- Teardown: `cdk destroy` (removes the S3 Vectors bucket + index, the catalog bucket, and the KB source/widget buckets)

Pinned: `aws-cdk-lib==2.260.0`, CDK CLI `2.1129.0`.

Handler unit tests stub `boto3` in `sys.modules` and monkeypatch the client getters, so they need no boto3 install and no live AWS.

## Vector store

Amazon S3 Vectors (`s3vectors.CfnVectorBucket` + `CfnIndex`); the KB `StorageConfiguration` type is `S3_VECTORS`, referencing the index by `IndexArn` (that one field only - passing IndexName/VectorBucketArn too makes CloudFormation reject the config as ambiguous). Config lives in `config.yaml` under `vector_store`:
- index: 1024 dims (Titan Text Embeddings v2), `data_type` float32, `distance_metric` cosine
- index name: `bedrock-knowledge-base-default-index`; vector bucket: `gavilan-library-vectors`
- `non_filterable_metadata_keys` (immutable at index creation) mark Bedrock's internal keys (`AMAZON_BEDROCK_TEXT`, `AMAZON_BEDROCK_METADATA`, `x-amz-bedrock-kb-*`) non-filterable, or ingestion fails on the filterable-metadata limit

## `/query` contract (widget builds against this)

`POST /query` request shape (multi-turn): `{ "messages": [ {"role": "user"|"assistant", "content": "<text>"}, ... ] }`, oldest first, newest user turn last. Backward-compatible with the legacy `{ "query": "<text>" }` shape (treated as a single-message conversation). History is client-sent - the Lambda is stateless (no server-side conversation store) - and trimmed server-side to the last 10 messages (`MAX_HISTORY_MESSAGES`) before seeding the Converse loop.

Response shape: `{ "answer": "<text>", "sources": [ {"uri": "<page url>", "excerpt": "<snippet>"} ] }`

`sources` is deduplicated by uri; it accumulates from every `search_library_info` retrieval the model ran during the loop, plus one synthetic source per tool that returned a result: the library A-Z databases page for `database_catalog`, a per-query Primo discovery-search URL for each of `search_book_catalog` and `search_course_reserves`, and one source per canonical URL a MATCHED `library_links` lookup returned (a no-match/no-topic listing returns the whole table as a browse aid and contributes no source). `_extract_source` prefers the public `source_url` (the scraper's per-document metadata sidecar) over the internal S3 URI; a source that resolves only to an `s3://` URI is omitted so no bucket path reaches the client. If the model answers without a tool call (e.g. a greeting), `sources` is `[]`.

Opt-in debug payload: a request with `include_full_context: true` gets two extra response fields - `full_context` (the un-deduped, un-truncated KB passages from `search_library_info`) and `tool_calls` (an ordered trace of EVERY tool the loop invoked: `{tool, input, status, returned_results, result}`, where `result` is the exact JSON the model received as that call's `toolResult` content). `full_context` covers one of the five tools; `tool_calls` covers all of them, which is what the answer-quality eval grades groundedness against. The widget never sets the flag, so its responses stay exactly `{answer, sources}`. Recording the trace changes nothing about what is sent to the model.

Query flow (`run_agent`): the newest user turn is the current question; the trimmed history seeds the Converse `messages`, and the system prompt is passed via the Converse `system` parameter (never concatenated into the user message). Each turn calls `Converse` with the five-tool `toolConfig` and the output guardrail; on `stopReason == tool_use` the loop executes every requested tool, appends the `toolResult`s, and calls again, until `end_turn` (or a max-iteration cap). There is no `<context>` passage-injection - retrieved passages reach the model as tool results.

## Repo layout

- `infra/` - CDK Python app. Provisions the S3 Vectors store (`CfnVectorBucket` + `CfnIndex`), the KB role, `AWS::Bedrock::KnowledgeBase` (S3_VECTORS storage) + `AWS::Bedrock::DataSource` type S3, the KB source bucket, the scraper Lambda (+ deps layer, weekly schedule, one-click deploy Trigger), the dedicated catalog bucket, and the query path (Lambda + own role, HTTP API with `POST /query` and `GET /warm`). Also hosts the widget (private S3 + CloudFront OAC, `BucketDeployment` of `frontend/widget.js` only) in the SAME stack (OAC cross-stack cyclical dependency), and defines the Bedrock guardrails (`CfnGuardrail` + `CfnGuardrailVersion`). Outputs a paste-ready embed tag + CloudFront domain + `/query` URL.
  - `app.py` - CDK entrypoint; loads `config.yaml`, passes to stack
  - `infra/infra_stack.py` - the stack (`GavilanChatbotStack`)
  - `infra/config.py` - `load_config()`; resolves repo-root `config.yaml` from `__file__`
  - `tests/unit/` - `test_infra_stack.py` (Template.from_stack assertions), `test_handler.py` (boto3 stubbed, payload-2.0 events)
- `app/` - query Lambda. `handler.py`: HTTP API (payload 2.0) entrypoint; `run_agent()` runs the agentic Converse tool-use loop over five tools (`search_library_info` -> KB `Retrieve`; `database_catalog` -> reads the catalog from S3; `search_book_catalog` + `search_course_reserves` -> live Primo API calls, client inline in `handler.py`, no import; `library_links` -> reads the bundled link table). Wiring from env vars set by the stack. boto3 provided by the runtime.
  - `system_prompt.md` - finalized system prompt (`<tools>` section carries the four-tool routing guidance; `<textbook_flow>` routes course textbooks to `search_course_reserves`); read once at cold start, passed via Converse `system`. Packaged via `Code.from_asset(app/)`. NOTE: `library_links` is deliberately absent from `<tools>` - it routes purely off its `toolSpec` description.
  - `data/database_catalog.json` - bundled seed catalog: the hand-authored not-held list + a fallback held list, merged with the S3 held list at read time.
  - `data/library_links.json` - bundled, hand-authored table of canonical Gavilan URLs behind the `library_links` tool (library home page, college site, online textbook collections, research guides, bookstore, campus maps, public safety, ILL, laptop record). Fully static: no scraper, no S3, no cache. Edit + redeploy to change.
  - `primo_search.py` - standalone CLI for exploring the Primo discovery API (dev tool; NOT imported by the handler and NOT in the `from_asset` bundle).
- `scraper/` - scraper Lambda source. `scraper.py` (pure fetch/extract, incl. `extract_database_catalog` HTML parse) + `lambda_function.py` (S3 upload, KB ingestion trigger, catalog regeneration: parse -> Sonnet enrichment -> guard -> write to the catalog bucket). Own `.venv`/tests (needs trafilatura).
- `eval/` - Bedrock RAG eval harness (boto3 tooling, NOT CDK). Runs on demand against deployed infra; cannot run offline. Retrieve-only formatter (chunking eval) + retrieve-and-generate formatter (answer quality, bring-your-own-inference). `capture_outputs.py` is a STUB until the bot is deployed. Separate `eval_config.yaml`.
- `frontend/` - embeddable widget. `widget.js` is the ONLY file shipped (production-clean, no mock code); reads its endpoint from its `<script>` tag's `data-api-url`, POSTs a multi-turn `{messages: [...]}` array, renders `{answer, sources}`. `mock.js` (dev-only fetch stub) + `demo.html` (offline harness) + `test/widget.contract.test.js` (zero-dep Node tests) never ship; dependency direction is one-way (widget never references the mock).
- `config.yaml` - declarative settings at repo root; embedding model, `vector_store` (S3 Vectors names, data_type, distance_metric, non-filterable keys), `scraper` seed URLs + schedule, `chunking`, `retrieval.number_of_results`, `generation.model_id`, `catalog` (enrichment model, S3 key, guard threshold, cache TTL), `primo` (the live-catalog knobs `timeout_seconds`, `number_of_results`, `availability_budget_seconds`, wired as `PRIMO_*` env; `search_course_reserves` reuses the same knobs), `library_links.data_file` (the bundled link-table filename; the stack feeds the SAME value to the Lambda asset include and the `LIBRARY_LINKS_FILE` env so they cannot drift), `cors.allow_origins` (the HTTP API browser allowlist), guardrail settings. CDK reads it at synth via `infra/config.py`. Edit values here, do not hardcode in the stack.
- `docs/` - design docs (`architecture.md`, `build-plan.md`, architecture diagram).

## Excluded (do not reintroduce)

- **Contextual grounding EXCLUDED.** AWS doesn't support it for conversational chatbots; requires fragile `guardContent` message-tagging (silent-failure trap). The system prompt handles grounding. Revisit via standalone `ApplyGuardrail`, not inline Converse tagging.
- **WAF EXCLUDED.** WAF can't attach to HTTP API v2 (would need a second CloudFront fronting the API). Thin threat surface; API Gateway throttling is the real cost-abuse control. Revisit only on a compliance mandate.
- **CORS `allow_origins: "*"` EXCLUDED.** Locked to `cors.allow_origins` in config.yaml (`https://www.gavilan.edu` + a dev-only `http://localhost:8000`); `infra/config.py` rejects a wildcard at synth. The browser sends a HOST-only `Origin`, so do not add the `/library/` path. CORS is browser-enforced only and is NOT a security boundary (curl/scripts ignore it) - throttling is still the cost cap - but a wildcard would let any page drive the billable `/query` endpoint from its visitors' browsers. Don't "fix" a CORS console error by widening this; add the real origin.
- **`generative-ai-cdk-constructs` Bedrock L2s EXCLUDED (deprecated).** That is WHY the stack is L1 `Cfn*`. Do not reintroduce.

## Guardrail note

Needs `bedrock:ApplyGuardrail` on the guardrail ARN alongside `InvokeModel`, or it silently fails at runtime. The Lambda pins to a published numbered guardrail version; live policy/message edits require a new version.

## Hard rules

- **No Git by default.** Do not commit, stage, push, or take any other git/GitHub action unless the task order explicitly instructs a commit. When it does, follow that instruction exactly and do nothing else with git.
- **Behavior and tool routing live in the system prompt + tool descriptions, not hardcoded Lambda branches.** Tool choice is the model's (`toolChoice` auto); textbook clarifying questions and out-of-scope handling are prompt instructions.

## Writing style

Avoid em dashes; use " - " or parentheses instead.

## When testing

Never hide failures. Show all test results, including failures, in full.

## Lessons

Things learned the hard way. Read before repeating the same work.

- **Verify CDK construct APIs against the installed package, not memory.** The Bedrock construct surface is in flux and training data is stale. For `aws-cdk-lib==2.260.0`, introspect the installed package. Example current gotcha: `CfnKnowledgeBase` `S3VectorsConfiguration` is a `oneOf` - pass `IndexArn` alone; adding `IndexName`/`VectorBucketArn` too makes CloudFormation reject it at validation ("2 subschemas matched instead of one").
- **HTTP API v2 is in `aws-cdk-lib` core as of 2.260.0.** `HttpApi` + `CorsPreflightOptions` live in `aws_cdk.aws_apigatewayv2`, `HttpLambdaIntegration` in `aws_cdk.aws_apigatewayv2_integrations`. The old `-alpha` packages are gone (import fails) and are NOT needed. HTTP API not REST (~71% cheaper for a Lambda-proxy job). `HttpLambdaIntegration` defaults to payload format 2.0.
- **CloudFront is slow to create AND destroy (~15-30 min each).** The widget distribution dominates `cdk deploy`/`destroy` wall-clock. Actual serving behavior (OAC read, cache, HTTPS redirect) is only verifiable at deploy.
- **OAC + `auto_delete_objects` dependency cycle.** Do NOT add an explicit `distribution.node.add_dependency(bucket)`: the origin already references the bucket, and the explicit edge pulls in the bucket's auto-delete custom resource, which `DependsOn` the OAC bucket policy, which `DependsOn` the distribution -> a synth-blocking cycle. Surfaces in `Template.from_stack` (tests) even when plain `cdk synth` looks fine; keep infra tests in the loop.
- **Config keys reach the from_asset Lambdas ONLY as stack-set env vars** (the bundle excludes config.yaml). A new runtime knob needs three touches - config.yaml, stack env-wiring, handler read - or it silently no-ops. Synth-time-only keys (throttle limits) are read by the stack directly and don't need the bridge.
- **Invoking a Claude model needs a us.-prefixed inference profile** (bare model IDs are rejected), which requires `InvokeModel*` on the profile ARN plus foundation-model ARNs across routed regions, not the single-ARN on-demand grant. Applies to both the query Lambda's generation model and the scraper's catalog-enrichment model.
- **A live third-party API in the agent loop must never be able to kill a request.** `search_book_catalog`/`search_course_reserves` hit the undocumented Primo discovery endpoint (search + a per-record delivery call for availability), whose fields use a `$$C..$$V..` / `$$R..$$V..$$M..` encoding - parse it defensively (never index blindly; a shape change must degrade, not throw). Every call is timed out with a total availability budget, and any failure soft-fails to a "catalog unavailable" toolResult so the loop still answers. Availability from the delivery call is a report of what the catalog SHOWS, not a guarantee a copy is on the shelf - the prompt phrases it that way, and `total == 0` is the only authoritative not-held signal (Primo relevance scores are query-relative, so the model judges matches).