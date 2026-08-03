# Infrastructure (CDK)

AWS CDK (Python) app that provisions the entire Gavilan Library Chatbot: the Bedrock Knowledge Base and its Amazon S3 Vectors store, the scraper Lambda (plus its weekly schedule and one-click deploy trigger), the catalog bucket, the query Lambda and HTTP API, the feedback path (an SNS topic with one email subscription plus its own small Lambda, created only when `feedback.notify_email` is set), the Bedrock guardrail (one input screen), and the CloudFront-fronted widget bucket. Everything is L1 `Cfn*` constructs (see the architecture doc for why).

The app reads all changeable settings from the repo-root `config.yaml` at synth time; edit values there, not in the stack.

## Layout

- `app.py` — CDK entrypoint; loads `config.yaml` and instantiates the stack.
- `infra/infra_stack.py` — the stack (`GavilanChatbotStack`).
- `infra/config.py` — `load_config()`, CORS-origin resolution, and feedback resolution/validation; resolves `config.yaml` from the repo root.
- `tests/unit/` — `test_infra_stack.py` (`Template.from_stack` assertions), `test_handler.py` (query-Lambda tests) and `test_feedback_handler.py` (feedback-Lambda tests), all with boto3 stubbed so no live AWS is needed.

## Prerequisites

See the [root README](../README.md#prerequisites): AWS credentials, Bedrock model access, Python 3.11+, the CDK CLI, and a bootstrapped account/region.

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

Pinned versions: `aws-cdk-lib==2.260.0`, CDK CLI `2.1129.0`.

`cdk deploy` outputs a paste-ready widget embed tag, the CloudFront domain, the `/query` URL, and either the `/feedback` URL or a `FeedbackStatus` line explaining why there isn't one (an empty `feedback.notify_email` creates no endpoint - see the feedback block in `config.yaml`). CloudFront is slow to create and destroy (~15-30 min each), which dominates deploy/destroy wall-clock.
