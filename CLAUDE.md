# CLAUDE.md

## What this is

RAG chatbot for Gavilan College Library. Answers operational student questions (hours, checkout, textbooks, "what does the library offer"). Not an AI research librarian; research questions get pointed to a human librarian. AWS-native.

## Stack

- **RAG:** Amazon Bedrock Managed Knowledge Base (Web Crawler connector → OpenSearch Serverless NextGen vector store)
- **Generation:** Bedrock-hosted Claude via `Retrieve` + own generate call (NOT `RetrieveAndGenerate` — full system-prompt control required)
- **Backend:** Lambda + API Gateway (Python)
- **Infra:** AWS CDK (Python) — everything declared as code, nothing built by hand
- **Guardrails:** Bedrock Guardrails (content + PII)
- **Config:** `config.yaml` — model IDs, chunking, reranking, thresholds live here, not in code
- **Frontend:** custom JS widget

## Read the docs before design work

Architecture and rationale are in `docs/`:
- `docs/architecture.md` — stack decisions, phases, data flow, verified facts

When a task touches architecture or a "why was X chosen" question, read these first. Do not re-derive or contradict decisions recorded there.

## Commands

The CDK app lives in `infra/`. The `cdk` CLI is installed globally. All infra commands
run from `infra/` with the project virtualenv active.

First-time setup (from `infra/`):
- `python3 -m venv .venv` (already created by `cdk init`)
- `source .venv/bin/activate`
- `python -m pip install -r requirements.txt -r requirements-dev.txt`

Then, from `infra/` with the venv active:
- Infra synth (offline, no creds): `cdk synth`
- Infra tests: `python -m pytest`
- Infra deploy: `cdk deploy` (needs AWS creds — company account, pending)
- Infra teardown: `cdk destroy` (IMPORTANT: also removes the OSS collection; deleting the KB alone does not)

Pinned: `aws-cdk-lib==2.260.0`, CDK CLI `2.1129.0`.

- Handler unit tests (`tests/unit/test_handler.py`) stub `boto3` in `sys.modules` and
  monkeypatch the client getters, so they need no boto3 install and no live AWS.

## Knowledge Base / vector store facts

Vector index field names. These MUST stay identical between the `CfnIndex` mappings and
the KB `field_mapping` (defined once in `config.yaml` under `vector_store.fields`):
- vector field: `bedrock-knowledge-base-default-vector` (knn_vector, 1024 dims = Titan Text Embeddings v2, faiss/hnsw/l2)
- text field: `AMAZON_BEDROCK_TEXT_CHUNK`
- metadata field: `AMAZON_BEDROCK_METADATA`
- index name: `bedrock-knowledge-base-default-index`

## Repo layout

- `infra/` — CDK Python app (created by `cdk init`). The stack provisions the full
  ingestion side plus the query path: OpenSearch Serverless collection +
  encryption/network security policies, KB execution IAM role, data access policy, vector
  index, the `AWS::Bedrock::KnowledgeBase` (VECTOR, OSS storage), the
  `AWS::Bedrock::DataSource` (type WEB, FIXED_SIZE chunking), a query-path Lambda with its
  OWN execution role, and an HTTP API (API Gateway v2) with a `POST /query` route.
  It ALSO hosts the widget: a private S3 bucket (block-all-public, bucket-owner-enforced)
  fronted by a CloudFront distribution using **OAC** (`origins.S3BucketOrigin.with_origin_access_control`,
  NOT OAI/S3Origin), with a `BucketDeployment` uploading ONLY `frontend/widget.js` (never
  `mock.js`/`demo.html`) and invalidating `/widget.js` on deploy. Hosting is in the SAME
  stack as the backend (OAC has a cross-stack cyclical-dependency problem; one-click install
  wants one deploy). The stack outputs a ready-to-paste embed tag
  (`<script src="https://{cloudfront}/widget.js" data-api-url="https://{api}/query" defer></script>`)
  plus the raw CloudFront domain and `/query` URL. Guardrails, WAF, and auth are NOT wired
  yet. Structure:
  - `app.py` — CDK app entrypoint; loads `config.yaml` and passes it to the stack
  - `infra/infra_stack.py` — the stack (`GavilanChatbotStack`); reads all knobs from config
  - `infra/config.py` — `load_config()`; resolves the repo-root `config.yaml` from `__file__`
  - `infra/__init__.py` — package marker
  - `tests/unit/test_infra_stack.py` — stack assertion tests
  - `tests/unit/test_handler.py` — handler unit tests (boto3 stubbed, payload-2.0 events)
  - `requirements.txt` — pinned `aws-cdk-lib==2.260.0`, `constructs`, `PyYAML`
  - `requirements-dev.txt` — `pytest`
  - `cdk.json` — toolkit config (`app: python3 app.py`)
  - `.venv/` — virtualenv (gitignored)
- `app/` — Lambda code. `handler.py`: HTTP API (payload format 2.0) entrypoint with two
  separate steps - `retrieve()` (KB `Retrieve`, NOT RetrieveAndGenerate; returns
  `{text, source}` per chunk) and `generate()` (Bedrock Converse). Wiring comes from env
  vars set by the stack. boto3 is provided by the Lambda runtime (not vendored, not a dev dep).
  - `system_prompt.md` - the real, finalized system prompt (the Gavilan Library assistant
    role, scope, grounding, handoff, textbook flow, tone, fixed rules). Read once at cold
    start via `Path(__file__).parent`, and passed to Converse via the `system` parameter
    (never concatenated into the user message). It is packaged with the Lambda because the
    stack uses `Code.from_asset(app/)`, which bundles the whole directory (verified in the
    synthesized asset).
  - Prompt/handler contract: the prompt expects retrieved passages inside `<context>` tags,
    and the handler wraps chunks in exactly that tag (`handler.CONTEXT_TAG == "context"`).
    The user message is the `<context>` block followed by `Question: <query>`.
  - **`POST /query` response JSON shape** (the frontend builds against this):
    `{ "answer": "<text>", "sources": [ {"uri": "<page url>", "excerpt": "<snippet>"} ] }`.
    `sources` is deduplicated by uri, in retrieval order; passages with no resolvable source
    uri are omitted from `sources` (they still inform the answer); on empty retrieval
    `sources` is `[]` and the prompt tells the model to say it does not have the info.
