"""Offline chunking simulator: compare chunking strategies without touching AWS.

WHY THIS EXISTS. A Bedrock data source's `chunkingConfiguration` is IMMUTABLE - changing it means
deleting the data source, recreating it, and re-indexing the whole corpus. So every strategy you
want to compare on the live KB costs an index and an ingestion, which makes "just try it and see"
expensive and slow. This module answers most of the question for free: it replicates the chunk
BOUNDARIES locally over the same markdown the KB ingests, and reports what those boundaries do to
the corpus and to the answers we care about.

WHAT IT MEASURES, AND WHY THAT METRIC. Chunk counts and size histograms are easy and nearly
useless on their own - a strategy is not better because it makes more chunks. The metric that
predicts real failures is EVIDENCE INTEGRITY: for each golden question, does the span of text that
answers it survive inside ONE chunk, or does a boundary cut through it? A split span is how a
retrievable fact becomes unretrievable - the half that ranks carries only half the answer. This is
the failure we hit in production, where a page's "Library Circulation Desk" label sat far enough
from its "Technical Support for Laptops" heading that a 300-token boundary could land between them
and leave the model able to say "contact us" without being able to say who "us" is.

WHAT IT DOES NOT MEASURE. Ranking. Whether a chunk actually gets RETRIEVED for a query is a
property of the embeddings, not of the boundaries, and nothing offline can tell you that - that is
what the retrieve-only Bedrock eval (run_retrieve_eval.py) is for. Use this to narrow the field
cheaply, then A/B what survives.

HONESTY ABOUT THE SIMULATION. Token counts here are an approximation (see estimate_tokens); the
real chunker uses the embedding model's tokenizer, which this does not have. That is fine for the
question being asked - boundaries move by a few tokens, not by whole sections - but it means the
numbers are directional, not exact. SEMANTIC is deliberately NOT simulated: its boundaries come
from embedding similarity, so any offline stand-in would be a guess wearing a measurement's
clothes. MARKDOWN_SECTION is included instead as a CONTROL, not a stand-in for it - I first added
it expecting structure-aware splitting to set an upper bound, and the run showed the opposite
(19% integrity, median 43 tokens). Respecting document structure is not automatically good when
the structure is fine-grained, and that result is worth keeping visible.

A SINGLE evidence-window width will mislead you, so the report sweeps several (SWEEP_WINDOWS).
Judge a strategy on whether it holds across the sweep, not on one number.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from dataset_loader import QAPair

# Titan Text Embeddings v2 accepts at most this many tokens per request. A chunk over the limit
# fails ingestion outright, which is the one hard constraint in here rather than a preference.
EMBEDDING_TOKEN_LIMIT = 8192

# Rough chars-per-token for English prose. Deliberately crude - see the module docstring on why
# approximate boundaries are good enough for an integrity question.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Approximate token count. NOT the embedding model's tokenizer - see module docstring."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Chunk:
    """One chunk as a strategy would produce it, with its offsets into the source document."""

    doc: str
    text: str
    start: int
    end: int

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


# --- Strategies -------------------------------------------------------------------------------
#
# Each takes a document's text and returns its chunks. They mirror the Bedrock strategies of the
# same name; MARKDOWN_SECTION is ours (see module docstring).


def chunk_none(doc: str, text: str) -> List[Chunk]:
    """NONE: the whole file is one chunk. Viable only while every document fits the embedding
    limit - which is exactly what the report checks rather than assumes."""
    return [Chunk(doc=doc, text=text, start=0, end=len(text))]


def chunk_fixed_size(doc: str, text: str, max_tokens: int, overlap_percentage: int) -> List[Chunk]:
    """FIXED_SIZE: ~max_tokens per chunk with an overlap tail carried from the previous chunk.

    Splits on whitespace so a chunk never ends mid-word. The overlap is what makes a split span
    sometimes survive anyway - a boundary that cuts an answer can still leave it whole in the
    NEXT chunk, and the integrity check below counts that as intact because retrieval would."""
    if not text.strip():
        return []
    max_chars = max_tokens * _CHARS_PER_TOKEN
    overlap_chars = int(max_chars * (max(0, min(100, overlap_percentage)) / 100))
    stride = max(1, max_chars - overlap_chars)

    chunks: List[Chunk] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            # Back off to the last whitespace so words stay whole.
            space = text.rfind(" ", start, end)
            if space > start:
                end = space
        chunks.append(Chunk(doc=doc, text=text[start:end].strip(), start=start, end=end))
        if end >= len(text):
            break
        start += stride
    return [c for c in chunks if c.text]


def chunk_hierarchical(doc: str, text: str, parent_tokens: int, child_tokens: int) -> List[Chunk]:
    """HIERARCHICAL: children are embedded, but retrieval returns the PARENT chunk.

    Since what reaches the model is the parent, integrity is a property of the parent boundaries -
    so that is what this returns. `child_tokens` is accepted for shape parity and to make the
    caller state it explicitly; it does not move the boundaries that matter here."""
    del child_tokens  # embedded, but never what the model is handed
    return chunk_fixed_size(doc, text, max_tokens=parent_tokens, overlap_percentage=0)


_SECTION_RE = re.compile(r"(?m)^#{1,6} ")


def chunk_markdown_section(doc: str, text: str) -> List[Chunk]:
    """NOT a Bedrock strategy. Splits on EVERY markdown heading, however small.

    Included as a cautionary control rather than an aspiration: respecting document structure is
    not automatically good, because this corpus's structure is fine-grained (FAQ pages are a
    heading per question). It produces a median chunk of ~43 tokens and scores worst of anything
    here - useful evidence that the problem with small chunks is smallness, not where the
    boundaries fall."""
    starts = [m.start() for m in _SECTION_RE.finditer(text)]
    if not starts:
        return chunk_none(doc, text)
    if starts[0] != 0:
        starts.insert(0, 0)
    bounds = starts + [len(text)]
    chunks = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        body = text[start:end].strip()
        if body:
            chunks.append(Chunk(doc=doc, text=body, start=start, end=end))
    return chunks


def build_strategies(max_tokens: int, overlap_percentage: int) -> Dict[str, callable]:
    """The strategies the report compares. `max_tokens`/`overlap_percentage` come from the app's
    real config.yaml so FIXED_SIZE reflects what is actually deployed, not a guess."""
    return {
        f"FIXED_SIZE({max_tokens}tok/{overlap_percentage}%)": lambda d, t: chunk_fixed_size(
            d, t, max_tokens, overlap_percentage
        ),
        "FIXED_SIZE(600tok/20%)": lambda d, t: chunk_fixed_size(d, t, 600, 20),
        "HIERARCHICAL(1500/300)": lambda d, t: chunk_hierarchical(d, t, 1500, 300),
        "NONE (whole file)": chunk_none,
        "MARKDOWN_SECTION (control)": chunk_markdown_section,
    }


# Evidence-window sizes the report sweeps. A SINGLE window is misleading: a wide one flags splits
# that only matter for long answers, a narrow one flatters every strategy because almost any chunk
# can hold two sentences. The sweep is what makes a verdict defensible - a strategy that holds at
# every width is genuinely safe, and one that degrades as answers get longer tells you exactly
# where its ceiling is.
SWEEP_WINDOWS = (200, 300, 500, 700)


# --- Evidence spans ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the to was were "
    "will with you your can do does how what when where which who why".split()
)


def _content_words(text: str) -> List[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2]


@dataclass
class Evidence:
    """The span of a source document that answers a question, plus how confident we are in it."""

    question: str
    doc: str
    start: int
    end: int
    overlap: float  # 0..1 share of the reference answer's content words found in the span

    @property
    def confident(self) -> bool:
        """Below this the located span is guesswork and its verdict should not be trusted."""
        return self.overlap >= 0.35


def locate_evidence(pair: QAPair, docs: Dict[str, str], window_chars: int = 700) -> Optional[Evidence]:
    """Find where in its source document a question's answer lives.

    Uses the highest-overlap sliding window between the document and the reference answer. This is
    an APPROXIMATION and the report says so per row: baseline_qa.csv holds written answers, not
    verbatim quotes, so there is no exact span to match. Add an `evidence` column to the dataset
    and this is replaced by ground truth - which is worth doing before anyone makes an expensive
    decision on the strength of a low-overlap row."""
    doc_name = _match_doc(pair.source, docs)
    if doc_name is None:
        return None
    text = docs[doc_name]
    wanted = set(_content_words(pair.reference_answer))
    if not wanted:
        return None

    best_start, best_score = 0, 0.0
    step = max(1, window_chars // 4)
    for start in range(0, max(1, len(text) - window_chars + 1), step):
        window = text[start : start + window_chars]
        score = len(wanted & set(_content_words(window))) / len(wanted)
        if score > best_score:
            best_start, best_score = start, score
    return Evidence(
        question=pair.question,
        doc=doc_name,
        start=best_start,
        end=min(len(text), best_start + window_chars),
        overlap=best_score,
    )


def _match_doc(source: Optional[str], docs: Dict[str, str]) -> Optional[str]:
    """Map a dataset `source` value ("about-the-library.php") to a corpus filename."""
    if not source:
        return None
    stem = source.strip().lower().replace(".php", "").replace("_", "-")
    for name in docs:
        if stem and stem in name.lower():
            return name
    return None


def span_survives(evidence: Evidence, chunks: Sequence[Chunk]) -> bool:
    """Whether any single chunk of the evidence's document contains the whole span.

    ANY chunk, not the nearest one: overlap means a span cut by one boundary is often whole in the
    following chunk, and retrieval would surface that one. Counting it as split would overstate
    the damage."""
    return any(
        c.doc == evidence.doc and c.start <= evidence.start and c.end >= evidence.end
        for c in chunks
    )


# --- Report -----------------------------------------------------------------------------------


@dataclass
class StrategyReport:
    name: str
    chunks: int
    tokens_total: int
    tokens_min: int
    tokens_median: int
    tokens_max: int
    over_embedding_limit: int
    evidence_intact: int
    evidence_total: int
    split_questions: List[str] = field(default_factory=list)
    low_confidence: int = 0

    @property
    def integrity(self) -> float:
        return self.evidence_intact / self.evidence_total if self.evidence_total else 1.0


def evaluate_strategy(name, chunker, docs: Dict[str, str], evidence: Sequence[Evidence]) -> StrategyReport:
    """Chunk the whole corpus with one strategy and score it."""
    chunks: List[Chunk] = []
    for doc_name, text in docs.items():
        chunks.extend(chunker(doc_name, text))
    sizes = [c.tokens for c in chunks] or [0]

    intact, split = 0, []
    for ev in evidence:
        if span_survives(ev, chunks):
            intact += 1
        else:
            split.append(ev.question)

    return StrategyReport(
        name=name,
        chunks=len(chunks),
        tokens_total=sum(sizes),
        tokens_min=min(sizes),
        tokens_median=int(statistics.median(sizes)),
        tokens_max=max(sizes),
        over_embedding_limit=sum(1 for s in sizes if s > EMBEDDING_TOKEN_LIMIT),
        evidence_intact=intact,
        evidence_total=len(evidence),
        split_questions=split,
        low_confidence=sum(1 for e in evidence if not e.confident),
    )


def load_corpus(corpus_dir: Path) -> Dict[str, str]:
    """Read a directory of scraped .md files - the same content the KB ingests."""
    docs = {}
    for path in sorted(Path(corpus_dir).glob("*.md")):
        docs[path.name] = path.read_text(encoding="utf-8")
    if not docs:
        raise ValueError(f"no .md files found in {corpus_dir}")
    return docs


def render(reports: Sequence[StrategyReport], docs: Dict[str, str], evidence_count: int) -> str:
    """Human-readable comparison, ordered so the decision is legible rather than just the data."""
    corpus_tokens = sum(estimate_tokens(t) for t in docs.values())
    out = [
        "CHUNKING COMPARISON (offline simulation - boundaries only, not ranking)",
        "",
        f"corpus: {len(docs)} documents, ~{corpus_tokens:,} tokens total",
        f"evidence spans located: {evidence_count}",
        "",
        f"{'strategy':<30} {'chunks':>7} {'med tok':>8} {'max tok':>8} {'>limit':>7} {'evidence intact':>16}",
        "-" * 82,
    ]
    for r in reports:
        flag = "  <-- FAILS INGESTION" if r.over_embedding_limit else ""
        out.append(
            f"{r.name:<30} {r.chunks:>7} {r.tokens_median:>8} {r.tokens_max:>8} "
            f"{r.over_embedding_limit:>7} {r.evidence_intact:>7}/{r.evidence_total:<8}"
            f"({r.integrity:.0%}){flag}"
        )

    out += ["", "Answers split across a chunk boundary:"]
    for r in reports:
        if r.split_questions:
            out.append(f"  {r.name}:")
            out += [f"    - {q}" for q in r.split_questions]
    if not any(r.split_questions for r in reports):
        out.append("  (none - no strategy splits any located answer)")

    return "\n".join(out)


def render_sweep(rows: Dict[int, Dict[str, StrategyReport]], strategy_names: Sequence[str]) -> str:
    """Evidence integrity per strategy across evidence-window widths (see SWEEP_WINDOWS).

    Read it as a robustness check, not extra detail: a flat 100% row means the strategy never cuts
    an answer regardless of how long that answer is, while a falling row shows the answer length at
    which it starts losing them."""
    out = [
        "",
        "EVIDENCE INTEGRITY vs ANSWER LENGTH (how much context an answer needs, in characters)",
        "",
        f"{'strategy':<30}" + "".join(f"{w:>10}" for w in rows),
        "-" * (30 + 10 * len(rows)),
    ]
    for name in strategy_names:
        cells = []
        for window in rows:
            r = rows[window][name]
            cells.append(f"{r.integrity:>9.0%}")
        out.append(f"{name:<30}" + "".join(cells))
    return "\n".join(out)


def _render_caveat(low: int, evidence_count: int) -> str:
    out = []
    if low:
        out += [
            "",
            f"CAVEAT: {low} of {evidence_count} evidence spans were located with low word overlap,",
            "so their verdicts are guesses. baseline_qa.csv holds written answers, not verbatim",
            "quotes. Add an `evidence` column with the exact sentence before betting on those rows.",
        ]
    return "\n".join(out)
