# Gavilan Library Chatbot — Build Plan

**Companion doc:** `architecture.md` (what the system is).
**This doc:** todo tracker. Done / Next / V2.
**Updated:** 2026-07-15

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
- [x] **Query path** — agentic Converse tool-use loop (`run_agent`); four tools (`search_library_info`, `database_catalog`, `search_book_catalog`, `search_course_reserves`); `{answer, sources}` contract; input guardrail pre-loop, output guardrail on every Converse call
- [x] **Self-updating catalog** — scraper parses databases.php (HTML anchor parse) + Sonnet enrichment (subjects/aliases) -> catalog S3 bucket; tool reads from S3 with TTL cache; hand-authored not-held seed merged at read time; robustness guard keeps last-good on failure
- [x] **Live Primo catalog tools** — `search_book_catalog` (general catalog) + `search_course_reserves` (course reserves) call the Ex Libris Primo discovery API (search + per-record availability); evidence-not-verdict (model judges, `total == 0` the only not-held signal); timeout + availability budget + soft-fail; defensive `$$`-encoding parse; `<textbook_flow>` routes course textbooks to reserves
- [x] **Single-session multi-turn** — client sends the `messages` history each request; stateless Lambda (no server-side store), trimmed to the last 10 messages before seeding the loop; legacy `{query}` still accepted (NOT the DynamoDB-backed design - see V2)
- [x] **System prompt** — XML-structured, hard-grounding, `<tools>` routing guidance for the four tools + `<textbook_flow>`
- [x] **Widget frontend** — vanilla JS, Shadow DOM isolation, self-injecting, reads `data-api-url`; production `widget.js` only (mock/demo/tests never ship)
- [x] **Frontend polish** — widget sends the multi-turn `messages` array; markdown answer rendering; expandable chat window; sources UI; maroon Gavilan branding
- [x] **Eval harness** — shared boto3 job-runner + CSV loader; retrieve-only + retrieve-and-generate formatters; baseline retrieve eval run

### Next

- [x] **Deploy + validate live** — deployed to the gavilan account; `/query` validated end-to-end (four tools, multi-turn, guardrails, live Primo). Remaining spot checks: catalog populated from a real scrape, widget from the hosted script tag
- [ ] **Provide sponsor context** — final seed URLs + any blacklist into `config.yaml`; sponsor Q&A set into `eval/datasets/`
- [ ] **CORS lockdown** — restrict `allow_origins` to the library widget domain before launch
- [ ] **Parallelize Primo availability calls** — the per-record delivery calls run sequentially under a wall-clock budget; fan them out so slow availability lookups don't push toward the Lambda timeout
- [ ] **Docs refresh** — CLAUDE.md + architecture.md done (four tools, multi-turn, Primo dependency); README + build-plan + diagram this pass
- [ ] **Iterate** (eval-driven) — tune chunking, prompt behavior, guardrail thresholds; re-run retrieve + answer-quality evals
- [ ] Confirm with Darren/sponsor: any institutional WAF mandate

---

## V2

- [ ] **Persistent conversation store + logging** — server-side DynamoDB chat store (Lambda reads prior turns before the loop, writes after) and conversation logging (DynamoDB vs CloudWatch). Single-session multi-turn already shipped (client-carried, stateless - see Built); this is only the persistence/logging layer, which was NOT built.
- [ ] **Authenticated Alma API** — real-time due dates + copy counts via the staff Alma API (API key); the upgrade path beyond the public Primo discovery endpoint's "catalog shows available" ceiling
- [ ] Custom chunking Lambda (trafilatura) — if eval shows boilerplate hurting retrieval
- [ ] **More agent tools** — two live-catalog tools shipped (Primo general catalog + course reserves); add others as gaps surface in eval
- [ ] Production hardening: auth (if sponsor wants), WAF (only if compliance-mandated)
