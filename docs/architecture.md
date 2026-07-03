# Gavilan Library Chatbot — Architecture

**What:** RAG chatbot for Gavilan College Library. Answers operational questions (hours, checkout, textbooks, "what does the library offer"), routes research questions and out-of-scope issues.
**Client:** Gavilan College Library via Cal Poly DxHub.
**Status:** v1 built.
**Updated:** 2026-07-02

---

## Stack (v1)

| Layer | Choice | Why / why not |
|---|---|---|
| Ingestion | Bedrock KB **Web Crawler** connector | Seed URLs = sponsor list; exclusion regex = blacklist; citations built in. |
| RAG engine | **Bedrock Managed Knowledge Base** | Managed chunk/embed/store. |
| Vector store | **OpenSearch Serverless NextGen** | Scale-to-zero kills the idle cost floor. Not S3 Vectors: crawler is OSS-locked + S3 Vectors has no IaC support yet. |
| Embeddings | Titan Text Embeddings v2 (1024-dim) | Managed default. |
| Retrieval | KB **`Retrieve`** (NOT `RetrieveAndGenerate`) | Full system-prompt control required for out-of-scope + textbook behaviors. `RetrieveAndGenerate` doesn't allow this. |
| LLM (generation) | Bedrock-hosted Claude via **Converse** (Haiku, TBD) | All-AWS, no external OpenAI dep, clean data posture. System prompt via Converse `system` param. |
| Orchestration/API | **Lambda + HTTP API (API Gateway v2)** | Bot backend. Out-of-scope routing + textbook flow live in the system prompt. |
| Guardrails | **Bedrock Guardrails** (content + PII) | Content filters (Hate/Sexual HIGH, Insults/Violence/Misconduct MEDIUM, Prompt Attack HIGH input-only) + PII (anonymize contact PII, block credential/financial PII), input and output. Attached to Converse via `guardrailConfig`. |
| Widget | Custom vanilla-JS embed, Shadow DOM | Self-injecting single file, reads `data-api-url` from its script tag. Shadow DOM isolates from host-site CSS. |
| Widget hosting | **S3 + CloudFront (OAC)**, same stack | One `cdk deploy` ships backend + widget. OAC (not OAI/S3Origin). Same stack because OAC has a cross-stack cyclical-dependency problem. |
| Config | **`config.yaml` (declarative)** | Model IDs, chunking, retrieval, guardrail settings live in config, not code. Serves eval-driven iteration. |
| Deploy | CloudFormation / CDK (Python, L1 constructs) | Serves one-click AWS install. |
| Logs/eval | CloudWatch (structured logs); conversation logging TBD | Guardrail assessment logged per request. Conversation logging (DynamoDB vs CloudWatch) not yet decided. |

---

## Data flow

**Ingest (on sync):** Crawler -> sponsor seed URLs, child links, include/exclude regex, robots.txt -> Smart Parsing -> FIXED_SIZE chunk -> Titan v2 embed -> writes vectors into OSS NextGen. Re-sync on schedule (weekly baseline).

**Query (runtime, v1):** Widget -> API Gateway (HTTP API, POST /query) -> Lambda. Lambda does (1) KB `Retrieve` -> chunks from OSS, then (2) Bedrock `Converse` with the system prompt in the `system` param and the `<context>`-wrapped chunks + question in the message. Guardrail (attached via `guardrailConfig`) screens input before generation and output after. Response `{answer, sources[]}` returns to the widget. On guardrail intervention, the blocked message returns with empty sources.

**Widget delivery:** Browser loads the host library page -> its `<script>` tag fetches `widget.js` from CloudFront (served from private S3 via OAC) -> widget injects into the page -> widget calls the API Gateway `/query` (the query flow above).

---

## Decisions (evaluated, resolved)

