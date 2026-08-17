"""Tests for the offline chunking simulator.

These pin the properties a decision would rest on: that a boundary genuinely lands where the
strategy says it does, that overlap is allowed to rescue a split span, that the embedding limit is
enforced rather than assumed, and that the evidence locator is honest about low-confidence rows.
"""

import pytest

from chunking import (
    EMBEDDING_TOKEN_LIMIT,
    Evidence,
    build_strategies,
    chunk_fixed_size,
    chunk_hierarchical,
    chunk_markdown_section,
    chunk_none,
    estimate_tokens,
    evaluate_strategy,
    locate_evidence,
    span_survives,
)
from dataset_loader import QAPair


def test_none_returns_the_whole_document_as_one_chunk():
    (chunk,) = chunk_none("d.md", "hello world")
    assert chunk.text == "hello world"
    assert (chunk.start, chunk.end) == (0, 11)


def test_fixed_size_respects_the_token_budget_and_never_splits_a_word():
    text = " ".join(f"word{i}" for i in range(500))
    chunks = chunk_fixed_size("d.md", text, max_tokens=100, overlap_percentage=0)

    assert len(chunks) > 1
    for c in chunks:
        assert c.tokens <= 100 + 1  # +1 for the ceil in estimate_tokens
        assert not c.text.startswith("ord")  # a mid-word split would leave a fragment
        assert not c.text.endswith("wor")


def test_fixed_size_overlap_repeats_text_between_neighbours():
    text = " ".join(f"w{i}" for i in range(400))
    no_overlap = chunk_fixed_size("d.md", text, max_tokens=50, overlap_percentage=0)
    overlapped = chunk_fixed_size("d.md", text, max_tokens=50, overlap_percentage=50)

    # Overlap costs more chunks for the same text - that is what buys span survival.
    assert len(overlapped) > len(no_overlap)
    assert overlapped[1].start < no_overlap[1].start


def test_hierarchical_reports_parent_boundaries_because_that_is_what_retrieval_returns():
    text = " ".join(f"w{i}" for i in range(2000))
    parents = chunk_hierarchical("d.md", text, parent_tokens=500, child_tokens=100)
    assert all(c.tokens <= 501 for c in parents)
    assert max(c.tokens for c in parents) > 100  # not the child size


def test_markdown_section_splits_on_headings():
    text = "# One\n\nalpha\n\n## Two\n\nbeta\n\n### Three\n\ngamma"
    chunks = chunk_markdown_section("d.md", text)
    assert len(chunks) == 3
    assert chunks[0].text.startswith("# One")
    assert chunks[1].text.startswith("## Two")


def test_markdown_section_falls_back_to_whole_file_when_there_are_no_headings():
    (chunk,) = chunk_markdown_section("d.md", "just prose, no headings at all")
    assert chunk.text == "just prose, no headings at all"


# --- Evidence integrity -----------------------------------------------------------------------


def _ev(start, end, doc="d.md", overlap=1.0):
    return Evidence(question="q", doc=doc, start=start, end=end, overlap=overlap)


def test_a_span_inside_one_chunk_survives():
    chunks = chunk_fixed_size("d.md", "x" * 4000, max_tokens=300, overlap_percentage=0)
    assert span_survives(_ev(10, 200), chunks)


def test_a_span_straddling_every_boundary_does_not_survive():
    # 300 tokens ~ 1200 chars with no overlap, so a 2000-char span cannot fit in any chunk.
    chunks = chunk_fixed_size("d.md", "x " * 4000, max_tokens=300, overlap_percentage=0)
    assert not span_survives(_ev(0, 2000), chunks)


def test_overlap_can_rescue_a_span_a_boundary_cuts():
    # THE reason span_survives checks ANY chunk rather than the nearest one: retrieval would
    # surface whichever chunk holds the answer whole, so counting the cut copy would overstate harm.
    text = "a " * 3000
    cut = chunk_fixed_size("d.md", text, max_tokens=100, overlap_percentage=0)
    rescued = chunk_fixed_size("d.md", text, max_tokens=100, overlap_percentage=75)
    span = _ev(390, 460)
    assert not span_survives(span, cut)
    assert span_survives(span, rescued)


def test_a_span_in_another_document_is_never_counted_as_surviving():
    chunks = chunk_none("other.md", "x" * 500)
    assert not span_survives(_ev(0, 100, doc="d.md"), chunks)


# --- Locating evidence ------------------------------------------------------------------------


def test_locate_evidence_finds_the_matching_passage():
    docs = {
        "about-the-library.md": ("filler " * 200)
        + "The Gilroy Library is open Monday through Thursday from 9 AM to 8 PM."
        + (" filler" * 200)
    }
    pair = QAPair(
        question="What are the hours?",
        reference_answer="Gilroy Library open Monday through Thursday 9 AM to 8 PM",
        source="about-the-library.php",
    )
    ev = locate_evidence(pair, docs)
    assert ev is not None and ev.confident
    assert "Monday through Thursday" in docs["about-the-library.md"][ev.start : ev.end]


