"""Shared boto3 job-runner for Amazon Bedrock native RAG evaluation.

The machinery both eval types reuse. Deliberately generic: the caller passes an
`EvaluationJobSpec` whose `task_type`, `metric_names`, and `inference_config` are built by
the type-specific formatters.

All AWS-account-specific values (role ARN, bucket, region, evaluator model) come from
eval_config.yaml.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import boto3

# Terminal statuses returned by get_evaluation_job.
TERMINAL_STATUSES = frozenset({"Completed", "Failed", "Stopped"})


@dataclass
class EvaluationJobSpec:
    """Type-agnostic description of one Bedrock evaluation job.

    The type-specific formatters fill task_type, metric_names, and inference_config
    appropriately; everything else is generic.
    """

    job_name: str
    dataset_s3_uri: str
    task_type: str
    metric_names: List[str]
    inference_config: Dict[str, Any]
    dataset_name: str = "RagDataset"
    job_description: Optional[str] = None


def s3_uri(bucket: str, *parts: str) -> str:
    """Join a bucket and key parts into an s3:// URI."""
    key = "/".join(p.strip("/") for p in parts if p)
    return f"s3://{bucket}/{key}" if key else f"s3://{bucket}"


def build_create_job_request(
    spec: EvaluationJobSpec, config: Dict[str, Any]
) -> Dict[str, Any]:
    """Assemble the create_evaluation_job kwargs.

    Kept separate from the API call so tests can assert the request shape offline.
    """
    request: Dict[str, Any] = {
        "jobName": spec.job_name,
        "roleArn": config["role_arn"],
        "applicationType": config.get("application_type", "RagEvaluation"),
        "inferenceConfig": spec.inference_config,
        "outputDataConfig": {"s3Uri": s3_uri(config["bucket"], config["output_prefix"])},
        "evaluationConfig": {
            "automated": {
                "datasetMetricConfigs": [
                    {
                        "taskType": spec.task_type,
                        "dataset": {
                            "name": spec.dataset_name,
                            "datasetLocation": {"s3Uri": spec.dataset_s3_uri},
                        },
                        "metricNames": spec.metric_names,
                    }
                ],
                "evaluatorModelConfig": {
                    "bedrockEvaluatorModels": [
                        {"modelIdentifier": config["evaluator_model_id"]}
                    ]
                },
            }
        },
    }
    if spec.job_description:
        request["jobDescription"] = spec.job_description
    return request


def _s3_client(config: Dict[str, Any], client: Any = None) -> Any:
    return client if client is not None else boto3.client(
        "s3", region_name=config.get("region")
    )


def _bedrock_client(config: Dict[str, Any], client: Any = None) -> Any:
    return client if client is not None else boto3.client(
        "bedrock", region_name=config.get("region")
    )


def upload_jsonl(
    local_path: Union[str, Path],
    config: Dict[str, Any],
    key: str,
    s3_client: Any = None,
) -> str:
    """Upload a local JSONL file to the eval bucket. Returns its s3:// URI."""
    local_path = Path(local_path)
    bucket = config["bucket"]
    client = _s3_client(config, s3_client)
    client.upload_file(str(local_path), bucket, key)
    return s3_uri(bucket, key)


def create_evaluation_job(
    spec: EvaluationJobSpec, config: Dict[str, Any], bedrock_client: Any = None
) -> str:
    """Start a Bedrock evaluation job. Returns the created job's ARN."""
    request = build_create_job_request(spec, config)
    client = _bedrock_client(config, bedrock_client)
    response = client.create_evaluation_job(**request)
    return response["jobArn"]


def get_job_status(
    job_identifier: str, config: Dict[str, Any], bedrock_client: Any = None
) -> str:
    """Return the current status of an evaluation job."""
    client = _bedrock_client(config, bedrock_client)
    return client.get_evaluation_job(jobIdentifier=job_identifier)["status"]


def poll_until_complete(
    job_identifier: str,
    config: Dict[str, Any],
    bedrock_client: Any = None,
    interval_seconds: int = 30,
    timeout_seconds: int = 3600,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Poll until the job reaches a terminal status. Returns that status.

    Raises TimeoutError if the job is still running after timeout_seconds. `sleep` is
    injectable so tests run instantly.
    """
    client = _bedrock_client(config, bedrock_client)
    elapsed = 0
    while True:
        status = client.get_evaluation_job(jobIdentifier=job_identifier)["status"]
        if status in TERMINAL_STATUSES:
            return status
        if elapsed >= timeout_seconds:
            raise TimeoutError(
                f"Evaluation job {job_identifier} still '{status}' after "
                f"{timeout_seconds}s."
            )
        sleep(interval_seconds)
        elapsed += interval_seconds


def get_results_location(
    job_identifier: str, config: Dict[str, Any], bedrock_client: Any = None
) -> str:
    """Return the S3 URI where Bedrock wrote the job's results."""
    client = _bedrock_client(config, bedrock_client)
    job = client.get_evaluation_job(jobIdentifier=job_identifier)
    return job["outputDataConfig"]["s3Uri"]
