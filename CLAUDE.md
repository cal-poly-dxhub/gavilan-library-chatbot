# CLAUDE.md

## What this is

RAG chatbot for Gavilan College Library. Answers operational student questions (hours, checkout, textbooks, "what does the library offer"). Not an AI research librarian; research questions get pointed to a human librarian. AWS-native.

## Stack

- **RAG:** Amazon Bedrock Managed Knowledge Base (Web Crawler connector -> OpenSearch Serverless NextGen vector store)
- **Generation:** Bedrock-hosted Claude via `Retrieve` + own generate call (NOT `RetrieveAndGenerate`; full system-prompt control required)
- **Backend:** Lambda + HTTP API (API Gateway v2), Python
- **Infra:** AWS CDK (Python), L1 `Cfn*` constructs; everything as code
- **Guardrails:** Bedrock Guardrails (content + PII)
- **Config:** `config.yaml` at repo root; single source of truth for changeable knobs
- **Frontend:** vanilla JS widget (Shadow DOM, dependency-free)

## Read the docs before design work

Architecture, decisions, and rationale are in `docs/architecture.md`. When a task touches architecture or a "why was X chosen" question, read it first. Do not re-derive or contradict decisions recorded there.

## Commands

CDK app lives in `infra/`. All infra commands run from `infra/` with the venv active.

First-time setup (from `infra/`):
- `source .venv/bin/activate`
- `python -m pip install -r requirements.txt -r requirements-dev.txt`

Then (from `infra/`, venv active):
- Synth (offline, no creds): `cdk synth`
- Tests: `python -m pytest`
- Deploy: `cdk deploy` (needs AWS creds; account pending)
- Teardown: `cdk destroy` (also removes the OSS collection; deleting the KB alone does not)

Pinned: `aws-cdk-lib==2.260.0`, CDK CLI `2.1129.0`.

Handler unit tests stub `boto3` in `sys.modules` and monkeypatch the client getters, so they need no boto3 install and no live AWS.

## Vector store invariant

Vector index field names MUST stay identical between the `CfnIndex` mappings and the KB `field_mapping` (defined once in `config.yaml` under `vector_store.fields`):
- vector field: `bedrock-knowledge-base-default-vector` (knn_vector, 1024 dims = Titan Text Embeddings v2, faiss/hnsw/l2)
- text field: `AMAZON_BEDROCK_TEXT_CHUNK`
- metadata field: `AMAZON_BEDROCK_METADATA`
- index name: `bedrock-knowledge-base-default-index`

## `/query` contract (widget builds against this)

`POST /query` response shape:
`{ "answer": "<text>", "sources": [ {"uri": "<page url>", "excerpt": "<snippet>"} ] }`

`sources` is deduplicated by uri, in retrieval order; passages with no resolvable source uri are omitted (they still inform the answer); on empty retrieval `sources` is `[]`.

Prompt/handler contract: the prompt expects retrieved passages inside `<context>` tags; the handler wraps chunks in exactly that tag (`handler.CONTEXT_TAG == "context"`). User message is the `<context>` block followed by `Question: <query>`. The system prompt is passed via the Converse `system` parameter, never concatenated into the user message.

## Repo layout

- `infra/` - CDK Python app. Provisions ingestion (OSS collection + security policies, KB role, data-access policy, `CfnIndex`, `AWS::Bedrock::KnowledgeBase`, `AWS::Bedrock::DataSource` type WEB) and the query path (Lambda + own role, HTTP API with `POST /query`). Also hosts the widget (private S3 + CloudFront OAC, `BucketDeployment` of `frontend/widget.js` only) in the SAME stack (OAC cross-stack cyclical dependency), and defines the Bedrock guardrail (`CfnGuardrail` + `CfnGuardrailVersion`). Outputs a paste-ready embed tag + CloudFront domain + `/query` URL.
  - `app.py` - CDK entrypoint; loads `config.yaml`, passes to stack
  - `infra/infra_stack.py` - the stack (`GavilanChatbotStack`)
  - `infra/config.py` - `load_config()`; resolves repo-root `config.yaml` from `__file__`
  - `tests/unit/` - `test_infra_stack.py` (Template.from_stack assertions), `test_handler.py` (boto3 stubbed, payload-2.0 events)
- `app/` - Lambda. `handler.py`: HTTP API (payload 2.0) entrypoint; `retrieve()` (KB `Retrieve`, returns `{text, source}` per chunk) then `generate()` (Bedrock Converse). Wiring from env vars set by the stack. boto3 provided by the runtime.
  - `system_prompt.md` - finalized system prompt; read once at cold start, passed via Converse `system`. Packaged via `Code.from_asset(app/)`.
