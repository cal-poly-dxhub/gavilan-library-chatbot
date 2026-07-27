# Answer-quality loop (promptfoo)

A fast tweak-and-rerun tuning tool. It fires each of 22 golden questions at the **deployed**
`/query` endpoint and has a Bedrock-hosted Claude judge score the answer on two axes:

- **behavior** - did the bot do the right thing? (answer the operational question, route the
  out-of-scope one, confirm/deny a database authoritatively, send textbooks to reserves...)
- **groundedness** - is the answer supported by what was actually retrieved, or invented?

Groundedness is the axis that matters most here: grounding in this system is enforced by the
system prompt, not by a guardrail (contextual grounding is deliberately excluded - see
`docs/architecture.md`), so hallucination is the standing risk and this is the check on it.

Change the system prompt / retrieval settings / tool descriptions, redeploy, re-run, and read
the side-by-side grid in `promptfoo view`.

**This is not a CI gate, and should not become one.** It calls the real deployed bot (paid
Bedrock generation + live Primo lookups) and a paid judge model on every row, it is slow
because each row runs a full agentic tool-use loop, and an LLM judge is noisy at the margin.
Run it on demand and before a release.

## How this differs from `eval/`

| | `eval/` (Bedrock RAG eval) | `eval/promptfoo/` (this) |
|---|---|---|
| Measures | retrieval quality, plus Bedrock's built-in R&G metrics | end-to-end answer behavior + groundedness |
| Talks to | the knowledge base / Bedrock eval service | the **deployed HTTP endpoint**, as a client |
| Grades against | `reference_answer` text | behavioral rubrics (no text matching) |
| Feedback loop | submit a job, wait, read S3 | seconds to minutes, cached, browsable grid |

They are complementary and independent. This directory touches nothing outside itself.

## Prerequisites

**1. A supported Node.js.** promptfoo requires `^20.20.0 || >=22.22.0` and refuses to start
otherwise, with a clear message. Check with `node --version`. If yours is out of range
(Node 22.20.x in particular is *not* supported), install a supported version - e.g.
`brew install node@22`, or `nvm install 22`.

**2. AWS credentials that can invoke the judge model on Bedrock.** The judge is
`us.anthropic.claude-sonnet-4-6` in `us-west-2` (the same model family the bot generates
with). Credentials come from the ambient environment, so any normal AWS setup works:

```sh
export AWS_PROFILE=gavilan
```

The principal needs `bedrock:InvokeModel` on the `us.`-prefixed inference profile ARN **and**
on the underlying foundation-model ARNs in the regions the profile routes to - the same grant
shape the query Lambda needs (see the "Lessons" section of `CLAUDE.md`). A single-ARN
on-demand grant is not enough and fails at runtime, not at config-validation time.

No OpenAI key is needed. promptfoo defaults to OpenAI for `llm-rubric`; the Bedrock grader is
pinned explicitly in `defaultTest.options.provider` so it never reaches for one.

**3. The deployed endpoint URL**, read from an env var - it is never hardcoded, because it
changes per deploy:

```sh
export GAV_QUERY_URL="https://<api-id>.execute-api.us-west-2.amazonaws.com/query"
```

Take it from the `cdk deploy` stack output. CORS is irrelevant here - these are server-side
requests, and CORS is browser-enforced only.

## Running

From this directory:

```sh
cd eval/promptfoo
npx promptfoo@latest eval
npx promptfoo@latest view      # side-by-side grid at http://localhost:15500
```

Useful flags:

```sh
npx promptfoo@latest eval --no-cache          # REQUIRED after redeploying - see below
npx promptfoo@latest eval --filter-pattern 06  # run one row while iterating (matches description)
npx promptfoo@latest eval -j 2                # lower concurrency (default 4)
npx promptfoo@latest validate config          # offline check, no calls, no cost
```

### ⚠️ Always use `--no-cache` after you redeploy

