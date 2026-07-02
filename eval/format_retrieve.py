"""Retrieve-only formatter for Bedrock native RAG evaluation.

Turns loaded QAPairs into a retrieve-only evaluation job: the KB-retrieval-quality
evaluator. It measures whether the Knowledge Base retrieves relevant, complete context
(Builtin.ContextRelevance / Builtin.ContextCoverage).

The formatter owns these type-specific pieces and hands a ready EvaluationJobSpec to the
shared runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence, Union

from dataset_loader import QAPair
from runner import EvaluationJobSpec

# Retrieve-only task type and metrics, per the canonical AWS retrieve-only examples.
TASK_TYPE = "General"
METRIC_NAMES = ["Builtin.ContextCoverage", "Builtin.ContextRelevance"]


def to_conversation_turn_record(pair: QAPair) -> Dict[str, Any]:
    """Build one retrieve-only JSONL record (a single conversation turn) from a QAPair."""
    return {
        "conversationTurns": [
            {
                "prompt": {"content": [{"text": pair.question}]},
                "referenceResponses": [
                    {"content": [{"text": pair.reference_answer}]}
                ],
            }
        ]
    }


def _assert_single_turn(record: Dict[str, Any]) -> None:
    """Retrieve-only allows exactly one conversation turn per line."""
    turns = record.get("conversationTurns")
    if not isinstance(turns, list) or len(turns) != 1:
        raise ValueError(
            "retrieve-only records must have exactly one conversation turn, got "
            f"{0 if not isinstance(turns, list) else len(turns)}."
        )


def write_jsonl(pairs: Sequence[QAPair], path: Union[str, Path]) -> Path:
    """Write the retrieve-only JSONL dataset (one JSON object per line). Returns the path."""
    if not pairs:
        raise ValueError("No QAPairs to write; the dataset would be empty.")
    if len(pairs) > 1000:
        raise ValueError(
            f"Retrieve-only datasets allow up to 1000 prompts, got {len(pairs)}."
        )
    path = Path(path)
    with open(path, "w", encoding="utf-8") as f:
        for pair in pairs:
            record = to_conversation_turn_record(pair)
            _assert_single_turn(record)
            f.write(json.dumps(record) + "\n")
    return path


def build_inference_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Build the retrieve-only inferenceConfig (live-KB Retrieve) from eval_config."""
    retrieve_cfg = config["retrieve"]
    vector_search: Dict[str, Any] = {
        "numberOfResults": retrieve_cfg["number_of_results"]
    }
    search_type = retrieve_cfg.get("search_type")
    if search_type:
        vector_search["overrideSearchType"] = search_type
    return {
        "ragConfigs": [
            {
                "knowledgeBaseConfig": {
                    "retrieveConfig": {
                        "knowledgeBaseId": retrieve_cfg["knowledge_base_id"],
                        "knowledgeBaseRetrievalConfiguration": {
                            "vectorSearchConfiguration": vector_search
                        },
                    }
                }
            }
        ]
    }


def build_spec(
    job_name: str, dataset_s3_uri: str, config: Dict[str, Any]
) -> EvaluationJobSpec:
    """Assemble the EvaluationJobSpec the shared runner consumes for a retrieve-only job."""
    return EvaluationJobSpec(
        job_name=job_name,
        dataset_s3_uri=dataset_s3_uri,
        task_type=TASK_TYPE,
        metric_names=list(METRIC_NAMES),
        inference_config=build_inference_config(config),
        job_description="Retrieve-only RAG eval (context relevance + coverage).",
    )
