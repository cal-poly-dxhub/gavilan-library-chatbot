# Gavilan Library Chatbot — Build Plan

**Companion doc:** `architecture.md` (what the system is).
**This doc:** todo tracker. Done / Next / V2.
**Updated:** 2026-07-08

---

## V1

### Built

- [x] Project docs (architecture, CLAUDE.md, README)
- [x] Repo + CDK scaffold (structure, gitignore)
- [x] **CDK infra** (synths clean, tested):
  - [x] Amazon S3 Vectors store (`CfnVectorBucket` + `CfnIndex`, 1024-dim, cosine, float32; non-filterable metadata keys)
  - [x] Bedrock Knowledge Base (S3_VECTORS storage, referenced by IndexArn) + S3 data source (FIXED_SIZE chunking)
  - [x] KB source bucket + scraper Lambda (deps layer, weekly EventBridge schedule, one-click deploy Trigger)
  - [x] Dedicated catalog S3 bucket + wiring
  - [x] Query Lambda + own role, HTTP API (API Gateway v2, POST /query + GET /warm), stage throttling
  - [x] Two Bedrock guardrails (input screen + output backstop) + numbered versions
  - [x] Widget hosting: S3 + CloudFront (OAC) in the same stack; BucketDeployment of widget.js; CfnOutput embed tag
  - [x] `config.yaml` single source of truth
- [x] **Query path** — agentic Converse tool-use loop (`run_agent`); two tools (`search_library_info`, `database_catalog`); `{answer, sources}` contract; input guardrail pre-loop, output guardrail on every Converse call
- [x] **Self-updating catalog** — scraper parses databases.php (HTML anchor parse) + Sonnet enrichment (subjects/aliases) -> catalog S3 bucket; tool reads from S3 with TTL cache; hand-authored not-held seed merged at read time; robustness guard keeps last-good on failure
- [x] **System prompt** — XML-structured, hard-grounding, `<tools>` routing guidance for the two tools
- [x] **Widget frontend** — vanilla JS, Shadow DOM isolation, self-injecting, reads `data-api-url`; production `widget.js` only (mock/demo/tests never ship)
- [x] **Eval harness** — shared boto3 job-runner + CSV loader; retrieve-only + retrieve-and-generate formatters; baseline retrieve eval run

### Next

- [ ] **Provide sponsor context** — final seed URLs + any blacklist into `config.yaml`; sponsor Q&A set into `eval/datasets/`
- [ ] **Deploy + validate live** — source URIs resolve, `/warm`, real `/query` end-to-end, guardrail mask-vs-block sanity check, catalog populated from a real scrape, widget from the hosted script tag
- [ ] **Iterate** (eval-driven) — tune chunking, prompt behavior, guardrail thresholds; re-run retrieve + answer-quality evals
- [ ] **CORS lockdown** — restrict `allow_origins` to the library widget domain before launch
- [ ] Confirm with Darren/sponsor: any institutional WAF mandate

---

## V2

- [ ] **Conversation history / multi-turn** — DynamoDB chat store; Lambda reads prior turns before the loop, writes after; multi-turn system prompt. Requires architecture + prompt + diagram updates.
- [ ] **Conversation logging** (DynamoDB vs CloudWatch) — deferred with multi-turn
- [ ] Custom chunking Lambda (trafilatura) — if eval shows boilerplate hurting retrieval
- [ ] Additional agent tools as gaps surface in eval
- [ ] Production hardening: auth (if sponsor wants), WAF (only if compliance-mandated)
