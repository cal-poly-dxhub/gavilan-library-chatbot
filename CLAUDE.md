# CLAUDE.md

## What this is

RAG chatbot for Gavilan College Library. Answers operational student questions (hours, checkout, textbooks, "what does the library offer"). Not an AI research librarian; research questions get pointed to a human librarian. AWS-native.

## Stack

- **RAG:** Amazon Bedrock Managed Knowledge Base (S3 data source) over an Amazon S3 Vectors store (Titan Embed v2, 1024-dim, cosine, semantic-only)
- **Query path:** an agentic Bedrock `Converse` tool-use loop (`run_agent`); the model calls tools and the loop feeds results back until `end_turn`. Four tools: `search_library_info` (KB retrieval), `database_catalog` (research-database availability + subject lookup, authoritative), `search_book_catalog` (Primo general catalog), `search_course_reserves` (Primo course reserves). The curated canonical-URL table is NOT a tool: it takes no input and returns the same rows every call, so it is injected into the Converse `system` payload every request (see `_links_block`). System prompt + date + link table via the Converse `system` param. Requests carry a multi-turn `messages` array (see `/query` contract).
- **Live catalog (Primo):** `search_book_catalog` + `search_course_reserves` call the Ex Libris Primo discovery API directly - outbound HTTPS to a third party inside the agent loop, NOT an AWS API (no IAM). The query Lambda therefore needs outbound internet to reach Primo; a VPC without a NAT path would silently break these two tools. Every call is timed out and soft-fails, so a slow/broken Primo never kills `/query`.
- **Ingestion:** a scraper Lambda pulls the library site into the KB source bucket (KB re-ingests) and regenerates the database catalog to S3, on a weekly schedule + on deploy
- **Backend:** Lambda + HTTP API (API Gateway v2), Python
- **Infra:** AWS CDK (Python), L1 `Cfn*` constructs; everything as code
- **Guardrails:** Bedrock Guardrails (content + PII)
- **Config:** `config.yaml` at repo root; single source of truth for changeable knobs
- **Frontend:** vanilla JS widget (Shadow DOM, dependency-free). Bilingual chrome (English + Español) from one string table, switched by a control in the panel header; an explicit choice is sent as `language` on the request
- **Demo site:** one static page (`frontend/demo-site.html`) on its OWN S3 + CloudFront, embedding the production widget from the production CDN. `cdk deploy` stamps the live API + widget URLs AND the `cost_model` block into it, and outputs `DemoSiteUrl`. Toggle with `demo_site.enabled` in config.yaml
- **Cost visibility (demo only):** the demo page carries a session cost meter + a monthly estimator behind one control in the DEMO banner. It is fed by the widget's opt-in `data-usage-events` attribute (absent from the production embed) and the handler's opt-in `include_usage` flag. Rates + measured constants live in `config.yaml` under `cost_model`; re-measure with `eval/measure_usage.py`

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

### Changing `chunking` in config.yaml

Bedrock chunking is immutable, so this is a data-source REPLACEMENT, not an update. Two things follow:

1. The data source name folds in the chunking settings (`gavilan-library-kb-s3-fixedsize-600t20p`). CloudFormation creates a replacement before deleting the original, so a fixed name collides inside the KB and the deploy dies with `409 AlreadyExists`. Do not "simplify" that name back to a constant.
2. **The replacement starts EMPTY and `cdk deploy` does not refill it.** The scraper's deploy Trigger only re-fires when the scraper's own code changes, so a chunking-only deploy leaves a knowledge base with zero indexed documents - `/query` will answer everything with "I don't have that information". Kick ingestion by hand right after the deploy:

Get the CURRENT knowledge-base id from the stack output rather than pasting one from here -
the KB is replaced whenever the vector index is, so a hardcoded id in this file goes stale
(it did: the old `GLBDBZXOFU` no longer exists).

```
KB=$(AWS_PROFILE=gavilan aws cloudformation describe-stacks --stack-name GavilanChatbotStack \
  --region us-west-2 --query "Stacks[0].Outputs[?OutputKey=='KnowledgeBaseId'].OutputValue" --output text)
AWS_PROFILE=gavilan aws bedrock-agent list-data-sources --knowledge-base-id $KB --region us-west-2
AWS_PROFILE=gavilan aws bedrock-agent start-ingestion-job \
  --knowledge-base-id $KB --data-source-id <NEW_ID> --region us-west-2
```

