#!/usr/bin/env python3
"""Measure what a real question actually costs, against the deployed /query endpoint.

    python measure_usage.py --api https://<id>.execute-api.<region>.amazonaws.com/query

READ-ONLY in the sense that matters: it only POSTs /query, exactly as a student's browser
does, with `include_usage: true` added so the response reports the billable units the loop
consumed. It creates nothing and changes nothing. It DOES cost money - every question is a
real Bedrock call - so it is a deliberate, on-demand tool, never part of CI.

WHY THIS EXISTS. The cost constants the demo site's estimator uses have to be measured, not
guessed, because three properties of this architecture make the obvious guess wrong:

  1. One question is NOT one model call. The Converse loop runs until end_turn, and every
     iteration resends the whole accumulated context.
  2. Retrieved KB passages ride in the input tokens. The priming retrieval alone puts eight
     600-token chunks into the prompt before the model says anything, so input tokens are
     dominated by retrieval, not by the student's sentence.
  3. Conversation history is client-carried and resent every turn, so the cost of question
     five is not the cost of question one. That is why this measures BY POSITION in a
     conversation and fits a slope, rather than reporting one average and calling it done.

The output is the `usage_model` block for config.yaml, printed ready to paste, plus the
sample size and spread behind every number so the figures can be argued with.
"""

from __future__ import annotations

import argparse
import json
import ssl
import statistics
import sys
import urllib.error
import urllib.request


