import json

import pytest

import format_retrieve
from dataset_loader import QAPair
from format_retrieve import (
    METRIC_NAMES,
    TASK_TYPE,
    build_inference_config,
    build_spec,
    to_conversation_turn_record,
    write_jsonl,
)
from runner import build_create_job_request

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

PAIRS = [
    QAPair(question="What are the hours?", reference_answer="Open 9 to 5.", source="s1"),
    QAPair(question="How long is checkout?", reference_answer="Three weeks."),
]


def test_conversation_turn_record_shape():
    record = to_conversation_turn_record(PAIRS[0])
    assert record == {
        "conversationTurns": [
            {
                "prompt": {"content": [{"text": "What are the hours?"}]},
                "referenceResponses": [{"content": [{"text": "Open 9 to 5."}]}],
            }
        ]
    }
    # Single turn only, and no BYOIR-only keys leak in.
    turn = record["conversationTurns"][0]
    assert len(record["conversationTurns"]) == 1
    assert "referenceContexts" not in turn
    assert "output" not in turn


def test_write_jsonl_one_object_per_line(tmp_path):
    out = write_jsonl(PAIRS, tmp_path / "retrieve.jsonl")
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line, pair in zip(lines, PAIRS):
        record = json.loads(line)  # each line is valid JSON
        assert len(record["conversationTurns"]) == 1
        turn = record["conversationTurns"][0]
        assert turn["prompt"]["content"][0]["text"] == pair.question
        assert turn["referenceResponses"][0]["content"][0]["text"] == pair.reference_answer


def test_write_jsonl_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        write_jsonl([], tmp_path / "empty.jsonl")


def test_write_jsonl_rejects_over_1000(tmp_path):
    many = [QAPair(question=f"q{i}", reference_answer=f"a{i}") for i in range(1001)]
    with pytest.raises(ValueError) as exc:
        write_jsonl(many, tmp_path / "big.jsonl")
    assert "1000" in str(exc.value)


def test_assert_single_turn_rejects_multi_turn():
    bad = {
        "conversationTurns": [
            {"prompt": {"content": [{"text": "q1"}]},
             "referenceResponses": [{"content": [{"text": "a1"}]}]},
            {"prompt": {"content": [{"text": "q2"}]},
             "referenceResponses": [{"content": [{"text": "a2"}]}]},
        ]
    }
    with pytest.raises(ValueError):
        format_retrieve._assert_single_turn(bad)


def test_build_inference_config_shape():
    ic = build_inference_config(CONFIG)
    assert ic == {
        "ragConfigs": [
            {
                "knowledgeBaseConfig": {
                    "retrieveConfig": {
                        "knowledgeBaseId": "KBTEST123",
                        "knowledgeBaseRetrievalConfiguration": {
                            "vectorSearchConfiguration": {
                                "numberOfResults": 5,
                                "overrideSearchType": "SEMANTIC",
                            }
                        },
                    }
                }
            }
        ]
    }


def test_inference_config_omits_search_type_when_absent():
    cfg = dict(CONFIG)
    cfg["retrieve"] = {"knowledge_base_id": "KB1", "number_of_results": 3}
    ic = build_inference_config(cfg)
    vsc = ic["ragConfigs"][0]["knowledgeBaseConfig"]["retrieveConfig"][
        "knowledgeBaseRetrievalConfiguration"
    ]["vectorSearchConfiguration"]
    assert vsc == {"numberOfResults": 3}


def test_build_spec_uses_retrieve_task_and_metrics():
    spec = build_spec("gavilan-retrieve-001", "s3://b/in/d.jsonl", CONFIG)
    assert spec.task_type == TASK_TYPE == "General"
    assert spec.metric_names == ["Builtin.ContextCoverage", "Builtin.ContextRelevance"]
    assert spec.metric_names == METRIC_NAMES
    assert spec.dataset_s3_uri == "s3://b/in/d.jsonl"
    assert spec.inference_config == build_inference_config(CONFIG)


def test_spec_is_accepted_by_shared_runner():
    # The formatter output must slot into the shared runner without extra work.
    spec = build_spec("gavilan-retrieve-001", "s3://b/in/d.jsonl", CONFIG)
    request = build_create_job_request(spec, CONFIG)
    dmc = request["evaluationConfig"]["automated"]["datasetMetricConfigs"][0]
    assert dmc["taskType"] == "General"
    assert dmc["metricNames"] == ["Builtin.ContextCoverage", "Builtin.ContextRelevance"]
    assert request["inferenceConfig"]["ragConfigs"][0]["knowledgeBaseConfig"][
        "retrieveConfig"
    ]["knowledgeBaseId"] == "KBTEST123"
    assert request["applicationType"] == "RagEvaluation"
