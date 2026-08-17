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

## Decisions (resolved)

1. **`Retrieve` via a tool, not `RetrieveAndGenerate`.** Full system-prompt control over
   out-of-scope and textbook behavior.

2. **Four tools, model-routed - and the link table is not one of them.** Routing is tool
   descriptions plus system prompt plus `toolChoice` auto, never Lambda branches. The curated URL
   table used to be a fifth tool routed by its `toolSpec` description alone, which was the cleanest
   available test of whether description-only routing works. It failed: the description named
   campus maps explicitly and the model still skipped the call whenever it had already formed an
   answer. A constant function does not belong behind a tool call, so it moved into the `system`
   payload.

3. **Self-updating catalog with a robustness guard.** The held list is derived from the site each
   full scrape; the not-held list is hand-authored and merged at read time, because absence cannot
   be scraped. A minimum-count and required-field guard keeps the last-good catalog rather than
   overwriting it with garbage.

4. **L1 `Cfn*` constructs.** L1 core covers the KB, the data source, S3 Vectors and guardrails; the
   `generative-ai-cdk-constructs` Bedrock L2s are deprecated and excluded.

5. **Same-stack widget hosting, OAC not OAI.** Cross-stack OAC hits a cyclical dependency.

6. **The guardrail screens prompt attacks and nothing else.** One guardrail, one category, input
   only - and the output guardrail is deleted rather than disabled. The reason is ordering, not
   cost. `ApplyGuardrail` runs *before* the system prompt does, so any other policy it carried
   would pre-empt the prompt's crisis handling: a content filter that blocks a message about
   self-harm answers a student in trouble with canned decline copy, and PII anonymization silently
   rewrites their message so a name and a street arrive as `{NAME}` and `{ADDRESS}` - stripping
   exactly the details that make an urgent message legible. The system prompt owns safety instead,
   because it sees the whole question, can tell a request for a book about suicide prevention from
   a person in crisis, and can *respond* rather than only refuse. PROMPT_ATTACK stays because it is
   an attack on the prompt itself, it is input-only by definition, and nothing else defends a
   public unauthenticated endpoint. Consequences: nothing screens the answer, so there is no
   answer-side assessment to log; and contextual grounding stays excluded on separate grounds -
   unsupported for chatbot use, needs fragile `guardContent` tagging, and the prompt handles
   grounding.

7. **WAF excluded; CORS locked, not permissive.** HTTP API v2 cannot take WAF directly, so
   throttling is the cost-abuse control. `cors.allow_origins` lists the library site (a host-only
   Origin - the `/library/` path is irrelevant to CORS), a dev-only localhost entry, and the demo
   site's friendly hostname. That last one is hand-listed rather than derived: the stack appends
   the demo distribution's `*.cloudfront.net` origin at deploy, but a browser on the custom
   hostname sends a different origin string, CORS matches the full string exactly, and the hostname
   is a DNS + ACM decision made outside this stack. The stack also appends its own widget
   distribution's origin, since the settings editor is served from there and PUTs `/theme`
   cross-origin. `infra/config.py` rejects `*` at synth. This is spend hygiene, not a security
   boundary - CORS is browser-enforced only, so throttling remains the actual cost cap.
   `allowHeaders` carries `Authorization` because Save sets it from JavaScript, which makes every
   `PUT /theme` preflighted; leave it out and the request dies at the OPTIONS with a CORS error
   rather than a 401.

8. **Live-catalog tools return evidence, not a verdict.** Primo relevance is query-relative - a
   held item can score below not-held noise - so the two catalog tools return top candidates for
   the model to judge, `total == 0` is the only clean not-held signal, and the handler applies no
   score threshold. Availability is reported as what the catalog *shows*, not a guarantee. Per-call
   timeouts, a total availability budget, soft-fail to a "catalog unavailable" `toolResult`, and
   defensive parsing of Primo's `$$`-encoded fields keep a slow or broken third party from killing
   the loop.

9. **The demo site is packaging, not a second frontend.** `cdk deploy` returns a public URL that
   already shows the widget working in a library-looking page, so the product can be sent as a link
   with no setup. It embeds the *shipped* widget over the *shipped* delivery path - one `<script>`
   tag, no fork - so it cannot drift from what the library pastes on their site. It gets its own
   bucket, because `BucketDeployment` prunes with `s3 sync --delete` and sharing the widget bucket
   would put `widget.js` one misconfiguration away from deletion, and its own distribution, so the
   demo's `noindex` header never touches the production widget. The endpoint is discovered rather
   than hardcoded, because one-click install into any account is a project goal.