promptfoo caches provider responses keyed on the request. The request does not change when you
edit the system prompt and redeploy - only the *answer* does. So a plain re-run **replays the
old answers and silently shows you no change**. This was verified, not assumed: with the cache
on, swapping the backend under a fixed request returned the stale answer; with `--no-cache` it
picked up the new one.

Rule of thumb: `--no-cache` when the bot changed, cache on when only the rubrics changed.

Concurrency defaults to 4, comfortably under the API Gateway stage throttle (10 rps steady /
20 burst), so the eval will not throttle itself even on a larger set.

## Reading the results

Each row gets four checks plus one reporting line, and `promptfoo view` shows them per-cell:

| metric | what fails it |
|---|---|
| `behavior` | did the wrong thing: answered an out-of-scope question, routed an in-scope one, sent the student to the wrong place, asserted something it could not know |
| `groundedness` | contradicted the tool output, invented a specific checkable fact, constructed a URL, or claimed a holding with no support |
| `not_empty` | blank answer - the endpoint or the agent loop broke (distinguishes "broken" from "bad") |
| `no_dashes` | an em dash or en dash, which the system prompt bans outright |
| `tools_called` | nothing - it always passes. Its `reason` is the ordered list of tools the model invoked and what each returned. See "Seeing which tools ran". |

Three rows in `dataset.yaml` carry one extra deterministic check each: row 06 (not-held
database) must actually say the database is not available, row 09 must not emit a
phone-shaped string, and row 10 must not name the president. These are deliberately few -
they are invariants of the system prompt, not guesses about wording. Everything else is
judged behaviorally.

**Read the judge's `reason`, not just pass/fail.** With an LLM judge on a set this size, a
single flip is noise; a cluster of related failures is signal. And check a failure against
the "Known gaps" table below before treating it as a regression.

## What the groundedness judge sees

The eval sets `include_full_context: true`, and the Lambda returns an opt-in debug payload
alongside `{answer, sources}`:

| field | what it is |
|---|---|
| `full_context` | `[{text, source}]` - the un-deduped, un-truncated **knowledge-base** passages. One tool's output. |
| `tool_calls` | the ordered trace of **every** tool call the loop made: `{tool, input, status, returned_results, result}`, where `result` is the exact JSON the model got back as that call's `toolResult` content. |

