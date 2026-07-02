"""Query-path Lambda for the Gavilan Library Chatbot.

Two separate steps:
  1. retrieve()  -> Bedrock Knowledge Base `Retrieve` for relevant chunks + their sources.
  2. generate()  -> Bedrock Converse over those chunks, under the real system prompt
     (app/system_prompt.md).

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
    it does not have the information (rather than guess).
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

# Bedrock Guardrail (content filters + PII) attached to the Converse call. Set by the CDK
# stack from config.yaml. If unset (e.g. local without a deployed guardrail), the Converse
# call is made WITHOUT a guardrailConfig rather than failing.
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION")
GUARDRAIL_TRACE = os.environ.get("GUARDRAIL_TRACE", "enabled")

# Converse stopReason when the guardrail blocks the request or response.
_GUARDRAIL_STOP_REASON = "guardrail_intervened"

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


def _guardrail_config():
    """Converse guardrailConfig, or None if no guardrail is wired (id + version required).

    No guardContent tagging: there is no contextual grounding, so the existing
    <context>-wrapped message structure is unchanged and the guardrail just filters
    content + PII across the whole input and output."""
    if not GUARDRAIL_ID or not GUARDRAIL_VERSION:
        return None
    return {
        "guardrailIdentifier": GUARDRAIL_ID,
        "guardrailVersion": GUARDRAIL_VERSION,
        "trace": GUARDRAIL_TRACE,
    }


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
    """Bedrock Converse generation under the system prompt + guardrail. The system prompt
    goes in Converse `system`; the <context> block and the question go in the user message.

    Returns {"answer": <text>, "blocked": <bool>}. When the guardrail intervenes, `answer`
    is the guardrail's configured blocked message and `blocked` is True."""
    user_text = f"{_build_context_block(chunks)}\n\nQuestion: {query}"

    kwargs = {
        "modelId": GENERATION_MODEL_ID,
        "system": [{"text": SYSTEM_PROMPT}],
        "messages": [{"role": "user", "content": [{"text": user_text}]}],
    }
    guardrail = _guardrail_config()
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

    chunks = retrieve(query)
    result = generate(query, chunks)
    # On a guardrail block the answer is the blocked message; don't attach library sources
    # to it (v1: just return the message, no re-retrieve or escalation).
    sources = [] if result["blocked"] else _build_sources(chunks)
    return _response(200, {"answer": result["answer"], "sources": sources})
