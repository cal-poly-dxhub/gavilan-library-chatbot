"""Retrieve-and-generate formatter for Bedrock native RAG evaluation.

The answer-quality evaluator. Unlike retrieve-only (which points at the live KB), this
uses BRING-YOUR-OWN-INFERENCE: it scores OUR bot's actual outputs, not the KB's built-in
RetrieveAndGenerate. 


"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from dataset_loader import QAPair
from runner import EvaluationJobSpec

TASK_TYPE = "General"
# Answer-quality metrics always applied.
BASE_METRIC_NAMES = [
    "Builtin.Correctness",
    "Builtin.Completeness",
    "Builtin.Faithfulness",
    "Builtin.Helpfulness",
    "Builtin.Harmfulness",
]
# Added only when captured outputs carry citations.
CITATION_METRIC_NAMES = ["Builtin.CitationCoverage", "Builtin.CitationPrecision"]

# R&G allows up to 5 conversation turns; v1 datasets are single-turn.
MAX_TURNS = 5

# See NAMING CAVEAT in the module docstring.
RETRIEVED_PASSAGES_KEY = "retrievedPassages"


# --- Shared "captured output" types (capture stage produces, formatter consumes) --------


@dataclass(frozen=True)
class RetrievedPassage:
    """One passage the bot retrieved (or a citation's supporting reference)."""

    text: str
    name: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class Citation:
    """Links a span of the generated answer to the passages that support it."""

    text: str  # the answer span text
    start: int  # character span start in the generated answer
    end: int  # character span end
    references: List[RetrievedPassage] = field(default_factory=list)


@dataclass(frozen=True)
class CapturedOutput:
    """What our bot returned for one question. citations is optional (may be None)."""

    answer: str
    passages: List[RetrievedPassage] = field(default_factory=list)
    citations: Optional[List[Citation]] = None


# --- Serialization ----------------------------------------------------------------------


def _passage_record(passage: RetrievedPassage) -> Dict[str, Any]:
    record: Dict[str, Any] = {"content": {"text": passage.text}}
    if passage.name is not None:
        record["name"] = passage.name
    if passage.metadata is not None:
        record["metadata"] = passage.metadata
    return record


def _citation_record(citation: Citation) -> Dict[str, Any]:
    return {
        "generatedResponsePart": {
            "textResponsePart": {
                "span": {"start": citation.start, "end": citation.end},
                "text": citation.text,
            }
        },
        "retrievedReferences": [_passage_record(r) for r in citation.references],
    }


def _dummy_citations(answer: str) -> List[Citation]:
    """Placeholder citations for outputs with none.

    Per the task decision, absent citations still get a (clearly marked) dummy so the JSONL
    is structurally complete; the citation METRICS are dropped for the job instead (see
    select_metrics). This is not scored.
    """
    return [
        Citation(
            text=answer,
            start=0,
            end=len(answer),
            references=[RetrievedPassage(text="PLACEHOLDER - no citation captured")],
        )
    ]


def to_conversation_turn_record(
    pair: QAPair, captured: CapturedOutput, rag_source_identifier: str
) -> Dict[str, Any]:
    """Build one R&G BYOI JSONL record (single conversation turn)."""
    citations = captured.citations if captured.citations else _dummy_citations(captured.answer)
    output: Dict[str, Any] = {
        "text": captured.answer,
        # Must match ragSourceIdentifier in the inferenceConfig.
        "knowledgeBaseIdentifier": rag_source_identifier,
        RETRIEVED_PASSAGES_KEY: {
            "retrievalResults": [_passage_record(p) for p in captured.passages]
        },
        "citations": [_citation_record(c) for c in citations],
    }
    return {
        "conversationTurns": [
            {
                "prompt": {"content": [{"text": pair.question}]},
                "referenceResponses": [
                    {"content": [{"text": pair.reference_answer}]}
                ],
                "output": output,
            }
        ]
    }


def _assert_single_turn(record: Dict[str, Any]) -> None:
    """v1 R&G datasets are single-turn (the schema allows up to MAX_TURNS)."""
    turns = record.get("conversationTurns")
    if not isinstance(turns, list) or len(turns) != 1:
        raise ValueError(
            "v1 retrieve-and-generate records must have exactly one conversation turn, "
            f"got {0 if not isinstance(turns, list) else len(turns)}."
        )


def has_citations(captured_outputs: Sequence[CapturedOutput]) -> bool:
    """True only if every captured output carries citations.

    Citation metrics are computed across the whole dataset, so they are included only when
    every line has real citations. If any is missing, citations are treated as absent for
    the job (metrics dropped; missing lines get dummy citations).
    """
    return bool(captured_outputs) and all(bool(c.citations) for c in captured_outputs)


def select_metrics(captured_outputs: Sequence[CapturedOutput]) -> List[str]:
    """Base metrics, plus citation metrics only when citations are present dataset-wide."""
    metrics = list(BASE_METRIC_NAMES)
    if has_citations(captured_outputs):
        metrics += CITATION_METRIC_NAMES
    return metrics


def write_jsonl(
    pairs: Sequence[QAPair],
    captured_outputs: Sequence[CapturedOutput],
    path: Union[str, Path],
    rag_source_identifier: str,
) -> Path:
    """Write the R&G BYOI JSONL dataset (one JSON object per line). Returns the path."""
    if not pairs:
        raise ValueError("No QAPairs to write; the dataset would be empty.")
    if len(pairs) != len(captured_outputs):
        raise ValueError(
            f"pairs ({len(pairs)}) and captured_outputs ({len(captured_outputs)}) "
            "must be the same length and aligned by question."
        )
    if len(pairs) > 1000:
        raise ValueError(
            f"RAG eval datasets allow up to 1000 prompts, got {len(pairs)}."
        )
    path = Path(path)
    with open(path, "w", encoding="utf-8") as f:
        for pair, captured in zip(pairs, captured_outputs):
            record = to_conversation_turn_record(pair, captured, rag_source_identifier)
            _assert_single_turn(record)
            f.write(json.dumps(record) + "\n")
    return path


def build_inference_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Build the R&G BYOI inferenceConfig (precomputed source) from eval_config."""
    return {
        "ragConfigs": [
            {
                "precomputedRagSourceConfig": {
                    "retrieveAndGenerateSourceConfig": {
                        "ragSourceIdentifier": config["generate"]["rag_source_identifier"]
                    }
                }
            }
        ]
    }


def build_spec(
    job_name: str,
    dataset_s3_uri: str,
    config: Dict[str, Any],
    captured_outputs: Sequence[CapturedOutput],
) -> EvaluationJobSpec:
    """Assemble the EvaluationJobSpec the shared runner consumes for an R&G job.

    Drops the citation metrics when the captured outputs lack citations.
    """
    return EvaluationJobSpec(
        job_name=job_name,
        dataset_s3_uri=dataset_s3_uri,
        task_type=TASK_TYPE,
        metric_names=select_metrics(captured_outputs),
        inference_config=build_inference_config(config),
        job_description="Retrieve-and-generate RAG eval (answer quality).",
    )
