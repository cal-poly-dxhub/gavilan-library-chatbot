# Answer-quality loop (promptfoo)

A tweak-and-rerun tuning tool. It fires 22 golden questions at the **deployed** `/query` endpoint
and has a Bedrock-hosted Claude judge score each answer on two axes:

- **behavior** - did the bot do the right thing? (answer the operational question, route the
  out-of-scope one, confirm or deny a database authoritatively, send textbooks to reserves)
- **groundedness** - is the answer supported by what was actually retrieved, or invented?

Groundedness is the axis that matters. Grounding here is enforced by the system prompt, not by a
guardrail - contextual grounding is deliberately excluded, see `docs/architecture.md` - so
hallucination is the standing risk and this is the check on it.

Change the prompt, retrieval settings or tool descriptions, redeploy, re-run, read the
side-by-side grid in `promptfoo view`.

**This is not a CI gate and should not become one.** Every row calls the real deployed bot (paid
Bedrock generation, live Primo lookups) plus a paid judge model, each row runs a full agentic
tool-use loop so it is slow, and an LLM judge is noisy at the margin. Run it on demand and before
a release.

## How this differs from `eval/`

| | `eval/` (Bedrock RAG eval) | `eval/promptfoo/` (this) |
|---|---|---|
| Measures | retrieval quality, plus Bedrock's built-in R&G metrics | end-to-end answer behavior + groundedness |
| Talks to | the knowledge base / Bedrock eval service | the **deployed HTTP endpoint**, as a client |
| Grades against | `reference_answer` text | behavioral rubrics (no text matching) |
| Feedback loop | submit a job, wait, read S3 | seconds to minutes, cached, browsable grid |

Complementary and independent. This directory touches nothing outside itself.

## Prerequisites

**A supported Node.** promptfoo requires `^20.20.0 || >=22.22.0` and refuses to start otherwise
with a clear message. Note that Node 22.20.x is *not* supported.

**AWS credentials that can invoke the judge model.** The judge is
`us.anthropic.claude-sonnet-4-6` in `us-west-2`, the same model family the bot generates with.
Credentials come from the ambient environment, so `export AWS_PROFILE=gavilan` is enough. The
principal needs `bedrock:InvokeModel` on the `us.`-prefixed inference profile ARN **and** on the
underlying foundation-model ARNs in the regions that profile routes to - the same grant shape the
query Lambda needs. A single-ARN on-demand grant is not enough, and it fails at runtime rather
than at config validation.

No OpenAI key is needed. promptfoo defaults to OpenAI for `llm-rubric`; the Bedrock grader is
pinned in `defaultTest.options.provider` so it never reaches for one.

**The deployed endpoint URL**, from an env var, because it changes per deploy:

```sh
export GAV_QUERY_URL="https://<api-id>.execute-api.us-west-2.amazonaws.com/query"
```

Take it from the `ChatbotApiUrl` stack output. CORS is irrelevant here - these are server-side
requests, and CORS is browser-enforced only.

## Running

```sh
cd eval/promptfoo
npx promptfoo@latest eval
npx promptfoo@latest view      # side-by-side grid at http://localhost:15500

npx promptfoo@latest eval --no-cache           # REQUIRED after redeploying - see below
npx promptfoo@latest eval --filter-pattern 06  # one row while iterating (matches description)
npx promptfoo@latest eval -j 2                 # lower concurrency (default 4)
npx promptfoo@latest validate config           # offline check, no calls, no cost
```

Concurrency defaults to 4, comfortably under the API Gateway stage throttle (10 rps steady, 20
burst), so the eval will not throttle itself even on a larger set.

### Always use `--no-cache` after you redeploy

promptfoo caches provider responses keyed on the request, and the request does not change when
you edit the system prompt and redeploy - only the *answer* does. A plain re-run therefore
replays the old answers and silently shows you no change. Verified, not assumed: with the cache
on, swapping the backend under a fixed request returned the stale answer.

Rule of thumb: `--no-cache` when the bot changed, cache on when only the rubrics changed.

## Reading the results

Four checks plus one reporting line per row, shown per-cell in `promptfoo view`:

