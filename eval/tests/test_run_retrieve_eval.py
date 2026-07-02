import json
from pathlib import Path

from run_retrieve_eval import run_retrieve_only_eval

_SAMPLE_CSV = Path(__file__).resolve().parents[1] / "datasets" / "sample_qa.csv"

CONFIG = {
    "region": "us-west-2",
    "role_arn": "arn:aws:iam::123456789012:role/eval-role",
    "bucket": "test-eval-bucket",
    "input_prefix": "eval/input",
    "output_prefix": "eval/output",
    "evaluator_model_id": "us.anthropic.claude-3-5-sonnet-20240620-v1:0",
    "application_type": "RagEvaluation",
    "retrieve": {
        "knowledge_base_id": "KBTEST123",
        "number_of_results": 5,
        "search_type": "SEMANTIC",
    },
}


class FakeS3:
    def __init__(self):
        self.upload_args = None

    def upload_file(self, filename, bucket, key):
        self.upload_args = (filename, bucket, key)


class FakeBedrock:
    def __init__(self):
        self.create_kwargs = None

    def create_evaluation_job(self, **kwargs):
        self.create_kwargs = kwargs
        return {"jobArn": "arn:aws:bedrock:us-west-2:123456789012:evaluation-job/xyz"}


def test_run_retrieve_only_eval_wires_end_to_end(tmp_path):
    s3, bedrock = FakeS3(), FakeBedrock()
    jsonl_path = tmp_path / "out.jsonl"

    job_arn = run_retrieve_only_eval(
        _SAMPLE_CSV,
        "gavilan-retrieve-smoke",
        config=CONFIG,
        jsonl_path=jsonl_path,
        s3_client=s3,
        bedrock_client=bedrock,
    )

    assert job_arn.endswith("evaluation-job/xyz")

    # JSONL was written with one line per sample row (the sample CSV has 3).
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert len(json.loads(lines[0])["conversationTurns"]) == 1

    # Uploaded under the retrieve prefix with the job name.
    filename, bucket, key = s3.upload_args
    assert bucket == "test-eval-bucket"
    assert key == "eval/input/retrieve/gavilan-retrieve-smoke.jsonl"

    # The submitted job is a retrieve-only job pointing at the configured KB.
    req = bedrock.create_kwargs
    assert req["jobName"] == "gavilan-retrieve-smoke"
    assert req["outputDataConfig"] == {"s3Uri": "s3://test-eval-bucket/eval/output"}
    dmc = req["evaluationConfig"]["automated"]["datasetMetricConfigs"][0]
    assert dmc["taskType"] == "General"
    assert dmc["metricNames"] == ["Builtin.ContextCoverage", "Builtin.ContextRelevance"]
    assert dmc["dataset"]["datasetLocation"]["s3Uri"] == (
        "s3://test-eval-bucket/eval/input/retrieve/gavilan-retrieve-smoke.jsonl"
    )
    assert req["inferenceConfig"]["ragConfigs"][0]["knowledgeBaseConfig"][
        "retrieveConfig"
    ]["knowledgeBaseId"] == "KBTEST123"
