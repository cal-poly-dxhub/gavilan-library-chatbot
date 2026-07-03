"""Query-path Lambda for the Gavilan Library Chatbot.

Fronted by an API Gateway HTTP API (payload format 2.0) with two routes on this one Lambda:
  - POST /query -> the real query path (_handle_query):
      0. _apply_input_guardrail() -> Bedrock `ApplyGuardrail` (source=INPUT) on the BARE
         user query.
      1. retrieve()  -> Bedrock Knowledge Base `Retrieve` for relevant chunks + sources.
      2. generate()  -> Bedrock Converse over those chunks, under the real system prompt
         (app/system_prompt.md), with the OUTPUT guardrail (content filters, answer side
         only) attached as a backstop.
  - GET /warm -> _handle_warm(): a retrieval-only pre-warm to wake OpenSearch Serverless
      before the first real query (finding 1.3). No generation, no guardrail.

Wiring comes from env vars set by the CDK stack.

/query response JSON shape:
  {
    "answer": "<generated answer text>",
    "sources": [
      {"uri": "<source page url>", "excerpt": "<short snippet of that passage>"},
      ...
    ]
  }
  - `sources` is deduplicated by uri, in retrieval order. Passages with no resolvable
    source uri are omitted from `sources` (they still inform the answer).
  - On empty retrieval, `sources` is [] and the system prompt instructs the model to say
    it does not have the information.
"""

import base64
import json
import os
from pathlib import Path

import boto3

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
GENERATION_MODEL_ID = os.environ["GENERATION_MODEL_ID"]
# Lambda auto-sets AWS_REGION; BEDROCK_REGION lets the stack pin it explicitly.
REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION")
NUMBER_OF_RESULTS = int(os.environ.get("NUMBER_OF_RESULTS", "5"))

# Generation inference knobs (finding 3.4), wired from config.yaml by the stack. Defaults are
# a safety net for local runs without the env set; config.yaml is the source of truth.
GENERATION_MAX_TOKENS = int(os.environ.get("GENERATION_MAX_TOKENS", "600"))
GENERATION_TEMPERATURE = float(os.environ.get("GENERATION_TEMPERATURE", "0.2"))

# Max characters accepted for a user query (finding 2.6). Over this -> HTTP 400, BEFORE any
# retrieval or guardrail call. The real server-side size control; the widget maxlength is only
# advisory UX, and the platform limits (API GW 10MB / Lambda 6MB) are far too high to protect.
MAX_QUERY_CHARS = int(os.environ.get("MAX_QUERY_CHARS", "2000"))

# Two Bedrock guardrails, set by the CDK stack from config.yaml (see docs/audit-resolutions.md
# 2.1). Either pair may be unset locally, in which case that screen is skipped rather than
# failing:
#   INPUT  - screened on the bare user query BEFORE retrieval via the ApplyGuardrail API
#            (source=INPUT). PII is masked-and-proceeds; content/prompt-attack is blocked.
#   OUTPUT - attached to the Converse call as a backstop on the generated answer only.
INPUT_GUARDRAIL_ID = os.environ.get("INPUT_GUARDRAIL_ID")
INPUT_GUARDRAIL_VERSION = os.environ.get("INPUT_GUARDRAIL_VERSION")
OUTPUT_GUARDRAIL_ID = os.environ.get("OUTPUT_GUARDRAIL_ID")
OUTPUT_GUARDRAIL_VERSION = os.environ.get("OUTPUT_GUARDRAIL_VERSION")
GUARDRAIL_TRACE = os.environ.get("GUARDRAIL_TRACE", "enabled")

# Converse stopReason when the output guardrail blocks the generated response.
_GUARDRAIL_STOP_REASON = "guardrail_intervened"

# ApplyGuardrail response fields (verified against the installed bedrock-runtime model).
# Top-level action is "NONE" or "GUARDRAIL_INTERVENED"; both mask and block report
# GUARDRAIL_INTERVENED, so the mask-vs-block decision comes from the per-item action in the
# assessment: content/topic/word policies only ever "BLOCKED"; PII entities/regexes report
# "ANONYMIZED" when masked or "BLOCKED" when blocked.
_ACTION_INTERVENED = "GUARDRAIL_INTERVENED"
_ITEM_BLOCKED = "BLOCKED"
_ITEM_ANONYMIZED = "ANONYMIZED"