| metric | what fails it |
|---|---|
| `behavior` | did the wrong thing: answered an out-of-scope question, routed an in-scope one, sent the student to the wrong place, asserted something it could not know |
| `groundedness` | contradicted the tool output, invented a specific checkable fact, constructed a URL, or claimed a holding with no support |
| `not_empty` | blank answer - the endpoint or the agent loop broke. Distinguishes "broken" from "bad" |
| `no_dashes` | an em dash or en dash, which the system prompt bans outright |
| `tools_called` | nothing - it always passes. Its `reason` is the ordered list of tools the model invoked and what each returned |

Three rows carry one extra deterministic check each: row 06 (not-held database) must actually say
the database is not available, row 09 must not emit a phone-shaped string, and row 10 must not
name the president. Deliberately few - these are invariants of the system prompt, not guesses
about wording.

**Read the judge's `reason`, not just pass/fail.** On a set this size a single flip is noise; a
cluster of related failures is signal. Check a failure against "Known gaps" below before treating
it as a regression.

## What the judge sees

The eval sets `include_full_context: true`, and the Lambda returns an opt-in debug payload
alongside `{answer, sources}`:

| field | what it is |
|---|---|
| `full_context` | the un-deduped, un-truncated **knowledge-base** passages. One tool's output |
| `tool_calls` | the ordered trace of **every** tool call: `{tool, input, status, returned_results, result}`, where `result` is the exact JSON the model got back as that call's `toolResult` |
| `library_links` | the curated URL table handed to the model in its Converse `system` payload. Not a tool result, so it is in neither field above |

`transformResponse` renders `tool_calls` plus `library_links` into two metadata vars: `tool_trace`
(one line per call) and `tool_context` (every call's full output, with the curated table appended
under a `[SYSTEM CONTEXT]` heading). `tool_calls` is a strict superset of `full_context`, so the
KB-only view is no longer passed to the judge - feeding both would just duplicate the passages.

**This broke twice, the same way.** `full_context` is populated only from `search_library_info`,
so an answer built from `database_catalog` or either Primo tool reached the judge with an empty
context: three of the four tools were invisible, and the rubric had to be written around the
blind spot. Row 05, an authoritative and correct database answer, scored 0.1 on a context the
judge never received. The curated link table hit the same wall later for a different reason - it
stopped being a tool and moved into the `system` payload, so it vanished from `tool_calls` and a
correct, curated URL read as an invented one.

The general lesson, since it will happen again: **moving evidence out of a tool result silently
breaks this judge.** Anything the model sees that is not a `toolResult` has to be added to the
debug payload and named in the rubric, in the same commit that moves it.

## Known gaps: where the staff spec and the bot disagree

`dataset-staff-examples.yaml` encodes the library staff's own worked examples, and the rubrics are
written to that spec rather than to the current prompt - an eval written to match what the system
already does cannot tell you anything. So some rows are expected to fail, and not because the bot
is broken.

Derived from the current `app/system_prompt.md`, `app/data/library_links.json` and `config.yaml`,
**not** from a run. Re-run before treating any of it as a result.

| Staff row | Where it stands |
|---|---|
| S11 safety / medical (911) | Behaviour is covered: `<priority_responses>` fires and returns the 911 / campus-safety reply verbatim. Groundedness is the open half - see below |
| S05 financial aid office | The canonical-links block carries the campus-map URLs, so the bot can point at a real map instead of deflecting |
| S01, S04, S07, S08 link rows | The libguides, bookstore and Primo permalinks these answers use are now entries in `library_links.json`, so `<citations>` permits them. They used to be blocked outright |
| S02b laptop renewal form | The form is emailed to borrowers and is on no ingested page. Probably fine as-is: pointing at the circulation desk is the honest answer |
| S03 college-wide hours | The seed list carries library pages plus finaid, bookstore and the campus map, not college-wide hours. Accept library-only hours, or widen `scraper.tiers` |
| S10 emoji in the answer | `<tone>` bans emoji outright. The staff example ends with two. A spec disagreement, not a bug - decide which one wins and change the loser |

### The groundedness failure the judge gets right

