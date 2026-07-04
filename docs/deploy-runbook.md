# Deploy Runbook — Gavilan Library Chatbot

Step-by-step sequence for deploy day.

Commands run from `infra/` with the venv active unless noted (`CLAUDE.md` > Commands).

---

## Phase 1 — Before stand-up (prerequisites)

Everything here is done once, before the first deploy. Each item has a verify step.

### 1.1 Pick the region (all three services in ONE region)
- **Do:** Choose one region where **Bedrock Managed KB + Web Crawler connector + OpenSearch Serverless NextGen** are ALL available. Do not default to `us-west-2` without checking.
- **Verify:** Confirm all three in that region against current AWS availability before provisioning. Web Crawler is **Preview** and OSS-locked, so it is the gating service.
- **Note:** `config.yaml` does not pin a region; the stack resolves `Stack.region` at deploy, and the Lambda reads `BEDROCK_REGION` / `AWS_REGION`. `eval/eval_config.yaml` currently hardcodes `region: us-west-2` (placeholder) — update it to match once chosen.
- **Source:** `blocked-on-aws.md` (Config/values), `architecture.md` Open decisions #1, `architecture.md` Verified (Web Crawler "Preview. OSS-only").

### 1.2 CDK bootstrap with `aoss:` permissions on the cfn-exec role
- **Do:** Bootstrap the account/region. The CloudFormation execution role must be able to touch the OSS **data plane** (`aoss:`) at deploy time to create the vector index. The default bootstrap role carries `es:*`, NOT `aoss:*`.
- **Verify:** Confirm the cfn-exec role's attached policy includes `aoss:` actions (index create/update/delete/describe on the data plane). If you bootstrap with the default `AdministratorAccess` execution policy, `aoss:*` is covered by `*`. If you use a scoped custom bootstrap policy, `aoss:` must be added explicitly.
- **Fallback if wrong:** Re-bootstrap with a custom policy, e.g. `cdk bootstrap --cloudformation-execution-policies <policy-arns>`. **[VERIFY EXACT SYNTAX]** — confirm the exact flag and the policy ARN list against the installed CDK CLI (`2.1129.0`) before running.
- **Source:** `blocked-on-aws.md` (CDK execution role lacks `aoss:`), `CLAUDE.md` Lessons (deploy-time gaps: "cfn execution role needs `aoss:` permissions... default bootstrap has `es:*`, not `aoss:*`").
- **Cross-ref:** This is the IAM half of the index-creation blocker. The data-access-policy half is fixed in stack code (see 2.1 below).

### 1.3 Gavilan enables Claude model access in Bedrock
- **Do:** Gavilan enables access to the generation model and the evaluator model on the Bedrock **Model access** page, in the deploy region.
  - Generation model: `us.anthropic.claude-3-5-haiku-20241022-v1:0` (`config.yaml` > `generation.model_id`) — a `us.`-prefixed cross-region inference profile.
  - Evaluator model (for eval, Phase 3): `us.anthropic.claude-3-5-sonnet-20240620-v1:0` (`eval/eval_config.yaml`, placeholder).
- **Verify:** Both show "Access granted" in the Bedrock console for the region. The inference profile cannot invoke without the underlying foundation model access enabled.
- **Note:** A cross-region profile routes across multiple regions; the stack grants `InvokeModel*` on the profile ARN **plus** foundation-model ARNs across routed regions (audit 1.2). Model access must be enabled in those routed regions, not only the home region.
- **Source:** `audit-resolutions.md` 1.2 ("Deploy prerequisite: Gavilan enables Claude model access"), `CLAUDE.md` Lessons (us.-prefixed inference profile), `blocked-on-aws.md` (Eval: evaluator model enabled).