# Last-resort student-facing message if the input guardrail blocks but returns no message
# text (documented behavior is that `outputs` carries the configured block message, so this
# is only a defensive fallback and should not normally be reached).
_FALLBACK_BLOCK_MESSAGE = (
    "I can't help with that request. Try asking about the Gavilan College Library, like "
    "hours, checkouts, and finding materials."
)

# The tag the system prompt expects retrieved passages in. MUST match "<context>" in
# app/system_prompt.md (the prompt/handler contract).
CONTEXT_TAG = "context"

# Max characters of a passage surfaced as a source excerpt in the response.
_EXCERPT_CHARS = 300

# Warm path (finding 1.3). The widget fires GET /warm on page load to wake the OpenSearch
# Serverless collection before the first real query. WARM_PATH is matched against the request
# path; _WARM_QUERY is a throwaway retrieval query (the goal is to spin OSS up, not to get
# useful results).
WARM_PATH = "/warm"
_WARM_QUERY = "library hours"

# The real system prompt is packaged with the Lambda: app/system_prompt.md lives inside
# the from_asset(app/) bundle, next to this file. Read once at cold start.
_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()

_agent_runtime = None
_bedrock_runtime = None


def _agent_client():
    global _agent_runtime
    if _agent_runtime is None:
        _agent_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
    return _agent_runtime


def _bedrock_client():
    global _bedrock_runtime
    if _bedrock_runtime is None:
        _bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock_runtime


def _extract_source(result):
    """Pull a source URI from a KB Retrieve result. Web crawler -> page url; falls back to
    the bedrock source-uri metadata."""
    location = result.get("location") or {}
    for key in (
        "webLocation",
        "s3Location",
        "confluenceLocation",
        "salesforceLocation",
        "sharePointLocation",
    ):
        loc = location.get(key)
        if isinstance(loc, dict):
            uri = loc.get("url") or loc.get("uri")
            if uri:
                return uri
    metadata = result.get("metadata") or {}
    return metadata.get("x-amz-bedrock-kb-source-uri")


def retrieve(query):
    """Knowledge Base Retrieve API. Returns a list of {"text", "source"} dicts."""
    response = _agent_client().retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": NUMBER_OF_RESULTS}
        },
    )
    chunks = []
    for result in response.get("retrievalResults", []):
        text = (result.get("content") or {}).get("text")
        if text:
            chunks.append({"text": text, "source": _extract_source(result)})
    return chunks


def _build_context_block(chunks):
    """Wrap the retrieved chunks in the <context> tag the system prompt expects."""
    if not chunks:
        inner = "(no relevant passages were retrieved)"
    else:
        entries = []
        for i, chunk in enumerate(chunks, start=1):
            entry = f"[{i}] {chunk['text']}"
            if chunk.get("source"):
                entry += f"\nSource: {chunk['source']}"
            entries.append(entry)
        inner = "\n\n".join(entries)
    return f"<{CONTEXT_TAG}>\n{inner}\n</{CONTEXT_TAG}>"


def _output_guardrail_config():
    """Converse guardrailConfig for the OUTPUT backstop, or None if not wired (id + version
    required).

    This guardrail is content-filters-only with input strengths NONE and no PII policy, so
    attaching it to Converse screens the generated answer WITHOUT touching the retrieved
    <context> in the user message. Input screening happens separately in
    _apply_input_guardrail(), so no guardContent tagging is needed here."""
    if not OUTPUT_GUARDRAIL_ID or not OUTPUT_GUARDRAIL_VERSION:
        return None
    return {
        "guardrailIdentifier": OUTPUT_GUARDRAIL_ID,
        "guardrailVersion": OUTPUT_GUARDRAIL_VERSION,
        "trace": GUARDRAIL_TRACE,
    }


def _guardrail_output_text(response):
    """The text the guardrail returned in `outputs` - the masked query (on anonymize) or the
    configured block message (on block). Empty string if absent."""
    for out in response.get("outputs") or []:
        if isinstance(out, dict) and out.get("text"):
            return out["text"]
    return ""


