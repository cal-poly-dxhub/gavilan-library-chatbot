# Collaboration

Thanks for your interest in our solution. Having specific examples of replication and cloning allows us to continue to grow and scale our work. If you clone or download this repository, kindly shoot us a quick email to let us know you are interested in this work!

[wwps-cic@amazon.com]

---

# Disclaimers 

Customers are responsible for making their own independent assessment of the information in this document. 

This document: 

(a) is for informational purposes only, 

(b) references AWS product offerings and practices, which are subject to change without notice, 

(c) does not create any commitments or assurances from AWS and its affiliates, suppliers or licensors. AWS products or services are provided "as is" without warranties, representations, or conditions of any kind, whether express or implied. The responsibilities and liabilities of AWS to its customers are controlled by AWS agreements, and this document is not part of, nor does it modify, any agreement between AWS and its customers, and 

(d) is not to be considered a recommendation or viewpoint of AWS. 

Additionally, you are solely responsible for testing, security and optimizing all code and assets on GitHub repo, and all such code and assets should be considered: 

(a) as-is and without warranties or representations of any kind, 

(b) not suitable for production environments, or on production or other critical data, and 

(c) to include shortcuts in order to support rapid prototyping such as, but not limited to, relaxed authentication and authorization and a lack of strict adherence to security best practices. 

All work produced is open source. More information can be found in the GitHub repo.

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
- **Feedback:** `POST /feedback` emails a librarian, via SNS, when someone reports a wrong answer - carrying the pages that answer cited, because fixing the page fixes the bot. Set `feedback.notify_email` in `config.yaml` to switch it on
- **Infrastructure:** AWS CDK (Python)
- **Guardrails:** one Bedrock Guardrail, screening the input for prompt injection only (no other content filter, no PII policy, nothing on the output)
- **Frontend:** embeddable JS widget, plus a deployed demo page that embeds it

## Structure

- `infra/` — CDK app (Knowledge Base, S3 Vectors store, scraper, catalog bucket, Lambda, API Gateway, IAM)
- `app/` — Lambda backend (agentic tool-use loop + tools)
- `eval/` — retrieval and answer-quality evaluation harness
- `frontend/` — chat widget and the deployed demo page
- `config.yaml` — model, chunking, retrieval, catalog, feedback, and guardrail settings
- `docs/` — the [install guide](docs/install.md), architecture, and planning

## Architecture

![Architecture diagram](docs/architecture_diagram.png)

Full design: [`docs/architecture.md`](docs/architecture.md).

## Install

**[`docs/install.md`](docs/install.md) is the install guide**: AWS prerequisites, the one config
value to set first, `cdk deploy`, pasting the embed tag onto your site, and setting up the
librarian's settings page. Read it before you deploy.

The short version, once you have credentials, Bedrock model access, Node.js, the CDK CLI pinned
to `2.1129.0`, and a bootstrapped account/region:

```bash
cd infra
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt

cdk synth      # offline, no credentials needed
python -m pytest   # unit tests (boto3 stubbed, no live AWS)
cdk deploy     # needs AWS credentials + a bootstrapped account
```

`cdk deploy` provisions everything (KB, S3 Vectors store, scraper, catalog bucket, query Lambda, HTTP API, the guardrail, the CloudFront-fronted widget, and the demo site) and outputs a paste-ready embed tag, the CloudFront domain, the `/query` URL, and the demo site URL. CloudFront is slow to create and destroy (~15-30 min each), so the first deploy takes a while. Tear down with `cdk destroy`.

All changeable settings live in `config.yaml` at the repo root (the CDK app reads it at synth). The eval harness has its own separate config; see [`eval/README.md`](eval/README.md).

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

