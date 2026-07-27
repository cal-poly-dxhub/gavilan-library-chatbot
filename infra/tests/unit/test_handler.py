"""Unit tests for the query-path Lambda handler (app/handler.py).

boto3 is mocked (the client getters are monkeypatched), so no live AWS is touched.
Events use the API Gateway HTTP API payload format 2.0 structure.

Guardrail flow under test:
  - INPUT screen: ApplyGuardrail(source=INPUT) on the bare query BEFORE retrieval. PII is
    masked and we proceed on the masked text; a content-filter/prompt-attack hit blocks and
    returns immediately with no retrieval or generation.
  - OUTPUT backstop: attached to the Converse call, screens the generated answer only.
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
os.environ.setdefault("GENERATION_MAX_TOKENS", "600")
os.environ.setdefault("GENERATION_TEMPERATURE", "0.2")
os.environ.setdefault("MAX_QUERY_CHARS", "2000")
# Guardrail wiring (as the CDK stack would set it): a separate input screen and output backstop.
os.environ.setdefault("INPUT_GUARDRAIL_ID", "gr-input-1")
os.environ.setdefault("INPUT_GUARDRAIL_VERSION", "3")
os.environ.setdefault("OUTPUT_GUARDRAIL_ID", "gr-output-1")
os.environ.setdefault("OUTPUT_GUARDRAIL_VERSION", "7")
os.environ.setdefault("GUARDRAIL_TRACE", "enabled")

import handler  # noqa: E402


# --- ApplyGuardrail response builders (shapes verified against the bedrock-runtime model) --


def clean_input_response():
    """Nothing intervened: the query passes through unchanged."""
    return {"action": "NONE", "outputs": [], "assessments": []}


def masked_input_response(masked_text, *, raw_match="student@example.com", entity="EMAIL"):
    """PII anonymized: action=GUARDRAIL_INTERVENED, the masked text in `outputs`, and the
    per-entity action ANONYMIZED. `raw_match` is the sensitive text the real API echoes in
    `match` - the handler must NOT log it."""
    return {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": masked_text}],
        "assessments": [
            {
                "sensitiveInformationPolicy": {
                    "piiEntities": [
                        {
                            "match": raw_match,
                            "type": entity,
                            "action": "ANONYMIZED",
                            "detected": True,
                        }
                    ]
                }
            }
        ],
    }


def blocked_input_response(block_message, *, filter_type="HATE"):
    """A hard block: action=GUARDRAIL_INTERVENED, the block message in `outputs`, and a
    content-policy filter with action BLOCKED."""
    return {
        "action": "GUARDRAIL_INTERVENED",
        "outputs": [{"text": block_message}],
        "assessments": [
            {
                "contentPolicy": {
                    "filters": [
                        {
                            "type": filter_type,
                            "confidence": "HIGH",
                            "action": "BLOCKED",
                            "detected": True,
                        }
                    ]
                }
            }
        ],
    }


class FakeAgentRuntime:
    """Returns two chunks, each with a web source uri (as the web crawler would)."""

    def __init__(self, results=None):
        self.calls = []
        self._results = results if results is not None else [
            {
                "content": {"text": "The library is open 9am to 5pm."},
                "location": {"webLocation": {"url": "https://gav.edu/library/hours"}},
            },
            {
                "content": {"text": "Checkout period is 3 weeks."},
                "location": {"webLocation": {"url": "https://gav.edu/library/borrow"}},
            },
        ]

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return {"retrievalResults": self._results}


SEARCH_TOOL = "search_library_info"


def tool_use_turn(query="library hours", *, tool_use_id="tu-1", text=None, name=SEARCH_TOOL):
    """A Converse response asking to call a tool (stopReason=tool_use). The assistant message
    carries an optional text block plus a toolUse block, exactly as the real API returns."""
    content = []
    if text is not None:
        content.append({"text": text})
    content.append(
        {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": {"query": query}}}
    )
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
    }


def multi_tool_use_turn(*queries):
    """A single Converse response requesting several tool calls at once (the model can do this)."""
    content = [
        {"toolUse": {"toolUseId": f"tu-{i}", "name": SEARCH_TOOL, "input": {"query": q}}}
        for i, q in enumerate(queries, start=1)
    ]
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
    }


def end_turn(answer, *, trace=None):
    """A terminal Converse response (stopReason=end_turn) carrying the final answer text."""
    resp = {
        "output": {"message": {"role": "assistant", "content": [{"text": answer}]}},
        "stopReason": "end_turn",
    }
    if trace is not None:
        resp["trace"] = trace
    return resp


def search_then_answer(answer, *, query="library hours"):
    """The standard single-search flow: model searches once, then answers."""
    return [tool_use_turn(query=query), end_turn(answer)]


class FakeBedrockRuntime:
    """Stands in for the bedrock-runtime client, which serves BOTH apply_guardrail (input
    screen) and converse (the agent loop). Calls are recorded in separate lists.

    converse() has two modes:
      - scripted: pass `converse_script`, a list of full response dicts returned in order (the
        last one repeats if the loop calls more times than scripted). Use the tool_use_turn /
        end_turn builders to drive the agent loop.
      - legacy single-turn: no script -> every converse() returns one message built from
        `answer` / `stop_reason` / `trace`. stopReason defaults to end_turn, so the loop runs
        exactly once (no tool use) - the shape most non-loop tests want."""

    def __init__(
        self,
        answer="The library is open 9am to 5pm.",
        stop_reason=None,
        trace=None,
        guardrail_response=None,
        converse_script=None,
    ):
        self.converse_calls = []
        self.apply_guardrail_calls = []
        self._answer = answer
        self._stop_reason = stop_reason
        self._trace = trace
        self._guardrail_response = (
            guardrail_response if guardrail_response is not None else clean_input_response()
        )
        self._script = converse_script
        self._i = 0

    def apply_guardrail(self, **kwargs):
        self.apply_guardrail_calls.append(kwargs)
        return self._guardrail_response

    def converse(self, **kwargs):
        self.converse_calls.append(kwargs)
        if self._script is not None:
            resp = self._script[min(self._i, len(self._script) - 1)]
            self._i += 1
            return resp
        # Legacy single-turn: terminal by default so the agent loop runs exactly once.
        resp = {"output": {"message": {"role": "assistant", "content": [{"text": self._answer}]}}}
        resp["stopReason"] = self._stop_reason if self._stop_reason is not None else "end_turn"
        if self._trace is not None:
            resp["trace"] = self._trace
        return resp


def _wire(monkeypatch, agent, bedrock):
    monkeypatch.setattr(handler, "_agent_client", lambda: agent)
    monkeypatch.setattr(handler, "_bedrock_client", lambda: bedrock)


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


def _warm_event():
    """A GET /warm event (HTTP API payload format 2.0, no body)."""
    return {
        "version": "2.0",
        "routeKey": "GET /warm",
        "rawPath": "/warm",
        "requestContext": {"http": {"method": "GET", "path": "/warm"}},
        "isBase64Encoded": False,
    }


def _user_text(bedrock):
    """The text of the single user message sent to Converse."""
    return bedrock.converse_calls[0]["messages"][0]["content"][0]["text"]


def _seed_messages(bedrock):
    """The seed messages (role + flattened text) sent on the FIRST converse call. The loop mutates
    that same list when it appends tool turns, so assert seeds only with a terminal (end_turn)
    first turn, where nothing is appended."""
    out = []
    for m in bedrock.converse_calls[0]["messages"]:
        text = " ".join(b.get("text", "") for b in m.get("content", []) if "text" in b)
        out.append({"role": m["role"], "text": text})
    return out


def _all_seed_text(bedrock):
    """One string of all seed-message text, for 'was this turn dropped?' assertions."""
    return " ".join(m["text"] for m in _seed_messages(bedrock))


def _retrieved_query(agent):
    """The query text the KB Retrieve call actually ran on."""
    return agent.calls[0]["retrievalQuery"]["text"]


class _Boom(Exception):
    """A stand-in for an AWS/runtime fault (ThrottlingException, ValidationException, etc.)."""


def _raise_boom(*args, **kwargs):
    raise _Boom("simulated AWS failure")


def _last_json_log(capsys, event_name):
    """The last stdout line carrying a given structured-log event, parsed."""
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if event_name in ln]
    assert lines, f"expected a {event_name!r} log line; got:\n{out}"
    return json.loads(lines[-1])


def test_real_system_prompt_loaded_from_file():
    # The real prompt is loaded from app/system_prompt.md, not a placeholder string.
    assert handler.SYSTEM_PROMPT.startswith("<role>")
    assert "Gavilan College Library assistant" in handler.SYSTEM_PROMPT
    # The prompt tells the model about the search tool it drives in the agent loop.
    assert "search_library_info" in handler.SYSTEM_PROMPT
    assert "PLACEHOLDER" not in handler.SYSTEM_PROMPT


def test_happy_path_response_shape(monkeypatch):
    # Standard flow: the model searches once, the tool retrieves, the model answers.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        converse_script=search_then_answer("The library is open 9am to 5pm.")
    )
    _wire(monkeypatch, agent, bedrock)

    event = _payload_v2_event(json.dumps({"query": "What are the library hours?"}))
    resp = handler.lambda_handler(event, None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert set(body.keys()) == {"answer", "sources"}
    assert body["answer"] == "The library is open 9am to 5pm."
    # Sources come from the tool's retrieval during the loop.
    assert body["sources"] == [
        {"uri": "https://gav.edu/library/hours", "excerpt": "The library is open 9am to 5pm."},
        {"uri": "https://gav.edu/library/borrow", "excerpt": "Checkout period is 3 weeks."},
    ]
    # The tool ran Retrieve against the configured KB id, not RetrieveAndGenerate.
    assert agent.calls[0]["knowledgeBaseId"] == "KB123456"


def test_system_prompt_passed_via_converse_system_not_message_body(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hours?"})), None
    )

    # System prompt goes in the Converse `system` parameter...
    assert bedrock.converse_calls[0]["system"] == [{"text": handler.SYSTEM_PROMPT}]
    # ...and NOT concatenated into the user message.
    assert handler.SYSTEM_PROMPT not in _user_text(bedrock)


def test_tool_result_fed_back_to_model_with_passages_and_sources(monkeypatch):
    # After the model calls the tool, the loop runs Retrieve and feeds the passages back as a
    # toolResult in the NEXT converse call's messages (no <context> block; retrieval is a tool).
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("Open 9-5."))
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "What are the library hours?"})), None
    )

    # Two converse calls: the tool request, then the final answer.
    assert len(bedrock.converse_calls) == 2
    # The toolConfig advertises all tools; the search tool is present.
    tool_names = {
        t["toolSpec"]["name"] for t in bedrock.converse_calls[0]["toolConfig"]["tools"]
    }
    assert tool_names == {
        "search_library_info",
        "database_catalog",
        "search_book_catalog",
        "search_course_reserves",
        "library_links",
    }
    # The initial user message is the bare question (not a context-wrapped prompt).
    assert _user_text(bedrock) == "What are the library hours?"
    # The second call's messages carry the assistant tool_use turn AND a user toolResult.
    second_msgs = bedrock.converse_calls[1]["messages"]
    tool_results = [
        b["toolResult"] for m in second_msgs for b in m.get("content", []) if "toolResult" in b
    ]
    assert len(tool_results) == 1
    tr = tool_results[0]
    assert tr["toolUseId"] == "tu-1"
    assert tr["status"] == "success"
    passages = tr["content"][0]["json"]["passages"]
    assert {
        "text": "The library is open 9am to 5pm.",
        "source": "https://gav.edu/library/hours",
    } in passages


def test_empty_retrieval_returns_no_sources(monkeypatch):
    agent = FakeAgentRuntime(results=[])
    bedrock = FakeBedrockRuntime(
        converse_script=search_then_answer(
            "I do not have that information. Please ask a librarian."
        )
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "Do you have a pool?"})), None
    )

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["sources"] == []
    assert body["answer"].startswith("I do not have that information")
    # The empty result is signalled back to the model in the toolResult note.
    tr = [
        b["toolResult"]
        for m in bedrock.converse_calls[1]["messages"]
        for b in m.get("content", [])
        if "toolResult" in b
    ][0]
    result_json = tr["content"][0]["json"]
    assert result_json["passages"] == []
    assert "No relevant passages" in result_json["note"]


def test_sources_deduplicated_by_uri(monkeypatch):
    # Two chunks from the same page -> a single source entry.
    agent = FakeAgentRuntime(results=[
        {"content": {"text": "Open 9-5."}, "location": {"webLocation": {"url": "https://gav.edu/library/hours"}}},
        {"content": {"text": "Closed on holidays."}, "location": {"webLocation": {"url": "https://gav.edu/library/hours"}}},
    ])
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("Open 9-5."))
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hours?"})), None
    )
    sources = json.loads(resp["body"])["sources"]
    assert len(sources) == 1
    assert sources[0]["uri"] == "https://gav.edu/library/hours"


def test_chunk_without_source_omitted_from_sources(monkeypatch):
    agent = FakeAgentRuntime(results=[
        {"content": {"text": "A passage with no location."}},
    ])
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("ok"))
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hours?"})), None
    )
    body = json.loads(resp["body"])
    # No resolvable uri -> not in sources, but the chunk still reached the model via the tool.
    assert body["sources"] == []
    passages = [
        b["toolResult"]["content"][0]["json"]["passages"]
        for m in bedrock.converse_calls[1]["messages"]
        for b in m.get("content", [])
        if "toolResult" in b
    ][0]
    assert passages == [{"text": "A passage with no location.", "source": None}]


# --- public source URL vs internal S3 URI --------------------------------------
#
# The scraper writes each document's public original URL into its Bedrock metadata sidecar
# (metadata["source_url"]). _extract_source surfaces that; the internal s3:// URI is only a
# last-resort fallback, and a source that resolves ONLY to s3:// is dropped so no raw bucket
# path ever reaches the client.


def test_extract_source_prefers_public_source_url_metadata():
    # source_url wins over both a location entry and the internal s3 source-uri.
    result = {
        "content": {"text": "x"},
        "location": {"s3Location": {"uri": "s3://kb-bucket/docs/hours.txt"}},
        "metadata": {
            "source_url": "https://www.gavilan.edu/library/hours.php",
            "x-amz-bedrock-kb-source-uri": "s3://kb-bucket/docs/hours.txt",
        },
    }
    assert handler._extract_source(result) == "https://www.gavilan.edu/library/hours.php"


def test_extract_source_falls_back_to_location_url_when_no_source_url():
    # Web-crawler result with no source_url: the public page url from location is used.
    result = {"location": {"webLocation": {"url": "https://gav.edu/page"}}, "metadata": {}}
    assert handler._extract_source(result) == "https://gav.edu/page"


def test_extract_source_falls_back_to_s3_uri_when_no_public_url():
    # No source_url and no location: the internal s3 uri is returned (last resort). It is dropped
    # later by _build_sources, but _extract_source itself still surfaces it (e.g. for eval).
    result = {"metadata": {"x-amz-bedrock-kb-source-uri": "s3://kb-bucket/docs/x.txt"}}
    assert handler._extract_source(result) == "s3://kb-bucket/docs/x.txt"


def test_build_sources_omits_s3_only_sources():
    chunks = [
        {"text": "public passage", "source": "https://gav.edu/a"},
        {"text": "internal doc", "source": "s3://kb-bucket/docs/x.txt"},
        {"text": "no source at all", "source": None},
    ]
    # Only the public source survives; the s3:// one and the sourceless one are omitted.
    assert handler._build_sources(chunks) == [
        {"uri": "https://gav.edu/a", "excerpt": "public passage"}
    ]


def test_source_url_metadata_surfaces_as_public_uri_end_to_end(monkeypatch):
    # An S3-data-source chunk carrying the public source_url sidecar surfaces the PUBLIC url in
    # the response, not the s3:// path.
    agent = FakeAgentRuntime(results=[
        {
            "content": {"text": "Open 9am to 5pm."},
            "location": {"s3Location": {"uri": "s3://kb-bucket/docs/hours.txt"}},
            "metadata": {"source_url": "https://www.gavilan.edu/library/hours.php"},
        },
    ])
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("Open 9am to 5pm."))
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    body = json.loads(resp["body"])
    assert body["sources"] == [
        {"uri": "https://www.gavilan.edu/library/hours.php", "excerpt": "Open 9am to 5pm."}
    ]
    # The internal s3 path never appears anywhere in the response.
    assert "s3://" not in resp["body"]


def test_s3_only_source_omitted_end_to_end(monkeypatch):
    # A document with no public source_url (only the internal s3 uri) contributes NO source: the
    # answer still returns, but with no leaked bucket path.
    agent = FakeAgentRuntime(results=[
        {
            "content": {"text": "Some internal-only passage."},
            "location": {"s3Location": {"uri": "s3://kb-bucket/docs/old-doc.txt"}},
        },
    ])
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("Here is the answer."))
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "x?"})), None)

    body = json.loads(resp["body"])
    assert body["answer"] == "Here is the answer."
    assert body["sources"] == []
    assert "s3://" not in resp["body"]


# --- include_full_context flag -------------------------------------------------


def test_default_response_omits_full_context_widget_path_unchanged(monkeypatch):
    # THE regression that matters: without the flag (what the widget sends) the response is
    # exactly {answer, sources} - no full_context key, byte-for-byte the shipped contract.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("ok"))
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)
    body = json.loads(resp["body"])
    assert set(body.keys()) == {"answer", "sources"}
    assert "full_context" not in body


def test_flag_false_still_omits_full_context(monkeypatch):
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("ok"))
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hours?", "include_full_context": False})), None
    )
    assert "full_context" not in json.loads(resp["body"])


def test_include_full_context_returns_untruncated_undeduped_passages(monkeypatch):
    # With the flag, full_context is every retrieved passage in order - full text, no per-uri
    # dedup, sourceless passages kept - unlike the truncated/deduped public `sources`.
    long_text = "A" * 400  # exceeds the 300-char public excerpt cap
    agent = FakeAgentRuntime(results=[
        {"content": {"text": long_text}, "location": {"webLocation": {"url": "https://gav.edu/a"}}},
        {"content": {"text": "second chunk, same page"}, "location": {"webLocation": {"url": "https://gav.edu/a"}}},
        {"content": {"text": "chunk with no source"}},
    ])
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("ok"))
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hours?", "include_full_context": True})), None
    )
    body = json.loads(resp["body"])

    # Public sources: deduped to one uri, excerpt truncated to 300, sourceless chunk dropped.
    assert len(body["sources"]) == 1
    assert len(body["sources"][0]["excerpt"]) == 300

    # full_context: all three passages, full text, in retrieval order, sourceless one included.
    assert body["full_context"] == [
        {"text": long_text, "source": "https://gav.edu/a"},
        {"text": "second chunk, same page", "source": "https://gav.edu/a"},
        {"text": "chunk with no source", "source": None},
    ]


def test_missing_query_returns_400(monkeypatch):
    # No client should be called when the body has no query (not even the input screen).
    monkeypatch.setattr(
        handler, "_agent_client",
        lambda: (_ for _ in ()).throw(AssertionError("should not retrieve")),
    )
    monkeypatch.setattr(
        handler, "_bedrock_client",
        lambda: (_ for _ in ()).throw(AssertionError("should not call bedrock")),
    )
    event = _payload_v2_event(json.dumps({"not_a_query": "hi"}))
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 400
    assert "error" in json.loads(resp["body"])


# --- Input validation: type + length (findings 3.2, 2.6) -----------------------


def test_non_string_query_returns_400_not_500(monkeypatch):
    # {"query": 123} is truthy JSON but not a string: a clean 400, never a downstream 500.
    # No AWS call is made.
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": 123})), None)

    assert resp["statusCode"] == 400
    assert "error" in json.loads(resp["body"])
    assert bedrock.apply_guardrail_calls == []
    assert agent.calls == []


def test_blank_whitespace_query_returns_400(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "   "})), None)

    assert resp["statusCode"] == 400
    assert bedrock.apply_guardrail_calls == []


def test_query_is_stripped_before_screening(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "  hours?  "})), None)

    # The stripped query is what gets screened and handed to the agent, not the padded raw value.
    assert bedrock.apply_guardrail_calls[0]["content"] == [{"text": {"text": "hours?"}}]
    assert _user_text(bedrock) == "hours?"


def test_over_length_query_returns_400_before_any_aws_call(monkeypatch):
    # An oversized query is rejected (400) BEFORE any retrieval or guardrail call.
    monkeypatch.setattr(handler, "MAX_QUERY_CHARS", 10)
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "a" * 11})), None
    )

    assert resp["statusCode"] == 400
    assert "error" in json.loads(resp["body"])
    # No guardrail screen, no retrieval, no generation - the whole point of the early cap.
    assert bedrock.apply_guardrail_calls == []
    assert agent.calls == []
    assert bedrock.converse_calls == []


def test_query_exactly_at_the_limit_is_allowed(monkeypatch):
    # Strictly greater than the cap is rejected; exactly at the cap is fine.
    monkeypatch.setattr(handler, "MAX_QUERY_CHARS", 10)
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "a" * 10})), None
    )

    assert resp["statusCode"] == 200


def test_base64_encoded_body(monkeypatch):
    import base64

    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    raw = json.dumps({"query": "hours?"}).encode("utf-8")
    event = _payload_v2_event(base64.b64encode(raw).decode("utf-8"), is_base64=True)
    resp = handler.lambda_handler(event, None)
    assert resp["statusCode"] == 200
    assert "answer" in json.loads(resp["body"])


# --- Input screen (ApplyGuardrail source=INPUT) --------------------------------


def test_input_screen_called_with_bare_query_no_qualifiers(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    assert len(bedrock.apply_guardrail_calls) == 1
    call = bedrock.apply_guardrail_calls[0]
    assert call["guardrailIdentifier"] == handler.INPUT_GUARDRAIL_ID
    assert call["guardrailVersion"] == handler.INPUT_GUARDRAIL_VERSION
    assert call["source"] == "INPUT"
    # Bare user text, wrapped exactly as the API expects, with NO grounding qualifiers.
    assert call["content"] == [{"text": {"text": "hours?"}}]
    assert "qualifiers" not in call["content"][0]["text"]


def test_clean_input_proceeds_with_original_query(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime(
        guardrail_response=clean_input_response()
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "What are the hours?"})), None
    )

    assert resp["statusCode"] == 200
    # Clean input -> the agent runs on the original query (it becomes the initial user message).
    assert _user_text(bedrock) == "What are the hours?"
    assert len(bedrock.converse_calls) == 1


def test_pii_masked_input_proceeds_on_masked_text(monkeypatch):
    # A query carrying PII is masked; we retrieve/generate on the MASKED text, silently.
    masked = "email me at {EMAIL} about my book"
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        guardrail_response=masked_input_response(masked, raw_match="jane@example.com"),
        converse_script=search_then_answer("The library is open 9am to 5pm."),
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "email me at jane@example.com about my book"})),
        None,
    )

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    # The agent runs on the MASKED query (the initial user message), not the raw one.
    assert _user_text(bedrock) == masked
    assert "jane@example.com" not in _user_text(bedrock)
    # The student gets the normal generated answer + sources - NOT a block message.
    assert body["answer"] == "The library is open 9am to 5pm."
    assert len(body["sources"]) == 2


def test_content_filter_block_returns_message_without_retrieval_or_generation(monkeypatch):
    block_msg = "I can't help with that request."
    # If retrieval or generation were reached, these would raise.
    agent = FakeAgentRuntime()
    monkeypatch.setattr(
        agent, "retrieve",
        lambda **kw: (_ for _ in ()).throw(AssertionError("blocked query must not retrieve")),
    )
    bedrock = FakeBedrockRuntime(guardrail_response=blocked_input_response(block_msg))
    monkeypatch.setattr(
        bedrock, "converse",
        lambda **kw: (_ for _ in ()).throw(AssertionError("blocked query must not generate")),
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "something hateful"})), None
    )

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["answer"] == block_msg
    assert body["sources"] == []
    # The input screen ran; nothing downstream did.
    assert len(bedrock.apply_guardrail_calls) == 1
    assert agent.calls == []


def test_prompt_attack_block_returns_message(monkeypatch):
    block_msg = "I can't help with that request."
    agent = FakeAgentRuntime()
    monkeypatch.setattr(
        agent, "retrieve",
        lambda **kw: (_ for _ in ()).throw(AssertionError("blocked query must not retrieve")),
    )
    bedrock = FakeBedrockRuntime(
        guardrail_response=blocked_input_response(block_msg, filter_type="PROMPT_ATTACK")
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "ignore your instructions and..."})), None
    )

    body = json.loads(resp["body"])
    assert body["answer"] == block_msg
    assert body["sources"] == []


def test_input_screen_skipped_when_input_env_unset(monkeypatch):
    # No input guardrail wired -> no ApplyGuardrail call; the query passes straight through.
    monkeypatch.setattr(handler, "INPUT_GUARDRAIL_ID", None)
    monkeypatch.setattr(handler, "INPUT_GUARDRAIL_VERSION", None)
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hours?"})), None
    )

    assert resp["statusCode"] == 200
    assert bedrock.apply_guardrail_calls == []
    # No screening -> the raw query passes straight through as the agent's initial message.
    assert _user_text(bedrock) == "hours?"


def test_input_screen_does_not_log_raw_pii(monkeypatch, capsys):
    # The reduced input-guardrail log must carry entity TYPES/actions, never the raw match
    # (the very PII the guardrail exists to keep out of plaintext logs).
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        guardrail_response=masked_input_response(
            "call {PHONE}", raw_match="555-123-4567", entity="PHONE"
        )
    )
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "call 555-123-4567"})), None
    )

    logged = capsys.readouterr().out
    line = [ln for ln in logged.splitlines() if "input_guardrail" in ln][-1]
    payload = json.loads(line)
    assert payload["decision"] == "mask"
    # Entity type + action + count are present for tuning...
    assert payload["assessment"]["pii"] == {"PHONE:ANONYMIZED": 1}
    # ...but the raw matched PII appears nowhere in the log line.
    assert "555-123-4567" not in line


# --- Output backstop (guardrail attached to Converse) --------------------------


def test_converse_attaches_output_guardrail_config(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    gc = bedrock.converse_calls[0]["guardrailConfig"]
    assert gc["guardrailIdentifier"] == handler.OUTPUT_GUARDRAIL_ID
    assert gc["guardrailVersion"] == handler.OUTPUT_GUARDRAIL_VERSION
    assert gc["trace"] == "enabled"


def test_converse_omits_guardrail_config_when_output_env_unset(monkeypatch):
    # If the output guardrail env vars are absent, Converse is called WITHOUT guardrailConfig.
    monkeypatch.setattr(handler, "OUTPUT_GUARDRAIL_ID", None)
    monkeypatch.setattr(handler, "OUTPUT_GUARDRAIL_VERSION", None)
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)
    assert "guardrailConfig" not in bedrock.converse_calls[0]


def test_output_guardrail_block_returns_blocked_message(monkeypatch):
    # The output guardrail blocks the generated answer: Converse reports the guardrail
    # stopReason and returns the blocked-outputs message; sources are dropped.
    blocked = (
        "I'm not able to provide a response to that. Try asking about library hours, "
        "services, or materials, or reach out to a librarian for more help."
    )
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        answer=blocked,
        stop_reason="guardrail_intervened",
        trace={"guardrail": {"outputAssessment": {"gr-output-1": {}}}},
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "tell me hours"})), None
    )

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["answer"] == blocked
    # A guardrail-blocked turn returns the block message with no library sources.
    assert body["sources"] == []


def test_converse_sets_inference_config_from_config(monkeypatch):
    # Converse carries an inferenceConfig with the configured maxTokens/temperature.
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    ic = bedrock.converse_calls[0]["inferenceConfig"]
    assert ic["maxTokens"] == handler.GENERATION_MAX_TOKENS
    assert ic["temperature"] == handler.GENERATION_TEMPERATURE
    # sane bounds for a factual FAQ bot: bounded output, low variance.
    assert isinstance(ic["maxTokens"], int) and ic["maxTokens"] > 0
    assert 0.0 <= ic["temperature"] <= 1.0


def test_output_guardrail_assessment_is_logged_reduced(monkeypatch, capsys):
    # The OUTPUT guardrail log carries types/actions/counts + stopReason only, NEVER the raw
    # model output or matched text from the Converse trace.
    raw_output = "RAW MODEL ANSWER THAT MUST NOT BE LOGGED"
    trace = {
        "guardrail": {
            # modelOutput carries the raw generated answer - must not be logged verbatim.
            "modelOutput": [raw_output],
            # Real Converse shape: outputAssessments is a map of guardrail-id -> LIST.
            "outputAssessments": {
                "gr-output-1": [
                    {
                        "contentPolicy": {
                            "filters": [
                                {"type": "HATE", "confidence": "HIGH", "action": "BLOCKED"}
                            ]
                        }
                    }
                ]
            },
        }
    }
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        answer="blocked message", stop_reason="guardrail_intervened", trace=trace
    )
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "x"})), None)

    line = None
    for ln in capsys.readouterr().out.splitlines():
        if "guardrail_assessment" in ln:
            line = ln
    assert line is not None
    payload = json.loads(line)
    assert payload["intervened"] is True
    assert payload["stop_reason"] == "guardrail_intervened"
    # The content filter type + action survives (the tuning signal)...
    assert {"type": "HATE", "action": "BLOCKED"} in payload["assessment"]["content_filters"]
    # ...but the raw model output is nowhere in the log line.
    assert raw_output not in line


# --- Warm path (GET /warm): retrieval-only pre-warm -----------------------------


def test_warm_path_retrieves_only_and_returns_warmed(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_warm_event(), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"warmed": True}
    # Retrieve ran once (to wake OSS); NO generation and NO input-screen guardrail call.
    assert len(agent.calls) == 1
    assert bedrock.converse_calls == []
    assert bedrock.apply_guardrail_calls == []


def test_warm_path_uses_a_fixed_throwaway_query(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_warm_event(), None)

    # The warm query is the module's fixed string, not any user input.
    assert agent.calls[0]["retrievalQuery"]["text"] == handler._WARM_QUERY


def test_query_path_still_dispatches_when_path_is_query(monkeypatch):
    # Regression: a normal POST /query event routes to the real query path (retrieve+generate),
    # not the warm path.
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hours?"})), None
    )

    assert resp["statusCode"] == 200
    assert "answer" in json.loads(resp["body"])
    assert len(bedrock.converse_calls) == 1
    assert len(bedrock.apply_guardrail_calls) == 1


# --- Exception handling: clean, staged errors -----------------------------------


def _assert_clean_error(resp, capsys, expected_stage):
    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert "error" in body and "answer" not in body  # a clean JSON error, not a partial answer
    rec = _last_json_log(capsys, "query_failed")
    assert rec["stage"] == expected_stage
    assert "_Boom" in rec["error"]  # type + message, not a raw traceback


def test_input_guardrail_failure_returns_clean_staged_error(monkeypatch, capsys):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    monkeypatch.setattr(bedrock, "apply_guardrail", _raise_boom)
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    _assert_clean_error(resp, capsys, "input_guardrail")
    # Nothing downstream of the failed stage ran.
    assert agent.calls == []
    assert bedrock.converse_calls == []


def test_agent_tool_retrieve_failure_returns_clean_staged_error(monkeypatch, capsys):
    # The tool's Retrieve raises after the model requests it -> the whole agent step reports as
    # the "agent" stage (retrieval now lives inside the loop, not a separate stage).
    agent = FakeAgentRuntime()
    monkeypatch.setattr(agent, "retrieve", _raise_boom)
    bedrock = FakeBedrockRuntime(converse_script=[tool_use_turn()])
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    _assert_clean_error(resp, capsys, "agent")
    # Input screen ran (clean); the model made one converse call requesting the tool.
    assert len(bedrock.apply_guardrail_calls) == 1
    assert len(bedrock.converse_calls) == 1


def test_agent_converse_failure_returns_clean_staged_error(monkeypatch, capsys):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    monkeypatch.setattr(bedrock, "converse", _raise_boom)
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    _assert_clean_error(resp, capsys, "agent")
    # Converse failed on the first turn, before any tool retrieval.
    assert agent.calls == []


def test_warm_failure_returns_clean_error_not_opaque(monkeypatch, capsys):
    # /warm shouldn't be the one raw route left. A retrieve fault -> clean 502, not an opaque
    # unhandled 500. The widget ignores it anyway (fire-and-forget).
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    monkeypatch.setattr(agent, "retrieve", _raise_boom)
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_warm_event(), None)

    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert "error" in body and body.get("warmed") is None
    rec = _last_json_log(capsys, "query_failed")
    assert rec["stage"] == "warm"


# --- Agent tool-use loop --------------------------------------------------------


def test_agent_executes_tool_then_terminates(monkeypatch):
    # The canonical loop: tool_use turn -> run tool -> feed toolResult back -> end_turn.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("Open 9am to 5pm."))
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    body = json.loads(resp["body"])
    assert body["answer"] == "Open 9am to 5pm."
    # The tool ran exactly once, on the query the MODEL chose (not the user's raw text).
    assert len(agent.calls) == 1
    assert _retrieved_query(agent) == "library hours"
    # Two converse calls: request the tool, then answer after seeing the result.
    assert len(bedrock.converse_calls) == 2
    # The loop terminated (did not hit the cap).
    assert len(bedrock.converse_calls) < handler.MAX_AGENT_ITERATIONS


def test_no_tool_use_direct_answer_returns_empty_sources(monkeypatch):
    # The model answers a greeting directly (end_turn, no tool call): no retrieval, empty sources.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("Hi! How can I help with the library?")])
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hello"})), None)

    body = json.loads(resp["body"])
    assert body["answer"] == "Hi! How can I help with the library?"
    assert body["sources"] == []
    # The tool was never called.
    assert agent.calls == []
    assert len(bedrock.converse_calls) == 1


def test_sources_accumulate_across_multiple_tool_calls(monkeypatch):
    # The model searches twice (two sequential tool_use turns) before answering; sources from
    # BOTH retrievals accumulate. retrieve() is monkeypatched to return per-query results.
    def fake_retrieve(query):
        if "hours" in query:
            return [{"text": "Open 9-5.", "source": "https://gav.edu/hours"}]
        return [{"text": "3 week checkout.", "source": "https://gav.edu/borrow"}]

    monkeypatch.setattr(handler, "retrieve", fake_retrieve)
    bedrock = FakeBedrockRuntime(
        converse_script=[
            tool_use_turn(query="hours", tool_use_id="tu-1"),
            tool_use_turn(query="checkout", tool_use_id="tu-2"),
            end_turn("Open 9-5, 3 week checkout."),
        ]
    )
    monkeypatch.setattr(handler, "_bedrock_client", lambda: bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours and checkout?"})), None)

    body = json.loads(resp["body"])
    uris = {s["uri"] for s in body["sources"]}
    assert uris == {"https://gav.edu/hours", "https://gav.edu/borrow"}
    assert len(bedrock.converse_calls) == 3


def test_multiple_tool_use_blocks_in_one_response(monkeypatch):
    # The model requests TWO tool calls in a single turn; both execute and both toolResults come
    # back in ONE following user message.
    def fake_retrieve(query):
        return [{"text": f"result for {query}", "source": f"https://gav.edu/{query}"}]

    monkeypatch.setattr(handler, "retrieve", fake_retrieve)
    bedrock = FakeBedrockRuntime(
        converse_script=[multi_tool_use_turn("hours", "printing"), end_turn("Here is both.")]
    )
    monkeypatch.setattr(handler, "_bedrock_client", lambda: bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours and printing?"})), None)

    body = json.loads(resp["body"])
    # Both retrievals contributed sources.
    assert {s["uri"] for s in body["sources"]} == {
        "https://gav.edu/hours",
        "https://gav.edu/printing",
    }
    # The second converse call carries a single user message with TWO toolResult blocks.
    second_msgs = bedrock.converse_calls[1]["messages"]
    tool_result_msgs = [
        m for m in second_msgs
        if m.get("role") == "user" and any("toolResult" in b for b in m.get("content", []))
    ]
    assert len(tool_result_msgs) == 1
    results = [b for b in tool_result_msgs[0]["content"] if "toolResult" in b]
    assert len(results) == 2
    assert {r["toolResult"]["toolUseId"] for r in results} == {"tu-1", "tu-2"}


def test_max_iterations_cap_stops_the_loop(monkeypatch):
    # A model that ALWAYS asks for the tool must not loop forever: the cap bounds converse calls
    # and a response is still returned.
    agent = FakeAgentRuntime()
    # Script shorter than the cap; the fake repeats its last entry, so every turn is tool_use.
    bedrock = FakeBedrockRuntime(converse_script=[tool_use_turn(text="let me look that up")])
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    assert resp["statusCode"] == 200
    # Converse and the tool were each called exactly MAX_AGENT_ITERATIONS times, then we stopped.
    assert len(bedrock.converse_calls) == handler.MAX_AGENT_ITERATIONS
    assert len(agent.calls) == handler.MAX_AGENT_ITERATIONS
    # A best-effort answer still comes back (the model's running text here).
    body = json.loads(resp["body"])
    assert body["answer"] == "let me look that up"


def test_max_iterations_cap_falls_back_when_no_answer_text(monkeypatch):
    # If the capped loop never produced any answer text, a graceful fallback is returned.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[tool_use_turn()])  # no text block, ever
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    body = json.loads(resp["body"])
    assert body["answer"] == handler._MAX_ITERS_FALLBACK_MESSAGE
    assert len(bedrock.converse_calls) == handler.MAX_AGENT_ITERATIONS


def test_output_guardrail_applied_on_every_converse_call(monkeypatch):
    # The OUTPUT guardrail must attach to EVERY turn of the loop, not just the first, so the
    # final answer is always screened.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("Open 9-5."))
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    assert len(bedrock.converse_calls) == 2
    for call in bedrock.converse_calls:
        gc = call["guardrailConfig"]
        assert gc["guardrailIdentifier"] == handler.OUTPUT_GUARDRAIL_ID
        assert gc["guardrailVersion"] == handler.OUTPUT_GUARDRAIL_VERSION


def test_guardrail_block_mid_loop_returns_block_and_no_sources(monkeypatch):
    # If the OUTPUT guardrail intervenes on a turn AFTER a tool ran, the block message is
    # returned and the accumulated sources are dropped.
    agent = FakeAgentRuntime()
    blocked = "I'm not able to provide a response to that."
    bedrock = FakeBedrockRuntime(
        converse_script=[
            tool_use_turn(),
            {
                "output": {"message": {"role": "assistant", "content": [{"text": blocked}]}},
                "stopReason": "guardrail_intervened",
            },
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    body = json.loads(resp["body"])
    assert body["answer"] == blocked
    assert body["sources"] == []  # dropped despite the tool having retrieved
    assert len(agent.calls) == 1


def test_unknown_tool_request_returns_error_tool_result(monkeypatch):
    # Defensive: if the model asks for a tool we did not define, the loop returns a status=error
    # toolResult (so the model can recover) rather than crashing.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        converse_script=[
            tool_use_turn(name="some_other_tool"),
            end_turn("Sorry, I could not look that up."),
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    assert resp["statusCode"] == 200
    # The unknown tool did not trigger a KB retrieval...
    assert agent.calls == []
    # ...and the toolResult fed back carries status=error.
    tr = [
        b["toolResult"]
        for m in bedrock.converse_calls[1]["messages"]
        for b in m.get("content", [])
        if "toolResult" in b
    ][0]
    assert tr["status"] == "error"
    assert "Unknown tool" in tr["content"][0]["json"]["error"]


# --- Phase 2a: database_catalog tool -------------------------------------------


CATALOG_TOOL = "database_catalog"


def catalog_tool_use_turn(query_type="name", value="JSTOR", *, tool_use_id="tu-1"):
    """A Converse response asking to call database_catalog."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": tool_use_id,
                            "name": CATALOG_TOOL,
                            "input": {"query_type": query_type, "value": value},
                        }
                    }
                ],
            }
        },
        "stopReason": "tool_use",
    }


