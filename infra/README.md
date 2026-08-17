# Infrastructure (CDK)

AWS CDK (Python) app that provisions the entire Gavilan Library Chatbot: the Bedrock Knowledge Base and its Amazon S3 Vectors store, the scraper Lambda (plus one EventBridge rule per freshness tier and the one-click deploy trigger), the catalog bucket, the query Lambda and HTTP API, the theme-save path (`PUT /theme` behind its own Cognito pool, plus a key-scoped Lambda), the feedback path (an SNS topic with one email subscription plus its own small Lambda, created only when `feedback.notify_email` is set), the Bedrock guardrail (one input screen), the CloudFront-fronted widget bucket, and - when `demo_site.enabled` - a second bucket/distribution pair for the demo page. Everything is L1 `Cfn*` constructs (see the architecture doc for why).

The app reads all changeable settings from the repo-root `config.yaml` at synth time; edit values there, not in the stack.

## Layout

- `app.py` — CDK entrypoint; loads `config.yaml` and instantiates the stack.
- `infra/infra_stack.py` — the stack (`GavilanChatbotStack`).
- `infra/config.py` — `load_config()` (resolves `config.yaml` from the repo root) plus the synth-time validators: CORS origins (rejects `*`), feedback resolution, and the scraper tier map.
- `tests/unit/` — `test_infra_stack.py` (`Template.from_stack` assertions), `test_handler.py` (query Lambda), `test_feedback_handler.py`, `test_theme_handler.py`. All stub boto3, so no live AWS is needed.

## Prerequisites

See the [root README](../README.md#prerequisites): AWS credentials, Bedrock model access, Python 3.13 (matching the Lambda runtime), the CDK CLI, and a bootstrapped account/region.

## Commands

Run from this directory with the virtualenv active.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt

cdk synth        # emit CloudFormation offline (no credentials needed)
python -m pytest # unit tests (no boto3 install, no live AWS)
cdk deploy       # deploy (needs credentials + a bootstrapped account)
cdk diff         # compare deployed stack with local state
cdk destroy      # tear everything down
```

`aws-cdk-lib==2.260.0` is pinned in `requirements.txt`. The CDK CLI is not pinned in the repo; this was built against `2.1129.0`.

`cdk deploy` outputs the paste-ready embed tag (`WidgetEmbedTag`), the CloudFront domain, the `/query` URL, the knowledge-base id, the demo-site URL, the settings-editor URL (`WidgetThemeEditor`) and the one command that creates a librarian account for it (`ThemeAdminCreateUserCommand`), and either the `/feedback` URL or a `FeedbackStatus` line explaining why there isn't one (an empty `feedback.notify_email` creates no endpoint - see the feedback block in `config.yaml`). CloudFront is slow to create and destroy (~15-30 min each), which dominates deploy/destroy wall-clock.

Changing `chunking` in `config.yaml` is a data-source replacement, not an update, and the replacement starts empty - `cdk deploy` does not refill it. See the chunking note in `CLAUDE.md` for the ingestion job to kick by hand afterwards.
