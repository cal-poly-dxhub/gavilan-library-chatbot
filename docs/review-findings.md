# Full-Stack Adversarial Review Findings

**Reviewer:** Claude (Fable 5), audit-only pass. **Date:** 2026-07-02.
**Scope:** whole-system review per the audit brief, prioritized: (1) infra/ CDK, (2) cross-component consistency, (3) app/handler.py, (4) eval/ + frontend/ skim.
**Ground rules honored:** no code changed; known items in `docs/blocked-on-aws.md` are not re-flagged, only added to where there is something new.

Each finding: severity, location, what's wrong, why it matters, proposed fix (described, not applied), and whether it is verifiable offline or only at deploy/runtime.

---

## Area 1 - infra/ CDK stack

### 1.1 HIGH - Guardrail config changes silently never reach runtime (stale pinned version)

- **Location:** `infra/infra/infra_stack.py:381-386` (`ContentGuardrailVersion`), `:447` (`GUARDRAIL_VERSION` env), `config.yaml` guardrail section.
- **What's wrong:** `CfnGuardrailVersion` has only two properties: `guardrail_identifier` (stable) and `description` (a fixed literal). When guardrail settings in `config.yaml` change (filter strengths, PII actions, blocked messages), the `CfnGuardrail` resource updates its DRAFT, but the `CfnGuardrailVersion` resource sees no property change, so CloudFormation does nothing: no new version is published, and the Lambda's `GUARDRAIL_VERSION` env keeps pointing at the original version (1) with the *old* policy.
- **Why it matters:** `config.yaml` is declared the single source of truth ("Tune strengths/actions/messages here, not in code"), and the whole eval-driven-iteration workflow assumes edit-config-and-redeploy works. In reality every guardrail tuning deploy after the first is a silent no-op at runtime. Nothing fails; tests stay green; the deployed bot just runs stale guardrail policy indefinitely. This goes beyond the known "version pinning" note in `blocked-on-aws.md`, which covers console/live edits - the *declared config path itself* is broken for updates.
- **Proposed fix:** make the version resource content-addressed: compute a stable hash of the resolved guardrail config dict at synth and put it in the `CfnGuardrailVersion.description` (e.g. `f"config-{sha256(...)[:12]}"`). Any guardrail config change then replaces the version resource (publishing a new numbered version) and flows into the Lambda env automatically. Alternatively point the Lambda at `DRAFT` and accept mutability.
- **Verifiability:** reasoning is fully offline (CloudFormation update semantics); the symptom would only be observed at the second deploy.

### 1.2 MEDIUM - Generation-model ARN and IAM grant assume a bare on-demand foundation-model id; modern Claude models need an inference profile

- **Location:** `infra/infra/infra_stack.py:390-393` (`generation_model_arn`), `:417-422` (InvokeModel grant), `config.yaml` `generation.model_id`.
- **What's wrong:** the stack builds `arn:{partition}:bedrock:{region}::foundation-model/{model_id}` and grants `bedrock:InvokeModel` on exactly that ARN. That shape only works for models invokable on-demand by bare model id. The placeholder (`anthropic.claude-3-5-haiku-20241022-v1:0`) and effectively every newer Claude model are invoked through cross-region inference profiles (`us.anthropic...`), which (a) have a *different, account-scoped* ARN format (`arn:aws:bedrock:{region}:{account}:inference-profile/...`) that this template mangles, and (b) require `bedrock:InvokeModel` on the profile ARN *and* the underlying regional foundation-model ARNs.
- **Why it matters:** `blocked-on-aws.md` records "pick the real generation model" as a config-value swap. It is not: dropping a profile id into `config.yaml` produces a malformed policy ARN at synth-or-deploy, and even a valid on-demand id may return runtime `ValidationException` ("with on-demand throughput isn't supported") in the chosen region. The fix touches stack code, not just config.
- **Proposed fix:** branch the ARN construction on the id form (profile prefix like `us.` / `eu.` vs bare id); for profiles grant InvokeModel on the profile ARN plus the wildcard-region foundation-model ARNs for that model (`arn:{partition}:bedrock:*::foundation-model/anthropic.<model>`). Note the same applies to `Retrieve`'s embedding model only if it is ever swapped to a profile (Titan on-demand is fine today).
- **Verifiability:** the ARN-shape defect is verifiable offline; the on-demand-vs-profile requirement for the finally chosen model is deploy-time.

