import json

import pytest

import format_generate
from dataset_loader import QAPair
from format_generate import (
    BASE_METRIC_NAMES,
    CITATION_METRIC_NAMES,
    RETRIEVED_PASSAGES_KEY,
    CapturedOutput,
    Citation,
    RetrievedPassage,
    build_inference_config,
    build_spec,
    has_citations,
    select_metrics,
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
    "generate": {
        "rag_source_identifier": "gavilan-bot-v1",
        "bot_api_url": "https://example.test/query",
    },
}

PAIRS = [
    QAPair(question="What are the hours?", reference_answer="Open 9 to 5.", source="s1"),
    QAPair(question="How long is checkout?", reference_answer="Three weeks."),
]


def _captured_with_citations():
    return [
        CapturedOutput(
            answer="The library is open 9 to 5.",
            passages=[
                RetrievedPassage(text="Hours: 9-5", name="hours", metadata={"src": "web"}),
            ],
            citations=[
                Citation(
                    text="open 9 to 5",
                    start=16,
                    end=27,
                    references=[RetrievedPassage(text="Hours: 9-5")],
                )
            ],
        ),
        CapturedOutput(
            answer="Checkout is three weeks.",
            passages=[RetrievedPassage(text="Checkout: 3 weeks")],
            citations=[
                Citation(
                    text="three weeks",
                    start=12,
                    end=23,
                    references=[RetrievedPassage(text="Checkout: 3 weeks")],
                )
            ],
        ),
    ]


def _captured_without_citations():
    return [
        CapturedOutput(answer="Open 9 to 5.", passages=[RetrievedPassage(text="Hours: 9-5")]),
        CapturedOutput(answer="Three weeks.", passages=[RetrievedPassage(text="3 weeks")]),
    ]


def test_record_shape_with_citations():
    captured = _captured_with_citations()[0]
    record = to_conversation_turn_record(PAIRS[0], captured, "gavilan-bot-v1")

    assert len(record["conversationTurns"]) == 1
    turn = record["conversationTurns"][0]
    assert turn["prompt"] == {"content": [{"text": "What are the hours?"}]}
    assert turn["referenceResponses"] == [{"content": [{"text": "Open 9 to 5."}]}]

    output = turn["output"]
    assert output["text"] == "The library is open 9 to 5."
    assert output["knowledgeBaseIdentifier"] == "gavilan-bot-v1"
    assert output[RETRIEVED_PASSAGES_KEY] == {
        "retrievalResults": [
            {"content": {"text": "Hours: 9-5"}, "name": "hours", "metadata": {"src": "web"}}
        ]
    }
    citation = output["citations"][0]
    assert citation["generatedResponsePart"]["textResponsePart"] == {
        "span": {"start": 16, "end": 27},
        "text": "open 9 to 5",
    }
    assert citation["retrievedReferences"] == [{"content": {"text": "Hours: 9-5"}}]


def test_passage_optional_fields_omitted():
    captured = CapturedOutput(answer="A", passages=[RetrievedPassage(text="p")])
    record = to_conversation_turn_record(PAIRS[0], captured, "id1")
    result = record["conversationTurns"][0]["output"][RETRIEVED_PASSAGES_KEY][
        "retrievalResults"
    ][0]
    assert result == {"content": {"text": "p"}}  # no name / metadata keys


def test_absent_citations_get_dummy_but_still_present():
    captured = _captured_without_citations()[0]
    record = to_conversation_turn_record(PAIRS[0], captured, "id1")
    citations = record["conversationTurns"][0]["output"]["citations"]
    assert len(citations) == 1
    ref_text = citations[0]["retrievedReferences"][0]["content"]["text"]
    assert "PLACEHOLDER" in ref_text


def test_knowledge_base_identifier_matches_rag_source():
    # The per-line identifier must equal the inferenceConfig ragSourceIdentifier.
    record = to_conversation_turn_record(PAIRS[0], _captured_with_citations()[0], "gavilan-bot-v1")
    line_id = record["conversationTurns"][0]["output"]["knowledgeBaseIdentifier"]
    ic = build_inference_config(CONFIG)
    config_id = ic["ragConfigs"][0]["precomputedRagSourceConfig"][
        "retrieveAndGenerateSourceConfig"
    ]["ragSourceIdentifier"]
    assert line_id == config_id == "gavilan-bot-v1"


def test_has_citations_all_or_nothing():
    assert has_citations(_captured_with_citations()) is True
    assert has_citations(_captured_without_citations()) is False
    # Mixed -> treated as absent.
    mixed = [_captured_with_citations()[0], _captured_without_citations()[0]]
    assert has_citations(mixed) is False


def test_select_metrics_includes_citation_only_when_present():
    with_c = select_metrics(_captured_with_citations())
    without_c = select_metrics(_captured_without_citations())
    assert with_c == BASE_METRIC_NAMES + CITATION_METRIC_NAMES
    assert without_c == BASE_METRIC_NAMES
    assert "Builtin.CitationCoverage" not in without_c
    assert "Builtin.CitationPrecision" not in without_c


def test_base_metrics_are_expected_set():
    assert BASE_METRIC_NAMES == [
        "Builtin.Correctness",
        "Builtin.Completeness",
        "Builtin.Faithfulness",
        "Builtin.Helpfulness",
        "Builtin.Harmfulness",
    ]


def test_inference_config_shape():
    assert build_inference_config(CONFIG) == {
        "ragConfigs": [
            {
                "precomputedRagSourceConfig": {
                    "retrieveAndGenerateSourceConfig": {
                        "ragSourceIdentifier": "gavilan-bot-v1"
                    }
                }
            }
        ]
    }


def test_write_jsonl_single_turn_and_line_count(tmp_path):
    out = write_jsonl(PAIRS, _captured_with_citations(), tmp_path / "rng.jsonl", "gavilan-bot-v1")
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert len(record["conversationTurns"]) == 1
        assert "output" in record["conversationTurns"][0]


def test_write_jsonl_length_mismatch_raises(tmp_path):
    with pytest.raises(ValueError) as exc:
        write_jsonl(PAIRS, _captured_with_citations()[:1], tmp_path / "x.jsonl", "id")
    assert "same length" in str(exc.value)


def test_assert_single_turn_rejects_multi_turn():
    bad = {"conversationTurns": [{}, {}]}
    with pytest.raises(ValueError):
        format_generate._assert_single_turn(bad)


def test_spec_with_citations_accepted_by_shared_runner():
    spec = build_spec("gavilan-rng-001", "s3://b/in/d.jsonl", CONFIG, _captured_with_citations())
    request = build_create_job_request(spec, CONFIG)
    dmc = request["evaluationConfig"]["automated"]["datasetMetricConfigs"][0]
    assert dmc["taskType"] == "General"
    assert dmc["metricNames"] == BASE_METRIC_NAMES + CITATION_METRIC_NAMES
    assert request["inferenceConfig"]["ragConfigs"][0]["precomputedRagSourceConfig"][
        "retrieveAndGenerateSourceConfig"
    ]["ragSourceIdentifier"] == "gavilan-bot-v1"


def test_spec_without_citations_drops_citation_metrics():
    spec = build_spec("gavilan-rng-002", "s3://b/in/d.jsonl", CONFIG, _captured_without_citations())
    request = build_create_job_request(spec, CONFIG)
    dmc = request["evaluationConfig"]["automated"]["datasetMetricConfigs"][0]
    assert dmc["metricNames"] == BASE_METRIC_NAMES
