import pytest

import runner
from config import load_eval_config
from runner import (
    EvaluationJobSpec,
    build_create_job_request,
    create_evaluation_job,
    get_results_location,
    poll_until_complete,
    s3_uri,
    upload_jsonl,
)

# A config dict shaped like load_eval_config() output, with no real AWS values.
CONFIG = {
    "region": "us-west-2",
    "role_arn": "arn:aws:iam::123456789012:role/eval-role",
    "bucket": "test-eval-bucket",
    "input_prefix": "eval/input",
    "output_prefix": "eval/output",
    "evaluator_model_id": "us.anthropic.claude-3-5-sonnet-20240620-v1:0",
    "application_type": "RagEvaluation",
}

# A representative retrieve-and-generate inference_config (a formatter builds this later).
INFERENCE_CONFIG = {
    "ragConfigs": [
        {
            "precomputedRagSourceConfig": {
                "retrieveAndGenerateSourceConfig": {
                    "ragSourceIdentifier": "gavilan-rag"
                }
            }
        }
    ]
}


def _spec(**overrides):
    base = dict(
        job_name="gavilan-eval-001",
        dataset_s3_uri="s3://test-eval-bucket/eval/input/rag.jsonl",
        task_type="QuestionAndAnswer",
        metric_names=["Builtin.Correctness", "Builtin.Faithfulness"],
        inference_config=INFERENCE_CONFIG,
    )
    base.update(overrides)
    return EvaluationJobSpec(**base)


class FakeBedrock:
    def __init__(self, statuses=None, output_uri=None):
        self.create_kwargs = None
        self._statuses = list(statuses or [])
        self._output_uri = output_uri
        self.get_calls = 0

    def create_evaluation_job(self, **kwargs):
        self.create_kwargs = kwargs
        return {"jobArn": "arn:aws:bedrock:us-west-2:123456789012:evaluation-job/abc"}

    def get_evaluation_job(self, jobIdentifier):
        self.get_calls += 1
        status = self._statuses.pop(0) if self._statuses else "Completed"
        resp = {"status": status}
        if self._output_uri is not None:
            resp["outputDataConfig"] = {"s3Uri": self._output_uri}
        return resp


class FakeS3:
    def __init__(self):
        self.upload_args = None

    def upload_file(self, filename, bucket, key):
        self.upload_args = (filename, bucket, key)


def test_s3_uri_join():
    assert s3_uri("b", "a/", "/c") == "s3://b/a/c"
    assert s3_uri("b") == "s3://b"


def test_build_request_shape():
    request = build_create_job_request(_spec(), CONFIG)
    assert request["jobName"] == "gavilan-eval-001"
    assert request["roleArn"] == CONFIG["role_arn"]
    assert request["applicationType"] == "RagEvaluation"
    # inference_config is passed through verbatim (formatter owns its shape).
    assert request["inferenceConfig"] is INFERENCE_CONFIG
    assert request["outputDataConfig"] == {"s3Uri": "s3://test-eval-bucket/eval/output"}

    dmc = request["evaluationConfig"]["automated"]["datasetMetricConfigs"][0]
    assert dmc["taskType"] == "QuestionAndAnswer"
    assert dmc["dataset"] == {
        "name": "RagDataset",
        "datasetLocation": {"s3Uri": "s3://test-eval-bucket/eval/input/rag.jsonl"},
    }
    assert dmc["metricNames"] == ["Builtin.Correctness", "Builtin.Faithfulness"]
    assert request["evaluationConfig"]["automated"]["evaluatorModelConfig"] == {
        "bedrockEvaluatorModels": [
            {"modelIdentifier": CONFIG["evaluator_model_id"]}
        ]
    }


def test_build_request_omits_description_by_default():
    assert "jobDescription" not in build_create_job_request(_spec(), CONFIG)


def test_build_request_includes_description_when_set():
    request = build_create_job_request(_spec(job_description="nightly"), CONFIG)
    assert request["jobDescription"] == "nightly"


def test_application_type_defaults_when_absent():
    cfg = {k: v for k, v in CONFIG.items() if k != "application_type"}
    assert build_create_job_request(_spec(), cfg)["applicationType"] == "RagEvaluation"


def test_create_evaluation_job_calls_client_with_built_request():
    fake = FakeBedrock()
    job_arn = create_evaluation_job(_spec(), CONFIG, bedrock_client=fake)
    assert job_arn.endswith("evaluation-job/abc")
    assert fake.create_kwargs == build_create_job_request(_spec(), CONFIG)


def test_upload_jsonl_returns_uri_and_calls_s3():
    fake = FakeS3()
    uri = upload_jsonl("/tmp/rag.jsonl", CONFIG, "eval/input/rag.jsonl", s3_client=fake)
    assert uri == "s3://test-eval-bucket/eval/input/rag.jsonl"
    assert fake.upload_args == ("/tmp/rag.jsonl", "test-eval-bucket", "eval/input/rag.jsonl")


def test_poll_until_complete_returns_terminal_status():
    fake = FakeBedrock(statuses=["InProgress", "InProgress", "Completed"])
    calls = []
    status = poll_until_complete(
        "job-1", CONFIG, bedrock_client=fake, interval_seconds=5, sleep=calls.append
    )
    assert status == "Completed"
    assert fake.get_calls == 3
    assert calls == [5, 5]  # slept between the two non-terminal polls


def test_poll_until_complete_times_out():
    fake = FakeBedrock(statuses=["InProgress", "InProgress", "InProgress"])
    with pytest.raises(TimeoutError):
        poll_until_complete(
            "job-1",
            CONFIG,
            bedrock_client=fake,
            interval_seconds=5,
            timeout_seconds=0,
            sleep=lambda _: None,
        )


def test_get_results_location():
    fake = FakeBedrock(output_uri="s3://test-eval-bucket/eval/output/job-1")
    assert get_results_location("job-1", CONFIG, bedrock_client=fake) == (
        "s3://test-eval-bucket/eval/output/job-1"
    )


def test_load_eval_config_reads_real_file():
    # Uses the actual eval/eval_config.yaml (requires PyYAML).
    cfg = load_eval_config()
    for key in ("region", "role_arn", "bucket", "input_prefix", "output_prefix",
                "evaluator_model_id", "application_type"):
        assert key in cfg