### 1.3 HIGH - Whole-chain timeout budget is incoherent; the most common first query can systematically fail

- **Location:** `infra/infra/infra_stack.py:438` (Lambda `timeout=30s`), `frontend/widget.js:41` (`requestTimeoutMs: 20000`), plus the HTTP API's hard 30s integration cap; context in `docs/architecture.md` ("OSS NextGen ... scale-to-zero after 10min idle, cold start ~10-30s").
- **What's wrong:** the stack was deliberately designed around OSS NextGen scale-to-zero, whose own recorded cold-start is 10-30 seconds. A low-traffic library bot idles past 10 minutes constantly, so a large share of *first* queries pay that cold start inside `retrieve()`, then still need Converse generation (~2-8s). The budget chain is: widget aborts at **20s** < Lambda timeout **30s** = API Gateway's non-configurable **30s** integration cap < worst-case backend work (**~15-38s**). Three independently reasonable numbers that never got checked against each other or against the chosen vector store's documented latency profile.
- **Why it matters:** the very first impression for a typical visitor (fresh session after idle) is the widget's generic "couldn't reach the library assistant" error, with the successful answer arriving after the browser has already aborted. This is exactly the class of issue an incremental build misses: each piece is fine in isolation.
- **Proposed fix:** (a) raise the widget timeout to at least ~35s so the browser outlives the backend rather than the reverse; (b) accept that >30s requests die at the API Gateway cap and decide a mitigation consciously - e.g. a scheduled keep-warm ping that runs a tiny Retrieve to keep the collection resident, or an explicit "first answer may take up to 30s" typing state; (c) surface `requestTimeoutMs` as a documented knob. Measure real cold-start at deploy before over-engineering.
- **Verifiability:** the arithmetic mismatch is offline; actual cold-start magnitude is deploy/runtime.

### 1.4 LOW - The planned deploy-role data-access fix must also cover index *deletion*

- **Location:** `infra/infra/infra_stack.py:177-213` (data access policy); adds to the first two items in `blocked-on-aws.md`.
- **What's wrong / add:** the known landmine says the CDK/CloudFormation execution role must be added as a principal with index-*creation* rights. `CfnIndex` is also deleted and updated through the OSS data plane by that same role, so the eventual grant needs `aoss:DeleteIndex` (and `aoss:UpdateIndex`/`aoss:DescribeIndex`) too, or the first `cdk destroy` (and any index-replacing update) fails the same way the first deploy would have.
- **Why it matters:** `cdk destroy` is advertised in CLAUDE.md as the clean teardown path; a delete-time auth failure leaves the stack in `DELETE_FAILED` with a half-orphaned collection.
- **Proposed fix:** when adding the deploy role to the data access policy, grant the full index lifecycle set, not just `CreateIndex`.
- **Verifiability:** offline reasoning; symptom surfaces at first destroy.

### 1.5 LOW - No throttling exists on the HTTP API yet, and nothing marks it as launch-blocking

- **Location:** `infra/infra/infra_stack.py:457-474`; `docs/build-plan.md` (unchecked TODO).
- **What's wrong / add:** this is a known TODO, so only an add: the architecture explicitly rejected WAF *because* "API Gateway throttling handles the real cost-abuse risk", i.e. throttling is the load-bearing protection for an unauthenticated endpoint that fans out to paid Bedrock calls. Today the default stage has no `throttle` settings at all (account defaults, ~10k rps, protect nothing cost-wise). The gap between "our documented protection" and "what is synthesized" is currently unbounded Bedrock spend behind a public URL.
- **Proposed fix:** set default-stage route throttling now (it is a one-liner on the default stage: `default_stage.default_route_settings` / `CfnStage.RouteSettingsProperty`, e.g. rate 5 rps / burst 10 for a library widget), driven from `config.yaml`, so the protection ships with the first deploy instead of being remembered later.
- **Verifiability:** offline.

### 1.6 LOW - Query Lambda's log group is implicit: infinite retention, orphaned on destroy