def _classify_input_assessment(response):
    """Reduce an ApplyGuardrail(source=INPUT) response to one decision.

    Returns "block" if any policy hard-blocked the query (content filter, prompt attack,
    denied topic, blocked word, or a PII entity configured to BLOCK); "mask" if the only
    intervention was PII anonymization; "clean" if nothing intervened.

    Both mask and block report action=GUARDRAIL_INTERVENED at the top level, so we inspect
    the per-item actions in the assessment rather than trusting the top-level action alone."""
    if response.get("action") != _ACTION_INTERVENED:
        return "clean"

    blocked = False
    anonymized = False
    for assessment in response.get("assessments") or []:
        for f in (assessment.get("contentPolicy") or {}).get("filters", []) or []:
            if f.get("action") == _ITEM_BLOCKED:
                blocked = True
        for t in (assessment.get("topicPolicy") or {}).get("topics", []) or []:
            if t.get("action") == _ITEM_BLOCKED:
                blocked = True
        word_policy = assessment.get("wordPolicy") or {}
        for w in (word_policy.get("customWords") or []) + (
            word_policy.get("managedWordLists") or []
        ):
            if w.get("action") == _ITEM_BLOCKED:
                blocked = True
        sip = assessment.get("sensitiveInformationPolicy") or {}
        for item in (sip.get("piiEntities") or []) + (sip.get("regexes") or []):
            if item.get("action") == _ITEM_BLOCKED:
                blocked = True
            elif item.get("action") == _ITEM_ANONYMIZED:
                anonymized = True

    if blocked:
        return "block"
    if anonymized:
        return "mask"
    # Intervened but the assessment showed neither a hard block nor an anonymization. This
    # should not happen given the model's action enums; block conservatively rather than
    # silently forwarding a query the guardrail flagged.
    return "block"


def _reduce_assessments(assessments):
    """Privacy-safe summary of guardrail assessments for logging: policy/entity TYPES +
    actions + counts only, NEVER the raw matched text (item["match"] is the very PII/content
    the guardrail exists to keep out of plaintext logs - finding 3.3). Shared by the input
    screen and the output-backstop logging."""
    content_filters = []
    topics_blocked = 0
    words_blocked = 0
    pii = {}
    for assessment in assessments or []:
        for f in (assessment.get("contentPolicy") or {}).get("filters", []) or []:
            content_filters.append({"type": f.get("type"), "action": f.get("action")})
        for t in (assessment.get("topicPolicy") or {}).get("topics", []) or []:
            if t.get("action") == _ITEM_BLOCKED:
                topics_blocked += 1
        word_policy = assessment.get("wordPolicy") or {}
        for w in (word_policy.get("customWords") or []) + (
            word_policy.get("managedWordLists") or []
        ):
            if w.get("action") == _ITEM_BLOCKED:
                words_blocked += 1
        sip = assessment.get("sensitiveInformationPolicy") or {}
        for item in (sip.get("piiEntities") or []) + (sip.get("regexes") or []):
            # Bucket by entity type + action; never include item["match"].
            key = f"{item.get('type') or item.get('name')}:{item.get('action')}"
            pii[key] = pii.get(key, 0) + 1
    return {
        "content_filters": content_filters,
        "topics_blocked": topics_blocked,
        "words_blocked": words_blocked,
        "pii": pii,
    }


def _converse_trace_assessments(trace):
    """Collect the guardrail assessment objects out of a Converse response trace.guardrail,
    ignoring modelOutput and any other raw-text fields (finding 3.3). inputAssessment is a
    map of guardrail-id -> assessment; outputAssessments is a map of guardrail-id -> list of
    assessments (verified against the installed bedrock-runtime Converse model)."""
    guardrail = (trace or {}).get("guardrail") or {}
    collected = []
    input_assessment = guardrail.get("inputAssessment")
    if isinstance(input_assessment, dict):
        collected.extend(a for a in input_assessment.values() if isinstance(a, dict))
    output_assessments = guardrail.get("outputAssessments")
    if isinstance(output_assessments, dict):
        for entries in output_assessments.values():
            if isinstance(entries, list):
                collected.extend(a for a in entries if isinstance(a, dict))
    return collected


def _log_input_guardrail(response, decision):
    """Structured, PII-safe log of the input-screen outcome on every request."""
    print(
        json.dumps(
            {
                "event": "input_guardrail",
                "action": response.get("action"),
                "decision": decision,
                "assessment": _reduce_assessments(response.get("assessments") or []),
            },
            default=str,
        )
    )