### 1.4 Confirm the generation model id is final (not placeholder)
- **Do:** Confirm `config.yaml` > `generation.model_id` is the intended production model. It is currently Haiku 3.5, previously marked TBD.
- **Verify:** Value is deliberate and access is granted (1.3). Confirm any DxHub/sponsor model preference.
- **Source:** `blocked-on-aws.md` (Config/values: `model_id` placeholder Haiku, TBD), `architecture.md` Open decisions #2.

### 1.5 Sponsor context: seed URLs, blacklist, Q&A set
- **Do:** Load the sponsor URL list + blacklist into `config.yaml` (`data_source.web_crawler.seed_urls` / `exclude_patterns` / `include_patterns`), and the sponsor Q&A set into `eval/datasets/`.
- **Current state:** `config.yaml` has a single placeholder seed (`https://www.gavilan.edu/library/`), empty exclude/include, `scope: HOST_ONLY`. Sponsor content arrives **~July 19** (build-plan.md), which is after the AWS account is expected.
- **Decision needed:** Either wait for the ~July 19 handoff, OR deploy first against the placeholder seed to shake out infra landmines, then re-sync the crawler once real URLs land. **Deferring is acceptable** — the infra deploy and the content are independent. Mark which path you took.
- **Verify (if loading real content):** seeds are correct library URLs; exclude patterns are valid regex; Q&A CSV is in `eval/datasets/`.
- **Source:** `build-plan.md` V1 ("Provide context — sponsor URL list + blacklist (arrives ~July 19)"), `config.yaml` `data_source.web_crawler`.

### 1.6 Confirm the OSS collection is NextGen, not Classic
- **Do:** This is designed around OSS NextGen scale-to-zero. Classic silently bills an always-on idle floor (~$350/mo).
- **Verify:** Cannot be confirmed from the minimal synth. Confirm the collection type at/after deploy (also a Phase 2 watch item). The distinction "isn't visible in the minimal synth."
- **Fallback:** If it provisions as Classic, the collection type must be corrected — this changes the cost model the whole design rests on.
- **Source:** `blocked-on-aws.md` (OpenSearch collection type: confirm NextGen, not Classic), `architecture.md` Verified (OSS NextGen scale-to-zero, "Old ~$350/mo Classic floor gone").

### 1.7 Confirm no hardcoded placeholder account/ARN values remain
- **Do:** Account id, ARNs, and account-specific values resolve at deploy via CDK tokens. Confirm none are hardcoded.
- **Verify:** `cdk synth` clean; the account/region come from tokens (`Stack.account` / `Stack.region`), not literals. `eval/eval_config.yaml` still holds `000000000000` / `TBD` placeholders — those are filled in Phase 3, not needed for the stack deploy.
- **Source:** `blocked-on-aws.md` (Config/values: "confirm no hardcoded placeholders remain"), `audit-resolutions.md` 1.2 (account/region auto-resolve via tokens).

### 1.8 Pre-deploy smoke: synth + tests green
- **Do:** From `infra/`, venv active: `cdk synth` (offline, no creds) and `python -m pytest`.
- **Verify:** Both pass. Infra tests catch the OAC/auto-delete dependency cycle that plain synth can miss (`CLAUDE.md` Lessons).
- **Source:** `CLAUDE.md` Commands + Lessons (OAC + auto_delete cycle "surfaces in `Template.from_stack`").

---

## Phase 2 — Stand-up (the deploy + landmines during it)

### Expected dependency order
Ingestion side (OSS collection + security/data-access policies -> `CfnIndex` -> `AWS::Bedrock::KnowledgeBase` -> `AWS::Bedrock::DataSource` WEB), then the query path (Lambda + role, HTTP API `POST /query` + `GET /warm`), then widget hosting (private S3 + CloudFront OAC, `BucketDeployment` of `widget.js`). The KB attach depends on the index being ACTIVE (see 2.2). CloudFront dominates wall-clock (see 2.3). (`CLAUDE.md` Repo layout, `blocked-on-aws.md`.)

