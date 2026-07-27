# Answer-quality loop (promptfoo)

A fast tweak-and-rerun tuning tool. It fires each golden question at the **deployed**
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

Each row gets four checks, and `promptfoo view` shows them per-cell:

| metric | what fails it |
|---|---|
| `behavior` | did the wrong thing: answered an out-of-scope question, routed an in-scope one, sent the student to the wrong place, asserted something it could not know |
| `groundedness` | contradicted the retrieved passages, invented a specific checkable fact, constructed a URL, or claimed a holding with no support |
| `not_empty` | blank answer - the endpoint or the agent loop broke (distinguishes "broken" from "bad") |
| `no_dashes` | an em dash or en dash, which the system prompt bans outright |

Two rows carry one extra deterministic check each; row 06 (not-held database) must actually
say the database is not available, and row 10 must not name the president. Row 09 must not
emit a phone-shaped string. These are deliberately few - they are invariants of the system
prompt, not guesses about wording. Everything else is judged behaviorally.

**Read the judge's `reason`, not just pass/fail.** On an 11-row set with an LLM judge, a
single flip is noise; a cluster of related failures is signal.

## The groundedness caveat, and why it is written the way it is

The eval sets `include_full_context: true`, and the Lambda returns `full_context` -
`[{text, source}]`, the un-deduped, un-truncated passages the model actually saw.

**`full_context` is knowledge-base-only.** It is populated from `search_library_info`
retrievals. The other three tools - `database_catalog`, `search_book_catalog`,
`search_course_reserves` - return their results to the model through the tool-result path,
which is not captured in the response. (Verified in `app/handler.py`: `run_agent` accumulates
`collected_chunks` only from the KB search tool, and `_full_context` serializes exactly those.)

So on the database and catalog rows, `full_context` will often be empty even though the bot
answered from solid tool output. The groundedness rubric is written to handle this explicitly:
an empty passage list is never on its own a failure. It fails only on something concrete - a
contradiction, an invented specific, a constructed URL, or an unsupported holding claim.

If you later want true groundedness coverage on the catalog tools, the change is in the
Lambda (have the loop record non-KB tool results into `full_context` too), not here.

## Editing the question set

`dataset.yaml` is the whole dataset. Each entry is a question plus `expected_behavior`, the
note the behavior rubric interpolates. To add a question, append an entry:

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
| `promptfooconfig.yaml` | HTTP provider (env-driven URL), Bedrock judge, the two rubrics + shared deterministic checks |
| `dataset.yaml` | the 11 seed questions and their expected behavior |
| `README.md` | this |

Nothing outside this directory is touched, and nothing here is imported by the Lambda, the
CDK app, the widget, or the existing `eval/` harness.
