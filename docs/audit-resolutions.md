# Audit Findings — Resolutions

Tracks agreed fixes for `docs/review-findings.md`. Same numbering. One-line solutions.

**Status key:** RESOLVED (fix decided) / OPEN (not yet discussed) / DEFERRED (later) / DISMISSED (not a real issue)

---

## Area 1 — infra/ CDK stack

- [x] **1.1** HIGH — guardrail version never republishes on config change — **RESOLVED**
  - Fix: content-hash the resolved guardrail config into `CfnGuardrailVersion.description` (`sha256(json.dumps(config, sort_keys=True))[:12]`), so any config change forces CloudFormation to publish a new immutable numbered version. Both the ApplyGuardrail input call and the Converse output call reference that version. Chose hashing over DRAFT: DRAFT is a single mutable pointer with no reproducibility, no rollback, and silent drift (any edit hits runtime immediately); hashing gives the same edit-config-and-redeploy flow while keeping versions immutable and auditable.
- [x] **1.2** MEDIUM — model ARN assumes bare on-demand id; profiles need different ARN + grant — **RESOLVED**
  - Fix: `config.yaml` model reference becomes a `us.`-prefixed cross-region inference profile ID (geographic, not `global.`); handler passes it to Converse. CDK account/region auto-resolve via `Stack.account`/`Stack.region` tokens (one-click safe, no pre-known values). Hand-roll the IAM in L1 (stay L1-consistent, avoid the deprecated construct's `grantProfileUsage`): stmt 1 grants `InvokeModel*` on [inference-profile ARN (account+region scoped), source-region + destination-region foundation-model ARNs]; stmt 2 grants `GetInferenceProfile`/`ListInferenceProfiles`. Deploy prerequisite: Gavilan enables Claude model access in their Bedrock console.
- [x] **1.3** HIGH — timeout chain incoherent (cold start > widget/API caps) — **RESOLVED**
  - Fix: raise widget timeout to ~30s (API Gateway hard ceiling); add honest loading state; warm-on-page-load via a lightweight `/warm` route (retrieve only, no generation) fired when the widget loads. Optional later: scheduled peak-hours ping if page-load warming proves insufficient.
- [x] **1.4** LOW — deploy-role data-access fix must also cover index delete/update — **RESOLVED**
  - Fix: grant full index lifecycle (create/update/delete/describe) to the deploy role in the data-access policy, not just create, so `cdk destroy` and index-replacing updates don't fail. Folds into the 1.4 deploy landmine.
- [x] **1.5** LOW — no HTTP API throttling yet (the load-bearing cost-abuse protection) — **RESOLVED**
  - Fix: set `default_route_settings` (throttling_rate_limit + throttling_burst_limit) on the HTTP API stage via `CfnStage.RouteSettingsProperty`, values from config.yaml (~5-10 rps, burst ~10-20). Widget handles 429 gracefully. Pairs with 2.6 (input-size) as the full cost-abuse defense.
- [x] **1.6** LOW — query Lambda log group implicit (infinite retention, orphaned on destroy) — **RESOLVED**
  - Fix: explicit `logs.LogGroup` with retention (1-3 months) + `RemovalPolicy.DESTROY`, passed to the Function.
- [x] **1.7** NIT — network policy needlessly exposes OpenSearch dashboard — **RESOLVED**
  - Fix: drop the `dashboard` rule, collection-only public access.

## Area 2 — Cross-component consistency

- [x] **2.1** CRITICAL — PII anonymize corrupts retrieved context (masks library phone/address) — **RESOLVED**
  - Fix: input guardrail (PII + content + prompt-attack) runs via `ApplyGuardrail` on the bare query BEFORE retrieval; Converse-attached guardrail is OUTPUT-only and content-filters-only. Retrieved context never screened (it's public library info). Input PII = anonymize (not block). This one change also resolves 2.2 and 2.3.
- [x] **2.4** MEDIUM — /query contract can't feed R&G eval (sources truncated/deduped) — **RESOLVED**
  - Fix: add optional `include_full_context` flag to `/query` that returns the full untruncated, un-deduped retrieved passages (what the model actually saw) alongside the public `{answer, sources[]}`. Eval capture sets it; widget never does. Faithfulness scores against those. Keep excerpts in widget sources. Update `capture_outputs.py` docstring (handler is no longer a placeholder).
- [x] **2.5** LOW — doc/config drift (claimed reranking/threshold keys don't exist) — **RESOLVED**
  - Fix: correct the CLAUDE.md sentence to match actual config keys; add a comment in `eval_config.yaml` that `number_of_results` must mirror root config when evaluating production.
- [x] **2.6** LOW — no input-size limit anywhere in the chain — **RESOLVED**
  - Fix: widget `maxlength` ~1000 (advisory UX) + server-side length check in handler returning 400 over a config limit (~2000-4000 chars). Platform limits (API GW 10MB / Lambda 6MB) are far too high to protect; the app-level check is the real control. Pairs with 1.5.

## Area 3 — app/handler.py logic

- [x] **3.1** LOW — no exception handling; AWS failures are opaque 500s — **RESOLVED**
  - Fix: try/except around retrieve/generate, log structured `{event, error, stage}`, return a clean JSON error. No retry logic v1.
- [x] **3.2** LOW — non-string `query` passes validation, crashes downstream — **RESOLVED**
  - Fix: `isinstance(str)` check + strip in `_extract_query`, else return None (yields existing 400).
- [x] **3.3** LOW — full guardrail trace logged (puts masked PII into plaintext logs) — **RESOLVED**
  - Fix: log reduced assessment (policy/entity types + actions + counts + stopReason), not raw matched text. Pairs with 1.6 retention.
- [x] **3.4** LOW — Converse has no inferenceConfig (no maxTokens/temperature knobs) — **RESOLVED**
  - Fix: add `generation.max_tokens` (~500-700) and `generation.temperature` (low, 0-0.3) to config, pass through, set `inferenceConfig` on Converse.

## Area 4 — eval/ and frontend/

- [x] **4.1** LOW — `format_generate.py` references a docstring caveat that doesn't exist — **RESOLVED**
  - Fix: restore the caveat text (unresolved `retrievedPassages` vs `retrievedResults`, both candidate keys, what to confirm at first run) to the module docstring.
- [x] **4.2** NIT — widget fallback script lookup can bind to a foreign script tag — **RESOLVED**
  - Fix: scope fallback selector to `script[data-api-url][src*="widget.js"]`, or drop the fallback (`defer` makes `currentScript` reliable).