10. **Cost visibility is a demo affordance, and it is measured rather than modelled.** "What will
    this cost" is the client's second question, so the demo answers it: a meter for the
    conversation you just had, and a monthly estimate split into a fixed floor and a variable part,
    because one blended number answers neither question. It is invisible by default, behind one
    control in the demo banner, and nothing in the library-looking content mentions money. The
    per-question constants are measured over 60 live questions rather than derived, because the
    three things that dominate cost are invisible from outside the loop: a question is often
    several Converse calls each resending everything, retrieved passages ride in the input tokens,
    and the guardrail bills per 1,000-character text unit rather than per question. Published list
    prices only, every figure labelled an estimate, and the data path is two opt-ins that the
    production install does not set.

11. **Spanish is a frontend affordance, not a second corpus.** Measured before it was built:
    Spanish questions *already* retrieve correctly from the English knowledge base and come back
    grounded and specific, accented or not, including the authoritative database tool and the live
    catalog. So no translated corpus, no re-ingest, no chunking or retrieval change. The real gap
    was the shell - every piece of chrome was hardcoded English, so a Spanish speaker saw no signal
    the bot speaks Spanish *before typing*, and auto-detection cannot close that because it needs a
    message first. Hence a visible control rather than a sniffer, one string table so no copy sits
    inline in render code, and exactly one optional request field (allowlisted server-side, since
    it is client text heading for the system payload) plus one extra `system` block. Past turns are
    never retranslated: each message carries the language it was said in, and rewriting history
    would cost a model call per message and read as the bot editing itself. The blocked-input
    message is bilingual in config, because a block bypasses the model and nothing can translate it
    at runtime. The emergency reply stays verbatim in the language it is written in - the language
    block says so explicitly, since a blanket "reply in Spanish" arriving last could otherwise
    override a hand-verified safety message.

12. **Freshness is tiered and declarative, and everything downstream is change-gated.** "We changed
    our hours, when does the bot know?" had one honest answer under a single weekly scrape: up to a
    week. Now it is a day for hours and five for everything else. Cadences and tier membership live
    in `config.yaml`, validated at synth, and the stack builds one rule per tier by iterating that
    map - so retiming a tier or moving a page between them is a config edit. Scraping more often
    had to be nearly free, so each downstream cost is gated on real change: markdown uploads only
    when a `content-sha256` in S3 object metadata differs, ingestion starts only when the bucket
    moved, and the Sonnet catalog enrichment runs only when a fingerprint of the *parsed* database
    rows changes. A second run over unchanged content uploads nothing, indexes nothing, calls no
    model. None of it introduces a store: change detection reads S3 object metadata, S3
    `LastModified`, and Bedrock's own ingestion-job history. Two failure modes had to be closed.
    The stale-object prune keys off every configured URL rather than the tier's slice, because a
    daily three-page run must not decide what is stale, and it counts unchanged pages as live
    rather than only uploaded ones. And since Bedrock allows one ingestion job per data source, an
    overlap skips rather than throws - safe only because the "bucket newer than the last job" rule
    finds the deferred change next run. Deliberately excluded: a manual "refresh now" endpoint and
    a staff dashboard. Unauthenticated, that is a denial-of-wallet lever on a public endpoint with
    no WAF; authenticated, it is a permanent ownership burden after the engagement ends.

13. **Single-session multi-turn, client-carried.** The client sends the full history each request;
    the Lambda is stateless and trims to the last 10 messages. Legacy `{query}` is a single-message
    conversation.

14. **Feedback is a notification, not a dataset - and its payload is the source URLs.** The fix for
    a wrong answer is RAG-first: correct the library webpage and the next scrape of that page's
    tier corrects the bot. So the notification's job is to name *which page* to edit, which is what
    makes it a work order rather than a complaint box. Four constraints. **SNS, not SES**: SES
    starts every account in a sandbox restricted to pre-verified addresses and needs a support
    request to leave, which would make one-click install into a fresh account the client's problem;
    an SNS subscription needs one confirmation click. The cost is mail from an AWS address with no
    reply routing, which is why the optional reply address is body text. **No server-side store, by
    constraint**: the email is the record, so a failed publish loses the report - the alternative
    was a database of student complaints nobody agreed to keep. **The payload is an allowlist of
    five fields and the request is not**: no IP, user agent, session or generated id, and no
    conversation history; API Gateway supplies the first two and the handler never reads them, an
    unexpected sixth field is rejected rather than forwarded, and the comment never appears in a
    log line. **Nothing screens the text**: feedback never reaches the model, so the guardrail does
    not apply, and the controls are the caps, plain-text rendering, a constant subject and
    control-character stripping. It is gated harder than the demo site - with no destination
    configured the route is not created at all, because an endpoint that accepts reports it cannot
    deliver breaks the silence this feature exists to break.