The judge is fed `tool_calls`, rendered by the provider's `transformResponse` into two metadata
vars: `tool_trace` (one line per call) and `tool_context` (every call's full output). It is a
strict superset of `full_context`, so the KB-only view is no longer passed to the judge - doing
both would just duplicate the passages in the prompt.

**This used to be broken, and the fix is recent.** `full_context` is populated only from
`search_library_info`, so an answer built from `database_catalog`, `library_links`, or either
Primo tool reached the judge with an EMPTY context. Groundedness on those rows was not "lenient",
it was ungradeable: four of the five tools were invisible, and the rubric had to be written
around the blind spot ("an empty passage list is never on its own a failure"). Row 05, an
authoritative and correct database answer, scored 0.1 on a context the judge never received.

The rubric is now written to the real thing: a result from ANY tool is support, `held: false`
from `database_catalog` is a citable fact rather than a gap, and a URL in a `library_links`
result is an approved link rather than a suspected fabrication. An empty tool list is still not
a failure on its own - a greeting, a refusal, or a routing reply legitimately calls nothing.

Nothing about the bot's behavior changed to make this work. `tool_calls` is recorded from values
`run_agent` already computed, it is gated on the request flag, and the widget path (no flag) is
byte-identical to what it was.

## Seeing which tools ran

Tool routing is the model's - `toolChoice` is auto and the only steering is the system prompt
plus the `toolSpec` descriptions - so "did it call the tool we expected?" is a real question,
and `library_links` is the sharpest case: it is deliberately **not** named in the system prompt,
so its description alone has to earn the call.

Two places show it per row:

- the **`tools_called` check** in the grid. It always passes; its `reason` is the trace, e.g.
  `1. database_catalog {"query_type":"name","value":"JSTOR"} -> results`.
- **`metadata.tools_used` / `metadata.tool_trace` / `metadata.tool_context`** in `results.json`
  (gitignored). One-liner across all rows:

  ```sh
  npx promptfoo@latest eval --no-cache -o results.json
  python3 -c "import json;[print(r['testCase']['description'],'|',r['metadata']['tools_used']) for r in json.load(open('results.json'))['results']['results']]"
  ```

## Known gaps: where the staff spec and the deployed bot disagree

`dataset-staff-examples.yaml` encodes the library staff's own worked examples. **Some of those
rows are expected to fail today**, and not because the bot is broken - because the staff spec
and the deployed system prompt + knowledge base genuinely disagree. Knowing which is which
before you run it saves you chasing phantom regressions.

Verified against `app/system_prompt.md` and `config.yaml`:

| Staff row | What blocks it | Where the fix is |
|---|---|---|
| ~~**S11 safety / medical (911)**~~ **CLOSED, behavior-wise** | The `<priority_responses>` carve-out in the system prompt now fires on an emergency and returns the 911 / campus-safety response verbatim. Behavior scores 1.0. | Its groundedness still fails, and that is an INSTRUMENT problem, not the bot: the response is emitted from the system prompt with no tool call, so the judge sees a phone number and a URL it cannot trace and calls it invented. See "The one groundedness failure the judge gets wrong". |
| ~~**S05 financial aid office**~~ **CLOSED** | `library_links` returns the campus-map URLs, so the bot points the student at a real map instead of deflecting. | Nothing. It passes. |
| Links to libguides / bookstore / Primo (S01, S04, S07, S08) | `<citations>` forbids constructing URLs, and none of `gavilan.libguides.com`, `gavilan.bkstr.com`, or the Primo permalinks are in `scraper.seed_urls`. The bot cannot legitimately produce them. | Add those pages as scraper seeds so the links arrive as real retrieved sources. |
| S02b laptop renewal form link | The renewal form is emailed to borrowers; it is on no ingested page. | Probably fine as-is - pointing at the circ desk is the honest answer. |
| S03 college-wide hours | Only library pages are ingested. | Accept library-only hours, or widen the seed list. |
| S10 emoji in the model answer | `<tone>` bans emoji outright ("This is an institutional library assistant"). The staff example ends with 😊📚. | A spec disagreement, not a bug. **Decide which one wins** and change the loser. |

The link rows are the remaining product decision. Everything else in the set is a fair test of
the bot as it stands today.

## The one groundedness failure the judge gets wrong

**S11** is the only row still failing groundedness, and the judge is reasoning correctly from
what it can see:

> The bot provided a specific phone number '(408) 848-4703' and a URL
> 'https://www.gavilan.edu/public_safety/index.php' without calling any tools.

Both are real, and both are copied verbatim out of the `<priority_responses>` block in
`app/system_prompt.md`, which deliberately instructs the model to answer an emergency
*without* calling a tool - the response has to work when every tool and the KB are down. So
there is no tool output to trace it to, and there never will be.

**This was left unfixed on purpose.** The obvious patch - telling the rubric that a phone
number may come from the system prompt - would blunt the exact check this eval exists for:
invented contact details. If you want the row green, the honest fixes are to ingest the
public-safety page so the digits arrive as retrieved content, or add them to
`app/data/library_links.json` so `library_links` can supply them. Both are bot changes, not
rubric changes.

The rubrics are deliberately written to the **staff spec**, not to the current prompt. An eval
written to match what the system already does cannot tell you anything.

## Multi-turn questions

The `/query` contract is multi-turn and client-carried, and the provider body is built as a
template, so a row can carry prior conversation turns. Set `prior_turns` (oldest first); they
are prepended before the current `question`:

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

Rows that omit `prior_turns` send a single user turn, which is the common case. Verified: the
set currently sends 21 single-turn requests and 1 three-turn request, all with
`include_full_context: true`.

This is how you test conversational repair - the bot mishearing intent and the student
correcting it - which single-turn rows cannot reach.

## Editing the question set

There are two dataset files, both wired into `tests:`:

| file | what's in it |
|---|---|
| `dataset-staff-examples.yaml` | the library staff's 11 worked examples - the authoritative behavior spec |
| `dataset.yaml` | generic coverage of the four tools and the out-of-scope paths |

Each entry is a question plus `expected_behavior`, the note the behavior rubric interpolates.
To add a question, append an entry:

```yaml
- description: '12 short label for the grid'
  vars:
    question: What a student actually types
    expected_behavior: >-
      What doing the right thing looks like. Describe what the bot should DO.
```

**`expected_behavior` is guidance for the judge, not an answer to match.** Do not put hours,
prices, phone numbers, or titles in it. The bot answers from live data that changes; a
specific fact written here goes stale and turns the eval into a staleness detector instead of
a quality one. That is also why there is no `expected` / exact-match assertion anywhere in
this config.

This matters most when you have a document of hand-written model answers, like the staff
examples. **Do not paste those answers in as expected text.** They are full of live
specifics - "Library hours are ...", `(408) 848-4810`, edition numbers - and matching on them
would fail the bot for being current. Read each model answer, ask "what is this answer
*doing*?", and write that down instead. `dataset-staff-examples.yaml` is the worked example of
that translation.

Per-question deterministic checks go in an `assert:` block on the entry; they are added to the
four shared checks in `defaultTest`, not instead of them.

### Swap in database names you have verified

Rows 05 and 06 name specific databases. They were taken from the bundled catalog
(`app/data/database_catalog.json`): **Opposing Viewpoints In Context** from the held list, and
**JSTOR** from the hand-authored not-held list.

The not-held list is hand-authored and bundled, so JSTOR is stable. **The held list is
regenerated from the library's A-Z page on every weekly scrape and can drift.** Before
treating a row-05 failure as a bot bug, confirm the database is still held - check
<https://www.gavilan.edu/library/databases.php> or ask the deployed bot directly. Swap in a
different name if it has changed.

### Pointing at the bigger golden set

A 27-question set already exists at `eval/datasets/baseline_qa.csv`, deliberately **not** wired
in yet. It is built for the Bedrock harness: every row carries a `reference_answer` with
specific facts (exact semester hours, dates), which is the right shape for Bedrock's
correctness metrics and the wrong shape for behavioral rubrics - most of those facts will have
drifted, so grading against them measures staleness.

To adopt it, add a `expected_behavior` column (or a mapping file) that describes the behavior
rather than the text, then point `tests:` at it:

```yaml
tests: file://../datasets/baseline_qa.csv
```

promptfoo reads a CSV's columns as vars, so the column names have to line up with what the
rubrics reference (`question`, `expected_behavior`). Do this as its own change, with the cost
in mind: 27 rows × (1 bot call + 2 judge calls).

## Files

| file | what it is |
|---|---|
| `promptfooconfig.yaml` | HTTP provider (env-driven URL, multi-turn capable), Bedrock judge, the two rubrics + shared deterministic checks + the tool trace |
| `dataset-staff-examples.yaml` | the library staff's 11 worked examples, translated into behavior rubrics |
| `dataset.yaml` | 11 generic rows covering the four tools and the out-of-scope paths |
| `README.md` | this |
| `results.json` | run output, written by `-o results.json`. **Gitignored** - it is one deploy at one moment, not a repo artifact. |

22 rows total, each costing 1 bot call + 2 judge calls.

Nothing outside this directory is touched, and nothing here is imported by the Lambda, the
CDK app, the widget, or the existing `eval/` harness.
