"""Entrypoint for a retrieve-only RAG evaluation run.

Wires the pieces together: load Q&A CSV -> format retrieve-only JSONL -> upload -> submit
via the shared runner.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union

from config import load_eval_config
from dataset_loader import load_qa_csv
from format_retrieve import build_spec, write_jsonl
from runner import create_evaluation_job, upload_jsonl


def run_retrieve_only_eval(
    csv_path: Union[str, Path],
    job_name: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    jsonl_path: Optional[Union[str, Path]] = None,
    s3_client: Any = None,
    bedrock_client: Any = None,
) -> str:
    """Run a retrieve-only eval end to end. Returns the created job's ARN.

    csv_path     Q&A source CSV.
    job_name     unique Bedrock evaluation job name (also used for the S3 key).
    config       eval config dict; defaults to load_eval_config().
    jsonl_path   where to write the formatted JSONL; defaults to a temp file.
    s3_client / bedrock_client are injectable for testing.
    """
    config = config if config is not None else load_eval_config()

    pairs = load_qa_csv(csv_path)

    if jsonl_path is None:
        jsonl_path = Path(tempfile.gettempdir()) / f"{job_name}-retrieve.jsonl"
    dataset_file = write_jsonl(pairs, jsonl_path)

    key = f"{config['input_prefix']}/retrieve/{job_name}.jsonl"
    dataset_s3_uri = upload_jsonl(dataset_file, config, key, s3_client=s3_client)

    spec = build_spec(job_name, dataset_s3_uri, config)
    return create_evaluation_job(spec, config, bedrock_client=bedrock_client)


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run a retrieve-only RAG evaluation job.")
    parser.add_argument("csv_path", help="Path to the Q&A source CSV.")
    parser.add_argument("job_name", help="Unique Bedrock evaluation job name.")
    args = parser.parse_args()

    job_arn = run_retrieve_only_eval(args.csv_path, args.job_name)
    print(f"Started retrieve-only evaluation job: {job_arn}")


if __name__ == "__main__":
    _main()