1. **`Retrieve` over `RetrieveAndGenerate`.** Full system-prompt control is required for nuanced out-of-scope and textbook behaviors.
2. **OpenSearch NextGen over S3 Vectors.** Web Crawler is OSS-locked; S3 Vectors has no CloudFormation/CDK support yet. Future swap if that changes.
3. **L1 `Cfn*` constructs over higher-level abstractions.** `generative-ai-cdk-constructs` Bedrock L2s deprecated; `aws-bedrock-alpha` has no KB/DataSource constructs. L1 core covers everything. `CfnIndex` eliminates a custom Lambda index-creator.
4. **Same-stack widget hosting, OAC not OAI.** OAC is the current best practice (`S3BucketOrigin.with_origin_access_control`); `S3Origin` is deprecated. Same stack because cross-stack OAC hits a cyclical dependency.
5. **Contextual grounding EXCLUDED from guardrails.** AWS docs state grounding does not support conversational/chatbot use cases, and it requires fragile `guardContent` message-tagging (once any guardContent block is present, grounding evaluates only tagged blocks; documented silent-failure trap). The system prompt's hard-grounding covers v1. Revisit via the standalone `ApplyGuardrail` API if eval shows the prompt isn't holding. NOTE: v2 multi-turn would move the bot fully into the grounding-unsupported use case.
6. **WAF EXCLUDED for v1.** HTTP API (v2) cannot take WAF directly (WAF attaches only to REST API / CloudFront / ALB / AppSync); adding it would require fronting the API with a second CloudFront distribution. Threat surface is thin (no injectable DB, widget uses textContent not innerHTML, no auth, no sensitive data). The real risk (Bedrock cost-abuse) is better handled by API Gateway throttling (native, free). OPEN: confirm no compliance mandate for a WAF (Darren/sponsor); if mandated, scope the CloudFront-front-the-API work.
7. **Behavior in the system prompt, not routing code (v1).** Textbook clarifying questions and out-of-scope handling are prompt instructions, not Lambda branches. Escalate to deterministic routing only if eval shows unreliability.

---

## Open decisions

1. **Region** — verify KB + Web Crawler + OSS NextGen all available in one region before provisioning.
2. **LLM model** — Haiku leaning for FAQ scale; confirm any DxHub/sponsor preference.
3. **Refresh cadence** — crawler re-sync frequency (weekly baseline).
4. **Log store** — DynamoDB vs CloudWatch for conversation logging.

---

## Verified (2026-07)

- **Bedrock Managed KB:** GA June 2026. Web Crawler + 5 other connectors; managed chunk/embed/store/rerank; citations; native AgentCore integration.
- **Web Crawler connector:** seed URLs + child traversal, include/exclude regex, robots.txt, citations via `x-amz-bedrock-kb-source-uri`. **Preview. OSS-only for vector store.** "Smart Parsing" preserves HTML structure.
- **OSS NextGen:** GA May 28 2026. Scale-to-zero after 10min idle. ~$0.24/OCU-hr (active only) + ~$0.024/GB-mo storage. Cold start ~10-30s. Old ~$350/mo Classic floor gone.
- **S3 Vectors:** GA Dec 2025, ~90% cheaper, no idle floor. NOT crawler-compatible + no CFN/CDK yet. Future swap only.
- **Bedrock native RAG eval (BYOI):** LLM-as-judge, scores retrieval + generation separately, environment-agnostic since GA March 2025 (bring your own inference responses). `create_evaluation_job`: required jobName/roleArn/evaluationConfig/inferenceConfig/outputDataConfig; RAG uses `precomputedRagSourceConfig` with retrieve-only or retrieve-and-generate source config. Use `referenceResponses` (expected answers), not the old `referenceContexts`. Retrieve-only metrics ContextCoverage/ContextRelevance; R&G metrics Correctness/Completeness/Faithfulness/Helpfulness/Harmfulness + citation metrics.
- **Bedrock Guardrails:** six policy types (content filters, denied topics, word filters, PII, contextual grounding, Automated Reasoning). Content filters: Hate/Insults/Sexual/Violence/Misconduct/Prompt Attack, strength NONE/LOW/MED/HIGH. PII: block or anonymize, separate input/output actions. Attached to Converse via `guardrailConfig` (id/version/trace); blocked -> `stopReason: guardrail_intervened`. Needs `bedrock:ApplyGuardrail` alongside `InvokeModel`. Contextual grounding: NOT supported for conversational chatbot use; needs guardContent tagging. PII masking does NOT apply to invocation logs.
- **CloudFront + S3 (CDK):** OAC via `S3BucketOrigin.with_origin_access_control` (auto-creates the OAC + bucket policy). `S3Origin`/OAI deprecated. Bucket needs Bucket-owner-enforced ownership. CloudFront slow to create/destroy (~15-30 min). Cross-stack OAC = cyclical dependency; keep bucket + distribution in one stack.
- **Custom transformation Lambda:** runs during ingestion, controls chunking, works with managed crawler. The L2 escape hatch. `trafilatura` (boilerplate removal) runs here or in an L3 scraper. Deferred to v2 unless eval shows boilerplate hurts retrieval.
- **DxHub framework** (`cal-poly-dxhub/generic-chatbot-framework`): mineable for config-driven pattern, guardrail config, condensing-flow idea (multi-turn context). Stale on Bedrock/vector specifics (pre-2026, S3-ingest not web-crawl). Full control is ours.
- **Deleting a KB does NOT delete the backing OSS collection.** Manual cleanup required (`cdk destroy` handles it; deleting the KB alone does not).