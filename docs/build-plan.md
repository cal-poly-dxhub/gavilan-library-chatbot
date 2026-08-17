# Gavilan Library Chatbot - Build Plan

Todo tracker. `architecture.md` is the companion: what the system is, and why.
**Updated:** 2026-08-17

---

## Shipped

- **Infra** - S3 Vectors store, Bedrock KB + S3 data source (FIXED_SIZE, 600 tokens), source and catalog buckets, query Lambda + HTTP API with stage throttling, one input guardrail, widget hosting on S3 + CloudFront (OAC). All L1 `Cfn*`, all knobs in `config.yaml`.
- **Query path** - agentic Converse tool-use loop over four tools; `{answer, sources}` contract; input guardrail before the loop, none on the Converse call.
- **System prompt** - XML-structured, hard-grounding, `<tools>` routing, `<priority_responses>` answering safety messages verbatim without a tool call, `<citations>` governing the two permitted link sources.
- **Curated link table** - canonical Gavilan URLs, bundled. Started as a fifth tool and moved into the `system` payload after the model kept skipping the call.
- **Self-updating catalog** - scraper parses databases.php and enriches with Sonnet; the tool reads S3 with a TTL cache; the hand-authored not-held seed merges at read time; a guard keeps the last-good copy on failure.
- **Live Primo tools** - general catalog + course reserves. Evidence, not verdict: the model judges, `total == 0` is the only not-held signal, and timeouts plus a budget plus soft-fail keep a slow third party from killing a request.
- **Multi-turn** - client-carried history, stateless Lambda, trimmed to 10 messages. Legacy `{query}` still accepted. Not the DynamoDB design, which is V2.
- **Tiered cadence + change gating** - `fast` daily, `full` every five days; uploads, ingestion and enrichment each gated on real change, with no new store.
- **Widget** - vanilla JS, Shadow DOM, self-injecting, markdown answers, sources UI, Gavilan branding. Production `widget.js` only.
- **Bilingual chrome** - one string table plus an optional `language` request field. No corpus change: Spanish already retrieves correctly from the English KB.
- **Accessibility** - WCAG 2.1 AA audit, then remediation of every failing criterion in the widget. See `accessibility-audit.md`.
- **Runtime theming** - one `theme.json`, a hosted settings editor with a Cognito-gated `PUT /theme` save, a hosted guide, and a downloadable copy of the defaults.
- **Demo site** - a second bucket and distribution serving one library-styled page that embeds the shipped widget over the shipped delivery path.
- **Cost visibility** - demo-only meter and estimator behind two opt-ins absent from production, with constants measured against the deployed endpoint.
- **Eval** - Bedrock job-runner, both formatters, a baseline retrieve run, and the promptfoo answer-quality loop.
- **CI** - four hermetic GitHub Actions jobs. No AWS credentials, no deployed endpoint.
- **Validated live** - `/query` end to end. CORS locked to `cors.allow_origins`, wildcard rejected at synth.

## Next

- **Remaining library context** - the Q&A set into `eval/datasets/`. Seed URLs and exclusions are settled in `config.yaml`.
- **Parallelize Primo availability calls** - they run sequentially under a wall-clock budget; fan them out so slow lookups don't push toward the Lambda timeout.
- **A screen-reader session** - the one check standing between the accessibility work and a written conformance claim.
- **Iterate, eval-driven** - tune chunking and prompt behavior; re-run both evals.

## V2

- **Persistent conversation store + logging** - DynamoDB. Single-session multi-turn already shipped, client-carried; this is only the persistence layer.
- **Authenticated Alma API** - real-time due dates and copy counts, past the public Primo endpoint's "catalog shows available" ceiling.
- **Custom chunking Lambda** - only if eval shows boilerplate hurting retrieval.
- **More agent tools** - as gaps surface in eval.
- **Production hardening** - auth if required, WAF only if compliance-mandated.