def test_locate_evidence_flags_a_row_it_could_not_really_find():
    # An answer whose words appear nowhere gets a low-overlap span, and `confident` says so - the
    # report surfaces that count rather than letting a guessed span read as a measurement.
    docs = {"about-the-library.md": "unrelated text about parking permits and shuttle routes"}
    pair = QAPair(
        question="q",
        reference_answer="interlibrary loan requests take one to two weeks",
        source="about-the-library.php",
    )
    ev = locate_evidence(pair, docs)
    assert ev is not None and not ev.confident


def test_locate_evidence_returns_none_when_the_source_is_unknown():
    docs = {"about-the-library.md": "text"}
    assert locate_evidence(QAPair("q", "a", source=None), docs) is None
    assert locate_evidence(QAPair("q", "a", source="nope.php"), docs) is None


# --- Report -----------------------------------------------------------------------------------


def test_evaluate_strategy_counts_splits_and_flags_oversized_chunks():
    # One document far over the embedding limit: NONE cannot ingest it, which is a hard failure
    # rather than a quality preference.
    docs = {"d.md": "x" * (EMBEDDING_TOKEN_LIMIT * 4 + 5000)}
    report = evaluate_strategy("NONE", chunk_none, docs, [])
    assert report.over_embedding_limit == 1
    assert report.tokens_max > EMBEDDING_TOKEN_LIMIT


def test_evaluate_strategy_scores_integrity_over_the_whole_corpus():
    docs = {"a.md": "x" * 2000, "b.md": "y" * 2000}
    evidence = [_ev(0, 100, doc="a.md"), _ev(0, 1900, doc="b.md")]
    report = evaluate_strategy(
        "fixed", lambda d, t: chunk_fixed_size(d, t, 100, 0), docs, evidence
    )
    assert report.evidence_total == 2
    assert report.evidence_intact == 1  # the 1900-char span cannot fit a 400-char chunk
    assert report.integrity == pytest.approx(0.5)


def test_build_strategies_uses_the_deployed_settings_for_the_baseline_arm():
    # The baseline must be what is actually running, or the comparison is against a fiction.
    names = list(build_strategies(max_tokens=250, overlap_percentage=15))
    assert "FIXED_SIZE(250tok/15%)" in names
    assert "NONE (whole file)" in names


def test_estimate_tokens_never_returns_zero_for_nonempty_text():
    assert estimate_tokens("a") >= 1
    assert estimate_tokens("") >= 1


# --- Retrieval probe --------------------------------------------------------------------------
#
# The probe's job is to say whether the right DOCUMENT ranks. Its one dangerous failure mode is
# scoring questions the KB was never meant to answer, which is exactly what the first live run did
# - it reported a 37% miss rate that was entirely an artifact of databases.php being deliberately
# unindexed. These pin that it cannot happen silently again.

from retrieval_probe import DEFAULT_TOOL_ANSWERED, expected_doc, matches, probe, recall_at


class _FakeRuntime:
    """Returns a fixed uri list per query, in rank order."""

    def __init__(self, by_query):
        self.by_query = by_query
        self.calls = []

    def retrieve(self, **kwargs):
        query = kwargs["retrievalQuery"]["text"]
        self.calls.append(query)
        return {
            "retrievalResults": [
                {"metadata": {"source_url": u}} for u in self.by_query.get(query, [])
            ]
        }


def test_expected_doc_normalises_a_dataset_source():
    assert expected_doc("about-the-library.php") == "about-the-library"
    assert expected_doc("main_map.php") == "main-map"
    assert expected_doc(None) is None


def test_matches_tolerates_the_scrapers_host_prefix_and_hash():
    assert matches("about-the-library", "https://www.gavilan.edu/library/about-the-library.php")
    assert not matches("about-the-library", "https://www.gavilan.edu/library/howdoi.php")


def test_recall_at_counts_only_ranks_within_k():
    assert recall_at([1, 2, 5, None], 3) == pytest.approx(0.5)
    assert recall_at([1, 2, 5, None], 8) == pytest.approx(0.75)
    assert recall_at([], 3) == 0.0


def test_probe_scores_the_rank_of_the_expected_document():
    runtime = _FakeRuntime({"q1": ["https://x/other.php", "https://x/hours.php"]})
    pairs = [QAPair("q1", "a", source="hours.php")]
    result = probe(runtime, "kb", pairs, top_k=8)
    assert result["ranks"] == [2]
    assert result["scored"] == 1


def test_probe_excludes_tool_answered_rows_instead_of_failing_them():
    # databases.php is answered by the authoritative database_catalog tool and is deliberately not
    # indexed. Counting it as a retrieval miss invents a failure rate - the bug this test exists for.
    runtime = _FakeRuntime({})
    pairs = [
        QAPair("do you have JSTOR?", "no", source="databases.php"),
        QAPair("what are the hours?", "9-5", source="about-the-library.php"),
    ]
    result = probe(runtime, "kb", pairs, top_k=8)

    assert result["tool_rows"] == 1
    assert result["scored"] == 1  # only the KB-answerable row
    assert "do you have JSTOR?" not in runtime.calls  # not even retrieved
    assert result["misses"] == [] or result["misses"][0][0] != "do you have JSTOR?"


def test_tool_answered_default_covers_the_unindexed_page():
    assert "databases.php" in DEFAULT_TOOL_ANSWERED
