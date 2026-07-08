# Gavilan Library Chatbot

After-hours RAG chatbot for Gavilan College Library. It answers common student questions (hours, checkout, textbooks, what the library offers) when librarians are offline, and points research questions to a human librarian. Built with Cal Poly DxHub.

## Stack

AWS-native:

- **RAG:** Amazon Bedrock Managed Knowledge Base (S3 data source) over an Amazon S3 Vectors store (Titan Embed v2, 1024-dim, semantic search)
- **Generation:** Bedrock-hosted Claude via an agentic Converse tool-use loop
- **Tools:** `search_library_info` (KB retrieval) and `database_catalog` (self-updating database availability + subject lookup)
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

In development.

## License

