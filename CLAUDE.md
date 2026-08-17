# CLAUDE.md

## What this is

RAG chatbot for Gavilan College Library. Answers operational student questions (hours, checkout, textbooks, "what does the library offer"). Not an AI research librarian; research questions get pointed to a human librarian. AWS-native.

## Stack

- **RAG:** Amazon Bedrock Managed Knowledge Base (S3 data source) over an Amazon S3 Vectors store (Titan Embed v2, 1024-dim, cosine, semantic-only)
- **Query path:** an agentic Bedrock `Converse` tool-use loop (`run_agent`); the model calls tools and the loop feeds results back until `end_turn`. Four tools: `search_library_info` (KB retrieval), `database_catalog` (research-database availability + subject lookup, authoritative), `search_book_catalog` (Primo general catalog), `search_course_reserves` (Primo course reserves). The curated canonical-URL table is NOT a tool: it takes no input and returns the same rows every call, so it is injected into the Converse `system` payload every request (see `_links_block`). System prompt + date + link table via the Converse `system` param. Requests carry a multi-turn `messages` array (see `/query` contract).
- **Live catalog (Primo):** `search_book_catalog` + `search_course_reserves` call the Ex Libris Primo discovery API directly - outbound HTTPS to a third party inside the agent loop, NOT an AWS API (no IAM). The query Lambda therefore needs outbound internet to reach Primo; a VPC without a NAT path would silently break these two tools. Every call is timed out and soft-fails, so a slow/broken Primo never kills `/query`.
- **Ingestion:** a scraper Lambda pulls the library site into the KB source bucket (KB re-ingests) and regenerates the database catalog to S3, on a TIERED schedule + on deploy. Two tiers declared in `config.yaml` under `scraper.tiers`, one EventBridge rule each, tier name passed in the event: `fast` (hours/closures pages, daily) and `full` (the complete sweep, every 5 days). Everything downstream is CHANGE-GATED - see "Scrape cadence and change gating" below
- **Backend:** Lambda + HTTP API (API Gateway v2), Python
- **Infra:** AWS CDK (Python), L1 `Cfn*` constructs; everything as code
- **Guardrails:** ONE Bedrock Guardrail, screening the INPUT for `PROMPT_ATTACK` and nothing else (see "Guardrail note")
- **Config:** `config.yaml` at repo root; single source of truth for changeable knobs
- **Frontend:** vanilla JS widget (Shadow DOM, dependency-free). Bilingual chrome (English + Español) from one string table, switched by a control in the panel header; an explicit choice is sent as `language` on the request
- **Runtime theming:** the customer changes the highlight colour, the font family and the starter questions through ONE `theme.json` at the widget bucket root - no redeploy, no code edit. Fetched at init, merged over built-in defaults, soft-failing per key. A signed-in librarian edits it on the hosted settings editor and Save publishes it through `PUT /theme`; the S3-console upload is the fallback. See "Widget theme file" below and `docs/widget-theming.md`
- **Demo site:** one static page (`frontend/demo-site.html`) on its OWN S3 + CloudFront, embedding the production widget from the production CDN. `cdk deploy` stamps the live API + widget URLs AND the `cost_model` block into it, and outputs `DemoSiteUrl`. Toggle with `demo_site.enabled` in config.yaml
- **Cost visibility (demo only):** the demo page carries a session cost meter + a monthly estimator behind one control in the DEMO banner. It is fed by the widget's opt-in `data-usage-events` attribute (absent from the production embed) and the handler's opt-in `include_usage` flag. Rates + measured constants live in `config.yaml` under `cost_model`; re-measure with `eval/measure_usage.py`

## Scrape cadence and change gating

Answers "we changed our hours, when does the bot know?" with "hours within a day, everything else within five".

**Tiers** are declared entirely in `config.yaml` under `scraper.tiers`; each tier carries its own `schedule_cron` and its own `urls`, and every seed URL belongs to exactly one tier. `infra/config.py:resolve_scraper_tiers` validates that at synth (cron present, URLs non-empty, no URL in two tiers, a `full` tier exists). The stack builds one EventBridge rule per tier by iterating the map and passes `{"tier": "<name>"}` as the event input. Moving a page between tiers or changing a cadence is a config edit, no code change.

- `fast` - daily at 11:30 UTC. `about-the-library.php` (THE hours page: semester hours, semester date ranges, holiday closures), `finaid/index.php` (term-bounded office hours), `student/bookstore/index.php` (store hours + dated closure announcements).
- `full` - 10:00 UTC on the 1st/6th/11th/16th/21st/26th. **A full run fetches EVERY url in every tier**, not just the ones listed under it: it is the complete refresh, so it self-heals a failing fast tier, and the stale-object prune (which deletes whatever configuration no longer calls for) is safe by construction.

**The prune must never key off one tier's slice.** `scraper.all_seed_urls(tiers)` is the corpus; a daily 3-page run pruning against its own 3 URLs would delete five sixths of the knowledge base every night. Pinned by `test_a_fast_run_never_prunes_the_pages_it_did_not_fetch`.

**Three gates, no new store.** Change detection uses S3 object metadata, S3 `LastModified`, and Bedrock's own ingestion-job history. No DynamoDB, no state file.

1. **Upload** - each markdown object carries a `content-sha256` user-metadata fingerprint of the body + `source_url` + `title`. `scrape_timestamp` is deliberately excluded: it moves every run and hashing it would mark every page changed forever. A page whose fingerprint matches is not re-uploaded (needs `s3:GetObject` on the source bucket - HeadObject is authorized as GetObject).
2. **Ingestion** - starts only if this run changed something OR the bucket's newest object is newer than the last ingestion job's `startedAt`. That second clause is what makes a deferred run self-healing.
3. **Enrichment** - the Sonnet catalog call, the only meaningful per-run cost in this path. Gated on a fingerprint of the PARSED database rows, stored as `source_sha256` inside the catalog object itself. Identical rows means no model call and no S3 write. The parsed rows are hashed rather than the raw HTML so markup churn cannot bill a model call. **Order is load-bearing: parse, then the min-count guard, THEN the fingerprint** - a broken page must be rejected before its fingerprint could be recorded, or one bad scrape freezes the catalog forever (`test_guard_failure_does_not_record_a_fingerprint`).

**Concurrency.** Bedrock allows one ingestion job per data source (and rate-limits StartIngestionJob to one per ten seconds). The scraper lists jobs first and skips if one is STARTING/IN_PROGRESS; a race on the start call is caught and reported as deferred. Nothing fails loudly. The tiers are also scheduled 90 minutes apart so the path is rarely exercised.

