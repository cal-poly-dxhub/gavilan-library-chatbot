#!/usr/bin/env python3
"""Measure what the live Knowledge Base actually retrieves, per golden question.

    python retrieval_probe.py --kb-id GLBDBZXOFU --top-k 8

READ-ONLY. Calls bedrock-agent-runtime Retrieve once per dataset row and records which source
documents come back. Creates nothing, changes nothing.

WHY THIS EXISTS ALONGSIDE chunking.py. The offline simulator answers "do these boundaries cut an
answer in half" - a property of the text. It cannot answer "does the right chunk actually rank",
which is a property of the embeddings and only observable against a real index. Together they
cover the two ways chunking hurts you: an answer split across chunks, and a correct chunk that
never surfaces. Either alone will mislead.

THE METRIC. recall@k against the dataset's `source` column: of the k chunks returned, did any come
from the document that actually holds the answer? This is the standard retrieval metric and the
one that maps to a user-visible failure - if the right page never enters the top k, no amount of
prompt work recovers it, because the model never sees it.

It also reports NOISE: documents that surface often while never being the expected source. That
distinguishes "the corpus is too small to rank well" from "one specific page matches everything",
which are different problems with different fixes - the first is a chunking or depth question, the
second is a corpus question.
"""

from __future__ import annotations

import argparse
import collections
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from dataset_loader import load_qa_csv

_EVAL_DIR = Path(__file__).resolve().parent
_DEFAULT_DATASET = _EVAL_DIR / "datasets" / "baseline_qa.csv"


def _doc_name(uri: Optional[str]) -> str:
    """Reduce a chunk's source uri to a comparable document name."""
    if not uri:
        return "(no source)"
    return uri.rstrip("/").rsplit("/", 1)[-1]


def expected_doc(source: Optional[str]) -> Optional[str]:
    """Normalize a dataset `source` ("about-the-library.php") for comparison against a uri."""
    if not source:
        return None
    return source.strip().lower().replace(".php", "").replace("_", "-")


def matches(expected: str, retrieved_uri: str) -> bool:
    """Whether a retrieved chunk came from the expected document. Substring rather than equality:
    the scraper's slugs carry a host prefix and a content hash the dataset does not know about."""
    return expected in _doc_name(retrieved_uri).lower()


def retrieve(client, kb_id: str, query: str, top_k: int) -> List[str]:
    """The source uris of the chunks the KB returns, in rank order. SEMANTIC because the store is
    Amazon S3 Vectors, which offers nothing else - HYBRID would fail the call outright."""
    response = client.retrieve(
        knowledgeBaseId=kb_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
                "overrideSearchType": "SEMANTIC",
            }
        },
    )
    uris = []
    for result in response.get("retrievalResults", []):
        metadata = result.get("metadata") or {}
        location = result.get("location") or {}
        uri = (
            metadata.get("source_url")
            or (location.get("webLocation") or {}).get("url")
            or (location.get("s3Location") or {}).get("uri")
        )
        uris.append(uri or "")
    return uris


# Dataset rows whose `source` is one of these are answered by an authoritative TOOL, not by KB
# retrieval, and their page is deliberately not indexed. Scoring them against the KB measures a
# design decision and reports a ~37% failure rate that is entirely fictional - which is exactly
# what the first run of this probe did before this existed.
DEFAULT_TOOL_ANSWERED = ("databases.php",)


def probe(client, kb_id: str, pairs: Sequence, top_k: int, tool_answered=DEFAULT_TOOL_ANSWERED) -> Dict:
    """Run every scorable row and collect rank data.

    Rows without a `source`, and rows whose source is tool-answered, cannot be scored against the
    KB and are counted separately rather than silently failed."""
    ranks: List[Optional[int]] = []
    scored, skipped, misses = [], 0, []
    doc_hits: collections.Counter = collections.Counter()
    doc_correct: collections.Counter = collections.Counter()

    tool_rows = 0
    for pair in pairs:
        expected = expected_doc(pair.source)
        if not expected:
            skipped += 1
            continue
        if any(t.replace(".php", "") in expected for t in tool_answered):
            tool_rows += 1
            continue
        uris = retrieve(client, kb_id, pair.question, top_k)
        for uri in uris:
            doc_hits[_doc_name(uri)] += 1

        rank = next((i + 1 for i, uri in enumerate(uris) if matches(expected, uri)), None)
        ranks.append(rank)
        scored.append(pair.question)
        if rank is None:
            misses.append((pair.question, expected, [_doc_name(u) for u in uris[:3]]))
        else:
            doc_correct[_doc_name(uris[rank - 1])] += 1

    return {
        "ranks": ranks,
        "scored": len(scored),
        "skipped": skipped,
        "misses": misses,
        "doc_hits": doc_hits,
        "doc_correct": doc_correct,
        "tool_rows": tool_rows,
        "top_k": top_k,
    }


def recall_at(ranks: Sequence[Optional[int]], k: int) -> float:
    if not ranks:
        return 0.0
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks)


def render(result: Dict) -> str:
    ranks = result["ranks"]
    top_k = result["top_k"]
    found = [r for r in ranks if r is not None]

    out = [
        "LIVE RETRIEVAL PROBE (read-only; what the deployed index actually returns)",
        "",
        f"questions scored: {result['scored']}"
        f"   (tool-answered, not the KB's job: {result['tool_rows']}"
        f"; no source column: {result['skipped']})",
        f"retrieval depth:  numberOfResults = {top_k}, SEMANTIC",
        "",
        "recall@k - did the document holding the answer appear in the top k?",
    ]
    for k in (1, 3, 5, top_k):
        if k <= top_k:
            out.append(f"  recall@{k:<2} {recall_at(ranks, k):>6.0%}")
    if found:
        out.append(f"  median rank of the correct document: {int(statistics.median(found))}")

    if result["misses"]:
        out += ["", f"NEVER RETRIEVED ({len(result['misses'])}) - the answer was unreachable:"]
        for question, expected, got in result["misses"]:
            out.append(f"  - {question}")
            out.append(f"      wanted {expected}, top 3 were {', '.join(got) or '(nothing)'}")

    # A document that fills slots without ever being the answer is taking a slot from one that
    # would be - the corpus-level failure, distinct from a chunking one.
    noise = [
        (doc, hits)
        for doc, hits in result["doc_hits"].most_common()
        if result["doc_correct"][doc] == 0
    ]
    if noise:
        out += ["", "NOISE - surfaced but never the expected source:"]
        for doc, hits in noise[:8]:
            share = hits / max(1, result["scored"] * top_k)
            out.append(f"  {hits:>4} chunks ({share:>4.0%} of all slots)  {doc}")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kb-id", required=True, help="knowledge base id to probe")
    parser.add_argument("--top-k", type=int, default=8, help="numberOfResults (match production)")
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument(
        "--tool-answered", nargs="*", default=list(DEFAULT_TOOL_ANSWERED),
        help="sources answered by a tool rather than the KB; excluded from recall",
    )
    args = parser.parse_args(argv)

    import boto3

    client = boto3.client("bedrock-agent-runtime", region_name=args.region)
    pairs = load_qa_csv(args.dataset)
    result = probe(client, args.kb_id, pairs, args.top_k, tuple(args.tool_answered))
    print(render(result))

    if not result["scored"]:
        print("\nFAIL: no rows had a `source` column to score against.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
