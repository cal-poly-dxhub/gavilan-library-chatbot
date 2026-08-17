"""Entrypoint for a retrieve-and-generate RAG evaluation run.

Wires the REAL future flow: load Q&A CSV -> capture our bot's outputs -> format the R&G
BYOI JSONL -> upload -> submit via the shared runner.

"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union

from capture_outputs import capture_outputs
from config import load_eval_config
from dataset_loader import load_qa_csv
from format_generate import build_spec, write_jsonl
from runner import create_evaluation_job, upload_jsonl


def run_generate_eval(
    csv_path: Union[str, Path],
    job_name: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    jsonl_path: Optional[Union[str, Path]] = None,
    http_client: Any = None,
    s3_client: Any = None,
    bedrock_client: Any = None,
) -> str:
    """Run a retrieve-and-generate eval end to end. Returns the created job's ARN.

    csv_path      Q&A source CSV.
    job_name      unique Bedrock evaluation job name (also used for the S3 key).
    config        eval config dict; defaults to load_eval_config().
    jsonl_path    where to write the formatted JSONL; defaults to a temp file.
    http_client / s3_client / bedrock_client are injectable for testing.

    NOTE: capture_outputs is a stub until the bot is deployed; this call raises
    NotImplementedError there today.
    """
    config = config if config is not None else load_eval_config()

    pairs = load_qa_csv(csv_path)

    # The answer-quality eval scores OUR bot's real outputs, so capture comes first.
    captured = capture_outputs(pairs, config, http_client=http_client)

    rag_source_identifier = config["generate"]["rag_source_identifier"]
    if jsonl_path is None:
        jsonl_path = Path(tempfile.gettempdir()) / f"{job_name}-generate.jsonl"
    dataset_file = write_jsonl(pairs, captured, jsonl_path, rag_source_identifier)

    key = f"{config['input_prefix']}/generate/{job_name}.jsonl"
    dataset_s3_uri = upload_jsonl(dataset_file, config, key, s3_client=s3_client)

    spec = build_spec(job_name, dataset_s3_uri, config, captured)
    return create_evaluation_job(spec, config, bedrock_client=bedrock_client)


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run a retrieve-and-generate RAG evaluation job."
    )
    parser.add_argument("csv_path", help="Path to the Q&A source CSV.")
    parser.add_argument("job_name", help="Unique Bedrock evaluation job name.")
    args = parser.parse_args()

    job_arn = run_generate_eval(args.csv_path, args.job_name)
    print(f"Started retrieve-and-generate evaluation job: {job_arn}")


if __name__ == "__main__":
    _main()
