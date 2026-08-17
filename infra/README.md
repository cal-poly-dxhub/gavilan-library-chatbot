# Infrastructure (CDK)

AWS CDK (Python) app that provisions the whole chatbot - `docs/architecture.md` lists the pieces
and the reasoning, including why everything is L1 `Cfn*`. All changeable settings come from the
repo-root `config.yaml` at synth time; edit values there, not in the stack.

## Layout

- `app.py` - CDK entrypoint; loads `config.yaml` and instantiates the stack.
- `infra/infra_stack.py` - the stack (`GavilanChatbotStack`).
- `infra/config.py` - `load_config()`, plus the synth-time validators: CORS origins (rejects `*`), feedback resolution, and the scraper tier map.
- `tests/unit/` - `test_infra_stack.py` (`Template.from_stack` assertions), `test_handler.py`, `test_feedback_handler.py`, `test_theme_handler.py`. All stub boto3, so no live AWS is needed.

## Commands

Run from this directory with the virtualenv active. Account prerequisites and the first-deploy
walkthrough are in [the install guide](../docs/install.md); note Python 3.13, matching the Lambda
runtime.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt

cdk synth        # emit CloudFormation offline (no credentials needed)
python -m pytest # unit tests (no live AWS)
cdk deploy       # needs credentials + a bootstrapped account
cdk diff
cdk destroy
```

`aws-cdk-lib==2.260.0` is pinned in `requirements.txt`. The CDK CLI is not pinned in the repo;
this was built against `2.1129.0`.

Two things worth knowing before your first deploy. CloudFront dominates the wall clock, roughly
15-30 minutes to create and again to destroy. And changing `chunking` in `config.yaml` is a
data-source replacement rather than an update - the replacement starts empty and `cdk deploy`
does not refill it, so see the chunking note in `CLAUDE.md` for the ingestion job to kick by hand.

The deploy prints everything you need next: the paste-ready embed tag, the CloudFront domain, the
`/query` URL, the knowledge-base id, the demo-site URL, the settings-editor URL and the one
command that creates a librarian account for it, and either the `/feedback` URL or a
`FeedbackStatus` line explaining why there isn't one.
