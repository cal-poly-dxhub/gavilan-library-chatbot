"""Query-path Lambda for the Gavilan Library Chatbot.

Three steps:
  0. _apply_input_guardrail() -> Bedrock `ApplyGuardrail` (source=INPUT) on the BARE user
     query.
  1. retrieve()  -> Bedrock Knowledge Base `Retrieve` for relevant chunks + their sources.
  2. generate()  -> Bedrock Converse over those chunks, under the real system prompt
     (app/system_prompt.md), with the OUTPUT guardrail (content filters, answer side only)
     attached as a backstop.

Fronted by an API Gateway HTTP API (payload format 2.0). Wiring comes from env vars set
by the CDK stack.

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


def _reduced_input_assessment(response):
    """Privacy-safe summary of an input-guardrail assessment for logging: policy/entity
    TYPES and actions and counts only, never the raw matched text (which is the very PII the
    guardrail exists to keep out of plaintext logs - see finding 3.3)."""
    content_filters = []
    topics_blocked = 0
    words_blocked = 0
    pii = {}
    for assessment in response.get("assessments") or []:
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


def _log_input_guardrail(response, decision):
    """Structured, PII-safe log of the input-screen outcome on every request."""
    print(
        json.dumps(
            {
                "event": "input_guardrail",
                "action": response.get("action"),
                "decision": decision,
                "assessment": _reduced_input_assessment(response),
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
    """Structured log of the guardrail outcome on every request, so interventions and PII
    detections are measurable for later tuning. Goes to stdout -> CloudWatch Logs."""
    trace = response.get("trace") or {}
    print(
        json.dumps(
            {
                "event": "guardrail_assessment",
                "stop_reason": response.get("stopReason"),
                "intervened": response.get("stopReason") == _GUARDRAIL_STOP_REASON,
                "assessment": trace.get("guardrail"),
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
    """Pull the user query from an HTTP API (payload format 2.0) event body."""
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
    return data.get("query") or data.get("question")


def _response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def lambda_handler(event, context):
    query = _extract_query(event)
    if not query:
        return _response(400, {"error": "Missing 'query' in request body."})

    # Screen the bare query BEFORE retrieval. A content-filter / prompt-attack hit is blocked
    # here and returns immediately - no retrieval, no generation, no Bedrock spend (finding
    # 2.3). PII is masked and we proceed silently on the masked text (finding 2.1); the
    # retrieved <context> is never screened, so the library's own contact facts survive.
    decision, screened_query = _apply_input_guardrail(query)
    if decision == "block":
        return _response(200, {"answer": screened_query, "sources": []})

    chunks = retrieve(screened_query)
    result = generate(screened_query, chunks)
    # On an OUTPUT guardrail block the answer is the blocked message; don't attach library
    # sources to it (v1: just return the message, no re-retrieve or escalation).
    sources = [] if result["blocked"] else _build_sources(chunks)
    return _response(200, {"answer": result["answer"], "sources": sources})