15. **`POST /query` is public, and that is a decision rather than an omission.** The widget is
    embedded on a public library page, so every caller is anonymous by definition and there is
    no credential a student could hold. Three controls carry it instead of an authorizer:
    stage-level throttling on the default stage (the actual cost cap, and the reason WAF is
    excluded - see decision 7), the `PROMPT_ATTACK` input screen, and the exact-match CORS
    allowlist - which is spend hygiene, not a boundary, because CORS is browser-enforced only. A
    short-lived gate did sit here before launch: while the only thing driving billable `/query`
    was a shareable demo URL, and a link travels, the route sat behind a Cognito user pool and a
    native JWT authorizer with one shared account. It came off as a **deletion, not a flag** - a
    switchable gate would have kept the pool, the shared username and its enrolment commands in
    a public repo for a control nobody would ever turn back on, and removing code is visible in
    review where a changed YAML value is not. What survived the removal is one line of CORS
    config: `Authorization` stays in `allowHeaders`, because the settings editor's `PUT /theme`
    sends that header and losing it kills theme saves at the preflight rather than at the
    request. `GET /warm` and `POST /feedback` were never gated - warm runs no generation call,
    and putting a report path behind a password only loses reports.

16. **Branding is data the customer owns, not a code edit.** At pickup the client deploys into
    their own account, so the widget bucket is theirs - and the three things a customer asks to
    change should not need an engineer. Hand-editing the shipped `widget.js` was rejected outright:
    it forks the file the next deploy overwrites. Six constraints. **JSON, never JS** - a `.js`
    config is executable code on the library's own pages with the widget's privileges, so the file
    is data and every value is allowlisted; the colour reaches a stylesheet, so its hex pattern is
    a security boundary rather than tidiness. **Soft-fail per key**, because the person editing
    cannot redeploy and cannot read a stack trace: a bad colour costs them the colour, malformed
    JSON costs them the file, and an unthemed install emits no theme CSS at all, so the default
    rendering is provably unchanged. **The customer's file has to survive `cdk deploy`** - the
    deployment carries the `exclude` prop that scopes the sync, not the identically named argument
    on `Source.asset`, which only filters the asset and protects nothing; it needs two patterns,
    because `--exclude` fnmatches the full path so `theme.json` covers the root object only.
    **Highlight colour only, with the text on it derived** - black or white, whichever contrasts
    more, which bottoms out at 4.58:1 over the whole sRGB cube and so needs no validation and can
    reject nothing. A dozen colour fields is not a design system: the divider, the focus outline
    and its halo were each measured against the surfaces they land on, and exposing them would let
    a well-meaning edit undo 1.4.11 silently. Font is family only for the same reason - size,
    weight and line height are what keep the panel readable at 400% zoom. **Enumerated font
    keywords** mapping to stacks that resolve on macOS and Windows with no download, because a
    free-text family name ships a Times fallback to everyone except the person who typed it.
    **CORS and freshness come from the distribution, not the object**, since a console upload sets
    no metadata. The mount waits on the theme fetch under a 1.5s cap, so a themed install paints
    themed on its first frame and a dead CDN costs the launcher that much and no more. The
    accessibility conformance claim is scoped to the default colours, since the highlight is also a
    text colour on light surfaces.

