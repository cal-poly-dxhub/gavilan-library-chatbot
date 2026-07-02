"""Query-path Lambda for the Gavilan Library Chatbot.

Two separate steps per docs/architecture.md, deliberately NOT RetrieveAndGenerate:
  1. retrieve()  -> Bedrock Knowledge Base `Retrieve` for relevant chunks.
  2. generate()  -> Bedrock generation (Converse) over those chunks, under our own
     system prompt, so we keep full prompt control.

Fronted by an API Gateway HTTP API (payload format 2.0). All wiring comes from env
vars set by the CDK stack, never hardcoded here.

NOTE: the system prompt below is a PLACEHOLDER. The real behavior (textbook clarifying
questions, out-of-scope routing) is designed in a separate task, per the hard rule that
behavior lives in the system prompt. Do not invest in prompt quality here.
"""

import base64
import json
import os

import boto3

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
GENERATION_MODEL_ID = os.environ["GENERATION_MODEL_ID"]
# Lambda auto-sets AWS_REGION; BEDROCK_REGION lets the stack pin it explicitly.
REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION")
NUMBER_OF_RESULTS = int(os.environ.get("NUMBER_OF_RESULTS", "5"))

# PLACEHOLDER system prompt. Replace in the prompt-design task. Not tuned on purpose.
SYSTEM_PROMPT_PLACEHOLDER = (
    "PLACEHOLDER SYSTEM PROMPT. You are an assistant for the Gavilan College Library. "
    "Answer operational questions using only the provided context; if the answer is not "
    "in the context, say so. This stub is replaced with the real system prompt later."
)

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


def retrieve(query):
    """Knowledge Base Retrieve API. Returns a list of chunk text strings."""
    response = _agent_client().retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": NUMBER_OF_RESULTS}
        },
    )
    chunks = []
    for result in response.get("retrievalResults", []):
        text = result.get("content", {}).get("text")
        if text:
            chunks.append(text)
    return chunks


def generate(query, chunks):
    """Bedrock Converse generation grounded in the retrieved chunks."""
    if chunks:
        context = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(chunks))
        user_text = f"Context:\n{context}\n\nQuestion: {query}"
    else:
        user_text = f"Question: {query}"

    response = _bedrock_client().converse(
        modelId=GENERATION_MODEL_ID,
        system=[{"text": SYSTEM_PROMPT_PLACEHOLDER}],
        messages=[{"role": "user", "content": [{"text": user_text}]}],
    )
    return response["output"]["message"]["content"][0]["text"]


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
    answer = generate(query, chunks)
    return _response(200, {"answer": answer, "sources_used": len(chunks)})
