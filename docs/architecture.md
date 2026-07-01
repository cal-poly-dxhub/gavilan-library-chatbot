# Gavilan Library Chatbot — Architecture

**What:** RAG chatbot for Gavilan College Library. Answers operational questions (hours, checkout, textbooks, "what does the library offer"), routes research questions and out-of-scope issues to humans.
**Client:** Gavilan College Library via Cal Poly DxHub / CCC Summer Camp.
**Status:** Stack decided
**Updated:** 2026-07-01

---

## Stack (v1)

| Layer | Choice | Why / why not |
|---|---|---|
| Ingestion | Bedrock KB **Web Crawler** connector | Seed URLs = sponsor list; exclusion regex = blacklist; citations built in. Preview status = dependency risk. |
| RAG engine | **Bedrock Managed Knowledge Base** | Managed chunk/embed/search/rerank/cite. Not hand-rolled: AWS ships the pipeline; hand-rolling breaks the one-click-install goal. |
| Vector store | **OpenSearch Serverless NextGen** | Scale-to-zero kills the idle cost floor. Not S3 Vectors: crawler is OSS-locked + S3 Vectors has no IaC support yet. |
| Embeddings | Titan Text Embeddings v2 | Managed default. |
| LLM (generation) | Bedrock-hosted Claude (Haiku, TBD) | All-AWS, no external OpenAI dep, clean data posture. |
| Orchestration/API | **Lambda + API Gateway** | Bot backend. Holds decision-tree + out-of-scope routing (must NOT live in RAG). |
| Guardrails | **Bedrock Guardrails** | Content filtering (HATE/VIOLENCE/SEXUAL) + PII redaction (email/phone/name) on input and output. |
| Config | **`config.yaml` (declarative)** | Model IDs, chunking, reranking, guardrail settings, thresholds live in config, not code. Serves eval-driven iteration. |
| Widget | Custom JS embed |  |
| Deploy | CloudFormation / CDK | Serves one-click AWS install. |
| Logs/eval | DynamoDB or CloudWatch | Conversation logs + eval feedback. |


---

## Phases

**Phase 0 — Provision.** Pick region where KB + Web Crawler + OSS NextGen all exist. Stand up KB with Web Crawler + NextGen store. Deploy skeleton via CDK.

**Phase 1 — Managed RAG v1 (the default build).** Crawl sponsor URLs + blacklist. Managed parsing, start FIXED_SIZE chunking. Wire Lambda + API Gateway + widget. Semantic retrieval only. Get end-to-end working.

**Phase 2 — Tune retrieval.** Move to HIERARCHICAL chunking + hybrid search + reranking. Refine the system prompt for the textbook clarifying-question and out-of-scope behaviors. Measure against sponsor eval set.

**Phase 3 — Eval + harden.** Run sponsor Q&A through Bedrock native RAG eval. Wire Bedrock Guardrails (content + PII). Add conversation logging, chatbot policy doc, budget alerts. Ship case study.

**Escalations (eval-triggered only):**
- **Deterministic routing fork** — if prompt-driven approach proves unreliable (model skips the textbook clarifying question, or occasionally answers an out-of-scope IT question), pull that specific behavior out of the prompt into a Lambda decision branch. Also warranted if the sponsor requires a hard zero-tolerance out-of-scope guarantee.
- **Custom chunking Lambda** — if eval shows boilerplate/bad chunks hurting retrieval, insert a transformation Lambda running `trafilatura` into managed ingestion. Keeps crawler + store + install.
- **Hybrid route-to-S3** — layer a query router: high-frequency stable questions (hours, checkout) hit vetted S3-backed answers, rest falls back to semantic.
- **Agentic (roadmap)** — managed KB integrates natively with AgentCore; clean path to agentic RAG without rewrite.
- **S3 Vectors swap** — if/when it gets IaC support; unlocks ~90% cheaper store via the S3-data-source path.

---

## Data flow

**Ingest (on sync):** Crawler → sponsor seed URLs, child links, include/exclude regex, robots.txt → Smart Parsing → chunk → Titan v2 embed → OSS NextGen. Re-sync on schedule (weekly baseline).

**Query (runtime, v1):** Widget → API Gateway → Lambda → KB (embed, hybrid search, rerank, retrieve) → Bedrock LLM generates grounded answer + citations → Guardrails filter input → log → return. 

---

## Open decisions

1. **Region** — verify KB + Web Crawler + OSS NextGen all available in one region before provisioning. 
2. **LLM model** — Haiku leaning for FAQ scale; confirm any DxHub/sponsor preference.
3. **Refresh cadence** — crawler re-sync frequency (weekly baseline).
4. **Log store** — DynamoDB vs CloudWatch.

---

## Verified (2026-07-01)

- **Bedrock Managed KB:** GA June 2026. Web Crawler + 5 other connectors; managed chunk/embed/store/rerank; agentic retrieval; citations; native AgentCore integration.
- **Web Crawler connector:** seed URLs + child traversal, include/exclude regex, robots.txt, citations via `x-amz-bedrock-kb-source-uri`. **Preview. OSS-only for vector store.** "Smart Parsing" preserves HTML structure; parsing not hand-controlled by default.
- **OSS NextGen:** GA May 28 2026. Scale-to-zero after 10min idle. ~$0.24/OCU-hr (active only) + ~$0.024/GB-mo storage. Cold start ~10-30s. Old ~$350/mo Classic floor gone.
- **S3 Vectors:** GA Dec 2025, ~90% cheaper, no idle floor. But NOT crawler-compatible + no CFN/CDK yet. Future swap only.
- **Aurora Serverless (pgvector):** supported store, scale-to-zero at min ACU=0. Adds VPC/DB ops; not chosen.
- **Custom transformation Lambda:** runs during ingestion, controls chunking, works with managed crawler. The L2 escape hatch.
- **`trafilatura`:** standard Python boilerplate-removal lib (strips nav/header/footer). Runs in L2 Lambda or L3 scraper. No place at L1.
- **Bedrock native RAG eval:** LLM-as-judge, scores retrieval + generation separately. Use for sponsor Q&A set.
- **Bedrock Guardrails:** managed content filtering (HATE/VIOLENCE/SEXUAL, strength LOW/MED/HIGH) + PII filters (EMAIL/PHONE/NAME etc., ANONYMIZE or BLOCK), applied to input and output. Configurable via config.yaml.
- **DxHub framework** (`cal-poly-dxhub/generic-chatbot-framework`): fresh build, but mineable for config-driven pattern, guardrail config, reranking config, condensing-flow idea (multi-turn context). Stale on Bedrock/vector specifics (pre-2026, S3-ingest not web-crawl). Full control is ours.
- **Deleting a KB does NOT delete the backing OSS collection.** Manual cleanup required.