17. **The settings editor is a static page plus one key-scoped write, and it is the entry point.**
    The original position was no settings page at all - a second surface to authenticate, host and
    maintain after the engagement ends. What shipped keeps most of that saving, because the runtime
    contract never changed: the widget still reads a `theme.json` from a bucket, so deleting the
    editor would leave theming working. The console round trip was tested and it was the problem -
    the account holds around nineteen buckets, the demo-site bucket sits next to the widget one,
    and an upload into the wrong one succeeds silently and changes nothing. So there is **one entry
    point, not four**: the download, the "Choose defaults" refill, the account controls and the
    link to the guide live in a settings dialog inside the editor, and two earlier outputs pointing
    at the guide and the download were deleted, because three routes into one workflow is two more
    things to leave stale. The console deep link survives as the fallback for anyone not signed in.
    **Save is gated by its own Cognito pool** - managed login hosts every password flow, the page
    only redirects (authorization code + PKCE, no implicit grant), and the save Lambda's IAM
    reaches exactly one object. It revalidates against the widget's rules rather than trusting the
    page, because "the editor would never send that" is not a runtime guarantee for a file served
    to every visitor. **The account lifecycle is one CLI command total**; everything after it is
    self-service. No user in CloudFormation - every `AWS::Cognito::UserPoolUser` property is
    replacement-on-update, so a template-owned user could not change email without wiping the
    librarian's password. The page ships deploy-stamped like the demo page, so the committed file
    carries no absolute URL, and its duplicated copies of the widget's validation rules and the
    defaults file are pinned by the contract suite.

---

---

## Verified (2026-07)

- **Bedrock Managed KB:** managed chunk/embed/store; S3 data source; citations via
  `x-amz-bedrock-kb-source-uri`.
- **S3 Vectors:** the KB `StorageConfiguration` type `S3_VECTORS` is a `oneOf` referenced by
  `IndexArn` alone - adding `IndexName`/`VectorBucketArn` is rejected at validation.
  `non_filterable_metadata_keys` must be set at index creation for the Bedrock-internal keys, or
  ingestion fails on the filterable-metadata limit. Semantic search only.
- **Agentic Converse tool-use:** the request carries `toolConfig`; the model returns
  `stopReason: tool_use` with a `toolUse` block; you reply with a user message carrying a
  `toolResult` and loop until `end_turn`. `toolChoice` auto.
- **Bedrock Guardrails:** the input screen runs via the standalone `ApplyGuardrail` API and needs
  `bedrock:ApplyGuardrail` alongside `InvokeModel`. Verified but deliberately unused: the output
  backstop via `guardrailConfig` on Converse, and the PII policy - both removed, see decision 6.
  Contextual grounding is not supported for chatbot use.
- **Bedrock native RAG eval (BYOI):** LLM-as-judge via `create_evaluation_job` with
  `precomputedRagSourceConfig`. Retrieve-only metrics ContextCoverage/ContextRelevance; R&G
  metrics Correctness/Completeness/Faithfulness/Helpfulness/Harmfulness plus citation metrics.
- **CloudFront + S3 (CDK):** OAC via `S3BucketOrigin.with_origin_access_control`, which
  auto-creates the OAC and bucket policy. The bucket needs bucket-owner-enforced ownership.
  Slow to create and destroy, roughly 15-30 minutes each.
- **Scraper:** `httpx` + `trafilatura` for page markdown; `extract_database_catalog` parses
  databases.php by link anchor (name = anchor text, description = the rest of the cell), which
  stays reliable even where the page has no delimiter. **Output stability (2026-07-29):** all 19
  seed URLs scraped twice back to back produced byte-identical markdown, and the only field that
  moved anywhere was the sidecar's `scrape_timestamp` - which is why the content fingerprint
  covers the body, `source_url` and `title` and excludes it. **Hours source (2026-07-29):**
  `about-the-library.php` is the library's single authoritative hours page. `libraryservices.php`
  claims the current day's hours are on the homepage, but the homepage carries no hours in its
  HTML at all - every hours reference on it links to `about-the-library.php#hours` - so no
  additional page needs seeding to answer hours questions.
- **SNS email subscription:** `sns.Topic` + `sns_subs.EmailSubscription` synthesize a topic plus a
  standalone `AWS::SNS::Subscription` with `Protocol: email`, and `enforce_ssl=True` adds a topic
  policy denying non-TLS publishes. CloudFormation creates the subscription in
  `PendingConfirmation` and SNS mails the recipient a link - until it is clicked, `Publish`
  succeeds and nothing is delivered. That one-time click is the whole reason this is cheaper to
  hand off than SES, whose status on the deploy account was checked on 2026-07-29: sandbox, 200
  msg/day, no verified identities.
- **Primo discovery API:** verified against the public Ex Libris endpoint - `primaws/rest/pub/pnxs`
  search plus a per-record `/L/{recordid}?getDelivery=true` call for availability. No auth for
  discovery. `info.total` is the not-held signal; relevance is query-relative, not an absolute
  threshold. Fields are `$$`-encoded, and a reserve record can serve multiple courses.
  Availability is a holding-level rollup, not item-level truth - real-time due dates and copy
  counts would need the authenticated Alma API.