- `eval/` — eval harness (empty; planned): Q&A set + retrieval/faithfulness scoring
- `frontend/` — embeddable JS widget (Shadow DOM, vanilla JS, dependency-free).
  - `widget.js` — the ONLY file shipped to users. Production-clean: it contains just
    the real request path and NO mock code, mock data, or mock branching. It reads its
    backend endpoint from its own `<script>` tag's `data-api-url` attribute (the one swap
    point) and POSTs `{ query }` to that URL, rendering the `{ answer, sources }` contract.
    Until `data-api-url` is set it shows a graceful "not connected yet" message.
  - `mock.js` — dev-only backend stand-in, used ONLY by `demo.html` and the tests; it is
    never shipped. In the browser it transparently monkeypatches `window.fetch` to answer
    requests to the widget's `data-api-url` with canned responses (and a "trigger error"
    backdoor -> HTTP 500 to exercise the error state); in Node it exports `mockQuery`. The
    dependency direction is one-way: demo + tests reference the mock; `widget.js` never
    does and has no awareness it exists.
  - `demo.html` — offline dev harness (hostile-CSS Shadow DOM isolation test). Loads
    `mock.js` before `widget.js` (defer preserves order) so the widget's normal fetch is
    intercepted with no backend.
  - `test/widget.contract.test.js` — zero-dependency Node tests (`node
    test/widget.contract.test.js`): response-contract normalization + URL sanitizer in
    widget.js, mock routing/shape in mock.js, and a static scan asserting widget.js stays
    production-clean (no mock/backdoor/canned-source references).
- `config.yaml` — declarative settings at the repo root; single source of truth for
  changeable knobs (embedding model, vector store names/fields, crawler seed URLs +
  filters + scope + rate limits, chunking, `retrieval.number_of_results`,
  `generation.model_id`). The CDK app reads it at synth time via `infra/config.py`
  (resolved relative to `__file__`, so cwd does not matter). Edit values here rather than
  hardcoding in the stack.
- `docs/` — design docs (`architecture.md`)

## Hard rules

- **Do not touch Git.** Never commit or do anything related to GitHub.
- **Behavior lives in the system prompt, not routing code** (v1). Textbook clarifying questions and out-of-scope handling are prompt instructions, not Lambda branches.


## Writing style

Avoid em dashes (—); use " - " or parentheses instead.

## When testing

Never hide failures. Show all test results, including failures, in full.

## Lessons

Things learned the hard way. Read before repeating the same work.

- **Verify CDK construct APIs against the installed package, not memory.** The Bedrock/OSS
  construct surface is in flux and training data is stale. For `aws-cdk-lib==2.260.0`,
  introspecting the installed package settled several points that live docs got wrong:
  `CfnIndex` takes structured `mappings`/`settings` (not an `index_body` blob);
  `VectorKnowledgeBaseConfigurationProperty` has no `type` (the `type` is on
  `KnowledgeBaseConfigurationProperty`); `CfnIndex.MethodProperty` uses
  `name`/`engine`/`space_type`/`parameters`.
- **HTTP API v2 is in `aws-cdk-lib` core as of 2.260.0.** `HttpApi` +
  `CorsPreflightOptions` live in `aws_cdk.aws_apigatewayv2` and `HttpLambdaIntegration` in
  `aws_cdk.aws_apigatewayv2_integrations`. The old `aws-cdk.aws-apigatewayv2-alpha` /
  `-integrations-alpha` packages are gone (import fails) and are NOT needed. Decision:
  HTTP API, not REST (~71% cheaper for a Lambda-proxy job, no REST-only features needed).
  `HttpLambdaIntegration` defaults to payload format version 2.0.
- **Deploy-time gaps `cdk synth` cannot catch (no surprises later):**
  - The CDK/CloudFormation execution role needs `aoss:` permissions to create the index at
    deploy time (the default bootstrap role has `es:*`, not `aoss:*`). It must also appear
    as a Principal in the data access policy, since CloudFormation, not the KB role, is the
    principal that actually creates `CfnIndex`. Today the policy names only the KB role.
  - `CfnIndex` creation is eventually-consistent; the KB may race the index becoming ACTIVE
    on first real deploy. Fallback if it does: a custom-resource index creator.
  - **CloudFront is slow to create AND destroy (~15-30 min each).** The widget distribution
    dominates `cdk deploy`/`cdk destroy` wall-clock time. The distribution's actual serving
    behavior (OAC read of the S3 object, cache, HTTPS redirect) is only verifiable at deploy,
    not by `cdk synth`.
  - **OAC + `auto_delete_objects` dependency cycle.** Do NOT add an explicit
    `distribution.node.add_dependency(bucket)`: the origin already references the bucket, and
    the explicit edge pulls in the bucket's auto-delete custom resource, which `DependsOn` the
    OAC bucket policy, which `DependsOn` the distribution -> a synth-blocking cycle. The bug
    surfaces in `Template.from_stack` (tests) even when a plain `cdk synth` looks fine, so keep
    the infra tests in the loop.