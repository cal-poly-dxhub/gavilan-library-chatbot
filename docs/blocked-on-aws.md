# Blocked on AWS / PR

List of everything to do once receiving AWS account.

---

## Deploy-time landmines (INFRA) — surface only at first `cdk deploy`

- **Data access policy is missing the CDK execution role.** Policy names only the KB execution role, but CloudFormation (the CDK deploy role) is what actually creates the index. First deploy WILL fail on index creation until the deploy role is added as a principal with index-creation rights.
- **CDK execution role lacks `aoss:` permissions.** Default bootstrap role has `es:*`, not `aoss:*`. Same index-creation blocker. Grant `aoss:` to the deploy role (inline policy or `cdk bootstrap --cloudformation-execution-policies`).
- **`CfnIndex` eventual-consistency race.** The index may not be ACTIVE when the KB tries to attach on first deploy. Fallback ready: custom-resource index creator. May or may not trigger; watch for it.
- **Crawler `scope` / filter values unvalidated at synth.** Plain strings, not enum-checked. A typo synths green and fails at deploy. Verify exact `scope` token against AWS docs before deploy.
- **OpenSearch collection type: confirm NextGen, not Classic.** The scale-to-zero (NextGen) vs always-on (Classic) distinction isn't visible in the minimal synth. Verify the deployed collection is NextGen or it silently bills the idle cost floor we designed around.
- **CORS is permissive on the HTTP API.** TODO to lock to the widget's real domain before production.
- **Lambda source-URI extraction unverified.** The handler pulls citation source from the KB Retrieve result's `location.webLocation.url`, falling back to `x-amz-bedrock-kb-source-uri` metadata. Correct per the docs, but the real Retrieve result shape is unverified offline. If wrong, `sources` returns empty and citations silently don't render. Confirm at first real run.
- **Lambda asset bundles `__pycache__`.** `Code.from_asset(app/)` grabs the whole `app/` dir, so test-run bytecode caches (and any stray files) ship to Lambda. Add an asset exclude so only `handler.py` + `system_prompt.md` bundle.
- **CloudFront slow create/destroy (~15-30 min each).** Both the widget-hosting distribution's behavior (caching, distribution URL, HTTPS) and its create/teardown time only verify at deploy. Least-offline-verifiable piece in the stack.
- **Guardrail version pinning.** The Lambda pins to the published numbered guardrail version. To make live policy/blocked-message edits take effect, publish a new version or point the Lambda at `DRAFT`. Numbered version is the safer default. (`bedrock:ApplyGuardrail` is granted on the guardrail ARN, required alongside `InvokeModel`.)
- **Guardrail blocking behavior unverified.** Actual content-filter/PII blocking only verifiable at deploy. Per-request `guardrail_assessment` is logged so behavior can be measured and tuned once live.
- **Request round-trip unverified.** Widget POSTs `{query}`; confirmed to match the handler field, but full request/response against the deployed Lambda is unverified until the account lands (the mock conforms to the widget, so only a live call proves end-to-end).

## Config / values to fill when account lands (INFRA)

- `config.yaml` `generation.model_id` — placeholder Haiku id, marked TBD. Pick the real generation model.
- Region — verify Bedrock Managed KB + Web Crawler connector + OpenSearch Serverless NextGen are ALL available in the chosen region before provisioning. Don't default to us-west-2 without checking.
- Account-specific values across the stack (account id, ARNs) resolve at deploy; confirm no hardcoded placeholders remain.

---

## Eval harness — cannot RUN until AWS (+ bot) exists

Everything in `eval/` is coded and unit-tested offline (43 tests), but no eval job can run without the account. Ordered by what unblocks each.

**Retrieve-only eval (chunking/retrieval evaluator) — needs ACCOUNT + deployed KB only**
- Fill `eval/eval_config.yaml`: `knowledge_base_id`, `role_arn`, `bucket`, `region`, `evaluator_model_id`, prefixes (all TBD).
- S3 bucket for the eval dataset must have **CORS enabled** (required for console-created jobs).
- Evaluator model must be **enabled in the account** (Bedrock model access page).
- FIRST eval to run once account + KB are up. Answers FIXED_SIZE-vs-escalate chunking and SEMANTIC-vs-HYBRID search.

**Retrieve-and-generate eval (answer-quality evaluator) — needs ACCOUNT + deployed BOT**
- `capture_outputs.py` is a **hard stub (`NotImplementedError`)**. Fill once the bot is deployed: POST each question to the bot's `/query` endpoint, map the response into `CapturedOutput`. Bot returns `{answer, sources[]}`, so the mapping has a real shape to target, pending confirmation of the live shape.
- Fill `eval/eval_config.yaml` `generate`: `rag_source_identifier`, `bot_api_url` (both TBD).
- **UNVERIFIED KEY:** retrieved-passages field is `output.retrievedPassages` (BYOI notebook) vs `output.retrievedResults` (retrieve-only doc). Isolated as `RETRIEVED_PASSAGES_KEY`. **Confirm at first real R&G run** or the job produces garbage / errors.

---