- **Location:** `infra/infra/infra_stack.py:431-450`.
- **What's wrong:** no `log_group`/`log_retention` is configured, so the log group is auto-created on first invoke with never-expire retention and is not part of the stack (survives `cdk destroy`).
- **Why it matters:** contradicts the one-click install/uninstall goal (teardown leaves resources behind) and accumulates log storage cost forever; also the guardrail-assessment logs (see finding 3.3) then persist indefinitely.
- **Proposed fix:** create an explicit `logs.LogGroup` with a retention (e.g. 1-3 months) and `RemovalPolicy.DESTROY`, passed to the Function via `log_group`.
- **Verifiability:** offline.

### 1.7 NIT - Network policy publicly exposes the OpenSearch *dashboard* for no stated reason

- **Location:** `infra/infra/infra_stack.py:109-131`.
- **What's wrong:** the network policy allows public access to both the collection endpoint and the `dashboard` resource. Nothing in the system uses OpenSearch Dashboards; the KB talks to the collection API only.
- **Why it matters:** marginal attack-surface widening (still IAM/data-access-policy gated), and it misstates intent to a future reader/security reviewer.
- **Proposed fix:** drop the `dashboard` rule; keep collection-only public access, or add a comment justifying dashboard access if it is wanted for debugging.
- **Verifiability:** offline.

---

## Area 2 - Cross-component consistency (whole-system)

### 2.1 CRITICAL - The guardrail's PII ANONYMIZE config directly defeats the bot's core answers and its handoff flow

- **Location:** `config.yaml:99-101` (ANONYMIZE: EMAIL, PHONE, NAME, ADDRESS, USERNAME, AGE) vs `app/system_prompt.md` (`<handoff>`: "point them to a librarian... point them there"; `<scope>`: hours, **locations**, services) vs `app/handler.py:180` (retrieved context is embedded in the *user message*).
- **What's wrong:** Converse guardrail input processing evaluates and transforms the entire user-role message. In this design the user message is mostly the `<context>` block of crawled library pages - which is precisely where librarian names, reference-desk emails, phone numbers, and the library's street address live. With ANONYMIZE on input, the model receives `{NAME}`, `{EMAIL}`, `{PHONE}`, `{ADDRESS}` placeholders *instead of the retrieved facts*, so it cannot answer "how do I contact a librarian?", "what's the library's phone number?", "where is the library?" even when retrieval succeeded. With ANONYMIZE also on output, anything that survives is masked again in the answer. `AGE` anonymization similarly corrupts policy answers (e.g. borrower age rules).
- **Why it matters:** these are not edge cases; contact-a-librarian is the system prompt's designated fallback for every out-of-scope and not-in-context situation, and hours/location/contact are the bot's headline use cases. The failure is silent and total: HTTP 200, well-formed answer, green tests, wrong product. Guardrails (built in one session) and the system prompt/handler (built in others) were never checked against each other - the exact whole-system gap this audit was asked to find.
- **Proposed fix:** the pinned `aws-cdk-lib==2.260.0` `CfnGuardrail.PiiEntityConfigProperty` supports `input_action`/`output_action`/`input_enabled`/`output_enabled` (verified against the installed package). Recommended: for the contact-shaped entities (EMAIL, PHONE, NAME, ADDRESS, AGE) disable output anonymization and disable input anonymization (or leave input ANONYMIZE only if the retrieval leg is restructured so context is not in the guarded message - see 2.2/2.3). Keep BLOCK entities (SSN, cards, passwords) as-is; those should never appear on library pages. Re-derive the config.yaml schema (`action` -> per-direction actions) and the stack's flattening loop accordingly. At minimum: drop ADDRESS/NAME/PHONE/EMAIL from ANONYMIZE entirely and rely on the prompt, accepting that user-supplied contact PII passes through.
- **Verifiability:** the contract collision is verifiable offline (config vs prompt vs message structure); exact masking behavior confirms at deploy. Note for the eval phase: the planned Q&A dataset should include "how do I contact a librarian / where is the library" cases so this class of regression is caught by eval, not by students.

### 2.2 HIGH - PROMPT_ATTACK at HIGH evaluates the retrieved web content, not just the user's question