The source bucket is untouched by the replacement, so ingestion just re-indexes what is already there. Then re-run `eval/retrieval_probe.py` - the offline chunking eval measures boundaries, never ranking, so live recall is the only check that a bigger chunk did not retrieve worse.

Handler unit tests stub `boto3` in `sys.modules` and monkeypatch the client getters, so they need no boto3 install and no live AWS.

### CI

`.github/workflows/ci.yml` runs on PRs into `main`/`dev` and on pushes to them. Four parallel jobs, one per test surface, all hermetic (no AWS creds, no deployed endpoint):

| Job (check name) | Where | Command | Install |
| --- | --- | --- | --- |
| `infra tests (CDK stack + Lambda handler)` | `infra/` | `python -m pytest -v` | `requirements.txt` + `requirements-dev.txt` |
| `scraper tests` | `scraper/` | `python -m pytest tests -v` | `requirements.txt` + `requirements-dev.txt` |
| `eval harness tests` | `eval/` | `python -m pytest tests -v` | `requirements-dev.txt` ONLY |
| `widget tests` | repo root | `node frontend/test/widget.contract.test.js` | none (dependency-free, no package.json) |

Python pinned to 3.13 to match the Lambda runtime (`_LAMBDA_PYTHON`); Node 22 for the widget.

CI notes, so nobody "fixes" these back:
- **No `cdk synth` job.** `test_infra_stack.py` already builds the real stack via `load_config()` + `GavilanChatbotStack(...)`, so synth would only cover `app.py`/`cdk.json` glue, and it would need a Node + pinned CDK CLI install plus a network-bound asset bundle (the scraper deps layer shells out to `pip --platform`, Docker fallback).
- **The eval job installs `requirements-dev.txt` only, never `requirements.txt`.** `eval/tests/conftest.py` stubs boto3 *only if it is not already importable*; installing the real boto3 would silently disable the no-live-AWS guard.
- **No `paths:` filters.** A required check skipped by a paths filter reports as pending forever and blocks the PR. All four suites together are ~11s of test time, so filtering buys nothing.
- **The deployed-infra evals stay out of CI**: the Bedrock runners in `eval/` and the `eval/promptfoo/` answer-quality loop need real credentials, a live `/query`, and cost money per run.

## Vector store

Amazon S3 Vectors (`s3vectors.CfnVectorBucket` + `CfnIndex`); the KB `StorageConfiguration` type is `S3_VECTORS`, referencing the index by `IndexArn` (that one field only - passing IndexName/VectorBucketArn too makes CloudFormation reject the config as ambiguous). Config lives in `config.yaml` under `vector_store`:
- index: 1024 dims (Titan Text Embeddings v2), `data_type` float32, `distance_metric` cosine
- index name: `bedrock-knowledge-base-default-index`; vector bucket: `gavilan-library-vectors`
- `non_filterable_metadata_keys` (immutable at index creation) mark Bedrock's internal keys (`AMAZON_BEDROCK_TEXT`, `AMAZON_BEDROCK_METADATA`, `x-amz-bedrock-kb-*`) non-filterable, or ingestion fails on the filterable-metadata limit

## `/query` contract (widget builds against this)

`POST /query` request shape (multi-turn): `{ "messages": [ {"role": "user"|"assistant", "content": "<text>"}, ... ] }`, oldest first, newest user turn last. Backward-compatible with the legacy `{ "query": "<text>" }` shape (treated as a single-message conversation). History is client-sent - the Lambda is stateless (no server-side conversation store) - and trimmed server-side to the last 10 messages (`MAX_HISTORY_MESSAGES`) before seeding the Converse loop.

Response shape: `{ "answer": "<text>", "sources": [ {"uri": "<page url>", "excerpt": "<snippet>"} ] }`

