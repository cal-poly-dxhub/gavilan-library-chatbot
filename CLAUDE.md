# CLAUDE.md

## What this is

RAG chatbot for Gavilan College Library. Answers operational student questions (hours, checkout, textbooks, "what does the library offer"). Not an AI research librarian; research questions get pointed to a human librarian. AWS-native.

## Stack

- **RAG:** Amazon Bedrock Managed Knowledge Base (S3 data source) over an Amazon S3 Vectors store (Titan Embed v2, 1024-dim, cosine, semantic-only)
- **Query path:** an agentic Bedrock `Converse` tool-use loop (`run_agent`); the model calls tools and the loop feeds results back until `end_turn`. Two tools: `search_library_info` (KB retrieval) and `database_catalog` (database availability + subject lookup). System prompt via the Converse `system` param.
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

`POST /query` response shape:
`{ "answer": "<text>", "sources": [ {"uri": "<page url>", "excerpt": "<snippet>"} ] }`

`sources` is deduplicated by uri; it accumulates from every `search_library_info` retrieval the model ran during the loop, plus one synthetic source (the library A-Z databases page) when `database_catalog` returned a result. Passages with no resolvable source uri are omitted; if the model answers without a tool call (e.g. a greeting), `sources` is `[]`.

Query flow (`run_agent`): the user query is the first user message; the system prompt is passed via the Converse `system` parameter (never concatenated into the user message). Each turn calls `Converse` with the two-tool `toolConfig` and the output guardrail; on `stopReason == tool_use` the loop executes every requested tool, appends the `toolResult`s, and calls again, until `end_turn` (or a max-iteration cap). There is no `<context>` passage-injection - retrieved passages reach the model as tool results.

## Repo layout

- `infra/` - CDK Python app. Provisions the S3 Vectors store (`CfnVectorBucket` + `CfnIndex`), the KB role, `AWS::Bedrock::KnowledgeBase` (S3_VECTORS storage) + `AWS::Bedrock::DataSource` type S3, the KB source bucket, the scraper Lambda (+ deps layer, weekly schedule, one-click deploy Trigger), the dedicated catalog bucket, and the query path (Lambda + own role, HTTP API with `POST /query` and `GET /warm`). Also hosts the widget (private S3 + CloudFront OAC, `BucketDeployment` of `frontend/widget.js` only) in the SAME stack (OAC cross-stack cyclical dependency), and defines the Bedrock guardrails (`CfnGuardrail` + `CfnGuardrailVersion`). Outputs a paste-ready embed tag + CloudFront domain + `/query` URL.
  - `app.py` - CDK entrypoint; loads `config.yaml`, passes to stack
  - `infra/infra_stack.py` - the stack (`GavilanChatbotStack`)
  - `infra/config.py` - `load_config()`; resolves repo-root `config.yaml` from `__file__`
  - `tests/unit/` - `test_infra_stack.py` (Template.from_stack assertions), `test_handler.py` (boto3 stubbed, payload-2.0 events)
- `app/` - query Lambda. `handler.py`: HTTP API (payload 2.0) entrypoint; `run_agent()` runs the agentic Converse tool-use loop over two tools (`search_library_info` -> KB `Retrieve`; `database_catalog` -> reads the catalog from S3). Wiring from env vars set by the stack. boto3 provided by the runtime.
  - `system_prompt.md` - finalized system prompt (`<tools>` section carries the two-tool routing guidance); read once at cold start, passed via Converse `system`. Packaged via `Code.from_asset(app/)`.
  - `data/database_catalog.json` - bundled seed catalog: the hand-authored not-held list + a fallback held list, merged with the S3 held list at read time.
- `scraper/` - scraper Lambda source. `scraper.py` (pure fetch/extract, incl. `extract_database_catalog` HTML parse) + `lambda_function.py` (S3 upload, KB ingestion trigger, catalog regeneration: parse -> Sonnet enrichment -> guard -> write to the catalog bucket). Own `.venv`/tests (needs trafilatura).
- `eval/` - Bedrock RAG eval harness (boto3 tooling, NOT CDK). Runs on demand against deployed infra; cannot run offline. Retrieve-only formatter (chunking eval) + retrieve-and-generate formatter (answer quality, bring-your-own-inference). `capture_outputs.py` is a STUB until the bot is deployed. Separate `eval_config.yaml`.
- `frontend/` - embeddable widget. `widget.js` is the ONLY file shipped (production-clean, no mock code); reads its endpoint from its `<script>` tag's `data-api-url`, POSTs `{query}`, renders `{answer, sources}`. `mock.js` (dev-only fetch stub) + `demo.html` (offline harness) + `test/widget.contract.test.js` (zero-dep Node tests) never ship; dependency direction is one-way (widget never references the mock).
- `config.yaml` - declarative settings at repo root; embedding model, `vector_store` (S3 Vectors names, data_type, distance_metric, non-filterable keys), `scraper` seed URLs + schedule, `chunking`, `retrieval.number_of_results`, `generation.model_id`, `catalog` (enrichment model, S3 key, guard threshold, cache TTL), guardrail settings. CDK reads it at synth via `infra/config.py`. Edit values here, do not hardcode in the stack.
- `docs/` - design docs (`architecture.md`, `build-plan.md`, architecture diagram).

## Excluded (do not reintroduce)

- **Contextual grounding EXCLUDED.** AWS doesn't support it for conversational chatbots; requires fragile `guardContent` message-tagging (silent-failure trap). The system prompt handles grounding. Revisit via standalone `ApplyGuardrail`, not inline Converse tagging.
- **WAF EXCLUDED.** WAF can't attach to HTTP API v2 (would need a second CloudFront fronting the API). Thin threat surface; API Gateway throttling is the real cost-abuse control. Revisit only on a compliance mandate.
- **`generative-ai-cdk-constructs` Bedrock L2s EXCLUDED (deprecated).** That is WHY the stack is L1 `Cfn*`. Do not reintroduce.

## Guardrail note

Needs `bedrock:ApplyGuardrail` on the guardrail ARN alongside `InvokeModel`, or it silently fails at runtime. The Lambda pins to a published numbered guardrail version; live policy/message edits require a new version.

## Hard rules

- **Do not touch Git.** Never commit or do anything related to GitHub.
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