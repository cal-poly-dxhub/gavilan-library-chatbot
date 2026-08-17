# Gavilan Library Chatbot — Build Plan

**Companion doc:** `architecture.md` (what the system is).
**This doc:** todo tracker. Done / Next / V2.
**Updated:** 2026-08-17

---

## V1

### Built

- [x] Project docs (architecture, CLAUDE.md, README)
- [x] Repo + CDK scaffold (structure, gitignore)
- [x] **CDK infra** (synths clean, tested):
  - [x] Amazon S3 Vectors store (`CfnVectorBucket` + `CfnIndex`, 1024-dim, cosine, float32; non-filterable metadata keys)
  - [x] Bedrock Knowledge Base (S3_VECTORS storage, referenced by IndexArn) + S3 data source (FIXED_SIZE chunking, 600 tokens / 20% overlap)
  - [x] KB source bucket + scraper Lambda (deps layer, one EventBridge rule per freshness tier, one-click deploy Trigger)
  - [x] Dedicated catalog S3 bucket + wiring
  - [x] Query Lambda + own role, HTTP API (API Gateway v2, POST /query + GET /warm), stage throttling
  - [x] One Bedrock guardrail (input screen, PROMPT_ATTACK only) + numbered version
  - [x] Widget hosting: S3 + CloudFront (OAC) in the same stack; BucketDeployment of widget.js; CfnOutput embed tag
  - [x] `config.yaml` single source of truth
- [x] **Query path** — agentic Converse tool-use loop (`run_agent`); four tools (`search_library_info`, `database_catalog`, `search_book_catalog`, `search_course_reserves`); `{answer, sources}` contract; input guardrail pre-loop, no guardrail on the Converse call
- [x] **Self-updating catalog** — scraper parses databases.php (HTML anchor parse) + Sonnet enrichment (subjects/aliases) -> catalog S3 bucket; tool reads from S3 with TTL cache; hand-authored not-held seed merged at read time; robustness guard keeps last-good on failure
- [x] **Live Primo catalog tools** — `search_book_catalog` (general catalog) + `search_course_reserves` (course reserves) call the Ex Libris Primo discovery API (search + per-record availability); evidence-not-verdict (model judges, `total == 0` the only not-held signal); timeout + availability budget + soft-fail; defensive `$$`-encoding parse
- [x] **Curated link table** — the canonical Gavilan URLs, hand-authored and bundled with the query Lambda. Started as a fifth tool and moved into the Converse `system` payload after the model kept skipping the call
- [x] **Single-session multi-turn** — client sends the `messages` history each request; stateless Lambda (no server-side store), trimmed to the last 10 messages before seeding the loop; legacy `{query}` still accepted (NOT the DynamoDB-backed design - see V2)
- [x] **System prompt** — XML-structured, hard-grounding, `<tools>` routing guidance for the four tools, `<priority_responses>` for safety and emergency messages, `<citations>` governing the two permitted link sources
- [x] **Widget frontend** — vanilla JS, Shadow DOM isolation, self-injecting, reads `data-api-url`; production `widget.js` only (mock/demo/tests never ship)
- [x] **Frontend polish** — widget sends the multi-turn `messages` array; markdown answer rendering; expandable chat window; sources UI; maroon Gavilan branding
- [x] **Bilingual chrome** — English/Español from one string table, switched by a header control; optional allowlisted `language` request field adds one Converse `system` block. No corpus change: Spanish questions already retrieve correctly from the English knowledge base
- [x] **Accessibility pass** — WCAG 2.1 AA audit of the widget and demo page, then remediation of every failing criterion in the widget. See `accessibility-audit.md`
- [x] **Tiered scrape cadence + change gating** — `fast` (hours/closures) daily, `full` (complete sweep) every five days, both declared in `config.yaml`; uploads, ingestion and catalog enrichment each gated on whether content actually changed, with no new store
- [x] **Feedback path** — `POST /feedback` -> its own Lambda -> SNS -> one librarian email. Five-field allowlist, no server-side store, the cited source URLs are the payload
- [x] **Demo site** — a second bucket/distribution pair serving one library-styled page that embeds the shipped widget over the shipped delivery path; API and widget URLs stamped at deploy
- [x] **Cost visibility** — demo-only session meter + monthly estimator, fed by two opt-ins absent from production; per-question constants measured against the deployed endpoint, published list prices only
- [x] **Runtime theming** — one `theme.json` at the widget bucket root (highlight colour, font keyword, starter questions), read at init and merged over the built-in defaults; hosted settings editor with a Cognito-gated `PUT /theme` save, a hosted guide, and a downloadable copy of the defaults
- [x] **Eval harness** — shared boto3 job-runner + CSV loader; retrieve-only + retrieve-and-generate formatters; baseline retrieve eval run; promptfoo answer-quality loop against the deployed endpoint
- [x] **CI** — four hermetic GitHub Actions jobs (infra, scraper, eval, widget), no AWS credentials and no deployed endpoint

### Next

- [x] **Deploy + validate live** — `/query` validated end-to-end (four tools, multi-turn, guardrail, live Primo)
- [x] **CORS lockdown** — `allow_origins` restricted to the widget domain via `cors.allow_origins` in config.yaml; wildcard rejected at synth
- [ ] **Set `feedback.notify_email`** — it is empty today, so no `/feedback` endpoint is created and the deploy prints a `FeedbackStatus` line saying why. The recipient must also click the SNS confirmation email once
- [ ] **Provide library context** — remaining Q&A into `eval/datasets/`; seed URLs and the KB exclusion list are settled in `config.yaml`
- [ ] **Parallelize Primo availability calls** — the per-record delivery calls run sequentially under a wall-clock budget; fan them out so slow availability lookups don't push toward the Lambda timeout
- [ ] **Screen-reader session** — the accessibility work is verified in the DOM and in Chrome's accessibility tree, but no NVDA/JAWS/VoiceOver run has happened. That is the one check standing between this and a written conformance claim
- [ ] **Iterate** (eval-driven) — tune chunking and prompt behavior; re-run retrieve + answer-quality evals

---

## V2

- [ ] **Persistent conversation store + logging** — server-side DynamoDB chat store (Lambda reads prior turns before the loop, writes after) and conversation logging (DynamoDB vs CloudWatch). Single-session multi-turn already shipped (client-carried, stateless - see Built); this is only the persistence/logging layer, which was NOT built.
- [ ] **Authenticated Alma API** — real-time due dates + copy counts via the staff Alma API (API key); the upgrade path beyond the public Primo discovery endpoint's "catalog shows available" ceiling
- [ ] Custom chunking Lambda (trafilatura) — if eval shows boilerplate hurting retrieval
- [ ] **More agent tools** — two live-catalog tools shipped (Primo general catalog + course reserves); add others as gaps surface in eval
- [ ] Production hardening: auth (if required), WAF (only if compliance-mandated)