- `eval/` - Bedrock RAG eval harness (boto3 tooling, NOT CDK). Runs on demand against deployed infra; cannot run offline. Retrieve-only formatter (chunking eval) + retrieve-and-generate formatter (answer quality, bring-your-own-inference). `capture_outputs.py` is a STUB until the bot is deployed. Separate `eval_config.yaml`.
- `frontend/` - embeddable widget. `widget.js` is the ONLY file shipped (production-clean, no mock code); reads its endpoint from its `<script>` tag's `data-api-url`, POSTs `{query}`, renders `{answer, sources}`. `mock.js` (dev-only fetch stub) + `demo.html` (offline harness) + `test/widget.contract.test.js` (zero-dep Node tests) never ship; dependency direction is one-way (widget never references the mock).
- `config.yaml` - declarative settings at repo root; embedding model, vector store names/fields, crawler seeds/filters/scope/rate limits, chunking, `retrieval.number_of_results`, `generation.model_id`. CDK reads it at synth via `infra/config.py`. Edit values here, do not hardcode in the stack.
- `docs/` - design docs (`architecture.md`).

## Excluded-for-v1 decisions (rationale in architecture.md; do not reintroduce)

- **Contextual grounding EXCLUDED.** AWS doesn't support it for conversational chatbots; requires fragile `guardContent` message-tagging (silent-failure trap). The system prompt handles grounding. Revisit via standalone `ApplyGuardrail`, not inline Converse tagging.
- **WAF EXCLUDED.** WAF can't attach to HTTP API v2 (would need a second CloudFront fronting the API). Thin threat surface; API Gateway throttling is the real cost-abuse control. Revisit only on a compliance mandate.
- **`generative-ai-cdk-constructs` Bedrock L2s EXCLUDED (deprecated).** That is WHY the stack is L1 `Cfn*`. Do not reintroduce.

## Guardrail note

Needs `bedrock:ApplyGuardrail` on the guardrail ARN alongside `InvokeModel`, or it silently fails at runtime. The Lambda pins to a published numbered guardrail version; live policy/message edits require a new version.

## Hard rules

- **Do not touch Git.** Never commit or do anything related to GitHub.
- **Behavior lives in the system prompt, not routing code** (v1). Textbook clarifying questions and out-of-scope handling are prompt instructions, not Lambda branches.

## Writing style

Avoid em dashes; use " - " or parentheses instead.

## When testing

Never hide failures. Show all test results, including failures, in full.

## Lessons

Things learned the hard way. Read before repeating the same work.

- **Verify CDK construct APIs against the installed package, not memory.** The Bedrock/OSS construct surface is in flux and training data is stale. For `aws-cdk-lib==2.260.0`, introspecting the installed package settled several points live docs got wrong: `CfnIndex` takes structured `mappings`/`settings` (not an `index_body` blob); `VectorKnowledgeBaseConfigurationProperty` has no `type` (the `type` is on `KnowledgeBaseConfigurationProperty`); `CfnIndex.MethodProperty` uses `name`/`engine`/`space_type`/`parameters`.
- **HTTP API v2 is in `aws-cdk-lib` core as of 2.260.0.** `HttpApi` + `CorsPreflightOptions` live in `aws_cdk.aws_apigatewayv2`, `HttpLambdaIntegration` in `aws_cdk.aws_apigatewayv2_integrations`. The old `-alpha` packages are gone (import fails) and are NOT needed. HTTP API not REST (~71% cheaper for a Lambda-proxy job). `HttpLambdaIntegration` defaults to payload format 2.0.
- **Deploy-time gaps `cdk synth` cannot catch:**
  - The CloudFormation execution role needs `aoss:` permissions to create the index at deploy time (default bootstrap role has `es:*`, not `aoss:*`). It must also appear as a Principal in the data-access policy, since CloudFormation (not the KB role) is the principal that creates `CfnIndex`. Today the policy names only the KB role.
  - `CfnIndex` creation is eventually-consistent; the KB may race the index becoming ACTIVE on first deploy. Fallback: a custom-resource index creator.
  - **CloudFront is slow to create AND destroy (~15-30 min each).** The widget distribution dominates `cdk deploy`/`destroy` wall-clock. Actual serving behavior (OAC read, cache, HTTPS redirect) is only verifiable at deploy.
  - **OAC + `auto_delete_objects` dependency cycle.** Do NOT add an explicit `distribution.node.add_dependency(bucket)`: the origin already references the bucket, and the explicit edge pulls in the bucket's auto-delete custom resource, which `DependsOn` the OAC bucket policy, which `DependsOn` the distribution -> a synth-blocking cycle. Surfaces in `Template.from_stack` (tests) even when plain `cdk synth` looks fine; keep infra tests in the loop.