- **Location:** `config.yaml:93-95` vs `app/handler.py:174-191` (whole `<context>` + question sent as one untagged user message) vs the guardContent decision in `docs/architecture.md` decision 5 / CLAUDE.md.
- **What's wrong:** without `guardContent` input tagging, guardrail *input* filters evaluate the full user message, ~95% of which is arbitrary crawled web text. The prompt-attack filter is specifically documented by AWS to be used with input tagging in RAG shapes for this reason: instruction-like or imperative crawled content ("ignore the steps above and...", login-page copy, embedded forms) can false-positive at HIGH strength and hard-block a completely innocent question. The project's decision to avoid `guardContent` conflated two different mechanisms: the *contextual-grounding* guardContent trap (real, correctly avoided) and *input-scope tagging* for filters (independent of grounding, and the standard mitigation here).
- **Why it matters:** sporadic, content-dependent hard blocks of legitimate queries that are near-impossible to debug from the outside (the student just gets the blocked-input message), and whose frequency changes every crawl. It also weakens the actual protection: a prompt injection *hosted on a crawled page* is scanned (good) but the filter's signal is diluted across huge benign context (and vice versa).
- **Proposed fix:** two coherent options, pick one deliberately: (a) tag only the student's question as guarded input (`{"guardContent": {"text": {"text": query, "qualifiers": ["guard_content"]}}}` as a separate content block), leaving grounding off - this scopes input filters/PII to the user's words and would ALSO fix the input half of 2.1; verify against current Converse semantics at deploy since the "once anything is tagged, only tagged is evaluated" behavior is exactly what's being relied on. Or (b) keep the untagged shape but drop PROMPT_ATTACK to LOW/NONE and lean on `<fixed_rules>` in the system prompt. Option (a) is the one AWS designed for this case.
- **Verifiability:** structural risk is offline; actual false-positive rate is deploy/runtime (and should be watched via the existing `guardrail_assessment` logs).

### 2.3 MEDIUM - Retrieval runs on the raw, unscreened query; blocked inputs still cost money and leak PII into the retrieval path