**Deploy command:** `cdk deploy` (needs AWS creds). After first sync, the crawler must run to populate the KB before retrieval returns anything (relevant to Phase 3).

### 2.1 Index-creation auth: data-access policy + `aoss:` permissions
- **What:** First `cdk deploy` creates the `CfnIndex` in the OSS data plane. CloudFormation (the cfn-exec role), NOT the KB role, is the principal that creates it, so it must be (a) named in the OSS data-access policy with full index lifecycle rights, and (b) hold `aoss:` IAM permissions.
- **State:** The **data-access-policy half is FIXED in stack code** — audit 1.4 grants the deploy role full index lifecycle (create/update/delete/describe) in the data-access policy. The **IAM half is bootstrap-controlled** and was handled in Phase 1.2 (not in stack code).
- **When it surfaces:** During ingestion-stack creation, at index creation.
- **How you'll know it hit:** CloudFormation fails on the `CfnIndex` (or KB) resource with an access-denied / authorization error on the OSS data plane.
- **Fallback:** If it's the IAM half, re-bootstrap with `aoss:` (2.2 fallback in Phase 1). If somehow the policy half, confirm the data-access policy names the cfn-exec role via the **token-built ARN** (not `synthesizer.cloud_formation_execution_role_arn`, which emits `Fn::Sub` text a plain-JSON policy won't resolve — a known "don't fix this" trap).
- **Source:** `blocked-on-aws.md` (Data access policy missing CDK exec role; CDK exec role lacks `aoss:`), `audit-resolutions.md` 1.4, `CLAUDE.md` Lessons (data-access policy token-built ARN trap).

### 2.2 `CfnIndex` eventual-consistency race with KB attach
- **What:** `CfnIndex` creation is eventually consistent. The KB may try to attach before the index is ACTIVE on first deploy.
- **When it surfaces:** Right after index creation, at KB resource creation.
- **How you'll know it hit:** KB creation fails citing the index not found / not ready. May or may not trigger — watch for it.
- **Fallback:** A custom-resource index creator is the named fallback. If the race bites, add it so the index is confirmed ACTIVE before the KB attaches. **[VERIFY EXACT SYNTAX]** — the custom resource is not yet in the stack; implement per the fallback plan, do not assume an exact API.
- **Source:** `blocked-on-aws.md` (`CfnIndex` eventual-consistency race), `CLAUDE.md` Lessons (CfnIndex eventually-consistent; custom-resource fallback), `architecture.md` Decisions #3.

### 2.3 CloudFront slow create (~15-30 min) — not a hang
- **What:** The widget CloudFront distribution takes ~15-30 min to create (and again to destroy). It dominates deploy wall-clock.
- **When it surfaces:** Widget-hosting portion of the deploy, near the end.
- **How you'll know it's normal:** CloudFormation sits on the distribution resource for 15-30 min. This is expected, NOT a hang. Do not cancel.
- **Fallback:** None needed — just wait. Behavior (OAC read, caching, HTTPS redirect) is only verifiable once it finishes (Phase 3.7).
- **Source:** `blocked-on-aws.md` (CloudFront slow create/destroy), `CLAUDE.md` Lessons (CloudFront slow ~15-30 min), `architecture.md` Verified.

### 2.5 Crawler scope / filter values unvalidated at synth
- **What:** `scope`, `exclusion_filters`, `inclusion_filters` are plain strings, not enum-checked at synth. A typo synths green and fails at deploy.
- **When it surfaces:** At `AWS::Bedrock::DataSource` (WEB) creation.
- **How you'll know:** DataSource creation fails on an invalid `scope` token or a bad filter.
- **Verify before deploy:** `config.yaml` `scope: HOST_ONLY` is one of the documented tokens (DEFAULT / HOST_ONLY / SUBDOMAINS per the config comment). Confirm the exact `scope` token against current AWS Web Crawler docs, since the connector is Preview.
- **Fallback:** Correct the value in `config.yaml`, redeploy. `web_cfg.get("scope")` and `exclusion_filters` flow straight from config (`infra/infra/infra_stack.py:340-342`).
- **Source:** `blocked-on-aws.md` (Crawler scope/filter unvalidated at synth), `config.yaml` `data_source.web_crawler.scope`, `infra/infra/infra_stack.py:340`.

