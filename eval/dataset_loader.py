"""Read Q&A CSV into an internal representation.

This is the shared input side. The type-specific JSONL formatters (retrieve-only and
retrieve-and-generate) consume the `QAPair` list this produces.

CSV format (one row per question):
  question         (required) the user question.
  reference_answer (required) the expected end-to-end ANSWER. This maps to Bedrock's
                   `referenceResponses`, NOT expected retrieved passages.
  source | notes   (optional) provenance or notes for humans; kept but not sent to Bedrock.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

QUESTION_COLUMN = "question"
REFERENCE_ANSWER_COLUMN = "reference_answer"
# First one present wins; both are treated as the optional human-facing note.
OPTIONAL_SOURCE_COLUMNS = ("source", "notes")


@dataclass(frozen=True)
class QAPair:
    """One evaluation question and its expected end-to-end answer."""

    question: str
    reference_answer: str
    source: Optional[str] = None


def load_qa_csv(path: Union[str, Path]) -> List[QAPair]:
    """Parse a Q&A CSV into a list of QAPair.

    Raises ValueError on a missing header, missing required columns, or a row that has
    one of question/reference_answer but not the other. Fully blank rows are skipped.
    """
    path = Path(path)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is empty (no header row).")

        fields = {name.strip() for name in reader.fieldnames}
        missing = {QUESTION_COLUMN, REFERENCE_ANSWER_COLUMN} - fields
        if missing:
            raise ValueError(
                f"{path} missing required column(s): {sorted(missing)}. "
                f"Found columns: {sorted(fields)}."
            )
        source_column = next(
            (c for c in OPTIONAL_SOURCE_COLUMNS if c in fields), None
        )

        pairs: List[QAPair] = []
        # start=2 because row 1 is the header, so error messages match the file.
        for line_number, row in enumerate(reader, start=2):
            question = (row.get(QUESTION_COLUMN) or "").strip()
            reference_answer = (row.get(REFERENCE_ANSWER_COLUMN) or "").strip()

            if not question and not reference_answer:
                continue  # fully blank row

            if not question or not reference_answer:
                raise ValueError(
                    f"{path} row {line_number}: both '{QUESTION_COLUMN}' and "
                    f"'{REFERENCE_ANSWER_COLUMN}' are required."
                )

            source = None
            if source_column:
                source = (row.get(source_column) or "").strip() or None

            pairs.append(
                QAPair(
                    question=question,
                    reference_answer=reference_answer,
                    source=source,
                )
            )

        if not pairs:
            raise ValueError(f"{path} has a header but no data rows.")
        return pairs
