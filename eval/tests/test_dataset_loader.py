from pathlib import Path

import pytest

from dataset_loader import QAPair, load_qa_csv

_SAMPLE_CSV = Path(__file__).resolve().parents[1] / "datasets" / "sample_qa.csv"


def _write(tmp_path, text):
    p = tmp_path / "qa.csv"
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_sample_csv():
    pairs = load_qa_csv(_SAMPLE_CSV)
    assert len(pairs) == 3
    assert all(isinstance(p, QAPair) for p in pairs)
    assert pairs[0].question == "What are the library's hours?"
    assert pairs[0].reference_answer.startswith("The Gavilan College Library is open")
    # The optional source column is captured.
    assert pairs[0].source and pairs[0].source.startswith("SAMPLE")


def test_notes_column_used_when_source_absent(tmp_path):
    csv_text = (
        "question,reference_answer,notes\n"
        "Q1,A1,from the FAQ page\n"
    )
    pairs = load_qa_csv(_write(tmp_path, csv_text))
    assert pairs == [QAPair(question="Q1", reference_answer="A1", source="from the FAQ page")]


def test_source_optional(tmp_path):
    csv_text = "question,reference_answer\nQ1,A1\n"
    pairs = load_qa_csv(_write(tmp_path, csv_text))
    assert pairs[0].source is None


def test_blank_rows_skipped(tmp_path):
    csv_text = (
        "question,reference_answer,source\n"
        "Q1,A1,s1\n"
        ",,\n"
        "Q2,A2,\n"
    )
    pairs = load_qa_csv(_write(tmp_path, csv_text))
    assert [p.question for p in pairs] == ["Q1", "Q2"]
    assert pairs[1].source is None


def test_missing_required_column_raises(tmp_path):
    csv_text = "question,notes\nQ1,n1\n"
    with pytest.raises(ValueError) as exc:
        load_qa_csv(_write(tmp_path, csv_text))
    assert "reference_answer" in str(exc.value)


def test_half_filled_row_raises(tmp_path):
    csv_text = "question,reference_answer\nQ1,\n"
    with pytest.raises(ValueError) as exc:
        load_qa_csv(_write(tmp_path, csv_text))
    assert "row 2" in str(exc.value)


def test_header_only_raises(tmp_path):
    csv_text = "question,reference_answer\n"
    with pytest.raises(ValueError) as exc:
        load_qa_csv(_write(tmp_path, csv_text))
    assert "no data rows" in str(exc.value)