def _catalog_tool_results(bedrock, call_index=1):
    """The toolResult json blocks carried into a later converse call's messages."""
    return [
        b["toolResult"]
        for m in bedrock.converse_calls[call_index]["messages"]
        for b in m.get("content", [])
        if "toolResult" in b
    ]


# -- Catalog lookup functions (called directly; no loop) --


def test_catalog_name_lookup_held_database():
    result = handler._catalog_name_lookup("Opposing Viewpoints")
    assert result["held"] is True
    assert result["name"] == "Opposing Viewpoints In Context"
    assert "controversial issues" in result["subjects"]


def test_catalog_name_lookup_not_held_jstor_suggests_alternatives():
    result = handler._catalog_name_lookup("JSTOR")
    assert result["held"] is False
    assert result["name"] == "JSTOR"
    # A real held alternative is suggested.
    assert "EbscoHost Core Search" in result["suggested_alternatives"]


def test_catalog_name_lookup_psycinfo_alias_maps_to_held_neighbor():
    # "PsychINFO" (a common misspelling / alias) resolves to the not-held PsycINFO entry, whose
    # alternative is the held Psychology & Behavioral Sciences Collection.
    result = handler._catalog_name_lookup("PsychINFO")
    assert result["held"] is False
    assert result["name"] == "PsycINFO"
    assert result["suggested_alternatives"] == ["Psychology & Behavioral Sciences Collection"]


