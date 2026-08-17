# Eval harness

SDK/boto3 operational tooling that measures the chatbot's retrieval and generation
quality with Amazon Bedrock native RAG evaluation.

Operates on infra that already exists and runs on demand.

## Layout

- `datasets/` - Q&A CSV source data. `sample_qa.csv` shows the format; `baseline_qa.csv` is
  the 27-question golden set.
- `dataset_loader.py` - reads a Q&A CSV into `QAPair` objects.
- `runner.py` - shared boto3 job-runner both eval types reuse: upload JSONL to S3, build
  and submit `create_evaluation_job`, poll status, locate results.
- `format_retrieve.py` / `run_retrieve_eval.py` - retrieve-only eval (ContextCoverage,
  ContextRelevance).
- `format_generate.py` / `run_generate_eval.py` / `capture_outputs.py` - retrieve-and-generate
  eval, bring-your-own-inference. `capture_outputs.py` is a stub: it defines the shape and
  documents the intended implementation, but does not call the deployed `/query` yet.
- `chunking.py` / `run_chunking_eval.py` - offline chunk-boundary eval. It measures whether a
  window splits a golden answer, never how well a chunk ranks; pair it with
  `retrieval_probe.py` against the live index before trusting a chunking change.
- `retrieval_probe.py` - live recall@k against the deployed knowledge base.
- `measure_usage.py` - POSTs `/query` with `include_usage` and fits the results into the
  paste-ready `cost_model.measured` block for `config.yaml`. Spends real Bedrock money.
- `promptfoo/` - the answer-quality loop against the deployed endpoint. Separate tool, its
  own README, touches nothing here.
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
- Replace the placeholder values in `eval_config.yaml` (`role_arn`, `bucket`,
  `knowledge_base_id`, and `region`/`evaluator_model_id` if you differ) with your own
  deployed resources. The account id `123456789012` is a placeholder.

## Running the tests (offline)

Use an env with `requirements-dev.txt` only (`pytest` + `PyYAML`) and **no boto3**:

```
python3 -m venv eval/.venv
source eval/.venv/bin/activate
pip install -r eval/requirements-dev.txt
python -m pytest eval/tests -v
```

`tests/conftest.py` stubs boto3 only if it is not already importable, so running these in an
env that has the real boto3 (the infra virtualenv, say) silently disables the no-live-AWS
guard. That is why the CI job installs `requirements-dev.txt` and nothing else.

Runtime deps for real runs are in `eval/requirements.txt` (`boto3`, `PyYAML`).
