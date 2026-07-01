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

<!-- TODO: fill in as code lands. Repo is pre-build; these don't all exist yet. -->
- Infra synth (offline validation): `cdk synth`
- Infra deploy: `cdk deploy` (needs AWS creds — company account, pending)
- Infra teardown: `cdk destroy` (IMPORTANT: also removes the OSS collection; deleting the KB alone does not)
- Python tests: <!-- TODO: pytest invocation once test dir exists -->
- Lambda unit tests use `moto` to mock AWS (no live account needed)

## Repo layout

<!-- TODO: update as directories are created -->
- `infra/` — CDK app (KB, OSS NextGen, crawler, Lambda, API Gateway, IAM)
- `app/` — Lambda code: two separate functions — (1) call KB `Retrieve`, (2) call Bedrock to generate
- `eval/` — eval harness: Q&A set + retrieval/faithfulness scoring
- `frontend/` — JS widget
- `config.yaml` — declarative settings
- `docs/` — design docs

## Hard rules

- **Do not touch Git.** Never commit, or do anything related to Github. 
- **Behavior lives in the system prompt, not routing code** (v1). Textbook clarifying questions and out-of-scope handling are prompt instructions, not Lambda branches.


## Writing style

No em dashes. No emojis. Direct and concise.

## When testing

Never hide failures. Show all test results, including failures, in full.