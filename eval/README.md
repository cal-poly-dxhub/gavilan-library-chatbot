# Eval harness

boto3 tooling that measures retrieval and generation quality with Amazon Bedrock native RAG
evaluation. It operates on infra that already exists and runs on demand.

## Layout

- `datasets/` - Q&A CSVs. `sample_qa.csv` shows the format; `baseline_qa.csv` is the 27-question golden set.
- `dataset_loader.py` - reads a Q&A CSV into `QAPair` objects.
- `runner.py` - the shared job-runner: upload JSONL to S3, submit `create_evaluation_job`, poll, locate results.
- `format_retrieve.py` / `run_retrieve_eval.py` - retrieve-only eval (ContextCoverage, ContextRelevance).
- `format_generate.py` / `run_generate_eval.py` / `capture_outputs.py` - retrieve-and-generate, bring-your-own-inference. `capture_outputs.py` is a stub: it defines the shape but does not call `/query` yet.
- `chunking.py` / `run_chunking_eval.py` - offline chunk-boundary eval. It measures whether a window splits a golden answer, never how well a chunk ranks, so pair it with `retrieval_probe.py` before trusting a chunking change.
- `retrieval_probe.py` - live recall@k against the deployed knowledge base.
- `measure_usage.py` - POSTs `/query` with `include_usage` and fits the results into the `cost_model.measured` block for `config.yaml`. Spends real Bedrock money.
- `promptfoo/` - the answer-quality loop against the deployed endpoint. Separate tool, own README.
- `config.py` / `eval_config.yaml` - eval-infra config, separate from the root `config.yaml`.
- `tests/` - offline unit tests, boto3 stubbed.

## CSV format

One row per question, header required. `question` and `reference_answer` are both required, and
`reference_answer` is the expected end-to-end ANSWER (Bedrock `referenceResponses`), not expected
passages. An optional `source` or `notes` column is provenance and is not sent to Bedrock. Blank
rows are skipped; a row with only one required column is an error.

## Running for real

Needs Bedrock evaluation enabled in the region, access to the evaluator model, an S3 bucket for
input and output, and an IAM role Bedrock can assume with read/write on it. Put real values into
`eval_config.yaml` in place of the placeholders - `123456789012` is one.

## Running the tests (offline)

Use an env with `requirements-dev.txt` only (`pytest` + `PyYAML`) and **no boto3**:

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests -v
```

`tests/conftest.py` stubs boto3 only if it is not already importable, so running these in an env
that has the real boto3 silently disables the no-live-AWS guard. That is why the CI job installs
`requirements-dev.txt` and nothing else.