def test_catalog_name_lookup_alias_matches_held_database():
    # "EBSCO" is an alias of EbscoHost Core Search.
    result = handler._catalog_name_lookup("EBSCO")
    assert result["held"] is True
    assert result["name"] == "EbscoHost Core Search"


def test_catalog_name_lookup_unknown_is_authoritatively_not_held():
    result = handler._catalog_name_lookup("Totally Made Up Database 9000")
    assert result["held"] is False
    assert "not found" in result["note"].lower()
    # Falls back to the catalog's default alternative rather than a curated one.
    assert result["suggested_alternatives"] == ["EbscoHost Core Search"]


def test_catalog_subject_lookup_business_returns_business_databases():
    result = handler._catalog_subject_lookup("business")
    names = {db["name"] for db in result["databases"]}
    assert "Business Source Complete" in names
    assert "Statista.com" in names


def test_catalog_subject_lookup_psychology_returns_the_psych_collection():
    result = handler._catalog_subject_lookup("psychology")
    names = {db["name"] for db in result["databases"]}
    assert "Psychology & Behavioral Sciences Collection" in names


def test_catalog_subject_lookup_unknown_subject_is_empty_with_note():
    result = handler._catalog_subject_lookup("underwater basket weaving")
    assert result["databases"] == []
    assert "note" in result


