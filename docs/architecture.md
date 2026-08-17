# Gavilan Library Chatbot - Architecture

RAG chatbot for Gavilan College Library. Answers operational questions (hours, checkout,
textbooks, "what does the library offer") and routes research questions and out-of-scope issues
to a human. Built with Cal Poly DxHub, for Gavilan College Library.

**Status:** validated end-to-end (query path, four tools, multi-turn, guardrail).
**Updated:** 2026-08-17

---

## Stack

| Layer | Choice | Notes |
|---|---|---|
| Ingestion | Scraper Lambda -> S3 source bucket -> Bedrock KB (S3 data source) | Curated seed URLs scraped to clean markdown. Tiered cadence: `fast` (hours/closures) daily, `full` (complete sweep) every 5 days, one EventBridge rule per tier, both declared in `config.yaml`. Change-gated end to end. Plus a one-click deploy Trigger. |
| RAG engine | Bedrock Managed Knowledge Base | Managed chunk/embed/store. FIXED_SIZE chunking, 600 tokens, 20% overlap. |
| Vector store | Amazon S3 Vectors | `CfnVectorBucket` + `CfnIndex`; KB `StorageConfiguration` type `S3_VECTORS`, referenced by `IndexArn`. 1024-dim, cosine, `float32`, semantic-only. |
| Embeddings | Titan Text Embeddings v2 (1024-dim) | Managed default. |
| Query path | Agentic `Converse` tool-use loop (Lambda) | `run_agent`: the model calls tools; the loop feeds each `toolResult` back until `end_turn`, under an iteration cap. Multi-turn `messages` seed; system prompt via the Converse `system` param. |
| Tools | `search_library_info`, `database_catalog`, `search_book_catalog`, `search_course_reserves` | KB retrieval for general questions; authoritative research-database availability and subject listing; live Primo general-catalog and course-reserves search. Routed by tool descriptions + system prompt + `toolChoice` auto. |
| Curated links | Static JSON bundled with the query Lambda, injected into the Converse `system` payload | The library's and college's official front-door URLs, so the model cites real links instead of writing one from memory. Not a tool - see decision 2. Hand-authored; no scraper, no S3, no cache. |
| Live catalog (external) | Ex Libris Primo discovery API | The two catalog tools call Primo directly: a search plus a per-record availability call. Outbound HTTPS to a third party, not an AWS API, so the query Lambda needs outbound internet. Timed out and soft-failing, so a slow Primo never kills `/query`. |
| LLM (generation) | Bedrock-hosted Claude Sonnet 4.6 via Converse | Through a `us.`-prefixed cross-region inference profile. |
| Database catalog | Self-updating JSON in a dedicated S3 bucket | The scraper derives the held list from databases.php (HTML anchor parse + a Sonnet enrichment call for subjects/aliases); the hand-authored not-held list is a bundled seed merged at read time; the tool reads from S3 with per-container TTL caching. |
| Orchestration/API | Lambda + HTTP API (API Gateway v2) | `POST /query`, `GET /warm` and `POST /feedback` are public; `PUT /theme` is the one gated route (theme-admin JWT authorizer, see decision 17). Stage-level throttling covers every route. CORS preflight is API-level and locked to `cors.allow_origins` (never `*`), so a new route inherits it. |
| Feedback | `POST /feedback` -> own Lambda -> SNS topic -> one email subscription | A student reports a wrong answer; a librarian gets a plain-text email naming the pages that answer cited. No server-side store - the email is the record. Five-field allowlist; nothing about the requester is accepted or logged. See decision 14. |
| Guardrails | ONE Bedrock Guardrail: PROMPT_ATTACK, input only | `ApplyGuardrail` on the bare query before the loop, and nothing else. No other content filter, no PII policy, no guardrail on Converse. See decision 6. |
| Widget | Custom vanilla-JS embed, Shadow DOM | Self-injecting single file, reads `data-api-url` from its script tag. Shadow DOM isolates it from host-site CSS. Bilingual chrome (English + Español) from one string table, switched by a header control; an explicit choice rides along as the optional `language` request field. Nothing about a conversation is written down - transcript and language choice live in memory and go with the tab. |
| Widget hosting | S3 + CloudFront (OAC), same stack | One `cdk deploy` ships backend + widget. OAC, not OAI. Same stack because OAC hits a cross-stack cyclical dependency. |
| Widget theming | `theme.json` at the widget bucket root | Highlight colour, font keyword and starter questions, read at init and merged over the built-in defaults. Its own CloudFront behavior (CORS + 60s TTL, because a console upload sets no object metadata) and an `exclude` on the widget `BucketDeployment` so the deploy's prune cannot delete it. See decision 16. |
| Settings editor | `theme-editor.html` at the widget bucket root, `PUT /theme` behind its own Cognito pool | The one theming entry point (`WidgetThemeEditor`). Signed in, Save publishes to the live widget through a key-scoped Lambda; unsigned, the same page downloads a ready-to-upload `theme.json`. `WidgetThemeUpload` is the S3-console fallback and `theme-guide.html` documents it. Accounts come from `ThemeAdminCreateUserCommand`; everything after that is self-service. See decision 17. |
| Cost visibility | Demo page only: session meter + monthly estimator | Fed by two opt-ins absent from production: the handler's `include_usage` and the widget's `data-usage-events`. Rates and measured constants live in `config.yaml`, stamped into the page at deploy. Published list prices only - no Cost Explorer, no billing API. |
| Demo site | A second private S3 + CloudFront (OAC) pair, same stack | One static page embedding the production widget from the production CDN, with the `/query` and widget URLs stamped in at deploy. Its own bucket because `BucketDeployment` prunes; its own distribution so demo-only `noindex` headers never touch the production widget. Gated on `demo_site.enabled`. |
| Config | `config.yaml` (declarative) | Model ids, vector store, scraper, chunking, retrieval, catalog and guardrail settings live in config, not code. |
| Deploy | CloudFormation / CDK (Python, L1 constructs) | One-click AWS install. |
| Logs/eval | CloudWatch (structured logs); Bedrock RAG eval harness | The input screen's outcome is logged per request - types and actions only, never the question. There is no answer-side assessment to log. |