**Per-run logging.** One structured `scrape run summary` line: tier, pages fetched/changed/unchanged/failed, objects pruned, ingestion decision, and the enrichment block with the REAL `input_tokens`/`output_tokens` Bedrock reported (or `{"ran": false, "reason": ...}`).

## Read the docs before design work

The architecture is in `docs/architecture.md`. When a task touches architecture, read it first so you match what actually runs.

## Commands

CDK app lives in `infra/`. All infra commands run from `infra/` with the venv active.

First-time setup (from `infra/`):
- `source .venv/bin/activate`
- `python -m pip install -r requirements.txt -r requirements-dev.txt`

Then (from `infra/`, venv active):
- Synth (offline, no creds): `cdk synth`
- Tests: `python -m pytest`
- Deploy: `cdk deploy` (needs AWS creds; account pending)
- Teardown: `cdk destroy` (removes the S3 Vectors bucket + index, the catalog bucket, and the KB source/widget buckets)

`aws-cdk-lib==2.260.0` is pinned in `infra/requirements.txt`. The CDK CLI is not pinned anywhere in the repo; this was built against `2.1129.0`.

### Changing `chunking` in config.yaml

Bedrock chunking is immutable, so this is a data-source REPLACEMENT, not an update. Two things follow:

1. The data source name folds in the chunking settings (`gavilan-library-kb-s3-fixedsize-600t20p`). CloudFormation creates a replacement before deleting the original, so a fixed name collides inside the KB and the deploy dies with `409 AlreadyExists`. Do not "simplify" that name back to a constant.
2. **The replacement starts EMPTY and `cdk deploy` does not refill it.** The scraper's deploy Trigger only re-fires when the scraper's own code changes, so a chunking-only deploy leaves a knowledge base with zero indexed documents - `/query` will answer everything with "I don't have that information". Kick ingestion by hand right after the deploy:

Get the CURRENT knowledge-base id from the stack output rather than pasting one from here -
the KB is replaced whenever the vector index is, so a hardcoded id in this file goes stale
(it did: the old `GLBDBZXOFU` no longer exists).

```
KB=$(AWS_PROFILE=gavilan aws cloudformation describe-stacks --stack-name GavilanChatbotStack \
  --region us-west-2 --query "Stacks[0].Outputs[?OutputKey=='KnowledgeBaseId'].OutputValue" --output text)
AWS_PROFILE=gavilan aws bedrock-agent list-data-sources --knowledge-base-id $KB --region us-west-2
AWS_PROFILE=gavilan aws bedrock-agent start-ingestion-job \
  --knowledge-base-id $KB --data-source-id <NEW_ID> --region us-west-2
```

The source bucket is untouched by the replacement, so ingestion just re-indexes what is already there. Then re-run `eval/retrieval_probe.py` - the offline chunking eval measures boundaries, never ranking, so live recall is the only check that a bigger chunk did not retrieve worse.

Handler unit tests stub `boto3` in `sys.modules` and monkeypatch the client getters, so they need no boto3 install and no live AWS.

### CI

`.github/workflows/ci.yml` runs on PRs into `main`/`dev` and on pushes to them. Four parallel jobs, one per test surface, all hermetic (no AWS creds, no deployed endpoint):

| Job (check name) | Where | Command | Install |
| --- | --- | --- | --- |
| `infra tests (CDK stack + Lambda handler)` | `infra/` | `python -m pytest -v` | `requirements.txt` + `requirements-dev.txt` |
| `scraper tests` | `scraper/` | `python -m pytest tests -v` | `requirements.txt` + `requirements-dev.txt` |
| `eval harness tests` | `eval/` | `python -m pytest tests -v` | `requirements-dev.txt` ONLY |
| `widget tests` | repo root | `node frontend/test/widget.contract.test.js` | none (dependency-free, no package.json) |

Python pinned to 3.13 to match the Lambda runtime (`_LAMBDA_PYTHON`); Node 22 for the widget.

CI notes, so nobody "fixes" these back:
- **No `cdk synth` job.** `test_infra_stack.py` already builds the real stack via `load_config()` + `GavilanChatbotStack(...)`, so synth would only cover `app.py`/`cdk.json` glue, and it would need a Node + pinned CDK CLI install plus a network-bound asset bundle (the scraper deps layer shells out to `pip --platform`, Docker fallback).
- **The eval job installs `requirements-dev.txt` only, never `requirements.txt`.** `eval/tests/conftest.py` stubs boto3 *only if it is not already importable*; installing the real boto3 would silently disable the no-live-AWS guard.
- **No `paths:` filters.** A required check skipped by a paths filter reports as pending forever and blocks the PR. All four suites together are ~11s of test time, so filtering buys nothing.
- **The deployed-infra evals stay out of CI**: the Bedrock runners in `eval/` and the `eval/promptfoo/` answer-quality loop need real credentials, a live `/query`, and cost money per run.

## Vector store

Amazon S3 Vectors (`s3vectors.CfnVectorBucket` + `CfnIndex`); the KB `StorageConfiguration` type is `S3_VECTORS`, referencing the index by `IndexArn` (that one field only - passing IndexName/VectorBucketArn too makes CloudFormation reject the config as ambiguous). Config lives in `config.yaml` under `vector_store`:
- index: 1024 dims (Titan Text Embeddings v2), `data_type` float32, `distance_metric` cosine
- index name: `bedrock-knowledge-base-default-index`; vector bucket: `gavilan-library-vectors`
- `non_filterable_metadata_keys` (immutable at index creation) mark Bedrock's internal keys (`AMAZON_BEDROCK_TEXT`, `AMAZON_BEDROCK_METADATA`, `x-amz-bedrock-kb-*`) non-filterable, or ingestion fails on the filterable-metadata limit

## `/query` contract (widget builds against this)

**`POST /query` is PUBLIC**, as is `GET /warm`. No token, no API key, no authorizer - the widget is embedded on a public library page, so every caller is anonymous by definition. The abuse controls are stage-level throttling (the real cost cap, and why WAF is excluded), the `PROMPT_ATTACK` input screen and the exact-match CORS allowlist. A pre-launch Cognito gate on this route was DELETED at go-live, not made switchable; do not reintroduce one. `PUT /theme` is the one gated route on this API - see "Widget theme file".

`POST /query` request shape (multi-turn): `{ "messages": [ {"role": "user"|"assistant", "content": "<text>"}, ... ] }`, oldest first, newest user turn last. Backward-compatible with the legacy `{ "query": "<text>" }` shape (treated as a single-message conversation). History is client-sent - the Lambda is stateless (no server-side conversation store) - and trimmed server-side to the last 10 messages (`MAX_HISTORY_MESSAGES`) before seeding the Converse loop.

