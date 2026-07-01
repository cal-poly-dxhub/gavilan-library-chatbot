# Gavilan Library Chatbot

After-hours RAG chatbot for Gavilan College Library. It answers common student questions (hours, checkout, textbooks, what the library offers) when librarians are offline, and points research questions to a human librarian. Built with Cal Poly DxHub.

## Stack

AWS-native:

- **RAG:** Amazon Bedrock Managed Knowledge Base, Web Crawler connector into an OpenSearch Serverless (NextGen) vector store
- **Generation:** Bedrock-hosted Claude, via `Retrieve` plus generation call
- **Backend:** Lambda + API Gateway (Python)
- **Infrastructure:** AWS CDK (Python)
- **Guardrails:** Bedrock Guardrails (content filtering + PII redaction)
- **Frontend:** embeddable JS widget

## Structure

- `infra/` — CDK app (Knowledge Base, vector store, crawler, Lambda, API Gateway, IAM)
- `app/` — Lambda backend (retrieval + generation)
- `eval/` — retrieval and answer-quality evaluation harness
- `frontend/` — chat widget
- `config.yaml` — model, chunking, reranking, and guardrail settings
- `docs/` — architecture and planning

## Architecture

Full design, decisions, and rationale: [`docs/architecture.md`](docs/architecture.md).

## Status

In development.

## License