def _ssl_context():
    """A certificate-verifying context that also works on a macOS python.org build.

    Same problem and same fix as app/handler.py's _primo_ssl_context: that build ships no OS
    trust store, so a bare default context fails verification against API Gateway. Prefer
    certifi, fall back to the platform store only if it actually loaded CAs, then botocore's
    bundled CA. Never disables verification."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi absent; try the OS trust store next
        pass
    try:
        candidate = ssl.create_default_context()
        if candidate.cert_store_stats().get("x509_ca", 0) > 0:
            return candidate
    except Exception:  # noqa: BLE001
        pass
    try:
        import os

        import botocore

        cafile = os.path.join(os.path.dirname(botocore.__file__), "cacert.pem")
        if os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
    except Exception:  # noqa: BLE001
        pass
    return ssl.create_default_context()


_SSL = _ssl_context()

# Single-turn questions: the operational range the bot is actually for (hours, borrowing,
# databases, catalog, reserves, campus pointers). Deliberately spans the tools - a
# database_catalog question and a Primo catalog question do not cost the same, and an
# average built only from "what are your hours" would understate the loop.
SINGLE_TURN_QUESTIONS = [
    "What are the library hours?",
    "How do I check out a book?",
    "Can I borrow a laptop?",
    "Do you have JSTOR?",
    "What databases do you have for nursing?",
    "Where is the financial aid office?",
    "How do I get a library card?",
    "Do you have The Great Gatsby?",
    "What is on reserve for PSYC C1000?",
    "How do I request a book the library does not own?",
    "Can I print at the library?",
    "How do I cite sources in APA?",
    "Where is the bookstore?",
    "Do you have study rooms?",
    "Can I renew a book online?",
    "What is interlibrary loan?",
    "Do you have EBSCO?",
    "How do I contact a librarian?",
    "Do you have the textbook for ENGL 1A?",
    "What does the library offer for online students?",
]

# Multi-turn threads: real follow-up shapes, including the pronoun-y fragments that make
# history load-bearing ("what about on weekends?"). Depth is what the estimator's third
# slider controls, so it has to be measured at depth rather than extrapolated from one turn.
CONVERSATIONS = [
    [
        "What are the library hours?",
        "What about on weekends?",
        "Is that the same over the summer?",
        "Where exactly is the library?",
        "Can I get there by bus?",
        "And can I print once I am there?",
        "Do I need my own paper?",
        "How much does a page cost?",
    ],
    [
        "How do I check out a book?",
        "How long can I keep it?",
        "Can I renew it online?",
        "What if someone else has it?",
        "What happens if I return it late?",
        "Who do I talk to if I have a problem with my account?",
        "Are they there on Fridays?",
        "Can I email them instead?",
    ],
    [
        "Do you have databases for psychology?",
        "Can I use them from home?",
        "What do I need to log in?",
        "Which one is best for peer reviewed articles?",
        "How do I cite what I find?",
        "Can a librarian help me with this?",
        "Do I need an appointment?",
        "Is that available online too?",
    ],
    [
        "Do you have the textbook for ENGL 1A?",
        "What about course reserves?",
        "How long is the loan for a reserve item?",
        "Where do I pick it up?",
        "Is there an online copy?",
        "What if the bookstore has it instead?",
        "Do they rent textbooks?",
        "Where is the bookstore located?",
    ],
    [
        "Can I borrow a laptop?",
        "How long can I keep it?",
        "Do I need to be in a specific program?",
        "Can I renew it for the next semester?",
        "What if it stops working?",
        "Who do I return it to?",
        "Is there a late fee?",
        "Can I check out a hotspot too?",
    ],
]


def ask(api, messages, timeout):
    """POST one turn and return (answer, usage). Raises on any non-2xx or bad shape - a
    measurement run that silently swallowed failures would report a cheaper bot than exists."""
    body = json.dumps({"messages": messages, "include_usage": True}).encode("utf-8")
    req = urllib.request.Request(
        api, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        data = json.loads(resp.read())
    if "usage" not in data:
        raise RuntimeError(
            "response carried no `usage` block - is the deployed handler older than the "
            "include_usage flag?"
        )
    return data.get("answer", ""), data["usage"]


def flat(usage):
    """One usage dict flattened to the scalar fields the cost model needs, with guardrail
    units summed into the two policies this deployment actually bills for."""
    units = usage.get("guardrail_units", {}) or {}
    pii = units.get("sensitive_information_policy_units", 0) - units.get(
        "sensitive_information_policy_free_units", 0
    )
    return {
        "model_calls": usage.get("model_calls", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "retrievals": usage.get("retrievals", 0),
        "tool_calls": usage.get("tool_calls", 0),
        "guardrail_content_units": units.get("content_policy_units", 0),
        "guardrail_pii_units": max(pii, 0),
    }


_FIELDS = (
    "model_calls",
    "input_tokens",
    "output_tokens",
    "retrievals",
    "tool_calls",
    "guardrail_content_units",
    "guardrail_pii_units",
)


def summarize(rows, label):
    print(f"\n--- {label} (n={len(rows)}) ---")
    out = {}
    for f in _FIELDS:
        vals = [r[f] for r in rows]
        mean = statistics.fmean(vals)
        out[f] = mean
        spread = f"min {min(vals)} / median {statistics.median(vals)} / max {max(vals)}"
        print(f"  {f:28s} mean {mean:10.1f}   {spread}")
    return out


def _lsq(pts):
    """Least-squares (intercept, slope) for (x, y) points; (mean, 0) if x has no spread."""
    if not pts:
        return 0.0, 0.0
    if len(pts) < 2:
        return pts[0][1], 0.0
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom if denom else 0.0
    return my - slope * mx, slope


def fit_context(by_position):
    """Fit the PER-MODEL-CALL context size against conversation position.

    WHY NOT FIT input_tokens DIRECTLY. Two effects drive input tokens and they point in
    opposite directions, so a naive fit against position measures neither:

      - Depth: each prior turn adds its text to the history the client resends. Real, but
        small, because _seed_messages rebuilds from TEXT turns only - tool results are not
        carried across requests, so a turn adds a question plus an answer, not the passages
        that produced it.
      - Loop length: a question needing a second Converse call resends the whole ~10k-token
        context a second time. That is worth ~10,000 tokens, roughly fifty turns' worth of
        history growth.

    First questions trigger the second call far more often than follow-ups do (the priming
    retrieval usually satisfies a follow-up), so the loop-length effect is CONCENTRATED at
    position 1 and drags the raw slope negative - which would read as "conversations get
    cheaper as they go", the opposite of the truth about history.

    Dividing by model_calls removes the loop-length effect and leaves the depth effect
    measurable on its own. The estimator then multiplies the two back together."""
    pts = []
    for k, rows in sorted(by_position.items()):
        per_call = [r["input_tokens"] / r["model_calls"] for r in rows if r["model_calls"]]
        if per_call:
            pts.append((k, statistics.fmean(per_call)))
    return _lsq(pts)


def fit_calls(by_position):
    """Fit model calls per question against position: how much of the loop-length effect is
    a first-question phenomenon rather than a property of every question."""
    pts = [
        (k, statistics.fmean([r["model_calls"] for r in rows]))
        for k, rows in sorted(by_position.items())
        if rows
    ]
    return _lsq(pts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", required=True, help="deployed POST /query URL")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument(
        "--singles", type=int, default=len(SINGLE_TURN_QUESTIONS),
        help="how many single-turn questions to run (default: all)",
    )
    ap.add_argument(
        "--threads", type=int, default=len(CONVERSATIONS),
        help="how many multi-turn conversations to run (default: all)",
    )
    args = ap.parse_args()

    singles = []
    print("=== single-turn ===")
    for q in SINGLE_TURN_QUESTIONS[: args.singles]:
        try:
            _, usage = ask(args.api, [{"role": "user", "content": q}], args.timeout)
        except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
            print(f"  FAILED {q!r}: {exc}", file=sys.stderr)
            continue
        row = flat(usage)
        singles.append(row)
        print(
            f"  {row['model_calls']} calls  {row['input_tokens']:6d} in  "
            f"{row['output_tokens']:4d} out  | {q}"
        )

    by_position = {}
    print("\n=== multi-turn (history is client-carried and resent every turn) ===")
    for thread in CONVERSATIONS[: args.threads]:
        messages = []
        print(f"  thread: {thread[0][:48]!r}")
        for k, q in enumerate(thread):
            messages.append({"role": "user", "content": q})
            try:
                answer, usage = ask(args.api, messages, args.timeout)
            except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
                print(f"    FAILED at position {k + 1}: {exc}", file=sys.stderr)
                break
            messages.append({"role": "assistant", "content": answer})
            row = flat(usage)
            by_position.setdefault(k, []).append(row)
            print(
                f"    pos {k + 1}: {row['model_calls']} calls  "
                f"{row['input_tokens']:6d} in  {row['output_tokens']:4d} out"
            )

    if singles:
        summarize(singles, "single-turn averages")
    combined = singles + [r for rows in by_position.values() for r in rows]
    if combined:
        summarize(combined, "ALL questions")
    print("\n--- per-position averages (multi-turn) ---")
    for k, rows in sorted(by_position.items()):
        m = statistics.fmean([r["input_tokens"] for r in rows])
        o = statistics.fmean([r["output_tokens"] for r in rows])
        c = statistics.fmean([r["model_calls"] for r in rows])
        print(f"  position {k + 1} (n={len(rows)}): {m:8.0f} in  {o:6.0f} out  {c:4.2f} calls")

    all_rows = singles + [r for rows in by_position.values() for r in rows]
    total = len(all_rows)

    # Per-call context, the quantity that actually scales with depth.
    print("\n--- per-model-call context size by position (the real depth signal) ---")
    for k, rows in sorted(by_position.items()):
        per_call = [r["input_tokens"] / r["model_calls"] for r in rows if r["model_calls"]]
        if per_call:
            print(
                f"  position {k + 1} (n={len(per_call)}): "
                f"{statistics.fmean(per_call):8.0f} tokens/call"
            )

    ctx_base, ctx_slope = fit_context(by_position)
    calls_base, calls_slope = fit_calls(by_position)

    # Guardrail text units are close to "one for the input screen plus one per Converse turn",
    # but NOT exactly: a Bedrock text unit covers up to 1,000 characters, so a long answer
    # spends a second output unit. The estimator therefore uses the measured average rather
    # than the tidy formula; this line reports how often the formula actually held so the gap
    # is visible instead of assumed away.
    units_check = [
        (r["guardrail_content_units"], 1 + r["model_calls"]) for r in all_rows
    ]
    units_exact = sum(1 for got, want in units_check if got == want)
    units_avg = statistics.fmean([r["guardrail_content_units"] for r in all_rows])
    pii_avg = statistics.fmean([r["guardrail_pii_units"] for r in all_rows])

    calls = [r["model_calls"] for r in all_rows]
    outs = [r["output_tokens"] for r in all_rows]
    rets = [r["retrievals"] for r in all_rows]

    print("\n=== config.yaml usage_model block (measured, paste-ready) ===")
    print(f"  # Measured against the deployed endpoint over {total} real questions")
    print(f"  # ({len(singles)} single-turn + "
          f"{sum(len(r) for r in by_position.values())} across "
          f"{len(by_position.get(0, []))} multi-turn threads).")
    print(f"  sample_questions: {total}")
    print(f"  model_calls_avg: {round(statistics.fmean(calls), 2)}")
    print(f"  context_tokens_per_call_base: {round(ctx_base, 0):.0f}")
    print(f"  context_tokens_per_call_per_prior_turn: {round(ctx_slope, 0):.0f}")
    print(f"  output_tokens_avg: {round(statistics.fmean(outs), 0):.0f}")
    print(f"  retrievals_avg: {round(statistics.fmean(rets), 2)}")
    print(f"  guardrail_content_units_avg: {round(units_avg, 2)}")
    print(f"  guardrail_pii_units_avg: {round(pii_avg, 2)}")
    print(f"\n  # model_calls by position: base {calls_base:.2f}, "
          f"slope {calls_slope:+.3f} per prior turn")
    print(f"  # guardrail units == 1 + model_calls held for "
          f"{units_exact}/{len(units_check)} questions "
          f"(a text unit covers 1,000 chars, so a long answer spends an extra one)")


if __name__ == "__main__":
    main()