### 2.6 CORS is permissive (pre-production note)
- **What:** HTTP API CORS `allow_origins=["*"]` (`infra/infra/infra_stack.py:637-642`), GET allowed for the `/warm` ping. There's a standing `TODO` to lock origins to the widget's real domain before production.
- **When:** Not a deploy blocker. Flag for the pre-launch hardening pass, once the library page's real domain is known.
- **Source:** `blocked-on-aws.md` (CORS permissive), `infra/infra/infra_stack.py:630-642`, `build-plan.md` V2 (CORS lockdown).

### 2.7 Confirm NextGen at deploy
See Phase 1.6 — this is the deploy-time confirmation point. Check the provisioned collection is NextGen (scale-to-zero), not Classic. (`blocked-on-aws.md`.)

---

## Phase 3 — After stand-up (prove it works)

"Stack deployed" ≠ "bot works." These checks close that gap. Run in order; several depend on the crawler having synced at least once so retrieval returns content.

**Precondition:** trigger the Web Crawler data source sync and let it finish. Retrieval returns nothing until the KB has ingested pages. (`architecture.md` Data flow: "Ingest (on sync)".)

### 3.1 Warm path wakes OSS
- **Do:** `GET {api}/warm` — the widget fires this on page load. Handler runs retrieval-only, no generation, no guardrail (`app/handler.py:506-516`).
- **Expect:** HTTP 200 `{"warmed": true}`. First call after idle may take ~10-30s (OSS cold start).
- **If it fails:** Returns a 502 staged error (`stage: "warm"`). Check the KB id env var and the Lambda's KB `Retrieve` permissions.
- **Source:** `audit-resolutions.md` 1.3 (`/warm` route), `app/handler.py:506-516`, `architecture.md` Verified (OSS cold start ~10-30s).

### 3.2 Source-URI extraction populates `sources[]` (**highest-risk check**)
- **Do:** Run a real `POST /query` and inspect `sources[]`.
- **Why critical:** `_extract_source` pulls the citation URI from `location.webLocation.url`, falling back to `x-amz-bedrock-kb-source-uri` metadata (`app/handler.py:125-142`). The real Retrieve result shape is **unverified offline.** If the shape is wrong, `sources` comes back empty and citations silently don't render, even though the answer looks fine.
- **Expect:** `sources[]` is non-empty with resolvable page URLs, deduped by uri, in retrieval order.
- **How you'll know it hit:** Answer is populated but `sources` is `[]` on a query that clearly retrieved content.
- **Fallback:** Log a raw `retrievalResults[].location` from a live call, compare to the keys in `_extract_source`, and correct the key path. This is the single most likely silent failure post-deploy.
- **Source:** `blocked-on-aws.md` (Lambda source-URI extraction unverified), `app/handler.py:125-142`, `CLAUDE.md` `/query` contract.

### 3.3 Real `/query` end-to-end
- **Do:** `POST {api}/query` with `{"query": "What are the library hours?"}`.
- **Expect:** HTTP 200 `{"answer": "<text>", "sources": [...]}` with real retrieved content and populated sources (per 3.2). Round-trip against the deployed Lambda was unverified offline — this is the first real proof.
- **If it fails:** Staged 502 names the failing step (`input_guardrail` / `retrieve` / `generate`) in the structured log (`app/handler.py:482-497, 539-553`).
- **Source:** `blocked-on-aws.md` (Request round-trip unverified), `CLAUDE.md` `/query` contract, `app/handler.py:519-561`.