def test_run_catalog_tool_dispatches_name_vs_subject_and_bad_input():
    name_res, src = handler._run_catalog_tool({"query_type": "name", "value": "JSTOR"})
    assert name_res["held"] is False and src == handler._CATALOG_SOURCE
    subj_res, src2 = handler._run_catalog_tool({"query_type": "subject", "value": "history"})
    assert "databases" in subj_res and src2 == handler._CATALOG_SOURCE
    # Missing value -> error result, and NO synthetic source contributed.
    err, src3 = handler._run_catalog_tool({"query_type": "name", "value": ""})
    assert "error" in err and src3 is None


# -- Catalog tool inside the agent loop --


def test_agent_routes_named_database_query_to_catalog_not_search(monkeypatch):
    # The model requests database_catalog for "do you have JSTOR?"; the KB search tool is NOT run.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        converse_script=[
            catalog_tool_use_turn("name", "JSTOR"),
            end_turn("We do not have JSTOR, but you can try EbscoHost Core Search."),
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "do you have JSTOR?"})), None)

    body = json.loads(resp["body"])
    assert body["answer"].startswith("We do not have JSTOR")
    # KB retrieval never ran (catalog handled it).
    assert agent.calls == []
    # The catalog toolResult fed back reports not-held.
    (tr,) = _catalog_tool_results(bedrock)
    assert tr["status"] == "success"
    assert tr["content"][0]["json"]["held"] is False


def test_catalog_answer_contributes_synthetic_databases_source(monkeypatch):
    # A catalog answer has no scraped-page passage, so its source is the A-Z databases page.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        converse_script=[catalog_tool_use_turn("subject", "business"), end_turn("Here you go.")]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "what databases for business?"})), None
    )

    body = json.loads(resp["body"])
    assert body["sources"] == [handler._CATALOG_SOURCE]
    assert body["sources"][0]["uri"].endswith("databases.php")


def test_catalog_and_search_sources_merge_and_dedupe(monkeypatch):
    # Model uses BOTH tools: search (KB passages) + catalog (synthetic source). Sources merge.
    agent = FakeAgentRuntime()  # default two KB chunks
    bedrock = FakeBedrockRuntime(
        converse_script=[
            tool_use_turn(query="library hours", tool_use_id="tu-1"),
            catalog_tool_use_turn("name", "Opposing Viewpoints", tool_use_id="tu-2"),
            end_turn("Combined answer."),
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours and databases"})), None)

    uris = [s["uri"] for s in json.loads(resp["body"])["sources"]]
    # Two KB sources + one catalog source, no duplicates.
    assert "https://gav.edu/library/hours" in uris
    assert "https://gav.edu/library/borrow" in uris
    assert handler._CATALOG_SOURCE["uri"] in uris
    assert len(uris) == len(set(uris)) == 3


def test_catalog_source_deduped_across_multiple_catalog_calls(monkeypatch):
    # Two catalog calls contribute only ONE synthetic databases source.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        converse_script=[
            catalog_tool_use_turn("name", "JSTOR", tool_use_id="tu-1"),
            catalog_tool_use_turn("name", "PsycINFO", tool_use_id="tu-2"),
            end_turn("Neither is held."),
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "jstor and psycinfo?"})), None)
    assert json.loads(resp["body"])["sources"] == [handler._CATALOG_SOURCE]


def test_guardrail_block_drops_catalog_source(monkeypatch):
    # A guardrail block after a catalog call returns the block message with NO sources.
    agent = FakeAgentRuntime()
    blocked = "I'm not able to provide a response to that."
    bedrock = FakeBedrockRuntime(
        converse_script=[
            catalog_tool_use_turn("name", "JSTOR"),
            {
                "output": {"message": {"role": "assistant", "content": [{"text": blocked}]}},
                "stopReason": "guardrail_intervened",
            },
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "x"})), None)
    body = json.loads(resp["body"])
    assert body["answer"] == blocked
    assert body["sources"] == []


def test_both_tools_advertised_with_differentiated_descriptions(monkeypatch):
    # The toolConfig carries all tools, and the catalog input schema uses query_type/value.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("hi")])
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hi"})), None)

    tools = {t["toolSpec"]["name"]: t["toolSpec"] for t in bedrock.converse_calls[0]["toolConfig"]["tools"]}
    assert set(tools) == {
        "search_library_info",
        "database_catalog",
        "search_book_catalog",
        "search_course_reserves",
        "library_links",
    }
    cat_schema = tools["database_catalog"]["inputSchema"]["json"]
    assert set(cat_schema["properties"]) == {"query_type", "value"}
    assert cat_schema["properties"]["query_type"]["enum"] == ["name", "subject"]
    assert cat_schema["required"] == ["query_type", "value"]


# --- Phase 2b: catalog read from S3 (with seed fallback + not-held merge) -------------------


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeCatalogS3:
    """Stand-in for the S3 client the catalog tool reads its held list from."""

    def __init__(self, held=None, raise_exc=None):
        self._held = held
        self._raise = raise_exc
        self.get_calls = 0

    def get_object(self, Bucket, Key):
        self.get_calls += 1
        if self._raise is not None:
            raise self._raise
        return {"Body": _FakeBody(json.dumps({"held": self._held}).encode("utf-8"))}


def _use_s3_catalog(monkeypatch, fake):
    monkeypatch.setattr(handler, "CATALOG_BUCKET", "cat-bucket")
    monkeypatch.setattr(handler, "_catalog_s3_client", lambda: fake)
    handler._catalog_cache["catalog"] = None
    handler._catalog_cache["at"] = 0.0


def test_catalog_held_is_read_from_s3(monkeypatch):
    # The held list comes from the scraper-written S3 object, not the bundled seed.
    fake = _FakeCatalogS3(held=[
        {"name": "Fresh DB", "subjects": ["freshtopic"], "description": "brand new", "aliases": ["FDB"]}
    ])
    _use_s3_catalog(monkeypatch, fake)

    result = handler._catalog_name_lookup("Fresh DB")
    assert result["held"] is True and result["name"] == "Fresh DB"
    # Alias from S3 held matches too.
    assert handler._catalog_name_lookup("FDB")["held"] is True
    # Subject lookup uses the S3 held subjects.
    subj = handler._catalog_subject_lookup("freshtopic")
    assert {d["name"] for d in subj["databases"]} == {"Fresh DB"}


def test_not_held_survives_from_seed_when_held_comes_from_s3(monkeypatch):
    # The scraper only writes `held`; the hand-authored not_held stays in the bundled seed and is
    # merged at read time - so "do you have JSTOR?" still returns not-held + alternatives.
    _use_s3_catalog(monkeypatch, _FakeCatalogS3(held=[{"name": "Fresh DB", "subjects": [], "description": "d"}]))
    jstor = handler._catalog_name_lookup("JSTOR")
    assert jstor["held"] is False
    assert "Psychology & Behavioral Sciences Collection" not in jstor["suggested_alternatives"]  # JSTOR's alts
    assert jstor["suggested_alternatives"]  # has alternatives from the seed not_held


def test_s3_read_failure_falls_back_to_seed_held(monkeypatch):
    # If the S3 read fails (pre-first-scrape or an outage), the bundled seed held is used so the
    # tool still works. A seed database resolves as held.
    _use_s3_catalog(monkeypatch, _FakeCatalogS3(raise_exc=RuntimeError("no such key")))
    assert handler._catalog_name_lookup("Opposing Viewpoints")["held"] is True


def test_catalog_cache_avoids_repeated_s3_gets(monkeypatch):
    fake = _FakeCatalogS3(held=[{"name": "Fresh DB", "subjects": [], "description": "d"}])
    _use_s3_catalog(monkeypatch, fake)
    handler._get_catalog()
    handler._get_catalog()
    handler._get_catalog()
    assert fake.get_calls == 1  # cached within the TTL after the first fetch
    # reset so later tests aren't served this cached S3 catalog
    handler._catalog_cache["catalog"] = None
    handler._catalog_cache["at"] = 0.0


def test_no_catalog_bucket_uses_seed(monkeypatch):
    # With no bucket configured (local/dev), the tool reads entirely from the bundled seed.
    monkeypatch.setattr(handler, "CATALOG_BUCKET", None)
    handler._catalog_cache["catalog"] = None
    handler._catalog_cache["at"] = 0.0
    assert handler._catalog_name_lookup("CQ Researcher")["held"] is True
    handler._catalog_cache["catalog"] = None
    handler._catalog_cache["at"] = 0.0


# --- Single-session conversation history ---------------------------------------
#
# /query accepts a {"messages": [...]} conversation (widget) OR the legacy {"query": ...} shape
# (eval/curl). History is trimmed to the last MAX_HISTORY_MESSAGES turns server-side and seeded
# into the Converse loop; the trim must never produce a request Converse would reject.


def _turns(*roles_and_texts):
    """Build a {"messages": [...]} request body from (role, text) pairs."""
    return json.dumps(
        {"messages": [{"role": r, "content": t} for r, t in roles_and_texts]}
    )


