#!/usr/bin/env python3
"""Compare chunking strategies over the real corpus, offline.

    python run_chunking_eval.py --corpus-dir ./corpus
    python run_chunking_eval.py --from-s3 <kb-source-bucket>       # needs read-only AWS creds

Reads the app's root config.yaml so the FIXED_SIZE arm reflects what is actually DEPLOYED rather
than a guess, then reports what each strategy's boundaries do to the corpus and - the part worth
acting on - whether any of them cut through an answer. See chunking.py for what this can and
cannot tell you; the short version is boundaries yes, ranking no.

Exits non-zero if any strategy would produce a chunk over the embedding limit, so this is usable
as a pre-deploy guard and not only as a report.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from chunking import (
    SWEEP_WINDOWS,
    build_strategies,
    evaluate_strategy,
    load_corpus,
    locate_evidence,
    render,
    render_sweep,
    _render_caveat,
)
from dataset_loader import load_qa_csv

_EVAL_DIR = Path(__file__).resolve().parent
_DEFAULT_DATASET = _EVAL_DIR / "datasets" / "baseline_qa.csv"
_ROOT_CONFIG = _EVAL_DIR.parent / "config.yaml"


def _deployed_chunking() -> tuple[int, int]:
    """The live FIXED_SIZE settings from the app's config.yaml, so the baseline arm is the real
    one. Falls back to Bedrock's own defaults if the section is missing."""
    try:
        with open(_ROOT_CONFIG) as f:
            chunking = (yaml.safe_load(f) or {}).get("chunking", {})
    except OSError:
        chunking = {}
    return int(chunking.get("max_tokens", 300)), int(chunking.get("overlap_percentage", 20))


def _download_corpus(bucket: str, dest: Path) -> None:
    """Pull the .md documents the KB actually ingests. Read-only; sidecars are skipped."""
    import boto3

    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if not key.endswith(".md"):
                continue
            s3.download_file(bucket, key, str(dest / Path(key).name))
            count += 1
    if not count:
        raise SystemExit(f"no .md objects found in s3://{bucket}")
    print(f"downloaded {count} documents from s3://{bucket}", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--corpus-dir", type=Path, help="directory of scraped .md documents")
    source.add_argument("--from-s3", metavar="BUCKET", help="KB source bucket to read instead")
    parser.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET, help="golden Q&A CSV")
    args = parser.parse_args(argv)

    tmp = None
    try:
        if args.from_s3:
            tmp = Path(tempfile.mkdtemp(prefix="chunking-corpus-"))
            _download_corpus(args.from_s3, tmp)
            corpus_dir = tmp
        else:
            corpus_dir = args.corpus_dir

        docs = load_corpus(corpus_dir)
        pairs = load_qa_csv(args.dataset)

        max_tokens, overlap = _deployed_chunking()
        strategies = build_strategies(max_tokens, overlap)

        # Headline table at the widest window (the strictest test), then the sweep, because one
        # window in isolation can flatter or condemn a strategy depending on how long the answers
        # happen to be - see chunking.SWEEP_WINDOWS.
        sweep = {}
        for window in SWEEP_WINDOWS:
            ev = [e for e in (locate_evidence(p, docs, window_chars=window) for p in pairs) if e]
            sweep[window] = {
                name: evaluate_strategy(name, chunker, docs, ev)
                for name, chunker in strategies.items()
            }

        widest = max(SWEEP_WINDOWS)
        evidence = [e for e in (locate_evidence(p, docs, window_chars=widest) for p in pairs) if e]
        unlocatable = len(pairs) - len(evidence)
        reports = list(sweep[widest].values())

        print(render(reports, docs, len(evidence)))
        print(render_sweep(sweep, list(strategies)))
        print(_render_caveat(reports[0].low_confidence if reports else 0, len(evidence)))
        if unlocatable:
            print(
                f"\nNOTE: {unlocatable} dataset rows had no usable `source` column, so they are "
                "not scored here.",
                file=sys.stderr,
            )

        # A chunk over the embedding limit does not degrade retrieval - it fails ingestion. That
        # is a build break, so make it one.
        if any(r.over_embedding_limit for r in reports):
            print("\nFAIL: at least one strategy exceeds the embedding token limit.", file=sys.stderr)
            return 1
        return 0
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