---

## Data flow

**Ingest (tiered schedule + on deploy).** An EventBridge rule per tier invokes the scraper with
`{"tier": "<name>"}`. The Lambda resolves that to a URL list, fetches those pages, extracts clean
markdown, uploads only the pages whose content fingerprint changed, and starts a KB ingestion job
only if the bucket actually moved (FIXED_SIZE chunk -> Titan v2 embed -> S3 Vectors). A full run
also regenerates the database catalog from databases.php: a deterministic HTML anchor parse, a
min-count guard, a fingerprint gate, then a Sonnet call for subjects and aliases on newly-added
databases only, constrained to the parsed names. It keeps the last-good copy if validation fails
or the page is unchanged, and never blocks the KB scrape. One structured summary line per run:
tier, pages fetched/changed/unchanged, the ingestion decision, and the enrichment call's real
token counts. The deploy Trigger invokes with no tier, which resolves to the complete sweep.

**Query (runtime).** Widget -> API Gateway -> Lambda, over a public `POST /query` (see decision 15). The request carries a multi-turn `messages`
array (legacy `{query}` still accepted) plus an optional allowlisted `language` field. History is
client-sent - the Lambda is stateless - and trimmed to the last 10 messages before seeding the
loop. `ApplyGuardrail` screens the newest user turn for PROMPT_ATTACK, the only thing it screens,
and it never rewrites the turn. Then `run_agent` runs the Converse tool-use loop under the system
prompt: the model decides when to call each tool, the loop executes every `toolUse` and feeds the
`toolResult` back, repeating until `end_turn` or the iteration cap. No guardrail is attached to
any Converse call. `{answer, sources[]}` returns to the widget - `sources` accumulate from the
loop's KB retrievals, deduped by uri, plus one synthetic source per non-KB tool that returned a
result: the A-Z databases page for `database_catalog`, a per-query discovery-search URL for each
Primo tool. The curated link table contributes none. On a block, the blocked message returns with
empty sources and nothing downstream runs.

**Feedback (runtime).** Browser -> API Gateway -> a separate small Lambda whose role carries
`sns:Publish` and nothing else. It enforces a five-field allowlist (`comment`, `question`,
`answer`, `sources`, `reply_to`), a body-byte cap checked before parsing, and a comment-character
cap; an unexpected field is a `400` rather than a pass-through. It renders a plain-text email -
what the student said, the reported question and answer, the cited URLs, a Pacific timestamp -
and publishes it to a topic whose only subscription is the librarian address from config.
Response `202 {"received": true}`. Nothing is stored anywhere, and no IP, user agent, session or
generated id is accepted or recorded. The `Subject` is a constant and the optional reply address
is body text, never a mail header.

**Widget delivery.** The host page's `<script>` tag fetches `widget.js` from CloudFront (private
S3 via OAC). The widget fetches `theme.json` from the same distribution in parallel with page
load and waits on it, under a 1.5s cap, before injecting itself - so a themed install is themed
on its first paint. A missing `theme.json` is the normal state of a fresh install and renders the
built-in defaults.

**Demo delivery.** Identical, with the demo page standing in for the host library page: it
carries one `<script>` tag pointing at the *widget* distribution and posts cross-origin to the
same `/query`. Because it is the real embed rather than a copy, it exercises the production
delivery path end to end, including CORS - the demo origin is appended to `cors.allow_origins` at
deploy, so a CORS regression shows up on the demo instead of hiding behind a proxy.

---