`sources` is deduplicated by uri; it accumulates from every `search_library_info` retrieval the model ran during the loop, plus one synthetic source per tool that returned a result: the library A-Z databases page for `database_catalog`, a per-query Primo discovery-search URL for each of `search_book_catalog` and `search_course_reserves`, The curated link table contributes NO sources: it reports which links exist, not which one the answer used. `_extract_source` prefers the public `source_url` (the scraper's per-document metadata sidecar) over the internal S3 URI; a source that resolves only to an `s3://` URI is omitted so no bucket path reaches the client. If the model answers without a tool call (e.g. a greeting), `sources` is `[]`.

Optional reply language: a request may carry `language: "en"|"es"`. Present -> ONE extra Converse `system` block telling the model to write its whole reply in that language, so an explicit Español selection is honored even for a question typed in English. Absent or unrecognized -> no block at all, and the model keeps auto-detecting the language from the question (which it already does correctly). The code is ALLOWLISTED against `_LANGUAGES` and the prompt text is built from the handler's own table, never from the client's string - the field is client-supplied text heading for the system payload, so that is a security boundary, not tidiness. Nothing else is language-aware: retrieval, the four tools, and the KB are untouched (the KB is English and Spanish questions retrieve from it correctly - measured, see the Lessons entry).

Opt-in cost payload: a request with `include_usage: true` also gets `usage` - the BILLABLE units that one question consumed across the whole loop: `model_calls`, `input_tokens`, `output_tokens`, `cache_read/write_input_tokens`, `guardrail_calls`, `guardrail_units` (per policy, snake_cased from Bedrock's own field names), `retrievals`, `tool_calls`. Summed from what Bedrock reports on each response, so it captures the thing that is invisible from outside: one question is often several Converse calls, each resending the whole context. `include_full_context: true` implies it. The widget only sets the flag when its embed carries `data-usage-events` (the demo page does; the production embed tag does not), so a student's response is exactly `{answer, sources}`.

Opt-in debug payload: a request with `include_full_context: true` gets three extra response fields - `full_context` (the un-deduped, un-truncated KB passages from `search_library_info`), `tool_calls` (an ordered trace of EVERY tool the loop invoked: `{tool, input, status, returned_results, result}`, where `result` is the exact JSON the model received as that call's `toolResult` content), and `library_links` (the curated URL table the model was handed in its `system` payload). `full_context` covers one of the four tools; `tool_calls` covers all of them. `library_links` is neither - it is context, not a tool result, and WITHOUT it the eval's groundedness judge sees a curated URL with no supporting evidence and marks a correct link ungrounded. The widget never sets the flag, so its responses stay exactly `{answer, sources}`. Recording the trace changes nothing about what is sent to the model.

Query flow (`run_agent`): the newest user turn is the current question; the trimmed history seeds the Converse `messages`, and the system prompt is passed via the Converse `system` parameter (never concatenated into the user message). Each turn calls `Converse` with the four-tool `toolConfig` and the output guardrail; on `stopReason == tool_use` the loop executes every requested tool, appends the `toolResult`s, and calls again, until `end_turn` (or a max-iteration cap). There is no `<context>` passage-injection - retrieved passages reach the model as tool results.

## Repo layout

- `infra/` - CDK Python app. Provisions the S3 Vectors store (`CfnVectorBucket` + `CfnIndex`), the KB role, `AWS::Bedrock::KnowledgeBase` (S3_VECTORS storage) + `AWS::Bedrock::DataSource` type S3, the KB source bucket, the scraper Lambda (+ deps layer, weekly schedule, one-click deploy Trigger), the dedicated catalog bucket, and the query path (Lambda + own role, HTTP API with `POST /query` and `GET /warm`). Also hosts the widget (private S3 + CloudFront OAC, `BucketDeployment` of `frontend/widget.js` only) in the SAME stack (OAC cross-stack cyclical dependency), the demo site (a SECOND private S3 + CloudFront OAC pair, see below), and defines the Bedrock guardrails (`CfnGuardrail` + `CfnGuardrailVersion`). Outputs a paste-ready embed tag + CloudFront domain + `/query` URL + `DemoSiteUrl`.
  - `app.py` - CDK entrypoint; loads `config.yaml`, passes to stack
  - `infra/infra_stack.py` - the stack (`GavilanChatbotStack`)
  - `infra/config.py` - `load_config()`; resolves repo-root `config.yaml` from `__file__`
  - `tests/unit/` - `test_infra_stack.py` (Template.from_stack assertions), `test_handler.py` (boto3 stubbed, payload-2.0 events)
- `app/` - query Lambda. `handler.py`: HTTP API (payload 2.0) entrypoint; `run_agent()` runs the agentic Converse tool-use loop over four tools (`search_library_info` -> KB `Retrieve`; `database_catalog` -> reads the catalog from S3; `search_book_catalog` + `search_course_reserves` -> live Primo API calls, client inline in `handler.py`, no import). `_links_block()` renders the bundled link table into the `system` payload. Wiring from env vars set by the stack. boto3 provided by the runtime.
  - `system_prompt.md` - finalized system prompt (`<tools>` section carries the four-tool routing guidance; `<textbook_flow>` routes course textbooks to `search_course_reserves`); read once at cold start, passed via Converse `system`. Packaged via `Code.from_asset(app/)`. NOTE: the curated link table is deliberately absent from `<tools>` because it is no longer a tool - `<citations>` governs it instead, naming the injected CANONICAL GAVILAN LINKS block as one of exactly two permitted link sources.
  - `data/database_catalog.json` - bundled seed catalog: the hand-authored not-held list + a fallback held list, merged with the S3 held list at read time.
  - `data/library_links.json` - bundled, hand-authored table of canonical Gavilan URLs, rendered into the Converse `system` payload by `_links_block()` (library home page, college site, online textbook collections, research guides, bookstore, campus maps, public safety, ILL, laptop record). Fully static: no scraper, no S3, no cache. Edit + redeploy to change.
  - `primo_search.py` - standalone CLI for exploring the Primo discovery API (dev tool; NOT imported by the handler and NOT in the `from_asset` bundle).
- `scraper/` - scraper Lambda source. `scraper.py` (pure fetch/extract, incl. `extract_database_catalog` HTML parse) + `lambda_function.py` (S3 upload, KB ingestion trigger, catalog regeneration: parse -> Sonnet enrichment -> guard -> write to the catalog bucket). Own `.venv`/tests (needs trafilatura). `requirements-dev.txt` pins pytest to the same version as `infra/` and `eval/`; the tests need the runtime deps too, so install both files.
- `eval/` - Bedrock RAG eval harness (boto3 tooling, NOT CDK). Runs on demand against deployed infra; cannot run offline. Retrieve-only formatter (chunking eval) + retrieve-and-generate formatter (answer quality, bring-your-own-inference). `capture_outputs.py` is a STUB until the bot is deployed. Separate `eval_config.yaml`.
  - `measure_usage.py` - measures what a question actually COSTS, by POSTing `/query` with `include_usage` and fitting the results. Prints the paste-ready `cost_model.measured` block for config.yaml. On demand only (it spends real Bedrock money); re-run after anything that moves token usage - `retrieval.number_of_results`, `chunking`, the system prompt, the link table, `generation.max_tokens`.
- `frontend/` - embeddable widget + the demo page. TWO files ship, to two SEPARATE buckets:
  - `widget.js` - the production widget (production-clean, no mock code); reads its endpoint from its `<script>` tag's `data-api-url`, POSTs a multi-turn `{messages: [...]}` array, renders `{answer, sources}`. The ONLY file in the widget bucket. **Bilingual:** every user-visible string lives in the `STRINGS` table (keyed by language code, above the `END LOCALIZATION` banner); render code below that banner calls `t(key)` and holds no copy, which the contract suite enforces by scanning the file. The header carries a two-button English/Español toggle (real buttons, `aria-pressed`, group `aria-label`, visible focus ring - no ID-based ARIA, which cannot cross the shadow boundary), and switching sets `lang` on the host element AND the shadow root container. Each message is stamped with the language it was said in, so a switch relabels the chrome and NOT the transcript: past turns are never retranslated (that would cost a model call per message). The canned greeting + starter questions DO re-render on a switch, but only before the first message - they are the panel's opening state, not a turn anyone took. The language is session-only, deliberately: the widget stores nothing in the browser and a contract test pins that.
  - `demo-site.html` - the shareable demo page, uploaded as `index.html` to the demo bucket. A Gavilan-Library-styled sample page (local CSS only, nothing hotlinked from gavilan.edu) carrying the SAME one-line embed a library page would, so it cannot fork or drift from the shipped widget. Two placeholders, `__WIDGET_SRC__` and `__API_URL__`, are stamped at DEPLOY time (`s3deploy.Source.data` resolves CDK tokens during deployment) - nothing in it is account- or region-specific. Renaming either placeholder fails synth.
  - `mock.js` (dev-only fetch stub) + `demo.html` (offline mock harness) + `demo-live.html` (local page against the deployed API, needs the `localhost:8000` CORS entry) + `test/widget.contract.test.js` (zero-dep Node tests) never ship; dependency direction is one-way (widget never references the mock).
- `config.yaml` - declarative settings at repo root; `cost_model` (published AWS rates + the MEASURED per-question constants + the zero-traffic baseline inputs, stamped into the demo page at deploy), embedding model, `vector_store` (S3 Vectors names, data_type, distance_metric, non-filterable keys), `scraper` seed URLs + schedule, `chunking`, `retrieval.number_of_results`, `generation.model_id`, `catalog` (enrichment model, S3 key, guard threshold, cache TTL), `primo` (the live-catalog knobs `timeout_seconds`, `number_of_results`, `availability_budget_seconds`, wired as `PRIMO_*` env; `search_course_reserves` reuses the same knobs), `library_links.data_file` (the bundled link-table filename; the stack feeds the SAME value to the Lambda asset include and the `LIBRARY_LINKS_FILE` env so they cannot drift), `cors.allow_origins` (the HTTP API browser allowlist), `demo_site.enabled` (the shareable demo page; when on, the stack appends the demo distribution's origin to the CORS allowlist as a deploy-time token), guardrail settings. CDK reads it at synth via `infra/config.py`. Edit values here, do not hardcode in the stack.
- `docs/` - design docs (`architecture.md`, `build-plan.md`, architecture diagram).
- `.github/workflows/ci.yml` - the GitHub Actions checks (see CI under Commands). Four hermetic jobs, one per test surface.

## Excluded (do not reintroduce)

- **Contextual grounding EXCLUDED.** AWS doesn't support it for conversational chatbots; requires fragile `guardContent` message-tagging (silent-failure trap). The system prompt handles grounding. Revisit via standalone `ApplyGuardrail`, not inline Converse tagging.
- **WAF EXCLUDED.** WAF can't attach to HTTP API v2 (would need a second CloudFront fronting the API). Thin threat surface; API Gateway throttling is the real cost-abuse control. Revisit only on a compliance mandate.
- **CORS `allow_origins: "*"` EXCLUDED.** Locked to `cors.allow_origins` in config.yaml (`https://www.gavilan.edu`, a dev-only `http://localhost:8000`, and the demo site's custom hostname `https://gavbot-demo.calpoly.io`); `infra/config.py` rejects a wildcard at synth. Entries are matched as EXACT full origin strings, so each is scheme + host with no trailing slash and no path. The browser sends a HOST-only `Origin`, so do not add the `/library/` path. CORS is browser-enforced only and is NOT a security boundary (curl/scripts ignore it) - throttling is still the cost cap - but a wildcard would let any page drive the billable `/query` endpoint from its visitors' browsers. Don't "fix" a CORS console error by widening this; add the real origin.
- **`generative-ai-cdk-constructs` Bedrock L2s EXCLUDED (deprecated).** That is WHY the stack is L1 `Cfn*`. Do not reintroduce.

## Guardrail note

Needs `bedrock:ApplyGuardrail` on the guardrail ARN alongside `InvokeModel`, or it silently fails at runtime. The Lambda pins to a published numbered guardrail version; live policy/message edits require a new version.

The two blocked messages in `config.yaml` are BILINGUAL (English then Spanish, one string, blank line between). They are static guardrail configuration returned by Bedrock instead of a model reply, so nothing translates them at runtime and the widget's language control cannot reach them - a Spanish-speaking student who trips a guardrail would otherwise hit an English wall. Bedrock caps each at 500 characters, so keep the pair under it. `_FALLBACK_BLOCK_MESSAGE` in the handler (the defensive path when the guardrail returns no text) is bilingual for the same reason. Editing them takes effect only on the next `cdk deploy`, which publishes a new guardrail version and repins the Lambda.

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
- **A live third-party API in the agent loop must never be able to kill a request.** `search_book_catalog`/`search_course_reserves` hit the undocumented Primo discovery endpoint (search + a per-record delivery call for availability), whose fields use a `$$C..$$V..` / `$$R..$$V..$$M..` encoding - parse it defensively (never index blindly; a shape change must degrade, not throw). Every call is timed out with a total availability budget, and any failure soft-fails to a "catalog unavailable" toolResult so the loop still answers. Availability from the delivery call is a report of what the catalog SHOWS, not a guarantee a copy is on the shelf - the prompt phrases it that way, and `total == 0` is the only authoritative not-held signal (Primo relevance scores are query-relative, so the model judges matches).- **A constant does not belong behind a tool call.** `library_links` took no input and returned the same rows every call, and the model kept not calling it: it would answer "where is the financial aid office" from retrieved text, feel finished, and never fetch the map. That is the measured **Tool-Skip** failure mode (frontier models skip a required call 12-26% of the time - [ToolFailBench](https://arxiv.org/html/2607.04686v1)), and tool-description work cannot fix it, because a description is only read once the model has already decided to look something up. Worse, tool necessity is [linearly decodable from hidden states at AUROC 0.89-0.96](https://arxiv.org/html/2605.09252v1) - the model *knows* and doesn't act, so more instruction is not the missing ingredient. The same source found prompt-only interventions move the whole tool-call distribution rather than the boundary. Fix: preload it. Anything small, static, and usually-relevant belongs in context, not behind a call the model has to remember to make. Revisit only if such a table outgrows ~30 rows.
- **Moving data out of a tool result silently breaks the eval judge.** The groundedness judge grades against `tool_calls`. Anything the model sees that is NOT a tool result (the curated links, now a `system` block) appears nowhere in that trace, so a correct, curated URL reads as an invented one and scores as ungrounded - a scoring regression that looks exactly like a quality drop. Any change that relocates evidence must add it to the debug payload AND tell the judge prompt it exists, in the same commit.
- **A second `BucketDeployment` into the widget bucket would delete widget.js.** `BucketDeployment` defaults to `prune=True`, which is `aws s3 sync --delete`: on every deploy it removes destination objects its own source does not contain. Two deployments sharing one bucket therefore fight, and whichever CloudFormation runs last wins - production widget delivery would break intermittently, not reproducibly. `destination_key_prefix` does scope the prune (verified in `aws-cdk-lib==2.260.0`: "if it's set with prune: true, it will only prune files with the prefix"), and so does `exclude`, but both are a config away from wrong. The demo site gets its OWN bucket and its OWN distribution instead, so the interference is structurally impossible; `test_demo_site_does_not_change_widget_delivery` pins that the widget path is byte-identical with the demo on and off.
- **Deploy-time values reach a STATIC file via `s3deploy.Source.data`, not an env var.** The "config only reaches Lambdas as env vars" rule has no equivalent for a hosted page - a hardcoded endpoint in HTML would break one-click install in a fresh account. `Source.data(key, content)` resolves CDK tokens *during deployment*: CDK stages the file with `<<marker:0xbaba:N>>` placeholders and the deployment custom resource substitutes them from `SourceMarkers` (an `Fn::GetAtt` per marker). Verified in the synthesized template and the staged asset. Note it substitutes EVERY literal occurrence, comments included - so do not spell the placeholder out in the file's own documentation.

- **Cost scales with LOOP LENGTH, not conversation depth - the opposite of the obvious guess.** Measured over 60 live questions (`eval/measure_usage.py`, 2026-07-29): a question costs ~$0.043, and **91% of that is input tokens**. The intuition that "history is client-carried and resent every turn, so deep conversations get expensive fast" is measurably wrong here: a prior turn adds only **~54 tokens** to a ~10,700-token call, because `_seed_messages` rebuilds history from TEXT turns only - the tool results and retrieved passages behind an earlier answer are never resent. It also stops growing at the `MAX_HISTORY_MESSAGES` trim. What actually moves cost is whether the model needs a second Converse call (measured 1.23 calls/question), because that resends the entire ~10,700-token context again - worth about fifty turns of history growth in one go. First questions need that second call far more often than follow-ups do, which drags a naive cost-vs-position fit NEGATIVE and would read as "conversations get cheaper as they go". Divide by `model_calls` before fitting depth, or you measure neither effect. Practical consequence: the lever on cost is `retrieval.number_of_results` and the priming retrieval (they set the 10,700), not conversation length.
- **Serving Spanish was a FRONTEND problem, not a retrieval one.** Verified by hand against the deployed system (2026-07-29): Spanish questions already retrieve correctly from the ENGLISH knowledge base and come back grounded and specific, including the authoritative database tool ("¿Tienen JSTOR?" -> correctly not held, with held alternatives) and the live Primo catalog ("un libro sobre la Revolución Mexicana" -> 12 titles). Unaccented input works too. So do NOT translate the corpus, re-ingest, or touch chunking/retrieval for a language feature - all of that is cost and risk for a problem that does not exist. The actual gap was the shell around the conversation: every piece of widget chrome was hardcoded English, so before typing anything a Spanish speaker had no signal the bot speaks Spanish, and auto-detection cannot close that gap because it needs a message first. That is why the control is a VISIBLE affordance rather than a language sniffer, and why the whole backend change is one optional request field and one system block.
- **The offline chunking eval measures boundaries, never ranking.** `eval/run_chunking_eval.py` can tell you a 300-token window cuts 26% of golden answers in half; it cannot tell you whether a 600-token chunk still retrieves, because a bigger chunk averages its embedding over more text and can retrieve less precisely. The two halves need two instruments: pair it with `eval/retrieval_probe.py` against the live index. 300-token baseline to compare against: recall@1 71%, @3 88%, @5 94%, @8 100%.