### 3.4 Guardrail behavior live (the critical audit-2.1 fix)
Only tested offline; sanity-check both paths live.
- **PII query (mask-and-answer):** Send a query containing e.g. an email or phone number. **Expect:** HTTP 200, a real answer generated on the silently-masked query — NOT a block. PII entities are ANONYMIZE, so a masked query proceeds (`config.yaml` `guardrail.pii_anonymize_entities`; `app/handler.py:338-343`).
- **Content / prompt-attack query (block, no retrieval):** Send a query that trips a content filter or prompt attack. **Expect:** HTTP 200 carrying the configured block message (`blocked_input_messaging`) with `sources: []`, and **no retrieval or generation happened** (blocked before Bedrock spend). (`app/handler.py:544-547`.)
- **Verify in logs:** the `input_guardrail` structured log records `decision` (`proceed` / `block`) and a PII-safe reduced assessment — never raw matched text (audit 3.3, `app/handler.py:299-311`).
- **Why live:** audit 2.1 was the CRITICAL fix (input guardrail moved to `ApplyGuardrail` BEFORE retrieval so PII masking never corrupts retrieved contact info); actual blocking/masking is only verifiable at deploy.
- **Source:** `audit-resolutions.md` 2.1 + 3.3, `blocked-on-aws.md` (Guardrail blocking unverified), `config.yaml` guardrail section, `app/handler.py`.

### 3.5 Retrieve-only eval (first eval that can run)
- **Do:** Fill `eval/eval_config.yaml`: `knowledge_base_id` (from 2.8), `role_arn`, `bucket`, `region`, `evaluator_model_id`, prefixes. Ensure the eval S3 bucket has **CORS enabled** (required for console-created jobs) and the evaluator model is enabled (Phase 1.3). Then run the retrieve-only evaluator.
- **Expect:** Job completes; ContextCoverage / ContextRelevance metrics returned. This answers FIXED_SIZE-vs-escalate chunking and SEMANTIC-vs-HYBRID search.
- **Note:** `eval/eval_config.yaml` `number_of_results` must mirror root `config.yaml` when evaluating production (audit 2.5).
- **Source:** `blocked-on-aws.md` (Retrieve-only eval — needs account + deployed KB), `architecture.md` Verified (Bedrock RAG eval), `audit-resolutions.md` 2.5.

### 3.6 Retrieve-and-generate eval + the `RETRIEVED_PASSAGES_KEY` confirmation
- **Do:** Implement `eval/capture_outputs.py` (currently a hard `NotImplementedError` stub): POST each dataset question to `{bot_api_url}/query` and map the response into `CapturedOutput`. Use `include_full_context: true` so the response carries the full un-deduped passages the model saw (audit 2.4). Fill `eval_config.yaml` `generate`: `rag_source_identifier`, `bot_api_url`.
- **Confirm at first run:** the retrieved-passages JSONL key is `output.retrievedPassages` (BYOI notebook) vs `output.retrievedResults` (retrieve-only doc). Isolated in code as `RETRIEVED_PASSAGES_KEY`. If wrong, the job produces garbage or errors — confirm the live key and set it.
- **Expect:** Job completes with Correctness/Completeness/Faithfulness/Helpfulness metrics.
- **Source:** `blocked-on-aws.md` (R&G eval; UNVERIFIED KEY), `audit-resolutions.md` 2.4 + 4.1, `architecture.md` Verified.

### 3.7 Widget end-to-end from the CloudFront script tag
- **Do:** Paste the `WidgetEmbedTag` (from 2.8) into a test page. Load it over HTTPS.
- **Expect:** `widget.js` loads from the CloudFront domain (served from private S3 via OAC), the widget injects (Shadow DOM), fires `GET /warm` on load, and a typed question round-trips to `POST /query` rendering `{answer, sources}`. Confirms OAC read, caching, and HTTPS redirect — none of which were verifiable offline.
- **If it fails:** 403 from CloudFront => OAC/bucket-policy issue; CORS error in the browser => the permissive CORS (2.6) or origin mismatch; timeout => cold start (widget timeout raised to ~30s, audit 1.3).
- **Source:** `blocked-on-aws.md` (CloudFront behavior verify-at-deploy; Request round-trip unverified), `architecture.md` Widget delivery, `audit-resolutions.md` 1.3.

