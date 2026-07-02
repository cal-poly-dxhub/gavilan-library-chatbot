# Eval harness

SDK/boto3 operational tooling that measures the chatbot's retrieval and generation
quality with Amazon Bedrock native RAG evaluation.

Ooperates on infra that already exists and runs on demand.

## Layout

- `datasets/` - Q&A CSV source data. `sample_qa.csv` is a placeholder
  showing the format.
- `dataset_loader.py` - reads a Q&A CSV into `QAPair` objects.
- `runner.py` - shared boto3 job-runner both eval types reuse: upload JSONL to S3, build
  and submit `create_evaluation_job`, poll status, locate results.
- `config.py` / `eval_config.yaml` - eval-infra config (region, role ARN, bucket,
  prefixes, evaluator model). Separate from the app's root `config.yaml`.
- `tests/` - offline unit tests (boto3 stubbed).

## CSV format

One row per question. Header required.

| column             | required | meaning                                                        |
|--------------------|----------|----------------------------------------------------------------|
| `question`         | yes      | the user question                                              |
| `reference_answer` | yes      | expected end-to-end ANSWER (Bedrock `referenceResponses`), not expected passages |
| `source` or `notes`| no       | provenance/notes; not sent to Bedrock               |

Fully blank rows are skipped. A row with only one of question/reference_answer is an error.

## Prerequisites

- An AWS account with Amazon Bedrock evaluation enabled in the chosen region.
- Access granted to the evaluator (judge) model set in `eval_config.yaml`.
- An S3 bucket for eval input JSONL and Bedrock output. If results are browsed from a UI,
  the bucket needs a CORS configuration.
- An IAM role Bedrock can assume with read/write on that bucket and evaluator model access
  (set its ARN in `eval_config.yaml`).
- Fill in the TBD values in `eval_config.yaml` (region, `role_arn`, `bucket`,
  `evaluator_model_id`).

## Running the tests (offline)

The tests stub boto3, so they need only `pytest` and `PyYAML` (not boto3). Any Python
env with those works, for example the infra virtualenv:

```
./infra/.venv/bin/python -m pytest eval/tests -v
```

Or set up a dedicated env:

```
python3 -m venv eval/.venv
source eval/.venv/bin/activate
pip install -r eval/requirements-dev.txt
python -m pytest eval/tests -v
```

Runtime deps for real runs are in `eval/requirements.txt` (`boto3`, `PyYAML`).