Response shape: `{ "answer": "<text>", "sources": [ {"uri": "<page url>", "excerpt": "<snippet>"} ] }`

`sources` is deduplicated by uri; it accumulates from every `search_library_info` retrieval the model ran during the loop, plus one synthetic source per tool that returned a result: the library A-Z databases page for `database_catalog`, a per-query Primo discovery-search URL for each of `search_book_catalog` and `search_course_reserves`. The curated link table contributes NO sources: it reports which links exist, not which one the answer used. `_extract_source` prefers the public `source_url` (the scraper's per-document metadata sidecar) over the internal S3 URI; a source that resolves only to an `s3://` URI is omitted so no bucket path reaches the client. If the model answers without a tool call (e.g. a greeting), `sources` is `[]`.

Optional reply language: a request may carry `language: "en"|"es"`. Present -> ONE extra Converse `system` block telling the model to write its whole reply in that language, so an explicit Español selection is honored even for a question typed in English. Absent or unrecognized -> no block at all, and the model keeps auto-detecting the language from the question (which it already does correctly). The code is ALLOWLISTED against `_LANGUAGES` and the prompt text is built from the handler's own table, never from the client's string - the field is client-supplied text heading for the system payload, so that is a security boundary, not tidiness. Nothing else is language-aware: retrieval, the four tools, and the KB are untouched (the KB is English and Spanish questions retrieve from it correctly - measured, see the Lessons entry).

Opt-in cost payload: a request with `include_usage: true` also gets `usage` - the BILLABLE units that one question consumed across the whole loop: `model_calls`, `input_tokens`, `output_tokens`, `cache_read/write_input_tokens`, `guardrail_calls` (always 1 - the input screen; nothing is attached to Converse), `guardrail_units` (per policy, snake_cased from Bedrock's own field names), `retrievals`, `tool_calls`. Summed from what Bedrock reports on each response, so it captures the thing that is invisible from outside: one question is often several Converse calls, each resending the whole context. `include_full_context: true` implies it. The widget only sets the flag when its embed carries `data-usage-events` (the demo page does; the production embed tag does not), so a student's response is exactly `{answer, sources}`.

Opt-in debug payload: a request with `include_full_context: true` gets three extra response fields - `full_context` (the un-deduped, un-truncated KB passages from `search_library_info`), `tool_calls` (an ordered trace of EVERY tool the loop invoked: `{tool, input, status, returned_results, result}`, where `result` is the exact JSON the model received as that call's `toolResult` content), and `library_links` (the curated URL table the model was handed in its `system` payload). `full_context` covers one of the four tools; `tool_calls` covers all of them. `library_links` is neither - it is context, not a tool result, and WITHOUT it the eval's groundedness judge sees a curated URL with no supporting evidence and marks a correct link ungrounded. The widget never sets the flag, so its responses stay exactly `{answer, sources}`. Recording the trace changes nothing about what is sent to the model.

Query flow (`run_agent`): the newest user turn is the current question; the trimmed history seeds the Converse `messages`, and the system prompt is passed via the Converse `system` parameter (never concatenated into the user message). Each turn calls `Converse` with the four-tool `toolConfig` and NO `guardrailConfig` (there is no output guardrail); on `stopReason == tool_use` the loop executes every requested tool, appends the `toolResult`s, and calls again, until `end_turn` (or a max-iteration cap). There is no `<context>` passage-injection - retrieved passages reach the model as tool results.

## Widget theme file (`theme.json`)

At pickup the client deploys this stack into their OWN account, so the widget bucket and its
distribution are theirs. Three things they will ask to change - the highlight colour, the font
and the starter questions - are read at RUNTIME from `theme.json` at the widget bucket root.
No redeploy, no hand-editing a shipped file. `docs/widget-theming.md` is the developer guide;
the customer-facing copy lives in the shipped artefacts themselves.

**The file**

- **JSON, never JS.** A `.js` config is executable code on the library's page with the widget's
  privileges. Every value is allowlisted: the colour against a hex pattern (it is concatenated
  into a stylesheet, so that regex is a security boundary - anything carrying `;` or `}` would
  let the file restyle or hide the widget), the font against a table we own, the questions
  rendered as text nodes.
- **Soft-fails per key.** Malformed JSON, or the 404 every install serves until the first save,
  falls back entirely. An unthemed install emits ZERO theme CSS, so its rendering is provably
  byte-identical to the pre-theme widget.
- **Highlight colour ONLY.** No background or text knob: twelve colour values are not tokens, and
  the divider, focus ring and halo were each measured against specific light surfaces to clear
  3:1. Text ON the highlight is DERIVED - black or white, whichever contrasts better - and needs
  no validation, since the worst case over the whole sRGB cube is the black/white crossover at
  4.58:1. Everything drawn on the header follows that derived ink rather than a hardcoded `#fff`,
  or a pale highlight gets an invisible focus ring. The two-tone `--focus-ring`/`--focus-halo`
  pair is NOT themeable.
- **Font: enumerated keywords, family only.** `system` (default) / `sans` / `serif` / `mono` /
  `inherit`. Never size, weight or line-height - those are wired to the panel's zoom/reflow
  clamps, so a font-size field turns a branding change into an accessibility regression. Every
  stack resolves on macOS AND Windows with no download and no `@font-face`, because a customer
  typing a locally-installed family name would ship a Times fallback for everyone else and never
  see it. Bitter belongs to the DEFAULT theme, so the Google Fonts fetch is conditional on
  `fontFamily == "system"`.
- **Starter questions: per-language, max four each, 120 chars.** Over-long entries are dropped,
  not truncated. Spanish is optional and falls back to the customer's ENGLISH list, not to our
  built-in Spanish, which may ask about a service they removed. No machine translation.
- **No flash of default colours.** The fetch runs in parallel with page load and the mount WAITS
  on it, capped at `CONFIG.themeTimeoutMs` (1.5s). `mount()` itself stays synchronous and
  theme-free, which is what the contract tests drive.
- **Conformance scope:** the accessibility audit measured DEFAULT colours. Derived ink is safe at
  any highlight, but the highlight is also a text colour on light surfaces (links, source links,
  starter chips), so a custom one is the customer's to verify.

**Delivery, and the two ways a deploy could destroy the customer's work**

- **The customer's file must survive `cdk deploy`.** `BucketDeployment` prunes with `aws s3 sync
  --delete` and the source will never contain a file the customer wrote, so the widget deployment
  carries `exclude=["theme.json", "defaults/*"]`. That is the **BucketDeployment** prop, which
  scopes the sync command - NOT the identically named argument on `Source.asset`, which only
  filters what is packed into the asset and protects nothing. Both are in that one call.
  **TWO patterns, and the second is not redundant:** `--exclude` fnmatches the FULL path with the
  sync root prepended, so `theme.json` covers the root object and nothing under a prefix - with
  only that entry, `defaults/theme.json` is deleted on every deploy (measured against aws-cli
  2.35.11, not assumed). Pinned by `test_the_theme_file_is_outside_the_prune_scope`.
- **`content_disposition` applies to a WHOLE `BucketDeployment`**, which is why the download gets
  its own rather than riding along with `widget.js` - a `<script src>` that comes back as a file
  download is a broken widget. That second deployment shares the widget bucket, which is normally
  how `widget.js` gets deleted by accident (see the Lessons entry); it is safe on exactly two
  properties, `prune=False` and `destination_key_prefix="defaults"`, plus the matching exclude
  above. The stack NEVER writes the root `theme.json` - no seeding.
- **CORS + TTL come from the DISTRIBUTION, not the object**, because a console upload sets no
  metadata. `theme.json` gets its own cache behavior: a 60s cache policy plus a response-headers
  policy carrying `Access-Control-Allow-Origin: *` and `Cache-Control: max-age=60`. That `*` is
  NOT the wildcard `config.py` rejects for the API - that one guards a billable POST endpoint;
  this is a world-readable static file on the customer's own CDN, and an exact allowlist would
  add no secrecy and one silent failure mode (a staging subdomain nobody listed). `widget.js`
  delivery is untouched: the default behavior keeps `CACHING_OPTIMIZED` and takes no CORS header.

**The three shipped pages and files**

- **`defaults/theme.json` is the download** - a deployment-owned copy of the built-in defaults,
  ALREADY named `theme.json` (hence the prefix: a second file of that name at the root would BE
  the customer's), served `Content-Disposition: attachment` so a click saves it rather than
  rendering JSON in a tab. Its `_readme` is an ARRAY of lines documenting every key, every font
  keyword and both caps, with NO URL: once downloaded it travels alone and its reader has no
  GitHub access, and JSON takes no comments, so the file must explain itself. The contract suite
  pins its canonical form (byte equality against a two-space serialisation), its key order and
  the self-documentation. The repo must never contain a root `frontend/theme.json`.
- **`theme-guide.html` is the hosted guide**, one self-contained page at the bucket root and its
  own source of copy. No scripts, no external CSS or fonts, no absolute URLs; its links are
  RELATIVE (`defaults/theme.json`, `theme-editor.html`), so it works on any distribution domain
  with no deploy-time stamping. It rides the widget deployment as a SECOND source and must never
  move under `defaults/`, whose deployment-wide attachment header would download it instead of
  rendering it. It has NO stack output: its only route in the product is the guide link in the
  editor's settings panel.
- **`theme-editor.html` is the hosted settings editor**, the THIRD source of that deployment and
  the ONE theming page an output links. It seeds from the DEPLOYED root `theme.json` (falling
  back to `defaults/theme.json`, then built-ins) so the customer edits their live theme, and its
  Download button builds a `theme.json` in the pinned serialisation, `_readme` included. Shipped
  via `Source.data` and DEPLOY-STAMPED like the demo page: four placeholders
  (`__THEME_SAVE_URL__`, `__THEME_AUTH_BASE__`, `__THEME_CLIENT_ID__`, `__THEME_COGNITO_IDP__`)
  resolve at deploy, a missing one fails synth, and the committed file carries no absolute URL.
  Scripts are allowed on THIS page, unlike the guide, but no innerHTML and nothing persistent:
  sessionStorage holds exactly ONE key (`gavtheme-oauth`, the in-flight PKCE handshake, removed
  on return), tokens live in JS variables, and the refresh token is dropped unread. It duplicates
  the widget's validation rules and the defaults file BY DESIGN, and the contract suite pins
  every copy against `widget.js` and `defaults/theme.json` so none can drift silently; the save
  Lambda's Python copy is pinned by `test_theme_handler.py`.
- **The main view is the fields, the preview and Save; everything else is one control away.**
  Once Save wrote the live file the console upload became a fallback rather than the workflow, so
  the page stopped teaching it: the download, a "Choose defaults" refill and the account controls
  live in a native `<dialog>`. Two rules the contract suite pins. The download MOVED and must
  never disappear - an unsigned visitor has no other route to a `theme.json`, and its bytes are
  pinned identical to `defaults/theme.json`. And "Choose defaults" refills from the page's ONE
  embedded defaults block (`seedForm(builtinState)`), because a second hardcoded copy of the
  shipped values would drift.

**Saving, and the outputs**

- **Save is gated by its own Cognito pool** (`gavilan-library-theme-admin`) - the ONLY pool and
  the only sign-in anywhere in the product, since it guards a write rather than a read. Signed
  in, Save PUTs
  the built file to `PUT /theme`, behind a native JWT authorizer whose audience is the APP CLIENT
  ID - a Cognito ACCESS token has no `aud` claim, it has `client_id`, and API Gateway validates
  `client_id` only when `aud` is absent, so do not "fix" this to an ID token. `app/theme_handler.py`
  revalidates against the widget's rules (allowlisted keys, hex pattern, font keywords, 4/120
  caps - violations are a 400, not a soft drop), rewrites the file in the pinned serialisation,
  writes the ONE object its IAM allows and invalidates `/theme.json`. Unsigned, Save reads "Sign
  in to save" and the page is the download-only editor; unstamped (the repo copy), Save stays
  hidden entirely.
- **The account lifecycle is self-service; one CLI command total.** The pool signs in by EMAIL
  (immutable choice), self sign-up off, strong password policy, recovery by verified email,
  email changes verified before they apply (`keep_original`), 90-day temporary-password validity
  instead of the 7-day default. Sign-in, the forced first-password change and forgot-password are
  Cognito MANAGED LOGIN's hosted pages (v2 + `CfnManagedLoginBranding`, domain prefix
  `gavilan-theme-<account id>`); the editor page only redirects, authorization code + PKCE, no
  implicit grant, because tokens in URL fragments land in browser history. Change password and
  change email run as user-scoped `cognito-idp` calls with the librarian's own access token
  (scope `aws.cognito.signin.user.admin`). Accounts come from `ThemeAdminCreateUserCommand`:
  `admin-create-user` with `PROJECT_EMAIL_HERE` substituted twice (the email IS the `--username`),
  `--desired-delivery-mediums EMAIL` (the default is SMS - omit it and the invitation never
  arrives), `Name=email_verified,Value=true` (what makes self-service reset work), and NO
  temporary password (Cognito generates one; the invitation template must carry BOTH `{username}`
  and `{####}` or nothing is delivered). Another librarian is the same command with another
  address; a stale invitation is the same command plus `--message-action RESEND`. No user in
  CloudFormation and no admin email in config.yaml: every `AWS::Cognito::UserPoolUser` property
  is replacement-on-update, so a template-owned user could not change email without wiping the
  librarian's password.
- **TWO theming URL outputs, and the editor is the entry point.** `WidgetThemeEditor` carries the
  "Start here" framing and says Save publishes straight to the live widget, because an editor
  that reads like it only produces a file sends the customer into the console for a step Save
  already did. `WidgetThemeUpload` is the S3-console deep link, kept because the guide's console
  procedure names it BY OUTPUT NAME and because it is the only route for anyone not signed in.
  Both are `https://` so the Outputs tab renders them as links, and both carry a Description,
  because that tab is the only entry point anyone has: the bucket name is generated per install,
  the account holds ~19 buckets, and the demo-site bucket sits next to the widget one, where an
  upload succeeds silently and changes nothing.
  **`WidgetThemeGuide` and `WidgetThemeDownload` are DELETED - do not reintroduce them.** Once
  Save wrote the live file and the settings panel held both the download and the only link to the
  guide, those rows were a second and third route into one workflow. Pinned from the other side
  by `test_no_output_points_at_the_guide_or_the_defaults_download`, which fails on the VALUE too,
  so re-adding either link under a different output name does not slip through.

## `/feedback` (ARCHIVED - built, never wired to a UI)

`POST /feedback` -> its own small Lambda -> an SNS topic -> one email subscription. The backend
exists (`app/feedback_handler.py`, the stack wiring, `test_feedback_handler.py`), but the widget
never got a report-an-answer button, so nothing can call it. `config.yaml` ships the block at the
BOTTOM under an ARCHIVED header with `enabled: false`, and `resolve_feedback` therefore builds
nothing and emits no deploy output. It is out of the customer-facing docs on purpose.

Do not present it as a feature or point a librarian at `notify_email`. Turning it back on is a
config edit, but it needs the widget side built first, or it is an endpoint nobody can reach.

Three things worth keeping if it is ever revived, because each was decided the hard way:

- **`enabled: false`, not enabled-with-a-blank-address.** The blank-address path is the "you
  forgot something" state: it provisions nothing but prints a `FeedbackStatus` output telling you
  to go set an address. That is the opposite of archived. Off is a choice and says nothing.
- **SNS, not SES.** SES starts every account in a sandbox that only sends to pre-verified
  addresses and needs a support request per account to leave (verified 2026-07-29 on the deploy
  account: `ProductionAccessEnabled: false`). That would make one-click install into a fresh
  account the client's problem, for one plain-text notification. SNS email needs one confirmation
  click by the recipient.
- **No server-side store, by constraint.** No table, no bucket, no logged copy, no dead-letter
  queue holding report bodies - a failed publish loses the report, deliberately, because the
  alternative is a database of student complaints nobody agreed to keep. The cited source URLs
  were the entire payload: the fix for a wrong answer is a webpage edit (D-20260727-10), so the
  email was a work order naming the pages. `test_feedback_introduces_no_store` and the
  log-hygiene tests pin it.

## Repo layout

- `infra/` - the CDK app. `app.py` loads `config.yaml`; `infra/infra_stack.py` is the whole stack;
  `infra/config.py` holds `load_config()` and the synth-time validators
  (`resolve_cors_allow_origins` rejects `*`, `resolve_scraper_tiers` / `resolve_seed_urls`,
  and `resolve_feedback` for the archived path). `tests/unit/` covers the stack via
  `Template.from_stack` plus the three Lambdas with boto3 stubbed. See `infra/README.md`.
- `app/` - the Lambdas, each bundled alone so none drags in the others' dependencies.
  - `handler.py` - the query path, public and unauthenticated like `/warm`. `run_agent()` runs the Converse tool-use loop over four tools
    (`search_library_info` -> KB `Retrieve`; `database_catalog` -> the catalog from S3;
    `search_book_catalog` + `search_course_reserves` -> live Primo, client inline, no import).
    `_links_block()` renders the bundled link table into the `system` payload. Wiring from env
    vars; boto3 from the runtime.
  - `system_prompt.md` - `<tools>` carries the four-tool routing, including that course textbooks
    go to `search_course_reserves` and not the general catalog; `<priority_responses>` is checked
    first and answers safety and emergency messages verbatim with no tool call; `<citations>`
    names the injected CANONICAL GAVILAN LINKS block as one of exactly two permitted link
    sources, which is why the link table is deliberately absent from `<tools>`.
  - `data/database_catalog.json` - the hand-authored not-held list plus a fallback held list,
    merged with the S3 held list at read time.
  - `data/library_links.json` - the hand-authored canonical URLs. Fully static: no scraper, no
    S3, no cache. Edit and redeploy to change.
  - `feedback_handler.py` / `theme_handler.py` - the two small Lambdas. Neither imports
    `handler.py`. Each holds exactly the IAM it needs: one `sns:Publish`, one `s3:PutObject` on
    one key.
  - `primo_search.py` - a dev CLI for exploring the Primo API. Not imported, not bundled.
- `scraper/` - `scraper.py` (pure fetch/extract, the `extract_database_catalog` HTML parse, and
  the tier helpers shared by the Lambda and the CLI) + `lambda_function.py` (gated upload, gated
  ingestion, catalog regeneration: parse -> guard -> fingerprint gate -> enrichment -> write).
  Own `.venv` and tests; the tests need the runtime deps, so install both requirements files.
- `eval/` - the Bedrock RAG eval harness, on demand against deployed infra, plus `promptfoo/`
  for answer quality. `run_chunking_eval.py` and the unit tests are the only parts that run
  offline. `measure_usage.py` prints the `cost_model.measured` block for config.yaml and spends
  real money; re-run it after anything that moves token usage - `retrieval.number_of_results`,
  `chunking`, the system prompt, the link table, `generation.max_tokens`.
- `frontend/` - FIVE files ship, to two buckets. To the widget bucket: `widget.js` (which
  carries NO credential and NO auth code - it sends `Content-Type` and nothing else, because
  `/query` is public),
  `defaults/theme.json`, `theme-guide.html`, `theme-editor.html` (the last three are covered
  under "Widget theme file"). To the demo bucket: `demo-site.html`, uploaded as `index.html` -
  a Gavilan-styled sample page with local CSS only, carrying the SAME one-line embed a library
  page would, so it cannot fork from the shipped widget; placeholders for the widget src, the
  `/query` URL and the `cost_model` block are stamped at deploy, and renaming one fails synth.
  `mock.js`, `demo.html`, `demo-live.html` and `test/widget.contract.test.js` never ship, and the
  dependency direction is one-way - the widget never references the mock.
  - `widget.js` is **bilingual**: every user-visible string lives in the `STRINGS` table above the
    `END LOCALIZATION` banner, and render code below it calls `t(key)` and holds no copy, which
    the contract suite enforces by scanning the file. The header toggle is real buttons with
    `aria-pressed` and a group `aria-label` - no ID-based ARIA, which cannot cross the shadow
    boundary - and switching sets `lang` on the host element and the shadow root container. Each
    message is stamped with the language it was said in, so a switch relabels the chrome and NOT
    the transcript; retranslating past turns would cost a model call each. The greeting and
    starter questions DO re-render on a switch, but only before the first message, because they
    are the panel's opening state rather than a turn anyone took. The table covers the
    SCREEN-READER-ONLY text too - a label nobody can see is still read aloud, and it is invisible
    to any check that looks at the rendered page. Two things the toggle must NOT undo: the
    launcher takes no `aria-label` in any language (its visible text is its whole accessible name,
    WCAG 2.5.3), and re-seeding the opening state has to re-point the composer's
    `aria-describedby` at the NEW greeting bubble, or a switch leaves it aimed at a removed node.
    The widget uses NO browser storage at all - no `localStorage`, no `sessionStorage`, no
    cookie, no `indexedDB` - and the contract suite pins that by scanning the source.
- `config.yaml` - every changeable knob, read at synth by `infra/config.py`. Notable blocks:
  `scraper.tiers` (the only declaration of cadence and tier membership) + `kb_exclude_urls`;
  `vector_store`; `chunking`; `retrieval.number_of_results`; `generation.model_id`; `catalog`;
  `primo` (wired as `PRIMO_*` env, shared by both catalog tools); `library_links.data_file` (the
  stack feeds the SAME value to the asset include and the env var so they cannot drift);
  `cors.allow_origins`; `demo_site.enabled`; `feedback` (ARCHIVED, at the bottom of the file
  and shipped off - see the archived-feature note); `guardrail`; and
  `cost_model` (published rates + measured per-question constants + the zero-traffic baseline,
  stamped into the demo page - note `scrapes_per_month` and `reindexes_per_month` are SEPARATE
  numbers, because change gating means most runs re-index nothing).
- `docs/` - `install.md` (the deployer's five-step walkthrough), `architecture.md`,
  `build-plan.md`, the architecture diagram, the accessibility audit, and `widget-theming.md`.
  The customer-facing theming copy lives in the shipped artefacts themselves:
  `frontend/theme-guide.html` and the `_readme` inside `frontend/defaults/theme.json`.
- `.github/workflows/ci.yml` - four hermetic jobs, one per test surface. See CI under Commands.

## Excluded (do not reintroduce)

- **THE OUTPUT GUARDRAIL EXCLUDED, and every input policy except `PROMPT_ATTACK`.** Deleted, not disabled - no `guardrailConfig` on any Converse call, no PII policy, no HATE/SEXUAL/INSULTS/VIOLENCE/MISCONDUCT filter. The reason is ordering, not cost: the input screen runs before the system prompt, so a block or a silent PII rewrite pre-empts `<priority_responses>` and answers a student in crisis with canned decline copy - or hands the model `{NAME}`/`{ADDRESS}` where the address was the point. See "Guardrail note". Pinned by `test_exactly_one_guardrail_and_it_is_the_input_screen`, `test_guardrail_screens_prompt_attack_and_nothing_else`, `test_guardrail_has_no_pii_policy` and `test_converse_never_carries_a_guardrail_config`.
- **Contextual grounding EXCLUDED.** AWS doesn't support it for conversational chatbots; requires fragile `guardContent` message-tagging (silent-failure trap). The system prompt handles grounding. Revisit via standalone `ApplyGuardrail`, not inline Converse tagging.
- **WAF EXCLUDED.** WAF can't attach to HTTP API v2 (would need a second CloudFront fronting the API). Thin threat surface; API Gateway throttling is the real cost-abuse control. Revisit only on a compliance mandate.
- **CORS `allow_origins: "*"` EXCLUDED.** Locked to `cors.allow_origins` in config.yaml (`https://www.gavilan.edu`, a dev-only `http://localhost:8000`, and the demo site's custom hostname `https://gavbot-demo.calpoly.io`); `infra/config.py` rejects a wildcard at synth. Entries are matched as EXACT full origin strings, so each is scheme + host with no trailing slash and no path. The browser sends a HOST-only `Origin`, so do not add the `/library/` path. CORS is browser-enforced only and is NOT a security boundary (curl/scripts ignore it) - throttling is still the cost cap - but a wildcard would let any page drive the billable `/query` endpoint from its visitors' browsers. Don't "fix" a CORS console error by widening this; add the real origin.
- **`generative-ai-cdk-constructs` Bedrock L2s EXCLUDED (deprecated).** That is WHY the stack is L1 `Cfn*`. Do not reintroduce.
- **The FEEDBACK PATH is ARCHIVED, and SES + a server-side store stay EXCLUDED within it.** See the `/feedback` archived note above for all three decisions. Do not revive the path, re-document it for the customer, or "improve" it with a store.

## Guardrail note

ONE guardrail, screening ONE category: `PROMPT_ATTACK` on the INPUT, via `ApplyGuardrail(source=INPUT)` on the bare query before the loop. No other content filter, no PII policy, and NOTHING attached to the Converse call.

**Why it is that narrow.** The screen runs BEFORE the system prompt does, so anything it decides pre-empts `<priority_responses>`. A content filter that blocks a message about self-harm answers a student in crisis with canned decline copy; PII anonymization silently rewrites their message, so a name and a street reach the model as `{NAME}` and `{ADDRESS}` - it strips exactly the details that make an urgent message legible. The system prompt owns safety: it sees the whole question, it can tell a request for a book about suicide prevention from a person in trouble, and unlike a filter it can RESPOND rather than only refuse. `PROMPT_ATTACK` stays because it is an attack ON the prompt, it is input-only by definition, and nothing else defends a public unauthenticated endpoint. Do not "restore" the other categories or the PII policy without re-litigating that ordering problem.

Needs `bedrock:ApplyGuardrail` on the guardrail ARN alongside `InvokeModel`, or it silently fails at runtime. The Lambda pins to a published numbered guardrail version; live policy/message edits require a new version.

`blocked_input_messaging` in `config.yaml` is BILINGUAL (English then Spanish, one string, blank line between). It is static guardrail configuration returned by Bedrock instead of a model reply, so nothing translates it at runtime and the widget's language control cannot reach it - a Spanish-speaking student who trips it would otherwise hit an English wall. Bedrock caps it at 500 characters, so keep the pair under it. `_FALLBACK_BLOCK_MESSAGE` in the handler (the defensive path when the guardrail returns no text) is bilingual for the same reason. Editing it takes effect only on the next `cdk deploy`, which publishes a new guardrail version and repins the Lambda. There is no second message: `CfnGuardrail` requires a `blockedOutputsMessaging` property, so the stack passes the same string, and it is unreachable by construction.

## Hard rules

- **No Git by default.** Do not commit, stage, push, or take any other git/GitHub action unless the task order explicitly instructs a commit. When it does, follow that instruction exactly and do nothing else with git.
- **Behavior and tool routing live in the system prompt + tool descriptions, not hardcoded Lambda branches.** Tool choice is the model's (`toolChoice` auto); textbook routing, safety responses and out-of-scope handling are prompt instructions.

## Writing style

Avoid em dashes; use " - " or parentheses instead.

## When testing

Never hide failures. Show all test results, including failures, in full.

## Lessons

Things learned the hard way. Read before repeating the same work.

**CDK and AWS**

- **Verify CDK construct APIs against the installed package, not memory.** The Bedrock construct
  surface is in flux and training data is stale, so introspect `aws-cdk-lib==2.260.0`. Current
  gotcha: `CfnKnowledgeBase` `S3VectorsConfiguration` is a `oneOf` - pass `IndexArn` alone, and
  adding `IndexName`/`VectorBucketArn` makes CloudFormation reject it ("2 subschemas matched
  instead of one").
- **L1 `Cfn*` constructs do NOT enforce CloudFormation's property constraints at synth.** A
  274-character `AWS::Bedrock::Guardrail` `Description` (cap: 200) synthed clean, passed the whole
  infra suite, uploaded its assets, then died in early change-set validation - so the first
  enforcement point is a deploy that fails before touching a single resource. This is a general
  hazard of an all-L1 stack: `maxLength`, `pattern` and enum constraints are invisible until
  deploy. Put rationale in a code comment, not an AWS-visible description field, and pin the
  rendered length with a template assertion.
- **HTTP API v2 is in `aws-cdk-lib` core as of 2.260.0.** `HttpApi` + `CorsPreflightOptions` in
  `aws_cdk.aws_apigatewayv2`, `HttpLambdaIntegration` in `aws_cdk.aws_apigatewayv2_integrations`.
  The `-alpha` packages are gone and are not needed. HTTP API not REST (~71% cheaper for a
  Lambda-proxy job); `HttpLambdaIntegration` defaults to payload format 2.0.
- **CloudFront is slow to create AND destroy (~15-30 min each),** which dominates deploy and
  destroy wall-clock. Real serving behaviour (OAC read, cache, HTTPS redirect) is only verifiable
  at deploy.
- **OAC + `auto_delete_objects` dependency cycle.** Do NOT add an explicit
  `distribution.node.add_dependency(bucket)`: the origin already references the bucket, and the
  explicit edge pulls in the auto-delete custom resource, which `DependsOn` the OAC bucket policy,
  which `DependsOn` the distribution. Surfaces in `Template.from_stack` even when plain
  `cdk synth` looks fine, so keep infra tests in the loop.
- **Invoking a Claude model needs a `us.`-prefixed inference profile** - bare model ids are
  rejected - which requires `InvokeModel*` on the profile ARN plus foundation-model ARNs across
  routed regions, not the single-ARN on-demand grant. Applies to the query Lambda's generation
  model and the scraper's enrichment model alike.
- **Config keys reach the `from_asset` Lambdas ONLY as stack-set env vars** (the bundle excludes
  config.yaml). A new runtime knob needs three touches - config.yaml, stack env-wiring, handler
  read - or it silently no-ops. Synth-time-only keys are read by the stack directly.
- **Deploy-time values reach a STATIC file via `s3deploy.Source.data`, not an env var.** A
  hardcoded endpoint in HTML would break one-click install in a fresh account. `Source.data`
  resolves CDK tokens *during deployment*: CDK stages the file with `<<marker:0xbaba:N>>`
  placeholders and the deployment custom resource substitutes them from `SourceMarkers`. Verified
  in the synthesized template and the staged asset. It substitutes EVERY literal occurrence,
  comments included, so never spell a placeholder out in the file's own documentation.
- **A second `BucketDeployment` into the widget bucket would delete widget.js.** `prune=True` is
  the default, and it is `aws s3 sync --delete`: two deployments sharing a bucket fight, whichever
  CloudFormation runs last wins, and production widget delivery breaks intermittently rather than
  reproducibly. `destination_key_prefix` does scope the prune, and so does `exclude`, but both are
  one config away from wrong - so the demo site gets its OWN bucket and distribution, making the
  interference structurally impossible. **The theme-defaults deployment is the one exception, and
  it earns it by being unable to fight:** `prune=False` deletes nothing at all,
  `destination_key_prefix="defaults"` writes nowhere else, and the widget deployment's `exclude`
  covers that prefix. The real invariant is not "one deployment per bucket" but **one PRUNING
  deployment per bucket**, which `test_only_one_deployment_prunes_each_bucket` asserts directly.
- **`cdk deploy` does NOT re-scrape unless the scraper Lambda itself changed.** Verified in the
  synthesized template: `execute_on_handler_change=True` ties the install Trigger to the
  function's `currentVersion`, whose logical id hashes the code asset plus the function
  configuration (env vars, layers, memory, timeout, role). Deploy a widget tweak, a demo-page edit
  or a query-Lambda change and that id is byte-identical, the custom resource is unchanged,
  CloudFormation does not re-run it, and no scrape fires. That is why deploys can look like they
  did nothing. The trap is the corollary: a config edit that moves a scraper ENV VAR *does* change
  the hash and *does* re-fire, so a config-only change is not always free.
- **An API Gateway authorizer's 401 carries NO CORS headers, so a browser cannot read it.** The
  docs say API Gateway "adds the configured CORS headers to the response from an integration" -
  and an authorizer rejection never reaches the integration. So a rejected token comes back to
  `fetch()` as an opaque network failure with no readable status, and a page CANNOT distinguish
  an expired session from a dead network by inspecting the response. Do not write a UI that
  depends on seeing that 401. The workable fix is to never send the doomed request: record the
  token's expiry (minus a safety margin) and check it BEFORE the fetch. Keep the `res.status ===
  401` branch anyway - it is correct wherever the response is readable (same-origin, curl, a
  future gateway that does send the headers) and costs one comparison. Doc-verified 2026-08-03,
  NOT verified against a deployed endpoint. This now applies to ONE surface, `PUT /theme` from
  the settings editor - the only gated route left - and it is why `Authorization` has to be in
  `allow_headers`, or the save dies at the OPTIONS and the CORS error masks the whole auth path.
  (It was learned on a pre-launch sign-in gate over `POST /query`, which is now public.)
- **A constant does not belong behind a tool call.** The curated link table took no input and
  returned the same rows every call, and the model kept not calling it - it would answer "where is
  the financial aid office" from retrieved text, feel finished, and never fetch the map. That is
  the measured **Tool-Skip** failure mode (frontier models skip a required call 12-26% of the time
  - [ToolFailBench](https://arxiv.org/html/2607.04686v1)), and tool-description work cannot fix
  it, because a description is only read once the model has already decided to look something up.
  Worse, tool necessity is [linearly decodable from hidden states at AUROC
  0.89-0.96](https://arxiv.org/html/2605.09252v1): the model *knows* and does not act, so more
  instruction is not the missing ingredient, and prompt-only interventions move the whole
  tool-call distribution rather than the boundary. Fix: preload it. Anything small, static and
  usually-relevant belongs in context. Revisit only if such a table outgrows ~30 rows.
- **Moving data out of a tool result silently breaks the eval judge.** The groundedness judge
  grades against `tool_calls`, so anything the model sees that is NOT a tool result appears
  nowhere in that trace - and a correct, curated URL reads as an invented one. That scoring
  regression looks exactly like a quality drop. Any change that relocates evidence must add it to
  the debug payload AND tell the judge prompt it exists, in the same commit.
- **A live third-party API in the agent loop must never be able to kill a request.** The two Primo
  tools hit an undocumented discovery endpoint whose fields use a `$$C..$$V..` / `$$R..$$V..$$M..`
  encoding - parse it defensively, never index blindly, and make a shape change degrade rather
  than throw. Every call is timed out under a total availability budget, and any failure soft-fails
  to a "catalog unavailable" toolResult so the loop still answers. Availability is what the catalog
  SHOWS, not a guarantee a copy is on the shelf, and `total == 0` is the only authoritative
  not-held signal, because Primo relevance is query-relative.
- **Cost scales with LOOP LENGTH, not conversation depth - the opposite of the obvious guess.**
  Measured over 60 live questions (2026-07-29): ~$0.043 a question, **91% of it input tokens**.
  The intuition that client-carried history makes deep conversations expensive is measurably wrong
  here - a prior turn adds only **~54 tokens** to a ~10,700-token call, because `_seed_messages`
  rebuilds history from TEXT turns only and never resends the tool results behind an earlier
  answer, and it stops growing at the `MAX_HISTORY_MESSAGES` trim. What moves cost is whether the
  model needs a second Converse call (measured 1.23 per question), because that resends the entire
  context - worth about fifty turns of history growth in one go. First questions need that second
  call far more often than follow-ups, which drags a naive cost-vs-position fit negative and reads
  as "conversations get cheaper as they go", so divide by `model_calls` before fitting depth. The
  lever on cost is `retrieval.number_of_results` and the priming retrieval, not conversation
  length.
- **Serving Spanish was a FRONTEND problem, not a retrieval one.** Verified by hand against the
  deployed system (2026-07-29): Spanish questions already retrieve correctly from the ENGLISH
  knowledge base and come back grounded and specific, including the authoritative database tool
  ("¿Tienen JSTOR?" -> correctly not held, with alternatives) and the live catalog ("un libro
  sobre la Revolución Mexicana" -> 12 titles). Unaccented input works too. So do NOT translate the
  corpus, re-ingest, or touch chunking and retrieval for a language feature - that is cost and
  risk for a problem that does not exist. The gap was the shell: every piece of chrome was
  hardcoded English, so a Spanish speaker had no signal before typing, and auto-detection cannot
  close that because it needs a message first. Hence a visible affordance rather than a sniffer,
  and a backend change of one optional field and one system block.

**Scraping and change gating**

- **The extracted markdown is byte-stable; the metadata sidecar is not.** Measured 2026-07-29 by
  scraping all 19 seed URLs twice back to back: all 19 `.md` files byte-identical, all 19 sidecars
  different, and `scrape_timestamp` the only differing field. Content hashing works, but only if
  the timestamp is excluded - include it and every page looks changed on every run, the gate
  becomes an expensive no-op, and the KB re-ingests forever. Nothing volatile lives in the
  document body, so nothing had to be removed from the corpus. The catalog object had the same
  problem in a different place: its `generated_at` made it churn every run, which the fingerprint
  gate stops by skipping the write entirely.
- **Change gating silently removes the prune's safety net if you let it.** `prune_stale_objects`
  deletes objects not in the expected set, and that set unions in "what this run wrote" as a guard
  against the slug derivation drifting. The union used to cover the whole corpus for free, because
  every successful page was re-uploaded every run. Once unchanged pages stop uploading, a page can
  be live, correct and outside the union - one slug-scheme change away from deletion. Pages found
  UNCHANGED must count as live too (`live_object_keys`, not `uploaded_object_keys`). Caught by a
  test, not by review.
- **Skipping an ingestion job is only safe if something else remembers.** "Start a job if this run
  uploaded something" plus "skip when a job is already running" loses data: the deferred run's
  change is already in the bucket, so the next run sees the page as unchanged, uploads nothing and
  starts no job either, and the change sits unindexed indefinitely. The store-free fix is to
  compare the bucket's newest `LastModified` against the last ingestion job's `startedAt`, which
  is true exactly when there is unindexed content, whichever run put it there.
- **The offline chunking eval measures boundaries, never ranking.** It can tell you a 300-token
  window cuts 26% of golden answers in half; it cannot tell you whether a 600-token chunk still
  retrieves, because a bigger chunk averages its embedding over more text and can retrieve less
  precisely. Two halves, two instruments: pair it with `eval/retrieval_probe.py` against the live
  index. The 300-token baseline to compare against is recall@1 71%, @3 88%, @5 94%, @8 100%.