---

## Additional risks (NOT in source docs)

Flagged separately because they are not named in the source docs; treat as lower-confidence.

- **No KB-id CfnOutput.** The stack outputs the embed tag, CDN domain, and query URL but not the Knowledge Base id, which Phase 3.5 needs for `eval_config.yaml`. Minor friction, not a failure. (Observed at `infra/infra/infra_stack.py:746-763`.)
- **Crawler sync is a separate manual step, not part of `cdk deploy`.** The stack provisions the DataSource but does not run an ingestion sync. Nothing retrieves until the first crawl completes. The source docs describe ingest "on sync" but do not call out triggering it as a deploy-day step — do not expect Phase 3 retrieval to work until the crawler has run at least once.
- **Guardrail version republish depends on config hash.** Audit 1.1 hashes the resolved guardrail config into `CfnGuardrailVersion.description` so any change forces a new immutable version. Corollary for deploy day: if you edit guardrail policy or block messages between deploys and the version does NOT bump, the runtime keeps the old policy. Verify the published version incremented after any guardrail config change. (Derived from `audit-resolutions.md` 1.1 + `CLAUDE.md` Guardrail note; the "verify it bumped" check is not spelled out in the docs.)

---

## Source summary

- **Phase 1 (prereqs):** region single-region requirement and NextGen from `blocked-on-aws.md` + `architecture.md`; `aoss:` bootstrap and the deploy-time gaps from `CLAUDE.md` Lessons; Claude model access prerequisite from `audit-resolutions.md` 1.2; sponsor content timing (~July 19) from `build-plan.md`.
- **Phase 2 (stand-up):** all landmines from `blocked-on-aws.md` "Deploy-time landmines," cross-checked against `CLAUDE.md` Lessons. Data-access-policy-fixed / IAM-bootstrap-controlled split reflects `audit-resolutions.md` 1.4. Verified live against the repo: no Lambda asset exclude (`infra/infra/infra_stack.py:602`), permissive CORS (`:637`), crawler scope from config (`:340`), CfnOutput names (`:746-763`).
- **Phase 3 (validation):** source-URI risk, warm path, round-trip, and eval-key items from `blocked-on-aws.md`; guardrail-behavior checks from `audit-resolutions.md` 2.1/3.3 and `blocked-on-aws.md`; handler behavior verified against `app/handler.py`; contract against `CLAUDE.md`.

## Ambiguities / things not fully groundable

- **Exact CLI syntax withheld** for `cdk bootstrap --cloudformation-execution-policies` (1.2), the custom-resource index creator (2.2), and the Lambda asset `exclude` argument (2.4). All marked **[VERIFY EXACT SYNTAX]** — the docs establish WHAT must happen, not the verified command string. Do not run these guessed.
- **Sponsor-content path is a decision, not a fact** (1.5): deploy-first-then-sync vs wait-for-July-19 is left to the operator; the docs give the timing, not the choice.
- **NextGen-vs-Classic verification method** (1.6 / 2.7): the docs say to confirm it but do not specify the console path or CLI that reveals collection type. Left as "confirm at deploy" without a guessed command.
- **`scope` token** (2.5): `config.yaml` lists DEFAULT/HOST_ONLY/SUBDOMAINS in a comment, but the Web Crawler connector is Preview, so `blocked-on-aws.md` explicitly says verify the exact token against AWS docs before deploy. Not treated as settled.
- **Crawler sync trigger** is under-specified across all source docs (see Additional risks) — the ingest-on-sync flow is described but triggering it on deploy day is not called out as a step.
