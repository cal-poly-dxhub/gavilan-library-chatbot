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

- Lambda unit tests (once `app/` exists) will use `moto` to mock AWS (no live account needed)

## Knowledge Base / vector store facts

Vector index field names. These MUST stay identical between the `CfnIndex` mappings and
the KB `field_mapping` (defined once in `config.yaml` under `vector_store.fields`):
- vector field: `bedrock-knowledge-base-default-vector` (knn_vector, 1024 dims = Titan Text Embeddings v2, faiss/hnsw/l2)
- text field: `AMAZON_BEDROCK_TEXT_CHUNK`
- metadata field: `AMAZON_BEDROCK_METADATA`
- index name: `bedrock-knowledge-base-default-index`

## Repo layout

- `infra/` — CDK Python app (created by `cdk init`). The stack now provisions the full
  vector store, the Bedrock Knowledge Base, and its Web Crawler data source (all L1
  `Cfn*`): OpenSearch Serverless collection + encryption/network security policies, KB
  execution IAM role, data access policy, vector index, the `AWS::Bedrock::KnowledgeBase`
  (VECTOR, OSS storage), and the `AWS::Bedrock::DataSource` (type WEB, FIXED_SIZE
  chunking). The Lambda, API Gateway, and widget are NOT wired yet. Structure:
  - `app.py` — CDK app entrypoint; loads `config.yaml` and passes it to the stack
  - `infra/infra_stack.py` — the stack (`GavilanChatbotStack`); reads all knobs from config
  - `infra/config.py` — `load_config()`; resolves the repo-root `config.yaml` from `__file__`
  - `infra/__init__.py` — package marker
  - `tests/unit/test_infra_stack.py` — stack assertion tests
  - `requirements.txt` — pinned `aws-cdk-lib==2.260.0`, `constructs`, `PyYAML`
  - `requirements-dev.txt` — `pytest`
  - `cdk.json` — toolkit config (`app: python3 app.py`)
  - `.venv/` — virtualenv (gitignored)
- `app/` — Lambda code (empty; planned): two functions — (1) call KB `Retrieve`, (2) call Bedrock to generate
- `eval/` — eval harness (empty; planned): Q&A set + retrieval/faithfulness scoring
- `frontend/` — JS widget (empty; planned)
- `config.yaml` — declarative settings at the repo root; single source of truth for
  changeable knobs (embedding model, vector store names/fields, crawler seed URLs +
  filters + scope + rate limits, chunking). The CDK app reads it at synth time via
  `infra/config.py` (resolved relative to `__file__`, so cwd does not matter). Edit values
  here rather than hardcoding in the stack.
- `docs/` — design docs (`architecture.md`)

## Hard rules

- **Do not touch Git.** Never commit, or do anything related to Github. 
- **Behavior lives in the system prompt, not routing code** (v1). Textbook clarifying questions and out-of-scope handling are prompt instructions, not Lambda branches.


## Writing style

No em dashes. No emojis. Direct and concise.

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
- **Deploy-time gaps `cdk synth` cannot catch (no surprises later):**
  - The CDK/CloudFormation execution role needs `aoss:` permissions to create the index at
    deploy time (the default bootstrap role has `es:*`, not `aoss:*`). It must also appear
    as a Principal in the data access policy, since CloudFormation, not the KB role, is the
    principal that actually creates `CfnIndex`. Today the policy names only the KB role.
  - `CfnIndex` creation is eventually-consistent; the KB may race the index becoming ACTIVE
    on first real deploy. Fallback if it does: a custom-resource index creator.