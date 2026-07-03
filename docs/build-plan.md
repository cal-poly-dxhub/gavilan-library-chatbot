# Gavilan Library Chatbot — Build Plan

**Companion docs:** `architecture.md` (what/why), `blocked-on-aws.md`.
**This doc:** todo tracker. Done / Next / V2.
**Updated:** 2026-07-02

---

## V1

- [x] Project docs (architecture, CLAUDE.md, README, project overview)
- [x] Repo + CDK scaffold (`cdk init`, structure, gitignore)
- [x] **CDK infra skeleton** — synths clean:
  - [x] OpenSearch Serverless collection + encryption/network/data-access policies
  - [x] Vector index (`CfnIndex`, 1024-dim knn_vector)
  - [x] Bedrock Knowledge Base (wired to collection + index)
  - [x] Web Crawler data source (seed URLs + filters from config)
  - [x] Lambda (Retrieve + Converse) + own execution role
  - [x] HTTP API (API Gateway v2, POST /query)
  - [x] `config.yaml` single source of truth
  - [!] Deploy blocked: see `blocked-on-aws.md` (index-auth, aoss perms, index race, crawler values, NextGen confirm)
- [x] **Eval harness** — coded + 43 tests, cannot run until access:
  - [x] Structure + shared boto3 job-runner + CSV loader
  - [x] Retrieve-only formatter (chunking/retrieval evaluator)
  - [x] Retrieve-and-generate formatter (answer-quality evaluator)
  - [!] Capture-outputs is a hard stub (needs deployed bot); `retrievedPassages` key unverified; see `blocked-on-aws.md`
- [x] AWS architecture diagram (`diagrams` lib, PNG in docs/) — as-built; only WAF marked planned now
- [x] System prompt draft (`system-prompt.md`, XML-structured, hard-grounding, single-turn)
- [x] **Wire the real system prompt** — placeholder replaced. Prompt in Converse `system` param; handler wraps chunks in `<context>` tag (contract pinned + tested); `/query` returns `{answer, sources[]}`.
- [x] **Widget frontend** — vanilla JS, Shadow DOM isolation, self-injecting single file reading `data-api-url`. Mock extracted to `frontend/mock.js` (demo + tests only); `widget.js` production-clean. 18 tests.
- [x] **Widget hosting** — S3 + CloudFront (OAC) in the same CDK stack; `BucketDeployment` uploads only `widget.js`; CfnOutput assembles the ready-to-paste `<script>` tag. 22 infra tests.
- [x] **Guardrails** — Bedrock Guardrails (content filters + PII) wired into the Converse call via `guardrailConfig`. Grounding deliberately excluded (see below). Config-driven, blocked-response handling, per-request assessment logged. 30 tests.
- [ ] **Fable 5 audit** — full-stack adversarial review, audit-only, produces `docs/review-findings.md`. Triage findings after.
- [ ] **Provide context** — sponsor URL list + blacklist (arrives ~July 19) into `config.yaml`; sponsor Q&A set into `eval/datasets/`.
- [ ] **Iterate** (needs AWS account) — deploy, run retrieve-only eval, tune: FIXED_SIZE vs HIERARCHICAL chunking, SEMANTIC vs HYBRID search, prompt behavior. Eval-driven.
- [ ] Fix the deploy-time landmines (`blocked-on-aws.md`) at first deploy
- [ ] **API Gateway throttling** — cost-abuse protection on the HTTP API (native, free). This is the real protection for LLM-endpoint abuse; replaces WAF for v1 (see decisions).
- [ ] Conversation logging (DynamoDB vs CloudWatch)


## V2

- [ ] **Conversation history / multi-turn** — DynamoDB chat store; Lambda reads prior turns before retrieval, writes after generation; system prompt goes multi-turn (resolves "how long can I keep it" -> "it"). Requires architecture + prompt + diagram updates. NOTE: multi-turn moves the bot into the "conversational chatbot" use case AWS says contextual grounding does not support.
- [ ] Custom chunking Lambda (trafilatura) — if eval shows boilerplate hurting retrieval
- [ ] Hybrid route-to-S3 for high-frequency queries
- [ ] Agentic (AgentCore) path
- [ ] Production hardening: auth (if sponsor wants), CORS lockdown, S3 Vectors swap if it gets IaC support, WAF (only if compliance-mandated)