def _apply_input_guardrail(query):
    """Screen the bare user query BEFORE retrieval via ApplyGuardrail (source=INPUT).

    Returns a (decision, text) pair:
      ("proceed", <query>)          - clean, or PII masked; run retrieval/generation on <query>
                                      (the masked text when PII was present, silently).
      ("block", <blocked message>)  - content-filter / prompt-attack / PII-block hit; the
                                      caller returns the message with no retrieval or generation.

    If the input guardrail is not wired (local/dev), the query passes through untouched."""
    if not INPUT_GUARDRAIL_ID or not INPUT_GUARDRAIL_VERSION:
        return "proceed", query

    response = _bedrock_client().apply_guardrail(
        guardrailIdentifier=INPUT_GUARDRAIL_ID,
        guardrailVersion=INPUT_GUARDRAIL_VERSION,
        source="INPUT",
        # Bare user text, no qualifiers: qualifiers are the contextual-grounding tagging
        # path, which this project deliberately does not use.
        content=[{"text": {"text": query}}],
    )
    decision = _classify_input_assessment(response)
    _log_input_guardrail(response, decision)

    if decision == "block":
        return "block", _guardrail_output_text(response) or _FALLBACK_BLOCK_MESSAGE
    if decision == "mask":
        # Proceed silently on the masked query; the student is not told masking happened.
        return "proceed", _guardrail_output_text(response) or query
    return "proceed", query


def _first_text(response):
    """The first text block of a Converse response. On a guardrail block this is the
    configured blocked message; otherwise the generated answer."""
    content = (
        response.get("output", {}).get("message", {}).get("content", []) or []
    )
    for block in content:
        if isinstance(block, dict) and "text" in block:
            return block["text"]
    return ""


def _log_guardrail_assessment(response):
    """Structured, PII-safe log of the OUTPUT guardrail outcome on every generation, so
    interventions are measurable for later tuning. Logs stopReason + a REDUCED assessment
    (types/actions/counts), never the raw trace - trace.guardrail carries modelOutput and
    matched text, which must not land in plaintext logs (finding 3.3). -> CloudWatch Logs."""
    stop_reason = response.get("stopReason")
    print(
        json.dumps(
            {
                "event": "guardrail_assessment",
                "stop_reason": stop_reason,
                "intervened": stop_reason == _GUARDRAIL_STOP_REASON,
                "assessment": _reduce_assessments(
                    _converse_trace_assessments(response.get("trace"))
                ),
            },
            default=str,
        )
    )


