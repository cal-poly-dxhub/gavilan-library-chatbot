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
- **Frontend:** embeddable JS widget, plus a deployed demo page that embeds it

## Structure

- `infra/` — CDK app (Knowledge Base, S3 Vectors store, scraper, catalog bucket, Lambda, API Gateway, IAM)
- `app/` — Lambda backend (agentic tool-use loop + tools)
- `eval/` — retrieval and answer-quality evaluation harness
- `frontend/` — chat widget and the deployed demo page
- `config.yaml` — model, chunking, retrieval, catalog, and guardrail settings
- `docs/` — architecture and planning

## Architecture

![Architecture diagram](docs/architecture_diagram.png)

Full design: [`docs/architecture.md`](docs/architecture.md).

## Prerequisites

- An AWS account, and credentials configured locally (`aws configure` or an SSO profile).
- **Amazon Bedrock model access** granted in your target region for: Titan Text Embeddings v2 (embeddings), the Claude generation model set in `config.yaml`, and the scraper's enrichment model. Request access in the Bedrock console before deploying, or synth/deploy succeeds but runtime calls fail.
- Python 3.11+ and Node.js (the CDK CLI is a Node package).
- The AWS CDK CLI: `npm install -g aws-cdk` (pinned to `2.1129.0`).
- The account/region bootstrapped for CDK: `cdk bootstrap` (once per account/region).
- **Outbound internet from the query Lambda.** The two Primo catalog tools call the Ex Libris discovery API directly (not an AWS service, no IAM). The default deploy runs the Lambda outside a VPC, which has outbound access. If you place it in a VPC, it needs a NAT path or those two tools silently fail.

## Configure

All changeable settings live in `config.yaml` at the repo root (the CDK app reads it at synth). Before deploying, review:

- `data_source.web_crawler.seed_urls` / `scraper.seed_urls` — the library pages to ingest. The defaults point at the Gavilan library site; swap in your own.
- `cors.allow_origins` — the browser origins allowed to call `/query`. Set this to the site that will host the widget (a wildcard is rejected at synth). Drop the `localhost` dev entry before launch.
- `generation.model_id`, `catalog.*`, `primo.*`, `guardrail.*` — model IDs and tuning knobs, documented inline.

The eval harness has its own separate config; see [`eval/README.md`](eval/README.md).

## Deploy

All infra commands run from `infra/` with the virtualenv active.

```bash
cd infra
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt

cdk synth      # offline, no credentials needed
python -m pytest   # unit tests (boto3 stubbed, no live AWS)
cdk deploy     # needs AWS credentials + a bootstrapped account
```

`cdk deploy` provisions everything (KB, S3 Vectors store, scraper, catalog bucket, query Lambda, HTTP API, guardrails, the CloudFront-fronted widget, and the demo site) and outputs a paste-ready embed tag, the CloudFront domain, the `/query` URL, and the demo site URL. CloudFront is slow to create and destroy (~15-30 min each), so the first deploy takes a while. Tear down with `cdk destroy`.

## Demo site

The deploy also publishes a shareable demo page — the `DemoSiteUrl` output. Open it and the
chat widget is already there and already working: no local server, no config, nothing to paste.

It is the real thing, not a mockup of one. The page loads the same `widget.js` from the same
CloudFront distribution the library would embed, and posts to the same `/query` endpoint, so it
cannot drift from what ships. Both URLs are stamped into the page **at deploy time**, so a fresh
install in a different AWS account gets a correct page with no hand-editing.

The page around the widget is a Gavilan-Library-styled sample (hand-written CSS; nothing is
fetched from gavilan.edu) so the widget can be seen in context. It is labelled as a demo in a
banner at the top and served `noindex` both as a meta tag and as an `X-Robots-Tag` header.

It lives on its own S3 bucket and its own CloudFront distribution, separate from the widget's, so
nothing about it can affect production widget delivery. Turn it off with `demo_site.enabled:
false` in `config.yaml` and the next deploy removes the bucket, the distribution, and the CORS
entry that goes with it.

## License

MIT. See [LICENSE](LICENSE).

