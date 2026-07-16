# Gavilan Library Chatbot

After-hours RAG chatbot for Gavilan College Library. It answers common student questions (hours, checkout, textbooks, what the library offers) when librarians are offline, and points research questions to a human librarian. Built with Cal Poly DxHub.

## Stack

AWS-native:

- **RAG:** Amazon Bedrock Managed Knowledge Base (S3 data source) over an Amazon S3 Vectors store (Titan Embed v2, 1024-dim, semantic search)
- **Generation:** Bedrock-hosted Claude via an agentic Converse tool-use loop
- **Tools:** `search_library_info` (KB retrieval), `database_catalog` (research-database availability + subject lookup), `search_book_catalog` (Primo general catalog), and `search_course_reserves` (Primo course reserves)
- **Live catalog:** `search_book_catalog` + `search_course_reserves` query the Ex Libris Primo discovery API directly - the only external, non-AWS dependency (timed out and soft-failing so a slow/broken Primo never blocks a response)
- **Ingestion:** a scraper Lambda pulls the library site into the KB source bucket and regenerates the database catalog on a weekly schedule
- **Backend:** Lambda + API Gateway (Python)
- **Infrastructure:** AWS CDK (Python)
- **Guardrails:** Bedrock Guardrails (content filtering + PII redaction)
- **Frontend:** embeddable JS widget

## Structure

- `infra/` — CDK app (Knowledge Base, S3 Vectors store, scraper, catalog bucket, Lambda, API Gateway, IAM)
- `app/` — Lambda backend (agentic tool-use loop + tools)
- `eval/` — retrieval and answer-quality evaluation harness
- `frontend/` — chat widget
- `config.yaml` — model, chunking, retrieval, catalog, and guardrail settings
- `docs/` — architecture and planning

## Architecture

![Architecture diagram](docs/architecture_diagram.png)

Full design: [`docs/architecture.md`](docs/architecture.md).

## Status

Deployed to the gavilan AWS account and validated end-to-end (query path, four tools, multi-turn, guardrails). Pre-launch hardening (CORS lockdown, sponsor content) still pending.

## License