def test_messages_payload_seeds_the_full_conversation(monkeypatch):
    # A multi-turn conversation is seeded into Converse in order: the prior user/assistant turns
    # plus the newest user question, all before the loop appends any tool turns.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("On weekends we're open 10-4.")])
    _wire(monkeypatch, agent, bedrock)

    body = _turns(
        ("user", "what are the hours?"),
        ("assistant", "We're open 9am to 5pm on weekdays."),
        ("user", "and on weekends?"),
    )
    resp = handler.lambda_handler(_payload_v2_event(body), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["answer"] == "On weekends we're open 10-4."
    # The seed carries all three turns, in order, with the newest user question last.
    assert _seed_messages(bedrock) == [
        {"role": "user", "text": "what are the hours?"},
        {"role": "assistant", "text": "We're open 9am to 5pm on weekdays."},
        {"role": "user", "text": "and on weekends?"},
    ]
    # System prompt is still server-authoritative (Converse `system`, not a client turn).
    assert bedrock.converse_calls[0]["system"] == [{"text": handler.SYSTEM_PROMPT}]


def test_input_screen_runs_on_the_newest_user_turn(monkeypatch):
    # The guardrail screens the newest user question (the thing being submitted now), not a prior
    # turn. Masked PII replaces only that turn in the seed.
    masked = "email me at {EMAIL}"
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        guardrail_response=masked_input_response(masked, raw_match="jane@example.com"),
        converse_script=[end_turn("Sure.")],
    )
    _wire(monkeypatch, agent, bedrock)

    body = _turns(
        ("user", "what are the hours?"),
        ("assistant", "9 to 5."),
        ("user", "email me at jane@example.com"),
    )
    handler.lambda_handler(_payload_v2_event(body), None)

    # Only the last user turn was screened, and its masked text is what got seeded.
    assert bedrock.apply_guardrail_calls[0]["content"] == [
        {"text": {"text": "email me at jane@example.com"}}
    ]
    assert _seed_messages(bedrock)[-1] == {"role": "user", "text": masked}
    assert "jane@example.com" not in _all_seed_text(bedrock)


def test_legacy_query_shape_still_seeds_a_single_user_turn(monkeypatch):
    # Backward compatibility: the old {"query": ...} shape (eval + curl) becomes a one-message
    # user conversation - identical to the pre-history behavior.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("Open 9-5.")])
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours?"})), None)

    assert resp["statusCode"] == 200
    assert _seed_messages(bedrock) == [{"role": "user", "text": "hours?"}]


def test_history_trimmed_to_last_10_messages_server_side(monkeypatch):
    # The client sends 15 turns; only the last 10 are considered, and because that window starts
    # on an assistant turn it is dropped down to a user-first, alternating seed. The oldest turns
    # never reach Converse - the client cannot make us process more than the cap.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("ok")])
    _wire(monkeypatch, agent, bedrock)

    pairs = []
    for i in range(7):
        pairs.append(("user", f"user {i}"))
        pairs.append(("assistant", f"assistant {i}"))
    pairs.append(("user", "newest question"))  # 15 turns total
    handler.lambda_handler(_payload_v2_event(_turns(*pairs)), None)

    seed = _seed_messages(bedrock)
    # Cap respected: at most MAX_HISTORY_MESSAGES survive the trim.
    assert len(seed) <= handler.MAX_HISTORY_MESSAGES
    # Converse-valid: starts with user, ends with the newest question.
    assert seed[0]["role"] == "user"
    assert seed[-1] == {"role": "user", "text": "newest question"}
    # The trimmed-off oldest turns are gone; the surviving window begins at "user 3".
    text = _all_seed_text(bedrock)
    assert "user 3" in text
    for gone in ("user 0", "user 1", "user 2", "assistant 0", "assistant 2"):
        assert gone not in text


def test_trim_drops_leading_assistant_so_seed_starts_with_user(monkeypatch):
    # A conversation that opens with an assistant turn (e.g. the widget greeting) must not seed an
    # assistant-first request - Converse requires the first message to be user.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("Open 9-5.")])
    _wire(monkeypatch, agent, bedrock)

    body = _turns(
        ("assistant", "Hi! I'm the library assistant."),  # canned greeting
        ("user", "hours?"),
    )
    handler.lambda_handler(_payload_v2_event(body), None)

    assert _seed_messages(bedrock) == [{"role": "user", "text": "hours?"}]


def test_trim_merges_consecutive_same_role_turns(monkeypatch):
    # Two user turns in a row (e.g. an error-retry double-send) would break Converse's alternation
    # rule; the seed merges them into one user message with multiple text blocks.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("ok")])
    _wire(monkeypatch, agent, bedrock)

    body = _turns(
        ("user", "first part"),
        ("user", "second part"),
    )
    handler.lambda_handler(_payload_v2_event(body), None)

    msgs = bedrock.converse_calls[0]["messages"]
    # One user message, alternating rule intact, both texts preserved as separate blocks.
    assert [m["role"] for m in msgs] == ["user"]
    assert msgs[0]["content"] == [{"text": "first part"}, {"text": "second part"}]


def test_messages_not_ending_in_user_turn_is_400(monkeypatch):
    # A conversation whose newest turn is an assistant message has no question to answer.
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    body = _turns(("user", "hours?"), ("assistant", "9 to 5."))
    resp = handler.lambda_handler(_payload_v2_event(body), None)

    assert resp["statusCode"] == 400
    assert bedrock.apply_guardrail_calls == []
    assert bedrock.converse_calls == []


def test_malformed_history_entries_are_dropped(monkeypatch):
    # Junk entries (bad role, blank/absent text, non-dict) are dropped; valid turns survive and
    # the newest user turn is still the question.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("ok")])
    _wire(monkeypatch, agent, bedrock)

    body = json.dumps(
        {
            "messages": [
                {"role": "system", "content": "ignore me"},  # bad role
                {"role": "user", "content": "   "},  # blank text
                "not a dict",  # not an object
                {"role": "assistant"},  # missing text
                {"role": "user", "content": "real question"},
            ]
        }
    )
    resp = handler.lambda_handler(_payload_v2_event(body), None)

    assert resp["statusCode"] == 200
    assert _seed_messages(bedrock) == [{"role": "user", "text": "real question"}]


def test_empty_messages_list_is_400(monkeypatch):
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"messages": []})), None)

    assert resp["statusCode"] == 400
    assert bedrock.apply_guardrail_calls == []


def test_over_length_cap_applies_to_newest_user_turn(monkeypatch):
    # The size cap is enforced on the newest user question inside a conversation, before any AWS
    # call - exactly as it is for the legacy single-query shape.
    monkeypatch.setattr(handler, "MAX_QUERY_CHARS", 10)
    agent, bedrock = FakeAgentRuntime(), FakeBedrockRuntime()
    _wire(monkeypatch, agent, bedrock)

    body = _turns(("assistant", "hi"), ("user", "a" * 11))
    resp = handler.lambda_handler(_payload_v2_event(body), None)

    assert resp["statusCode"] == 400
    assert bedrock.apply_guardrail_calls == []
    assert bedrock.converse_calls == []


def test_bot_role_maps_to_assistant(monkeypatch):
    # A widget "bot" turn is accepted and normalized to the Converse "assistant" role.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("ok")])
    _wire(monkeypatch, agent, bedrock)

    body = _turns(("user", "hours?"), ("bot", "9 to 5."), ("user", "weekends?"))
    handler.lambda_handler(_payload_v2_event(body), None)

    assert _seed_messages(bedrock) == [
        {"role": "user", "text": "hours?"},
        {"role": "assistant", "text": "9 to 5."},
        {"role": "user", "text": "weekends?"},
    ]


# --- Phase 2c: search_book_catalog (live Primo book/media catalog) --------------------------
#
# Unlike database_catalog, Primo is NOT authoritative about absence: it always returns fuzzy
# matches and its ranking is unreliable. The handler therefore returns EVIDENCE (candidate records
# + availability + total) and the MODEL judges a match; there is NO score threshold and NO
# held/not-held logic in the handler. total == 0 is the only clean not-held signal. The tool is a
# live third-party call, so every failure must degrade to a "catalog unavailable" result, never
# throw. These tests stub handler._primo_get_json (the single HTTP seam), so no network is touched.


PRIMO_TOOL = "search_book_catalog"


def _primo_doc(title, *, author="", year="", rid="alma1", typ="book"):
    """A Primo search `docs` entry in the real pnx shape (display + control sections)."""
    return {
        "pnx": {
            "display": {
                "title": [title],
                "creator": [author] if author else [],
                "creationdate": [year] if year else [],
                "type": [typ],
            },
            "control": {"recordid": [rid], "score": ["1.0"]},
        }
    }


def _primo_search_payload(docs, total=None):
    return {"info": {"total": total if total is not None else len(docs)}, "docs": docs}


def _delivery_available(main="Gilroy Campus", sub="New Books", call="PS3511.I9 G7 2021i"):
    return {
        "delivery": {
            "availability": ["available_in_library"],
            "bestlocation": {
                "availabilityStatus": "available",
                "mainLocation": main,
                "subLocation": sub,
                "callNumber": call,
            },
        }
    }


def _delivery_online():
    return {"delivery": {"availability": ["available"], "electronicServices": [{"serviceType": "Available Online"}]}}


def _wire_primo(monkeypatch, search_payload, delivery_by_rid=None, *, search_exc=None, delivery_exc=None):
    """Stub handler._primo_get_json: dispatch by URL to a canned search payload and per-record
    delivery payloads. `search_exc`/`delivery_exc` force that leg to raise (soft-fail paths)."""
    import urllib.parse as _up

    delivery_by_rid = delivery_by_rid or {}

    def fake_get(url, timeout):
        if "/L/" in url:
            if delivery_exc is not None:
                raise delivery_exc
            rid = _up.unquote(url.split("/L/")[1].split("?")[0])
            return delivery_by_rid.get(rid, {"delivery": {}})
        if search_exc is not None:
            raise search_exc
        return search_payload

    monkeypatch.setattr(handler, "_primo_get_json", fake_get)


