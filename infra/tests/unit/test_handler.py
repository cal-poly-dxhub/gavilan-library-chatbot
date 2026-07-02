"""Unit tests for the query-path Lambda handler (app/handler.py).

boto3 is mocked (the client getters are monkeypatched), so no live AWS is touched.
Events use the API Gateway HTTP API payload format 2.0 structure.
"""

import json
import os
import sys
import types
from pathlib import Path

# app/handler.py lives at the repo root, outside the infra package.
_APP_DIR = Path(__file__).resolve().parents[3] / "app"
sys.path.insert(0, str(_APP_DIR))

# The handler does `import boto3`, but boto3 is only present in the Lambda runtime, not
# the local dev venv. Stub it so the import works; the tests monkeypatch the client
# getters anyway, so this stub is never actually called.
if "boto3" not in sys.modules:
    _fake_boto3 = types.ModuleType("boto3")
    _fake_boto3.client = lambda *args, **kwargs: None
    sys.modules["boto3"] = _fake_boto3

# Handler reads these at import time; set them before importing.
os.environ.setdefault("KNOWLEDGE_BASE_ID", "KB123456")
os.environ.setdefault("GENERATION_MODEL_ID", "anthropic.claude-3-5-haiku-20241022-v1:0")
os.environ.setdefault("BEDROCK_REGION", "us-west-2")
os.environ.setdefault("NUMBER_OF_RESULTS", "5")

import handler  # noqa: E402


class FakeAgentRuntime:
    def __init__(self):
        self.calls = []

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "retrievalResults": [
                {"content": {"text": "The library is open 9am to 5pm."}},
                {"content": {"text": "Checkout period is 3 weeks."}},
            ]
        }


class FakeBedrockRuntime:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {
                "message": {"content": [{"text": "The library is open 9am to 5pm."}]}
            }
        }


def _payload_v2_event(body, is_base64=False):
    """A minimal API Gateway HTTP API payload format 2.0 event."""
    return {
        "version": "2.0",
        "routeKey": "POST /query",
        "rawPath": "/query",
        "requestContext": {"http": {"method": "POST", "path": "/query"}},
        "isBase64Encoded": is_base64,
        "body": body,
    }


def test_happy_path(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    monkeypatch.setattr(handler, "_agent_client", lambda: agent)
    monkeypatch.setattr(handler, "_bedrock_client", lambda: bedrock)

    event = _payload_v2_event(json.dumps({"query": "What are the library hours?"}))
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["answer"] == "The library is open 9am to 5pm."
    assert body["sources_used"] == 2
    # Retrieve was called against the configured KB id, not RetrieveAndGenerate.
    assert agent.calls[0]["knowledgeBaseId"] == "KB123456"
    assert agent.calls[0]["retrievalQuery"] == {"text": "What are the library hours?"}
    # Generation received the placeholder system prompt.
    assert bedrock.calls[0]["system"][0]["text"].startswith("PLACEHOLDER")


def test_missing_query_returns_400(monkeypatch):
    # No client should be called when the body has no query.
    monkeypatch.setattr(handler, "_agent_client", lambda: (_ for _ in ()).throw(AssertionError("should not retrieve")))
    event = _payload_v2_event(json.dumps({"not_a_query": "hi"}))
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 400
    assert "error" in json.loads(resp["body"])


def test_base64_encoded_body(monkeypatch):
    import base64

    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    monkeypatch.setattr(handler, "_agent_client", lambda: agent)
    monkeypatch.setattr(handler, "_bedrock_client", lambda: bedrock)

    raw = json.dumps({"query": "hours?"}).encode("utf-8")
    event = _payload_v2_event(base64.b64encode(raw).decode("utf-8"), is_base64=True)
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 200