- **Location:** `app/handler.py:242-243` (`retrieve()` before `generate()`); `docs/architecture.md` data-flow ("Guardrail ... screens input before generation").
- **What's wrong:** the guardrail is attached only to the Converse call, so by the time an input is blocked (prompt attack, credential/financial PII), the raw query has already been embedded by Titan and run through OpenSearch. A query containing an SSN or password is BLOCKed from generation but was still transmitted to and processed by two other services; a prompt-attack query still triggers paid retrieval.
- **Why it matters:** (a) the PII BLOCK policy's intent ("never process this") is only half-enforced; (b) every blocked request still bills an embedding + OSS query + (on cold start) an OCU spin-up, which matters for an abuse scenario the throttling story (1.5) is supposed to bound.
- **Proposed fix:** call the standalone `ApplyGuardrail` API (source=INPUT) on the bare query *before* `retrieve()`; on intervention, return the blocked message immediately without retrieval or generation. The Lambda role already has `bedrock:ApplyGuardrail` on the guardrail ARN, so no IAM change. Keep the Converse-attached guardrail for output. This also naturally gives the clean input scoping wanted in 2.1/2.2 (guardrail sees only the user's words on input).
- **Verifiability:** offline (design); behavior at deploy.

### 2.4 MEDIUM - The /query response contract cannot feed the R&G eval harness that was built against it

- **Location:** `eval/format_generate.py:62-67` (`CapturedOutput.passages` = full retrieved passages) and `eval/capture_outputs.py` (capture plan: POST /query, map response) vs `app/handler.py:200-210` (`sources`: deduped by URI, truncated to 300 chars, unsourced passages dropped, `[]` on guardrail block).
- **What's wrong:** the answer-quality eval scores Faithfulness (and optionally citation metrics) against the passages the bot retrieved. The only passage-shaped thing `/query` exposes is `sources[].excerpt`, which is (a) truncated to 300 chars, (b) deduplicated per URI (multiple chunks from one page collapse to one excerpt), and (c) missing entirely for passages without a resolvable URI - which the handler explicitly says "still inform the answer". So the eval would judge groundedness against a strict subset of what the model actually saw, systematically depressing/mis-scoring Faithfulness. Additionally, the `capture_outputs.py` docstring is stale ("today's handler returns only a placeholder answer and does not yet expose retrieved passages") - the handler is finished and returns the real contract, so the stub's guidance misdescribes the system it will be implemented against.
- **Why it matters:** the eval harness is the project's stated iteration engine (chunking, search type, prompt tuning are all "eval-driven"). If its groundedness signal is computed on truncated context, tuning decisions get made on noise. This is a contract gap between two components built in different sessions, invisible to both components' unit tests.
- **Proposed fix (decide, don't drift):** either (a) have `/query` optionally return full passages - e.g. an `include_passages` request flag or debug header the capture stage sets, returning the un-truncated, un-deduped `chunks` list alongside the public `sources`; or (b) document that the R&G eval scores answer-vs-excerpt and accept the weaker Faithfulness signal; or (c) have the capture stage call KB Retrieve directly for passages (weakens the "score OUR bot's real path" premise; least preferred). Update the `capture_outputs.py` docstring either way.
- **Verifiability:** fully offline (both contracts are in the repo).

### 2.5 LOW - Documentation/config drift: claimed knobs that don't exist, duplicated values with no linkage

- **Location:** CLAUDE.md ("`config.yaml` - model IDs, chunking, **reranking, thresholds** live here") vs `config.yaml` (no reranking or threshold keys exist); `eval/eval_config.yaml:36` duplicates `number_of_results: 5` independently of root `config.yaml` `retrieval.number_of_results`.
- **What's wrong:** CLAUDE.md promises config keys that were never created (a reader tuning reranking will hunt for a knob that isn't there); the eval harness's retrieve settings can silently diverge from the deployed bot's (evaluating a different `numberOfResults` than production uses would invalidate the comparison the eval exists to make).
- **Proposed fix:** fix the CLAUDE.md sentence to match reality (or add the keys when the features land); add a comment in `eval_config.yaml` stating that `retrieve.number_of_results` must mirror root `config.yaml` when evaluating the production configuration (deliberate divergence is fine for A/B runs, but should be a choice).
- **Verifiability:** offline.

### 2.6 LOW - No input-size limit anywhere in the chain

- **Location:** `frontend/widget.js:363-367` (textarea, no maxlength), `app/handler.py:213-226` (no length check), stack (no body-size constraint beyond API GW's 10MB default).
- **What's wrong:** a single request can carry a ~megabytes-long "query" that is embedded by Titan, run through OSS, and pasted into a Claude prompt. Rate throttling (1.5) bounds requests/second but not tokens/request.
- **Why it matters:** trivially scriptable cost amplification on a public endpoint, plus organic failures (huge pastes -> model token-limit ValidationException -> unhandled 500, see 3.1).
- **Proposed fix:** cap in both places: `maxlength` (~1000 chars) on the widget textarea for UX, and a server-side check in `_extract_query`/handler returning 400 for queries over a config-driven limit (the widget cap is advisory only; the Lambda cap is the real control).
- **Verifiability:** offline.

---

## Area 3 - app/handler.py logic

### 3.1 LOW - No exception handling around retrieve/generate; every AWS-side failure is an opaque 500

- **Location:** `app/handler.py:237-247` (`lambda_handler`).
- **What's wrong:** `retrieve()` and `generate()` are called bare. Any `ThrottlingException`, `ValidationException` (wrong model id / token overflow), OSS timeout, or transient fault propagates out of the handler; API Gateway converts it to a generic 500. There is no structured error log (the traceback lands in CloudWatch, but without the query or a request-shaped event record like the guardrail assessment gets), no differentiated status, and no student-friendly body.
- **Why it matters:** functionally survivable - the widget's `!res.ok` path shows a retryable error bubble - but operationally blind: the first deploy's likely failure modes (model access not enabled, throttle, cold-start timeout per 1.3) will all present identically as bare 500s, making the deploy-time landmine list harder to work through. Deliberately LOW, not MEDIUM, because the user-facing behavior is already handled by the widget.
- **Proposed fix:** wrap the two calls in a try/except that logs a structured `{"event": "query_failed", "error": ..., "stage": "retrieve"|"generate"}` record and returns a 502/503-style JSON error consistent with the contract. Keep it thin; do not add retry logic in v1.
- **Verifiability:** offline.

### 3.2 LOW - Non-string `query` values pass validation and crash downstream

- **Location:** `app/handler.py:213-226` (`_extract_query`).
- **What's wrong:** `data.get("query") or data.get("question")` accepts any truthy JSON value; `{"query": 123}` or `{"query": {"a": 1}}` returns a non-string that survives the `if not query` check and then blows up inside boto3 parameter validation (an unhandled 500 per 3.1) instead of the intended 400.
- **Why it matters:** a public endpoint should 400 malformed input, not 500; noise in error metrics; trivially triggered.
- **Proposed fix:** validate `isinstance(value, str)` and strip before returning; return None otherwise (yields the existing 400).
- **Verifiability:** offline.

### 3.3 LOW - The full guardrail trace is logged to CloudWatch, which can put the very PII the guardrail masks into plaintext logs

- **Location:** `app/handler.py:157-171` (`_log_guardrail_assessment` prints `trace.guardrail` verbatim), `config.yaml:74` (`trace: enabled`).
- **What's wrong:** the guardrail assessment trace can include the matched content for filter/PII hits. Logging it wholesale means a student's email/phone/SSN attempt - which the guardrail carefully anonymized or blocked in the response - is preserved in CloudWatch logs (which, per the project's own verified notes, PII masking does not cover), with infinite retention today (1.6).
- **Why it matters:** undermines the privacy posture the guardrail exists to provide; logs become the sensitive store.
- **Proposed fix:** log a reduced assessment: the policy/entity *types* and actions triggered, counts, and `stopReason` - not raw matched text. That preserves the tuning signal the log exists for. Pair with a log-retention setting (1.6).
- **Verifiability:** offline design concern; exact trace payload shape confirms at deploy.

### 3.4 LOW - Converse is called with no inferenceConfig; generation knobs are neither set nor configurable

- **Location:** `app/handler.py:182-186`; `config.yaml` `generation` section (only `model_id`).
- **What's wrong:** no `maxTokens`/`temperature`/`stopSequences` are passed, so the deployed behavior rides on model-specific Converse defaults, and the "single source of truth for changeable knobs" config has nothing to tune for generation despite eval-driven prompt/behavior iteration being the plan.
- **Why it matters:** default `maxTokens` differs per model (and can be either wastefully high or clipping-low); temperature defaults are not chosen for a factual FAQ bot; when eval iteration starts there is no lever without a code change - the exact situation config.yaml exists to prevent.
- **Proposed fix:** add `generation.max_tokens` (e.g. 500-700 for "short, direct answers") and `generation.temperature` (low, e.g. 0-0.3) to config.yaml, pass through the stack as env vars, and set `inferenceConfig` on the Converse call.
- **Verifiability:** offline.

### 3.5 Notes - things checked and found correct (no finding)

- `<context>` tag contract: `CONTEXT_TAG` matches `system_prompt.md` exactly; empty-retrieval placeholder text is consistent with the prompt's "context does not contain the answer" branch.
- Source extraction (`webLocation.url` -> metadata fallback), dedup-by-URI ordering, excerpt truncation, blocked-response `sources: []`, base64 body handling, and 2.0-payload parsing all match the documented contracts and the stack's env wiring (`KNOWLEDGE_BASE_ID`, `GENERATION_MODEL_ID`, `NUMBER_OF_RESULTS`, guardrail id/version/trace).
- Guardrail attach logic degrades correctly when env is unset; blocked detection via `stopReason == "guardrail_intervened"` is right for both input and output blocks.

---

## Area 4 - eval/ and frontend/ (skim)

### 4.1 LOW - `format_generate.py` points at a "NAMING CAVEAT in the module docstring" that does not exist

- **Location:** `eval/format_generate.py:35-36` vs the module docstring (lines 1-8).
- **What's wrong:** `RETRIEVED_PASSAGES_KEY = "retrievedPassages"  # See NAMING CAVEAT in the module docstring` - but the docstring contains no caveat; it was evidently trimmed in some edit. The caveat is load-bearing: it is the *unresolved* `retrievedPassages` vs `retrievedResults` question that `blocked-on-aws.md` says must be confirmed at the first real R&G run, and the docstring was where the details lived.
- **Why it matters:** the person doing the first R&G run months from now follows the comment to a docstring that says nothing, and the outer-key/inner-key subtlety (the code currently nests `{"retrievedPassages": {"retrievalResults": [...]}}` - *both* candidate names, in different positions) is exactly the kind of thing that needs the original reasoning written down.
- **Proposed fix:** restore the caveat text to the module docstring (what is unresolved, the two candidate keys, where each was seen, what to confirm at first run).
- **Verifiability:** offline.

### 4.2 NIT - Widget's fallback script lookup can bind to a foreign script tag

- **Location:** `frontend/widget.js:56-64` (`apiUrl()` falls back to `document.querySelector("script[data-api-url]")`).
- **What's wrong:** if `document.currentScript` was unavailable at load, the fallback grabs the *first* `script[data-api-url]` on the host page - which on a page embedding two widgets (or any other tool using the same attribute convention) may not be this widget's tag.
- **Why it matters:** worst case the widget POSTs student questions to another embed's endpoint. Extremely unlikely on the library's page; flagged only because it is a one-line tighten.
- **Proposed fix:** scope the fallback selector to this script's own src (e.g. `script[data-api-url][src*="widget.js"]`) or drop the fallback (with `defer`, `currentScript` is reliably set during initial evaluation).
- **Verifiability:** offline.

### 4.3 Notes - checked and found correct (no finding)

- **frontend/widget.js:** POST body `{query}` matches the handler; response normalization matches the contract; all rendering is `textContent` (no innerHTML anywhere); `safeHttpUrl` blocks `javascript:`/`data:` hrefs; `rel="noopener noreferrer nofollow"` on source links; Shadow DOM + `:host { all: initial }` + re-neutralized inherited properties is sound. The 20s timeout issue is filed as 1.3, the missing maxlength as 2.6.
- **eval/:** runner request shape matches the project's verified `create_evaluation_job` notes (roleArn/applicationType/inferenceConfig/outputDataConfig/evaluationConfig, `precomputedRagSourceConfig` for BYOI, `referenceResponses` not `referenceContexts`); retrieve-only vs R&G formatter split is clean; 1000-line dataset caps and single-turn assertions are enforced; `capture_outputs` correctly refuses to fake data. The contract gap with `/query` is filed as 2.4.

---

## Summary

**Counts by severity:**

| Severity | Count | Findings |
|---|---|---|
| Critical | 1 | 2.1 |
| High | 3 | 1.1, 1.3, 2.2 |
| Medium | 4 | 1.2, 2.3, 2.4 |
| Low | 11 | 1.4, 1.5, 1.6, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 4.1 |
| Nit | 2 | 1.7, 4.2 |

(Total: 17 findings; medium count is 3 - 1.2, 2.3, 2.4.)

**Top 3 most important:**

1. **2.1 (Critical) - PII ANONYMIZE vs the bot's core job.** The guardrail masks librarian names, emails, phone numbers, and the library's address in both the retrieved context (input) and the answer (output). "Contact a librarian" is the system prompt's universal fallback and hours/location/contact are headline use cases, so the deployed bot would silently fail its primary purpose while returning well-formed 200s. Fix is concrete and offline-verifiable: per-direction PII actions (`input_action`/`output_action`) exist in the pinned CDK.
2. **1.1 (High) - guardrail tuning deploys are silent no-ops.** `CfnGuardrailVersion` never republishes, so every config.yaml guardrail edit after the first deploy changes DRAFT only while the Lambda stays pinned to the stale version. Breaks the config-as-single-source-of-truth contract invisibly. Fix: content-hash the guardrail config into the version resource's description to force republication.
3. **1.3 (High) - the timeout chain guarantees bad first impressions.** OSS scale-to-zero cold start (the project's own docs: 10-30s) + generation exceeds the widget's 20s abort and can exceed the 30s Lambda/API Gateway ceiling, so the first query of a typical visitor session errors out. Needs a deliberate latency budget (widget > backend) plus a keep-warm or UX decision.

The common thread in the serious findings: each subsystem is individually correct and well-tested, but three pairs of components (guardrail <-> prompt/message-structure, guardrail-version <-> config workflow, vector-store latency <-> timeout settings, plus eval <-> API contract at 2.4) were never checked against each other - the whole-system seams this audit was scoped to examine.
