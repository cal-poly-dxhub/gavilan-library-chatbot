# CLAUDE.md

## What this is

RAG chatbot for Gavilan College Library. Answers operational student questions (hours, checkout, textbooks, "what does the library offer"). Not an AI research librarian; research questions get pointed to a human librarian. AWS-native.

## Stack

- **RAG:** Amazon Bedrock Managed Knowledge Base (Web Crawler connector → OpenSearch Serverless NextGen vector store)
- **Generation:** Bedrock-hosted Claude via `Retrieve` + own generate call (NOT `RetrieveAndGenerate` — we need full system-prompt control)
- **Backend:** Lambda + API Gateway (Python)
- **Infra:** AWS CDK (Python) — everything declared as code, nothing built by hand
- **Guardrails:** Bedrock Guardrails (content + PII)
- **Config:** `config.yaml` — model IDs, chunking, reranking, thresholds live here, not in code
- **Frontend:** custom JS widget

## Read the docs before design work

Architecture and rationale are in `docs/`:
- `docs/architecture.md` — stack decisions, phases, data flow, verified facts

When a task touches architecture or a "why did we choose X" question, read these first. Do not re-derive or contradict decisions recorded there.

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

## Repo layout

- `infra/` — CDK Python app (created by `cdk init`). Currently a minimal Phase 0 skeleton:
  the OpenSearch Serverless vector collection only. KB, crawler, Lambda, API Gateway, IAM
  come next. Structure:
  - `app.py` — CDK app entrypoint; instantiates `GavilanChatbotStack`
  - `infra/infra_stack.py` — the stack (`GavilanChatbotStack`)
  - `infra/__init__.py` — package marker
  - `tests/unit/test_infra_stack.py` — stack assertion tests
  - `requirements.txt` — pinned `aws-cdk-lib==2.260.0`, `constructs`
  - `requirements-dev.txt` — `pytest`
  - `cdk.json` — toolkit config (`app: python3 app.py`)
  - `.venv/` — virtualenv (gitignored)
- `app/` — Lambda code (empty; planned): two functions — (1) call KB `Retrieve`, (2) call Bedrock to generate
- `eval/` — eval harness (empty; planned): Q&A set + retrieval/faithfulness scoring
- `frontend/` — JS widget (empty; planned)
- `config.yaml` — declarative settings (not created yet)
- `docs/` — design docs (`architecture.md`)

## Hard rules

- **Do not touch Git.** Never commit, or do anything related to Github. 
- **Behavior lives in the system prompt, not routing code** (v1). Textbook clarifying questions and out-of-scope handling are prompt instructions, not Lambda branches.


## Writing style

No em dashes. No emojis. Direct and concise.

## When testing

Never hide failures. Show all test results, including failures, in full.