def generate(query, chunks):
    """Bedrock Converse generation under the system prompt + OUTPUT guardrail. The system
    prompt goes in Converse `system`; the <context> block and the question go in the user
    message.

    Returns {"answer": <text>, "blocked": <bool>}. When the output guardrail intervenes,
    `answer` is the guardrail's configured blocked message and `blocked` is True."""
    user_text = f"{_build_context_block(chunks)}\n\nQuestion: {query}"

    kwargs = {
        "modelId": GENERATION_MODEL_ID,
        "system": [{"text": SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
        # Short, direct, low-variance answers for a factual FAQ bot (finding 3.4). Verified
        # key names/nesting against the installed bedrock-runtime Converse model.
        "inferenceConfig": {
            "maxTokens": GENERATION_MAX_TOKENS,
            "temperature": GENERATION_TEMPERATURE,
        },
    }
    guardrail = _output_guardrail_config()
    if guardrail:
        kwargs["guardrailConfig"] = guardrail

    response = _bedrock_client().converse(**kwargs)
    _log_guardrail_assessment(response)

    return {
        "answer": _first_text(response),
        "blocked": response.get("stopReason") == _GUARDRAIL_STOP_REASON,
    }


def _build_sources(chunks):
    """Deduplicate retrieved chunks by source uri for the response `sources` list."""
    sources = []
    seen = set()
    for chunk in chunks:
        uri = chunk.get("source")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        sources.append({"uri": uri, "excerpt": chunk["text"][:_EXCERPT_CHARS]})
    return sources


def _extract_query(event):
    """Pull and validate the user query from an HTTP API (payload format 2.0) event body.

    Returns a stripped, non-empty string, or None for anything invalid (missing body, bad
    JSON, non-dict payload, or a missing / non-string / blank query). None yields the caller's
    clean 400 - never a downstream 500 (finding 3.2)."""
    body = event.get("body")
    if body is None:
        return None
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    query = data.get("query") or data.get("question")
    # A non-string (e.g. {"query": 123} or {"query": {...}}) is truthy but would blow up inside
    # boto3 as an opaque 500. Reject it here as a clean 400 instead (finding 3.2).
    if not isinstance(query, str):
        return None
    query = query.strip()
    return query or None


def _response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


# Shown to the caller on any upstream failure; the widget renders its retry bubble on any
# non-2xx, so this stays generic and never leaks internals.
_UPSTREAM_ERROR_MESSAGE = (
    "The library assistant is temporarily unavailable. Please try again in a moment."
)


def _error_response(stage, exc):
    """Log a structured {event, stage, error} record and return a clean JSON error (finding
    3.1). 502: an upstream Bedrock/OSS dependency failed. Logs the exception type + message
    (not a raw traceback, and not the user query) so the failing stage is diagnosable without
    leaking a stack trace to the caller."""
    print(
        json.dumps(
            {
                "event": "query_failed",
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
            },
            default=str,
        )
    )
    return _response(502, {"error": _UPSTREAM_ERROR_MESSAGE})


def _request_path(event):
    """Request path for an HTTP API (payload format 2.0) event, e.g. '/query' or '/warm'."""
    http = (event.get("requestContext") or {}).get("http") or {}
    return http.get("path") or event.get("rawPath") or ""


def _handle_warm():
    """Warm path (GET /warm): a single KB Retrieve to wake the OpenSearch Serverless
    collection (which scales to zero after ~10min idle) before the student's first real
    query. No generation and no guardrail input screen - there is no user query to screen,
    and OSS scale-to-zero is the dominant cold-start cost, so warming retrieval is the whole
    point (finding 1.3). The Bedrock Converse path is deliberately left cold."""
    try:
        retrieve(_WARM_QUERY)
    except Exception as exc:  # noqa: BLE001 - warm is fire-and-forget; return a clean error
        return _error_response("warm", exc)
    return _response(200, {"warmed": True})


def _handle_query(event):
    """Query path (POST /query): validate -> input screen -> retrieve -> generate."""
    query = _extract_query(event)
    if not query:
        return _response(400, {"error": "Missing 'query' in request body."})
    # Server-side size cap (finding 2.6): reject an oversized query BEFORE any retrieval or
    # guardrail call. This is a clean 400, distinct from a guardrail block (a 200 carrying the
    # block message).
    if len(query) > MAX_QUERY_CHARS:
        return _response(
            400,
            {"error": f"Query exceeds the maximum length of {MAX_QUERY_CHARS} characters."},
        )

    # Everything past validation touches AWS; wrap it so any fault surfaces as a clean, staged
    # JSON error instead of an opaque 500 (finding 3.1). `stage` names the step that failed.
    # No retry logic in v1.
    stage = "input_guardrail"
    try:
        # Screen the bare query BEFORE retrieval. A content-filter / prompt-attack hit is
        # blocked here and returns immediately - no retrieval, no generation, no Bedrock spend
        # (finding 2.3). PII is masked and we proceed silently on the masked text (finding
        # 2.1); the retrieved <context> is never screened, so contact facts survive.
        decision, screened_query = _apply_input_guardrail(query)
        if decision == "block":
            return _response(200, {"answer": screened_query, "sources": []})
        stage = "retrieve"
        chunks = retrieve(screened_query)
        stage = "generate"
        result = generate(screened_query, chunks)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any AWS/runtime fault
        return _error_response(stage, exc)

    # On an OUTPUT guardrail block the answer is the blocked message; don't attach library
    # sources to it (v1: just return the message, no re-retrieve or escalation).
    sources = [] if result["blocked"] else _build_sources(chunks)
    return _response(200, {"answer": result["answer"], "sources": sources})


def lambda_handler(event, context):
    # Two clean routes on one Lambda: /warm (lightweight retrieval-only pre-warm) and
    # everything else -> the real /query path.
    if _request_path(event) == WARM_PATH:
        return _handle_warm()
    return _handle_query(event)
