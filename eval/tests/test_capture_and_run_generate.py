from pathlib import Path

import pytest

from capture_outputs import capture_outputs
from dataset_loader import QAPair
from run_generate_eval import run_generate_eval

_SAMPLE_CSV = Path(__file__).resolve().parents[1] / "datasets" / "sample_qa.csv"

CONFIG = {
    "region": "us-west-2",
    "role_arn": "arn:aws:iam::123456789012:role/eval-role",
    "bucket": "test-eval-bucket",
    "input_prefix": "eval/input",
    "output_prefix": "eval/output",
    "evaluator_model_id": "us.anthropic.claude-3-5-sonnet-20240620-v1:0",
    "application_type": "RagEvaluation",
    "generate": {
        "rag_source_identifier": "gavilan-bot-v1",
        "bot_api_url": "https://TBD.example/query",
    },
}


def test_capture_outputs_is_a_stub():
    pairs = [QAPair(question="q", reference_answer="a")]
    with pytest.raises(NotImplementedError) as exc:
        capture_outputs(pairs, CONFIG)
    # The message points at what is missing so it is ready to implement later.
    assert "bot_api_url" in str(exc.value)


def test_run_generate_eval_hits_capture_stub_first():
    # Entrypoint imports/constructs cleanly; invoking it reaches the capture stub, proving
    # capture is the first (currently unimplemented) step. It must NOT reach S3/Bedrock.
    with pytest.raises(NotImplementedError):
        run_generate_eval(_SAMPLE_CSV, "gavilan-rng-smoke", config=CONFIG)
