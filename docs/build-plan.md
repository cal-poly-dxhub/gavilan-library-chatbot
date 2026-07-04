# Gavilan Library Chatbot — Build Plan

**Companion docs:** `architecture.md` (what/why), `blocked-on-aws.md`, `deploy-runbook.md` (deploy-day sequence).
**This doc:** todo tracker. Done / Next / V2.
**Updated:** 2026-07-03

---

## V1

### Built (offline, synths clean, tested, never run on real AWS)

- [x] Project docs (architecture, CLAUDE.md, README, project overview)
- [x] Repo + CDK scaffold (`cdk init`, structure, gitignore)
- [x] **CDK infra** — synths clean:
  - [x] OpenSearch Serverless collection + encryption/network/data-access policies
  - [x] Vector index (`CfnIndex`, 1024-dim knn_vector)
  - [x] Bedrock Knowledge Base (wired to collection + index)
  - [x] Web Crawler data source (seed URLs + filters from config)
  - [x] Lambda (Retrieve + Converse) + own execution role
  - [x] HTTP API (API Gateway v2, POST /query)
  - [x] `config.yaml` single source of truth
  - [!] Deploy blocked: see `blocked-on-aws.md` / `deploy-runbook.md`
- [x] **Eval harness** — coded + tests, cannot run until access:
  - [x] Structure + shared boto3 job-runner + CSV loader
  - [x] Retrieve-only formatter (chunking/retrieval evaluator)
  - [x] Retrieve-and-generate formatter (answer-quality evaluator)
  - [x] `/query` gained `include_full_context` flag so capture can grab full untruncated passages (audit 2.4)
  - [!] Capture-outputs still a hard stub (needs deployed bot); `retrievedPassages` vs `retrievedResults` key unverified; see `blocked-on-aws.md`
- [x] AWS architecture diagram (`diagrams` lib, PNG in docs/) — as-built; only WAF marked planned
- [x] System prompt draft (`system-prompt.md`, XML-structured, hard-grounding, single-turn)
- [x] **Wire the real system prompt** — placeholder replaced. Prompt in Converse `system` param; handler wraps chunks in `<context>` tag (contract pinned + tested); `/query` returns `{answer, sources[]}`.
- [x] **Widget frontend** — vanilla JS, Shadow DOM isolation, self-injecting single file reading `data-api-url`. Mock extracted to `frontend/mock.js` (demo + tests only); `widget.js` production-clean.
- [x] **Widget hosting** — S3 + CloudFront (OAC) in the same CDK stack; `BucketDeployment` uploads only `widget.js`; CfnOutput assembles the ready-to-paste `<script>` tag.
- [x] **Guardrails** — two-guardrail design (post-audit): input screening via `ApplyGuardrail` on the bare query pre-retrieval (PII anonymize, content + prompt-attack block); Converse guardrail is output-only, content-filters-only. Retrieved context never screened. Version republishes on config change via config hash in the description. Grounding deliberately excluded (see architecture.md).

### Hardening / audit

- [x] **Fable 5 audit** — full-stack adversarial review, produces `docs/review-findings.md`
- [x] **Fix audit items** — all 19 findings resolved (17 discrete; 2.2/2.3 fold into 2.1) on `feat/audit-fixes`, decided in `docs/audit-resolutions.md`. 6 commits by subsystem. Includes the guardrail restructure (2.1 critical), inference profile (1.2), timeout+warm (1.3), handler hardening (3.1-3.4, 2.6), infra hygiene (1.4-1.7), eval/docs/widget nits (2.4/2.5/4.1/4.2)
- [x] **API Gateway throttling** — stage-level rate/burst (10rps/20burst) from config, covers all routes (audit 1.5). The real cost-abuse protection; replaces WAF for v1
- [x] **Comment sweep** — stripped change-narration from docstrings across handler/stack/tests/config, two passes, comments-only (tests unchanged)
- [x] **CLAUDE.md trim** — cut ~55% (repo-layout narrative → terse map, rationale → one-liners), lessons hand-added
- [~] **Deploy runbook** (`docs/deploy-runbook.md`) — in progress: before/during/after deploy-day sequence with per-landmine verify + fallback
- [ ] **Provide context** (gated ~July 19) — sponsor seed URLs + blacklist into `config.yaml`; sponsor Q&A set into `eval/datasets/`. Authoring our own Q&A set was rejected (would guess wrong and bias the eval); wait for theirs
- [ ] **First deploy** — work through `deploy-runbook.md`: region availability check, bootstrap with aoss perms, model-access enablement, then stand up
- [ ] **Deploy-time landmines** (`blocked-on-aws.md`) — surface at first deploy: data-access/aoss principal, CfnIndex race, crawler values, NextGen confirm, source-URI extraction, CloudFront timing, `__pycache__` bundling
- [ ] **Validate live** — source URIs resolve, `/warm` wakes OSS, real `/query` end-to-end, guardrail mask-vs-block sanity check (only tested offline), widget from hosted script tag
- [ ] **Iterate** (eval-driven) — run retrieve-only eval, tune: FIXED_SIZE vs HIERARCHICAL chunking, SEMANTIC vs HYBRID search, prompt behavior, guardrail thresholds
- [ ] Confirm with Darren/sponsor: any institutional WAF mandate (the one open question on the WAF-excluded decision)

---

## V2

- [ ] **Conversation history / multi-turn** — DynamoDB chat store; Lambda reads prior turns before retrieval, writes after generation; system prompt goes multi-turn. Requires architecture + prompt + diagram updates. 
- [ ] **Conversation logging** (DynamoDB vs CloudWatch) — deferred with multi-turn; no reason to decide before the core works
- [ ] Custom chunking Lambda (trafilatura) — if eval shows boilerplate hurting retrieval
- [ ] Hybrid route-to-S3 for high-frequency queries
- [ ] Agentic (AgentCore) path
- [ ] Production hardening: auth (if sponsor wants), CORS lockdown, S3 Vectors swap if it gets IaC support, WAF (only if compliance-mandated)