The `<priority_responses>` emergency reply is emitted from the system prompt with **no tool
call**, deliberately: it has to work when every tool and the knowledge base are down. So the
judge sees contact details it cannot trace to any evidence and calls them invented, which is
correct reasoning from what it can see.

The URL half is closed - the public-safety page is a curated entry, and the canonical-links block
reaches the judge as `[SYSTEM CONTEXT]`. The phone number is in no tool result and no curated
entry, so it still has no traceable source.

**Do not patch the rubric for it.** Telling the judge that a phone number may come from the
system prompt would blunt the exact check this eval exists for: invented contact details. The
honest fixes are bot changes - ingest the public-safety page, or add the number to a curated
entry.

## Multi-turn questions

The `/query` contract is multi-turn and client-carried, and the provider body is a template, so a
row can carry prior turns. Set `prior_turns` (oldest first); they are prepended before
`question`:

```yaml
- description: 'S02b laptop renewal follow-up'
  vars:
    prior_turns:
      - role: user
        content: I need a laptop.
      - role: assistant
        content: Gavilan Library has laptops available for students to borrow.
    question: no, I already have a laptop I just need to renew it.
    expected_behavior: >-
      Carry the correction forward and answer about RENEWAL, not borrowing.
```

Rows without `prior_turns` send a single user turn, which is the common case - the set currently
sends 21 single-turn requests and 1 three-turn request. This is how you test conversational
repair, which single-turn rows cannot reach.

## Editing the question set

Two dataset files, both wired into `tests:`: `dataset-staff-examples.yaml` (the staff's 11 worked
examples, the authoritative behavior spec) and `dataset.yaml` (11 generic rows covering the four
tools and the out-of-scope paths). To add a question, append an entry:

```yaml
- description: '12 short label for the grid'
  vars:
    question: What a student actually types
    expected_behavior: >-
      What doing the right thing looks like. Describe what the bot should DO.
```

**`expected_behavior` is guidance for the judge, not an answer to match.** Do not put hours,
prices, phone numbers or titles in it. The bot answers from live data that changes, so a specific
fact written here goes stale and turns the eval into a staleness detector. That is also why
there is no exact-match assertion anywhere in this config.

This matters most with a document of hand-written model answers, like the staff examples. Do not
paste those in as expected text - they are full of live specifics, and matching on them would
fail the bot for being current. Read each one, ask what the answer is *doing*, and write that
down. `dataset-staff-examples.yaml` is the worked example of that translation.

Per-question deterministic checks go in an `assert:` block on the entry; they are added to the
shared checks in `defaultTest`, not instead of them.

**Verify the database names before blaming the bot.** Rows 05 and 06 name specific databases from
the bundled catalog: Opposing Viewpoints In Context from the held list, JSTOR from the
hand-authored not-held list. The not-held list is hand-authored and bundled, so JSTOR is stable;
the held list is regenerated from the library's A-Z page on every full-tier scrape and can drift.

**The bigger golden set** at `eval/datasets/baseline_qa.csv` (27 questions) is deliberately not
wired in. It is built for the Bedrock harness: every row carries a `reference_answer` full of
specific facts, which is the right shape for correctness metrics and the wrong shape for
behavioral rubrics. To adopt it, add an `expected_behavior` column describing behavior rather
than text and point `tests:` at `file://../datasets/baseline_qa.csv`. promptfoo reads CSV columns
as vars, so the names have to line up with what the rubrics reference. Do it as its own change,
with the cost in mind: 27 rows x (1 bot call + 2 judge calls).

## Files

| file | what it is |
|---|---|
| `promptfooconfig.yaml` | HTTP provider (env-driven URL, multi-turn capable), Bedrock judge, the two rubrics + shared deterministic checks + the tool trace |
| `dataset-staff-examples.yaml` | the staff's 11 worked examples, translated into behavior rubrics |
| `dataset.yaml` | 11 generic rows covering the four tools and the out-of-scope paths |
| `results.json` | run output, written by `-o results.json`. Gitignored - it is one deploy at one moment, not a repo artifact |

22 rows total, each costing 1 bot call + 2 judge calls. Nothing outside this directory is
touched, and nothing here is imported by the Lambda, the CDK app, the widget, or `eval/`.