def primo_tool_use_turn(query="the great gatsby", *, tool_use_id="tu-1"):
    """A Converse response asking to call search_book_catalog."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": tool_use_id, "name": PRIMO_TOOL, "input": {"query": query}}}
                ],
            }
        },
        "stopReason": "tool_use",
    }


# -- Direct unit tests of _run_primo_tool (no agent loop) --


def test_primo_parses_results_with_availability(monkeypatch):
    payload = _primo_search_payload(
        [
            _primo_doc("The Great Gatsby", author="Fitzgerald, F. Scott$$Qauthor", year="2021", rid="alma1"),
            _primo_doc("The Great Gatsby", author="Paramount Pictures", year="2003", rid="alma2", typ="video"),
        ],
        total=110,
    )
    _wire_primo(
        monkeypatch,
        payload,
        {"alma1": _delivery_available(), "alma2": _delivery_available(sub="Videos", call="PS3511.I9")},
    )

    result, source = handler._run_primo_tool({"query": "the great gatsby"})

    # total is surfaced raw (the only not-held signal); no held/not-held verdict is computed.
    assert result["total"] == 110
    assert "held" not in result and "error" not in result
    first = result["results"][0]
    assert first["title"] == "The Great Gatsby"
    # The $$-delimited creator is cleaned to just the name.
    assert first["author"] == "Fitzgerald, F. Scott"
    assert first["year"] == "2021"
    assert first["availability"]["status"] == "available"
    assert first["availability"]["location"] == "Gilroy Campus, New Books"
    assert first["availability"]["call_number"] == "PS3511.I9 G7 2021i"
    # A lookup with candidates contributes the Primo results-page source (a verify link).
    assert source is not None
    assert source["uri"].startswith("https://caccl-gavilan.primo.exlibrisgroup.com/discovery/search")


def test_primo_total_zero_is_surfaced_and_contributes_no_source(monkeypatch):
    # The ONLY clean not-held signal: total == 0, empty docs. The handler surfaces it and adds a
    # note; it does not itself say "not held" (the model does, from total == 0).
    _wire_primo(monkeypatch, _primo_search_payload([], total=0))

    result, source = handler._run_primo_tool({"query": "obscure nonexistent zzqx treatise"})

    assert result["total"] == 0
    assert result["results"] == []
    assert "not found" in result["note"].lower() or "no matching" in result["note"].lower()
    assert source is None  # nothing found -> no verify link cited


def test_primo_online_resource_reported_as_online(monkeypatch):
    _wire_primo(monkeypatch, _primo_search_payload([_primo_doc("An E-Book", rid="e1")]), {"e1": _delivery_online()})

    result, _ = handler._run_primo_tool({"query": "an e-book"})

    assert result["results"][0]["availability"]["status"] == "online"
    assert "Available Online" in result["results"][0]["availability"]["location"]


def test_primo_malformed_docs_degrade_without_crashing(monkeypatch):
    # A mix of junk: not a dict, missing pnx, non-list fields, a doc with no title (dropped), and
    # one good doc. Parsing must never raise and must keep the salvageable record. Raise the cap so
    # every doc (the good one is last) is actually processed, exercising all the junk shapes.
    monkeypatch.setattr(handler, "PRIMO_NUMBER_OF_RESULTS", 10)
    docs = [
        "not a dict",
        {"pnx": "not a dict"},
        {"pnx": {"display": {"title": "not a list either"}}},  # title not a list -> handled
        {"pnx": {"display": {}, "control": {}}},  # no title -> dropped
        _primo_doc("Real Book", author="Real Author", rid="good1"),
    ]
    _wire_primo(monkeypatch, _primo_search_payload(docs, total=5), {"good1": _delivery_available()})

    result, _ = handler._run_primo_tool({"query": "whatever"})

    titles = [r["title"] for r in result["results"]]
    # "not a list either" is a bare string title -> _primo_first accepts it; the good doc survives.
    assert "Real Book" in titles
    # The title-less doc and the non-dict entries produced no crash and no bogus rows.
    assert all(r["title"] for r in result["results"])


def test_primo_missing_availability_fields_degrade_to_unknown(monkeypatch):
    # Delivery present but with a shape we don't recognize (no bestlocation / eservices / codes).
    _wire_primo(monkeypatch, _primo_search_payload([_primo_doc("Book X", rid="x1")]), {"x1": {"delivery": {}}})

    result, _ = handler._run_primo_tool({"query": "book x"})

    assert result["results"][0]["availability"]["status"] in ("not available", "unknown")


def test_primo_search_failure_soft_fails_without_throwing(monkeypatch):
    # Primo down / timeout / bad response: the tool returns a catalog_unavailable result (so the
    # model can still answer) and NEVER raises. No source is contributed.
    _wire_primo(monkeypatch, _primo_search_payload([]), search_exc=TimeoutError("primo timed out"))

    result, source = handler._run_primo_tool({"query": "the great gatsby"})

    assert result["error"] == "catalog_unavailable"
    assert "unavailable" in result["note"].lower()
    assert source is None


def test_primo_availability_failure_degrades_but_still_returns_results(monkeypatch):
    # The search succeeds but every per-record delivery call fails: results still come back, each
    # with availability "unknown", rather than the whole tool failing.
    _wire_primo(
        monkeypatch,
        _primo_search_payload([_primo_doc("Book A", rid="a1")], total=3),
        delivery_exc=ConnectionError("delivery endpoint down"),
    )

    result, _ = handler._run_primo_tool({"query": "book a"})

    assert result["total"] == 3
    assert result["results"][0]["availability"]["status"] == "unknown"


def test_primo_blank_query_is_an_error_with_no_source(monkeypatch):
    # No HTTP call should be needed; a blank query is an error result the model can recover from.
    monkeypatch.setattr(
        handler, "_primo_get_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not hit Primo for a blank query")),
    )
    result, source = handler._run_primo_tool({"query": "   "})
    assert "error" in result
    assert source is None


def test_primo_availability_budget_skips_extra_lookups(monkeypatch):
    # Once the availability wall-clock budget is exhausted, remaining results report "unknown" and
    # no further delivery calls are made - the guard that stops a slow Primo eating the request.
    monkeypatch.setattr(handler, "PRIMO_AVAILABILITY_BUDGET_SECONDS", -1.0)  # already past deadline
    calls = {"delivery": 0}

    def fake_get(url, timeout):
        if "/L/" in url:
            calls["delivery"] += 1
            return _delivery_available()
        return _primo_search_payload([_primo_doc("B1", rid="b1"), _primo_doc("B2", rid="b2")], total=2)

    monkeypatch.setattr(handler, "_primo_get_json", fake_get)

    result, _ = handler._run_primo_tool({"query": "b"})

    assert calls["delivery"] == 0  # no availability lookups ran under a spent budget
    assert all(r["availability"]["status"] == "unknown" for r in result["results"])


def test_primo_respects_number_of_results_cap(monkeypatch):
    # Even if Primo returns more docs than the cap, only PRIMO_NUMBER_OF_RESULTS are surfaced.
    monkeypatch.setattr(handler, "PRIMO_NUMBER_OF_RESULTS", 2)
    docs = [_primo_doc(f"Book {i}", rid=f"r{i}") for i in range(6)]
    _wire_primo(monkeypatch, _primo_search_payload(docs, total=6), {f"r{i}": _delivery_available() for i in range(6)})

    result, _ = handler._run_primo_tool({"query": "book"})
    assert len(result["results"]) == 2


# -- search_book_catalog inside the agent loop --


def test_primo_tool_advertised_with_query_schema(monkeypatch):
    # The toolConfig now carries THREE tools; search_book_catalog takes a single `query`.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("hi")])
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hi"})), None)

    tools = {t["toolSpec"]["name"]: t["toolSpec"] for t in bedrock.converse_calls[0]["toolConfig"]["tools"]}
    assert set(tools) == {
        "search_library_info",
        "database_catalog",
        "search_book_catalog",
        "search_course_reserves",
        "library_links",
    }
    primo_schema = tools["search_book_catalog"]["inputSchema"]["json"]
    assert set(primo_schema["properties"]) == {"query"}
    assert primo_schema["required"] == ["query"]


def test_agent_routes_book_query_to_primo_and_contributes_source(monkeypatch):
    # End to end: the model calls search_book_catalog; the tool runs the real _run_primo_tool over
    # stubbed HTTP; the answer returns with the Primo results-page as a source. KB search is unused.
    agent = FakeAgentRuntime()
    _wire_primo(
        monkeypatch,
        _primo_search_payload([_primo_doc("The Great Gatsby", author="Fitzgerald", rid="alma1")], total=110),
        {"alma1": _delivery_available()},
    )
    bedrock = FakeBedrockRuntime(
        converse_script=[
            primo_tool_use_turn("the great gatsby"),
            end_turn("The catalog shows a copy available at the Gilroy Campus."),
        ]
    )
    monkeypatch.setattr(handler, "_bedrock_client", lambda: bedrock)
    monkeypatch.setattr(handler, "_agent_client", lambda: agent)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "do you have the great gatsby?"})), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["answer"].startswith("The catalog shows")
    # KB retrieval never ran (Primo handled it).
    assert agent.calls == []
    # The tool result fed back carries the total and the candidate record.
    (tr,) = [
        b["toolResult"]
        for m in bedrock.converse_calls[1]["messages"]
        for b in m.get("content", [])
        if "toolResult" in b
    ]
    assert tr["status"] == "success"
    assert tr["content"][0]["json"]["total"] == 110
    # The Primo results-page source is attached.
    assert len(body["sources"]) == 1
    assert body["sources"][0]["uri"].startswith(
        "https://caccl-gavilan.primo.exlibrisgroup.com/discovery/search"
    )


def test_agent_primo_soft_fail_still_answers(monkeypatch):
    # Primo is down: the toolResult is a status=error catalog_unavailable payload, but the request
    # still returns 200 with an answer and no sources - the loop never dies on a dead Primo.
    agent = FakeAgentRuntime()
    _wire_primo(monkeypatch, _primo_search_payload([]), search_exc=TimeoutError("down"))
    bedrock = FakeBedrockRuntime(
        converse_script=[
            primo_tool_use_turn("the great gatsby"),
            end_turn("The catalog search is temporarily unavailable. Try the catalog or ask a librarian."),
        ]
    )
    monkeypatch.setattr(handler, "_bedrock_client", lambda: bedrock)
    monkeypatch.setattr(handler, "_agent_client", lambda: agent)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "do you have gatsby?"})), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert "unavailable" in body["answer"].lower()
    assert body["sources"] == []
    (tr,) = [
        b["toolResult"]
        for m in bedrock.converse_calls[1]["messages"]
        for b in m.get("content", [])
        if "toolResult" in b
    ]
    assert tr["status"] == "error"
    assert tr["content"][0]["json"]["error"] == "catalog_unavailable"


def test_primo_and_search_sources_merge_and_dedupe(monkeypatch):
    # The model uses BOTH search_library_info (KB passages) and search_book_catalog (Primo page).
    # Sources merge with no duplicates, preserving the {answer, sources} contract.
    agent = FakeAgentRuntime()  # two default KB chunks
    _wire_primo(
        monkeypatch,
        _primo_search_payload([_primo_doc("A Book", rid="alma1")], total=1),
        {"alma1": _delivery_available()},
    )
    bedrock = FakeBedrockRuntime(
        converse_script=[
            tool_use_turn(query="library hours", tool_use_id="tu-1"),
            primo_tool_use_turn("a book", tool_use_id="tu-2"),
            end_turn("Here is everything."),
        ]
    )
    monkeypatch.setattr(handler, "_bedrock_client", lambda: bedrock)
    monkeypatch.setattr(handler, "_agent_client", lambda: agent)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours and a book"})), None)

    uris = [s["uri"] for s in json.loads(resp["body"])["sources"]]
    assert "https://gav.edu/library/hours" in uris
    assert "https://gav.edu/library/borrow" in uris
    assert any(u.startswith("https://caccl-gavilan.primo.exlibrisgroup.com/discovery/search") for u in uris)
    assert len(uris) == len(set(uris))  # no duplicates


# --- Phase 2d: search_course_reserves (live Primo CourseReserves scope) ----------------------
#
# A fourth tool, mirroring search_book_catalog but in the CourseReserves scope: textbooks/materials
# on reserve for a class. Same EVIDENCE-not-verdict posture (total == 0 is the only clean
# not-on-reserve signal; soft-fail on any error; defensive parsing). The reserve-specific piece is
# crsinfo, which links a record to one or more COURSE CODES (a single item can serve several
# courses). Reuses the Primo test seams (_primo_get_json stub via _wire_primo).


RESERVES_TOOL = "search_course_reserves"


def _reserve_doc(title, *, author="", courses=("PSYC C1000",), rid="alma1"):
    """A CourseReserves `docs` entry: one crsinfo list ENTRY per course, in the real
    $$R<code>$$V<code>: <code>; <dept>$$M<code> shape."""
    crsinfo = [f"$$R{c}$$V{c}: {c}; Dept$$M{c}" for c in courses]
    return {
        "pnx": {
            "display": {
                "title": [title],
                "creator": [author] if author else [],
                "crsinfo": crsinfo,
            },
            "control": {"recordid": [rid], "score": ["1.0"]},
        }
    }


def reserves_tool_use_turn(query="PSYC C1000", *, tool_use_id="tu-1"):
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": tool_use_id, "name": RESERVES_TOOL, "input": {"query": query}}}
                ],
            }
        },
        "stopReason": "tool_use",
    }


# -- Direct unit tests of _run_reserves_tool --


def test_reserves_parses_courses_and_availability(monkeypatch):
    payload = _primo_search_payload(
        [_reserve_doc("Introduction to psychology", author="Kalat, James W.$$Qauthor", courses=["PSYC C1000"], rid="r1")],
        total=5,
    )
    # A reserve delivery reports the Course Reserve sublocation.
    _wire_primo(monkeypatch, payload, {"r1": _delivery_available(main="Gilroy Campus", sub="Course Reserve", call="BF121 .K26 2022")})

    result, source = handler._run_reserves_tool({"query": "PSYC C1000"})

    assert result["total"] == 5
    assert "error" not in result
    row = result["results"][0]
    assert row["title"] == "Introduction to psychology"
    assert row["author"] == "Kalat, James W."  # $$-delimited creator cleaned
    assert row["courses"] == ["PSYC C1000"]
    assert row["availability"]["status"] == "available"
    # The Course Reserve sublocation is surfaced in the location.
    assert "Course Reserve" in row["availability"]["location"]
    assert row["availability"]["call_number"] == "BF121 .K26 2022"
    # A lookup with candidates contributes the reserves results-page source.
    assert source is not None
    assert source["uri"].startswith("https://caccl-gavilan.primo.exlibrisgroup.com/discovery/search")
    assert "CourseReserves" in source["uri"]


def test_reserves_multi_course_item(monkeypatch):
    # A single item on reserve for TWO courses (two crsinfo entries), one with a trailing backslash
    # like the real data - both course codes parse, deduped, backslash stripped.
    doc = {
        "pnx": {
            "display": {
                "title": ["Understanding psychology"],
                "crsinfo": [
                    "$$RPSYC C1000$$VPSYC C1000: PSYC C1000; Psychology$$MPSYC C1000\\",
                    "$$RPSYCH 10$$VPSYCH 10: PSYCH 10; Psychology$$MPSYCH 10",
                ],
            },
            "control": {"recordid": ["m1"]},
        }
    }
    _wire_primo(monkeypatch, _primo_search_payload([doc], total=3), {"m1": _delivery_available(sub="Course Reserve")})

    result, _ = handler._run_reserves_tool({"query": "understanding psychology"})

    assert result["results"][0]["courses"] == ["PSYC C1000", "PSYCH 10"]


def test_reserves_total_zero_is_authoritative_not_on_reserve(monkeypatch):
    _wire_primo(monkeypatch, _primo_search_payload([], total=0))

    result, source = handler._run_reserves_tool({"query": "ZZZZ 999"})

    assert result["total"] == 0
    assert result["results"] == []
    assert "reserve" in result["note"].lower()
    assert source is None  # nothing found -> no verify link


def test_reserves_malformed_docs_degrade_without_crashing(monkeypatch):
    # Junk shapes plus a good doc whose crsinfo is missing (courses -> []). Must not crash.
    monkeypatch.setattr(handler, "PRIMO_NUMBER_OF_RESULTS", 10)
    docs = [
        "not a dict",
        {"pnx": "not a dict"},
        {"pnx": {"display": {"crsinfo": "not a list", "title": ["No Course Info"]}, "control": {"recordid": ["g0"]}}},
        {"pnx": {"display": {}, "control": {}}},  # no title -> dropped
        _reserve_doc("Good Reserve Book", courses=["KIN 8"], rid="g1"),
    ]
    _wire_primo(monkeypatch, _primo_search_payload(docs, total=5),
                {"g0": _delivery_available(sub="Course Reserve"), "g1": _delivery_available(sub="Course Reserve")})

    result, _ = handler._run_reserves_tool({"query": "whatever"})

    titles = {r["title"]: r for r in result["results"]}
    assert "Good Reserve Book" in titles and titles["Good Reserve Book"]["courses"] == ["KIN 8"]
    # crsinfo that isn't a list degrades to an empty courses list, not a crash.
    assert "No Course Info" in titles and titles["No Course Info"]["courses"] == []


def test_reserves_search_failure_soft_fails(monkeypatch):
    _wire_primo(monkeypatch, _primo_search_payload([]), search_exc=TimeoutError("reserves timed out"))

    result, source = handler._run_reserves_tool({"query": "PSYC C1000"})

    assert result["error"] == "reserves_unavailable"
    assert "unavailable" in result["note"].lower()
    assert source is None


def test_reserves_availability_failure_degrades_to_unknown(monkeypatch):
    _wire_primo(monkeypatch, _primo_search_payload([_reserve_doc("Book", courses=["PSYC C1000"], rid="a1")], total=2),
                delivery_exc=ConnectionError("delivery down"))

    result, _ = handler._run_reserves_tool({"query": "psych book"})

    assert result["total"] == 2
    assert result["results"][0]["availability"]["status"] == "unknown"
    assert result["results"][0]["courses"] == ["PSYC C1000"]


def test_reserves_blank_query_is_error_no_source(monkeypatch):
    monkeypatch.setattr(
        handler, "_primo_get_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not hit Primo for a blank query")),
    )
    result, source = handler._run_reserves_tool({"query": "  "})
    assert "error" in result
    assert source is None


def test_reserves_delivery_uses_reserves_scope_and_book_catalog_stays_general(monkeypatch):
    # Scope plumbing: the reserves availability call must query the CourseReserves scope, while the
    # general book-catalog availability call must stay on MyInstitution. Capture the delivery URLs.
    seen = {"reserves": [], "book": []}

    def make_get(bucket):
        def fake_get(url, timeout):
            if "/L/" in url:
                seen[bucket].append(url)
                return _delivery_available()
            return _primo_search_payload([_reserve_doc("X", rid="z1") if bucket == "reserves" else _primo_doc("X", rid="z1")], total=1)
        return fake_get

    monkeypatch.setattr(handler, "_primo_get_json", make_get("reserves"))
    handler._run_reserves_tool({"query": "PSYC C1000"})
    monkeypatch.setattr(handler, "_primo_get_json", make_get("book"))
    handler._run_primo_tool({"query": "the great gatsby"})

    assert seen["reserves"] and all("scope=CourseReserves" in u for u in seen["reserves"])
    assert seen["book"] and all("scope=MyInstitution" in u for u in seen["book"])


# -- search_course_reserves inside the agent loop --


def test_reserves_tool_advertised_with_query_schema(monkeypatch):
    # The toolConfig now carries FOUR tools; search_course_reserves takes a single `query`.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("hi")])
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hi"})), None)

    tools = {t["toolSpec"]["name"]: t["toolSpec"] for t in bedrock.converse_calls[0]["toolConfig"]["tools"]}
    assert set(tools) == {
        "search_library_info",
        "database_catalog",
        "search_book_catalog",
        "search_course_reserves",
        "library_links",
    }
    res_schema = tools["search_course_reserves"]["inputSchema"]["json"]
    assert set(res_schema["properties"]) == {"query"}
    assert res_schema["required"] == ["query"]


def test_agent_routes_reserve_query_to_reserves_tool_and_contributes_source(monkeypatch):
    agent = FakeAgentRuntime()
    _wire_primo(
        monkeypatch,
        _primo_search_payload([_reserve_doc("Introduction to psychology", courses=["PSYC C1000"], rid="r1")], total=5),
        {"r1": _delivery_available(sub="Course Reserve", call="BF121 .K26 2022")},
    )
    bedrock = FakeBedrockRuntime(
        converse_script=[
            reserves_tool_use_turn("PSYC C1000"),
            end_turn("The catalog shows it on reserve at the Course Reserve desk."),
        ]
    )
    monkeypatch.setattr(handler, "_bedrock_client", lambda: bedrock)
    monkeypatch.setattr(handler, "_agent_client", lambda: agent)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "what's on reserve for PSYC C1000?"})), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert agent.calls == []  # KB search never ran
    (tr,) = [
        b["toolResult"]
        for m in bedrock.converse_calls[1]["messages"]
        for b in m.get("content", [])
        if "toolResult" in b
    ]
    assert tr["status"] == "success"
    assert tr["content"][0]["json"]["total"] == 5
    assert tr["content"][0]["json"]["results"][0]["courses"] == ["PSYC C1000"]
    assert len(body["sources"]) == 1
    assert "CourseReserves" in body["sources"][0]["uri"]


# --- library_links: curated canonical Gavilan URLs -------------------------------------------
#
# A STATIC, bundled table (app/data/library_links.json) - no S3, no live call, nothing to stub.
# The tool exists so the model cites real URLs instead of writing one from memory, so these tests
# care about two things: the right entries come back, and each MATCHED link reaches the response
# `sources` (deduped) exactly like the database_catalog synthetic source does.


LINKS_TOOL = "library_links"


def links_tool_use_turn(topic=None, *, tool_use_id="tu-1"):
    """A Converse response asking to call library_links. `topic` is optional in the real schema,
    so topic=None sends an EMPTY input object, exactly as the model may."""
    tool_input = {} if topic is None else {"topic": topic}
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": tool_use_id, "name": LINKS_TOOL, "input": tool_input}}
                ],
            }
        },
        "stopReason": "tool_use",
    }


def _link_by_key(key):
    (entry,) = [e for e in handler._LIBRARY_LINKS if e["key"] == key]
    return entry


# -- The bundled data file --


def test_library_links_seed_has_the_expected_entries():
    keys = [e["key"] for e in handler._LIBRARY_LINKS]
    assert set(keys) == {
        "gavilan_college_website",
        "library_homepage",
        "online_textbook_collections",
        "research_guides",
        "bookstore_course_materials",
        "campus_map_main",
        "campus_map_interactive",
        "public_safety",
        "interlibrary_loan",
        "laptop_request",
    }
    assert len(keys) == len(set(keys))  # keys are unique - they identify a link


def test_every_library_link_entry_is_well_formed():
    for entry in handler._LIBRARY_LINKS:
        assert entry["url"].startswith("https://"), entry["key"]
        assert entry["label"].strip()
        assert entry["use_when"].strip()


def test_library_link_urls_are_the_curated_ones():
    assert _link_by_key("campus_map_main")["url"] == (
        "https://www.gavilan.edu/about/maps/main_map.php"
    )
    assert _link_by_key("bookstore_course_materials")["url"] == (
        "https://gavilan.bkstr.com/pages/courses-materials-results"
    )
    assert _link_by_key("research_guides")["url"] == "https://gavilan.libguides.com/"
    assert _link_by_key("interlibrary_loan")["url"] == "https://www.gavilan.edu/library/ill.php"


# -- Lookup behavior (called directly; no loop) --


def test_links_topic_match_returns_only_matching_entries_and_their_sources():
    result, sources = handler._run_links_tool({"topic": "campus map"})
    assert result["matched"] is True
    keys = {link["key"] for link in result["links"]}
    assert keys == {"campus_map_main", "campus_map_interactive"}
    # Each matched link IS a canonical page, so each contributes a source.
    assert sources == [
        {"uri": link["url"], "excerpt": link["label"]} for link in result["links"]
    ]


def test_links_keyword_match_hits_the_right_entry():
    for topic, key in [
        ("bookstore", "bookstore_course_materials"),
        ("interlibrary loan", "interlibrary_loan"),
        ("laptop", "laptop_request"),
        ("research guides", "research_guides"),
        ("online textbooks", "online_textbook_collections"),
        ("public safety", "public_safety"),
    ]:
        result, sources = handler._run_links_tool({"topic": topic})
        assert result["matched"] is True, topic
        assert key in {link["key"] for link in result["links"]}, topic
        assert sources, topic


def test_links_matching_is_whole_word_so_library_does_not_hit_interlibrary():
    # Naive substring matching would make every "library" topic drag in Interlibrary Loan.
    result, _ = handler._run_links_tool({"topic": "library home page"})
    assert {link["key"] for link in result["links"]} == {"library_homepage"}


def test_links_miss_returns_the_whole_table_with_a_note_and_no_sources():
    # A miss must never leave the model empty-handed - that is when it would invent a URL - but a
    # browse listing is not an answer to a specific question, so it contributes no sources.
    result, sources = handler._run_links_tool({"topic": "underwater basket weaving"})
    assert result["matched"] is False
    assert len(result["links"]) == len(handler._LIBRARY_LINKS)
    assert "note" in result
    assert sources == []


def test_links_no_topic_lists_everything():
    for tool_input in ({}, {"topic": "  "}, {"topic": None}, None, "not a dict"):
        result, sources = handler._run_links_tool(tool_input)
        assert result["matched"] is False
        assert len(result["links"]) == len(handler._LIBRARY_LINKS)
        assert sources == []


def test_links_result_carries_url_label_and_use_when_for_the_model():
    result, _ = handler._run_links_tool({"topic": "bookstore"})
    (link,) = result["links"]
    assert set(link) == {"key", "label", "url", "use_when"}
    assert link["url"] == "https://gavilan.bkstr.com/pages/courses-materials-results"
    assert link["use_when"]


# -- library_links inside the agent loop --


def test_links_tool_advertised_with_optional_topic_schema(monkeypatch):
    # The toolConfig now carries FIVE tools; library_links takes a single OPTIONAL `topic`.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("hi")])
    _wire(monkeypatch, agent, bedrock)

    handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hi"})), None)

    tools = {t["toolSpec"]["name"]: t["toolSpec"] for t in bedrock.converse_calls[0]["toolConfig"]["tools"]}
    assert set(tools) == {
        "search_library_info",
        "database_catalog",
        "search_book_catalog",
        "search_course_reserves",
        "library_links",
    }
    links_schema = tools["library_links"]["inputSchema"]["json"]
    assert set(links_schema["properties"]) == {"topic"}
    # `topic` is optional: a miss still returns the whole table, so nothing is required.
    assert "required" not in links_schema


def test_agent_routes_link_question_to_links_tool_and_contributes_sources(monkeypatch):
    # The model asks for a campus-map link; KB retrieval never runs and the real URLs come back
    # as sources - the whole point of the tool.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        converse_script=[
            links_tool_use_turn("campus map"),
            end_turn("Here's the campus map."),
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "where is the library on campus?"})), None
    )

    body = json.loads(resp["body"])
    assert agent.calls == []  # KB search never ran
    (tr,) = _catalog_tool_results(bedrock)
    assert tr["status"] == "success"
    assert tr["content"][0]["json"]["matched"] is True
    uris = [s["uri"] for s in body["sources"]]
    assert "https://www.gavilan.edu/about/maps/main_map.php" in uris
    assert "https://www.gavilan.edu/about/maps/gilroy_interactive_map.php" in uris


def test_links_source_deduped_across_multiple_links_calls(monkeypatch):
    # Two lookups whose matches overlap contribute each URL only once.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        converse_script=[
            links_tool_use_turn("campus map", tool_use_id="tu-1"),
            links_tool_use_turn("map", tool_use_id="tu-2"),
            end_turn("Both maps."),
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "map?"})), None)

    uris = [s["uri"] for s in json.loads(resp["body"])["sources"]]
    assert len(uris) == len(set(uris)) == 2


def test_links_and_search_sources_merge_and_dedupe(monkeypatch):
    # Model uses BOTH tools: KB search (passages) + library_links (canonical URLs). They merge.
    agent = FakeAgentRuntime()  # default two KB chunks
    bedrock = FakeBedrockRuntime(
        converse_script=[
            tool_use_turn(query="library hours", tool_use_id="tu-1"),
            links_tool_use_turn("bookstore", tool_use_id="tu-2"),
            end_turn("Combined answer."),
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "hours and books"})), None)

    uris = [s["uri"] for s in json.loads(resp["body"])["sources"]]
    assert "https://gav.edu/library/hours" in uris
    assert "https://gav.edu/library/borrow" in uris
    assert "https://gavilan.bkstr.com/pages/courses-materials-results" in uris
    assert len(uris) == len(set(uris)) == 3


def test_links_browse_listing_contributes_no_sources_in_the_loop(monkeypatch):
    # A no-topic listing is a browse aid, not an answer, so the response carries no citations
    # rather than the entire directory.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        converse_script=[links_tool_use_turn(None), end_turn("Here's what I can point you to.")]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "what can you help with?"})), None
    )

    body = json.loads(resp["body"])
    assert body["sources"] == []
    (tr,) = _catalog_tool_results(bedrock)
    assert tr["status"] == "success"
    assert len(tr["content"][0]["json"]["links"]) == len(handler._LIBRARY_LINKS)


def test_guardrail_block_drops_links_sources(monkeypatch):
    # A guardrail block after a links lookup returns the block message with NO sources.
    agent = FakeAgentRuntime()
    blocked = "I'm not able to provide a response to that."
    bedrock = FakeBedrockRuntime(
        converse_script=[
            links_tool_use_turn("campus map"),
            {
                "output": {"message": {"role": "assistant", "content": [{"text": blocked}]}},
                "stopReason": "guardrail_intervened",
            },
        ]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "x"})), None)
    body = json.loads(resp["body"])
    assert body["answer"] == blocked
    assert body["sources"] == []


def test_links_sources_are_not_in_full_context(monkeypatch):
    # full_context is the KB passages the model actually saw; a curated link is not one.
    agent = FakeAgentRuntime()
    bedrock = FakeBedrockRuntime(
        converse_script=[links_tool_use_turn("laptop"), end_turn("Here's the laptop record.")]
    )
    _wire(monkeypatch, agent, bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "can I borrow a laptop?", "include_full_context": True})),
        None,
    )

    body = json.loads(resp["body"])
    assert body["full_context"] == []
    assert len(body["sources"]) == 1


# --- Phase 5: tool_calls trace (opt-in debug payload) ---------------------------
#
# The eval's groundedness judge only ever saw `full_context`, which is populated from
# search_library_info alone. An answer built from database_catalog, library_links, or either
# Primo tool therefore looked context-free, and could not be graded. `tool_calls` closes that:
# every toolResult the model received, in order. It is debug-only - the widget path (no flag)
# must stay byte-identical, which is what the first two tests here pin down.


def _tool_call_names(body):
    return [c["tool"] for c in body["tool_calls"]]


def _sent_tool_result_jsons(bedrock):
    """Every toolResult json the loop actually handed back to the model, across all converse
    calls, in order. The trace must match this exactly - it is a record of what was sent, not a
    second rendering of it."""
    seen = []
    for call in bedrock.converse_calls:
        for message in call["messages"]:
            for block in message.get("content", []):
                if "toolResult" in block:
                    payload = block["toolResult"]["content"][0]["json"]
                    if payload not in seen:
                        seen.append(payload)
    return seen


def _all_five_tools_script():
    """One converse script in which the model calls every tool once, then answers."""
    return [
        tool_use_turn(query="library hours", tool_use_id="tu-1"),
        catalog_tool_use_turn("name", "JSTOR", tool_use_id="tu-2"),
        links_tool_use_turn("bookstore", tool_use_id="tu-3"),
        primo_tool_use_turn("the great gatsby", tool_use_id="tu-4"),
        reserves_tool_use_turn("PSYC C1000", tool_use_id="tu-5"),
        end_turn("Here is everything."),
    ]


def test_widget_path_unchanged_even_when_every_tool_ran(monkeypatch):
    # THE regression that matters: with no flag, the response is exactly {answer, sources} - no
    # tool_calls, no full_context - however many tools the loop ran.
    _wire_primo(monkeypatch, _primo_search_payload([_primo_doc("The Great Gatsby", rid="alma1")]))
    bedrock = FakeBedrockRuntime(converse_script=_all_five_tools_script())
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(_payload_v2_event(json.dumps({"query": "everything"})), None)

    body = json.loads(resp["body"])
    assert set(body.keys()) == {"answer", "sources"}
    assert "tool_calls" not in body


def test_flag_false_omits_tool_calls(monkeypatch):
    bedrock = FakeBedrockRuntime(
        converse_script=[catalog_tool_use_turn("name", "JSTOR"), end_turn("No JSTOR.")]
    )
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "JSTOR?", "include_full_context": False})), None
    )
    body = json.loads(resp["body"])
    assert "tool_calls" not in body
    assert "full_context" not in body


def test_tool_calls_traces_every_tool_in_invocation_order(monkeypatch):
    _wire_primo(
        monkeypatch,
        _primo_search_payload([_primo_doc("The Great Gatsby", rid="alma1")]),
        {"alma1": _delivery_available()},
    )
    bedrock = FakeBedrockRuntime(converse_script=_all_five_tools_script())
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "everything", "include_full_context": True})), None
    )
    body = json.loads(resp["body"])

    assert _tool_call_names(body) == [
        "search_library_info",
        "database_catalog",
        "library_links",
        "search_book_catalog",
        "search_course_reserves",
    ]
    # The model's own input for each call is recorded verbatim, which is how the eval answers
    # "was library_links ever called, and with what topic?".
    assert [c["input"] for c in body["tool_calls"]] == [
        {"query": "library hours"},
        {"query_type": "name", "value": "JSTOR"},
        {"topic": "bookstore"},
        {"query": "the great gatsby"},
        {"query": "PSYC C1000"},
    ]
    assert all(c["status"] == "success" for c in body["tool_calls"])


def test_tool_calls_results_are_exactly_what_was_sent_to_the_model(monkeypatch):
    # "In the form the model received them as toolResult content" - not a re-rendering.
    _wire_primo(monkeypatch, _primo_search_payload([_primo_doc("The Great Gatsby", rid="alma1")]))
    bedrock = FakeBedrockRuntime(converse_script=_all_five_tools_script())
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "everything", "include_full_context": True})), None
    )
    body = json.loads(resp["body"])

    assert [c["result"] for c in body["tool_calls"]] == _sent_tool_result_jsons(bedrock)


def test_tool_calls_exposes_the_catalog_result_full_context_cannot_show(monkeypatch):
    # The row that proved the bug: an authoritative database answer, graded against an EMPTY
    # context because full_context is KB-only. tool_calls carries the actual verdict.
    bedrock = FakeBedrockRuntime(
        converse_script=[
            catalog_tool_use_turn("name", "Opposing Viewpoints In Context"),
            end_turn("Yes, the library has it."),
        ]
    )
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(
            json.dumps({"query": "do you have Opposing Viewpoints?", "include_full_context": True})
        ),
        None,
    )
    body = json.loads(resp["body"])

    assert body["full_context"] == []  # still KB-only, unchanged
    (call,) = body["tool_calls"]
    assert call["tool"] == "database_catalog"
    assert call["returned_results"] is True
    assert call["result"]["held"] is True
    assert call["result"]["name"] == "Opposing Viewpoints In Context"


def test_tool_calls_exposes_library_links_result_and_its_topic(monkeypatch):
    bedrock = FakeBedrockRuntime(
        converse_script=[links_tool_use_turn("campus map"), end_turn("Here is the map.")]
    )
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "campus map?", "include_full_context": True})), None
    )
    body = json.loads(resp["body"])

    assert body["full_context"] == []
    (call,) = body["tool_calls"]
    assert call["tool"] == "library_links"
    assert call["input"] == {"topic": "campus map"}
    assert call["result"]["matched"] is True
    assert call["returned_results"] is True
    # The full curated rows the model was handed, keys and URLs intact - the eval can now see
    # exactly which links it was given rather than inferring them from the answer text.
    assert [link["key"] for link in call["result"]["links"]] == [
        "campus_map_main",
        "campus_map_interactive",
    ]
    assert all(link["url"] for link in call["result"]["links"])


def test_tool_calls_exposes_primo_records_with_availability(monkeypatch):
    _wire_primo(
        monkeypatch,
        _primo_search_payload([_primo_doc("The Great Gatsby", author="Fitzgerald$$Qauthor", rid="alma1")], total=7),
        {"alma1": _delivery_available()},
    )
    bedrock = FakeBedrockRuntime(
        converse_script=[primo_tool_use_turn("the great gatsby"), end_turn("The catalog shows a copy.")]
    )
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "gatsby?", "include_full_context": True})), None
    )
    body = json.loads(resp["body"])

    assert body["full_context"] == []
    (call,) = body["tool_calls"]
    assert call["tool"] == "search_book_catalog"
    assert call["result"]["total"] == 7
    assert call["result"]["results"][0]["title"] == "The Great Gatsby"
    assert call["result"]["results"][0]["availability"]["status"] == "available"
    assert call["returned_results"] is True


def test_tool_calls_exposes_course_reserves_records(monkeypatch):
    _wire_primo(
        monkeypatch,
        _primo_search_payload([_reserve_doc("Introduction to psychology", courses=["PSYC C1000"], rid="r1")], total=3),
    )
    bedrock = FakeBedrockRuntime(
        converse_script=[reserves_tool_use_turn("PSYC C1000"), end_turn("It is on reserve.")]
    )
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "psych textbook on reserve?", "include_full_context": True})),
        None,
    )
    body = json.loads(resp["body"])

    (call,) = body["tool_calls"]
    assert call["tool"] == "search_course_reserves"
    assert call["result"]["results"][0]["courses"] == ["PSYC C1000"]
    assert call["returned_results"] is True


def test_tool_calls_records_kb_passages_the_model_saw(monkeypatch):
    # The KB tool is traced too, so tool_calls is the single complete view of the evidence.
    bedrock = FakeBedrockRuntime(converse_script=search_then_answer("Open 9 to 5."))
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hours?", "include_full_context": True})), None
    )
    body = json.loads(resp["body"])

    (call,) = body["tool_calls"]
    assert call["tool"] == "search_library_info"
    assert call["returned_results"] is True
    assert [p["text"] for p in call["result"]["passages"]] == [c["text"] for c in body["full_context"]]


def test_tool_calls_marks_a_primo_soft_fail_as_an_errored_call(monkeypatch):
    _wire_primo(monkeypatch, None, search_exc=RuntimeError("primo down"))
    bedrock = FakeBedrockRuntime(
        converse_script=[primo_tool_use_turn("gatsby"), end_turn("Catalog search is unavailable.")]
    )
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "gatsby?", "include_full_context": True})), None
    )
    (call,) = json.loads(resp["body"])["tool_calls"]

    assert call["status"] == "error"
    assert call["returned_results"] is False
    assert call["result"]["error"] == "catalog_unavailable"


def test_tool_calls_records_an_unknown_tool_request(monkeypatch):
    bedrock = FakeBedrockRuntime(
        converse_script=[tool_use_turn(name="not_a_tool"), end_turn("Sorry, I cannot do that.")]
    )
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "x", "include_full_context": True})), None
    )
    (call,) = json.loads(resp["body"])["tool_calls"]

    assert call["tool"] == "not_a_tool"
    assert call["status"] == "error"
    assert call["returned_results"] is False


def test_direct_answer_reports_an_empty_trace(monkeypatch):
    # A greeting calls nothing. An empty trace is the honest record of that, and tells the judge
    # the answer was not supposed to have evidence behind it.
    bedrock = FakeBedrockRuntime(converse_script=[end_turn("Hi! How can I help?")])
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hi", "include_full_context": True})), None
    )
    body = json.loads(resp["body"])

    assert body["tool_calls"] == []
    assert body["full_context"] == []


def test_guardrail_block_still_reports_the_tools_that_ran(monkeypatch):
    # Sources are dropped on a block (they would be misleading next to a block message), but the
    # debug trace is not user-facing, and "what ran before the block" is exactly what you need.
    blocked = "I'm not able to provide a response to that."
    bedrock = FakeBedrockRuntime(
        converse_script=[
            catalog_tool_use_turn("name", "JSTOR"),
            {
                "output": {"message": {"role": "assistant", "content": [{"text": blocked}]}},
                "stopReason": "guardrail_intervened",
            },
        ]
    )
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "x", "include_full_context": True})), None
    )
    body = json.loads(resp["body"])

    assert body["answer"] == blocked
    assert body["sources"] == []
    assert _tool_call_names(body) == ["database_catalog"]


def test_multiple_calls_to_one_tool_are_all_traced(monkeypatch):
    # The model may search several times; each call is its own trace entry, in order.
    bedrock = FakeBedrockRuntime(
        converse_script=[multi_tool_use_turn("hours", "parking"), end_turn("Both answered.")]
    )
    _wire(monkeypatch, FakeAgentRuntime(), bedrock)

    resp = handler.lambda_handler(
        _payload_v2_event(json.dumps({"query": "hours and parking", "include_full_context": True})),
        None,
    )
    body = json.loads(resp["body"])

    assert _tool_call_names(body) == ["search_library_info", "search_library_info"]
    assert [c["input"]["query"] for c in body["tool_calls"]] == ["hours", "parking"]


# -- _returned_results (called directly) --


def test_returned_results_is_false_for_an_error_result():
    assert handler._returned_results({"error": "catalog_unavailable"}) is False


def test_returned_results_follows_the_matched_flag_for_links():
    # A links MISS returns the whole table, so a non-empty `links` list is not a match. `matched`
    # is the real signal and takes precedence.
    miss, _ = handler._run_links_tool({"topic": "zzz nothing matches this"})
    assert miss["links"]  # the whole directory came back
    assert handler._returned_results(miss) is False
    hit, _ = handler._run_links_tool({"topic": "campus map"})
    assert handler._returned_results(hit) is True


def test_returned_results_counts_a_not_held_verdict_as_a_result():
    # database_catalog is authoritative about absence: "not held" IS the answer, not a miss.
    assert handler._returned_results({"held": False, "name": "JSTOR"}) is True


def test_returned_results_is_false_for_empty_payload_lists():
    assert handler._returned_results({"passages": []}) is False
    assert handler._returned_results({"query": "x", "total": 0, "results": []}) is False
    assert handler._returned_results({"subject": "basketry", "databases": []}) is False
