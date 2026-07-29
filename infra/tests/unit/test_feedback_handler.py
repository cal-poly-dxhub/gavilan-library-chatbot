"""Unit tests for the feedback-path Lambda (app/feedback_handler.py).

boto3 is stubbed in sys.modules and the SNS client getter is monkeypatched, so no live AWS is
touched and no email is ever sent. Events use the API Gateway HTTP API payload format 2.0 shape.

What these tests are actually defending, beyond "it works":
  - the payload ALLOWLIST. Five fields; a sixth is a rejection, not a pass-through.
  - the caps. Feedback text never reaches a model, so the Bedrock guardrails do not screen it -
    the length caps and plain-text rendering are the only controls that exist.
  - the privacy contract. No IP, user agent, session id or conversation history is accepted or
    recorded, and the comment body appears in NO log line.
  - the cited source URLs reaching the email. Without them a report names nothing to fix, which
    is the entire point of the feature (RAG-first: the fix is a webpage edit).
"""

import json
import os
import sys
import types
from pathlib import Path

# app/feedback_handler.py lives at the repo root, outside the infra package.
_APP_DIR = Path(__file__).resolve().parents[3] / "app"
sys.path.insert(0, str(_APP_DIR))

# The handler does `import boto3`, which only exists in the Lambda runtime. Stub it; the SNS
# client getter is monkeypatched in every test, so the stub is never called.
if "boto3" not in sys.modules:
    _fake_boto3 = types.ModuleType("boto3")
    _fake_boto3.client = lambda *args, **kwargs: None
    sys.modules["boto3"] = _fake_boto3

# Read at import time by the module, as the CDK stack would set them. The caps are deliberately
# SMALLER than config.yaml's real values so the tests prove the caps come from the environment
# rather than from a constant that happens to match.
TOPIC_ARN = "arn:aws:sns:us-west-2:111122223333:GavilanChatbotStack-FeedbackTopic"
os.environ.setdefault("FEEDBACK_TOPIC_ARN", TOPIC_ARN)
os.environ.setdefault("FEEDBACK_MAX_COMMENT_CHARS", "100")
os.environ.setdefault("FEEDBACK_MAX_BODY_BYTES", "512")
os.environ.setdefault("FEEDBACK_MAX_SOURCES", "3")
os.environ.setdefault("AWS_REGION", "us-west-2")

import feedback_handler  # noqa: E402


class FakeSns:
    """Records publish() calls. `error` makes the next publish raise, standing in for any
    SNS/runtime fault."""

    def __init__(self, error=None):
        self.publishes = []
        self._error = error

    def publish(self, **kwargs):
        self.publishes.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"MessageId": "msg-1"}


def _wire(monkeypatch, sns):
    monkeypatch.setattr(feedback_handler, "_sns_client", lambda: sns)


# A realistic report: a student whose answer came from two library pages. The comment is short
# because the test environment caps comments at 100 characters.
def _report_body(**overrides):
    body = {
        "comment": "The hours it gave me are last semester's.",
        "question": "What time does the library close on Friday?",
        "answer": "The library is open 9am to 5pm Monday through Friday.",
        "sources": [
            "https://www.gavilan.edu/library/about-the-library.php",
            "https://www.gavilan.edu/library/howdoi.php",
        ],
    }
    body.update(overrides)
    return body


def _event(body, *, is_base64=False, with_client_metadata=False):
    """A POST /feedback event. `with_client_metadata` adds the requester's IP and user agent -
    API Gateway always sends them, and the handler must never read them."""
    http = {"method": "POST", "path": "/feedback"}
    if with_client_metadata:
        http["sourceIp"] = "203.0.113.47"
        http["userAgent"] = "Mozilla/5.0 (Macintosh) TestBrowser/1.0"
    return {
        "version": "2.0",
        "routeKey": "POST /feedback",
        "rawPath": "/feedback",
        "requestContext": {"http": http, "requestId": "req-abc-123"},
        "isBase64Encoded": is_base64,
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
    }


def _post(monkeypatch, body, **event_kwargs):
    """Send a report and return (response, fake_sns)."""
    sns = FakeSns()
    _wire(monkeypatch, sns)
    resp = feedback_handler.lambda_handler(_event(body, **event_kwargs), None)
    return resp, sns


def _message(sns):
    """The plain-text email body of the single publish call."""
    assert len(sns.publishes) == 1, sns.publishes
    return sns.publishes[0]["Message"]


def _body(resp):
    return json.loads(resp["body"])


def _log_events(capsys):
    """The structured log lines this request emitted, parsed, plus the raw stdout."""
    out = capsys.readouterr().out
    events = [json.loads(ln) for ln in out.splitlines() if ln.startswith("{")]
    return events, out


# --- Happy path ------------------------------------------------------------------------------


def test_valid_report_is_accepted_and_published(monkeypatch):
    resp, sns = _post(monkeypatch, _report_body())

    assert resp["statusCode"] == 202
    assert _body(resp) == {"received": True}
    assert resp["headers"]["Content-Type"] == "application/json"
    # Exactly one publish, to the configured topic.
    assert len(sns.publishes) == 1
    assert sns.publishes[0]["TopicArn"] == TOPIC_ARN


def test_email_carries_the_cited_source_urls(monkeypatch):
    # THE load-bearing assertion. The fix for a wrong answer is editing the page it came from
    # (D-20260727-10), so a notification without these URLs is a complaint box.
    resp, sns = _post(monkeypatch, _report_body())
    message = _message(sns)

    for uri in _report_body()["sources"]:
        assert uri in message, message
    # ...and it says what to do with them, for a reader who does not work on the chatbot.
    assert "pages to check" in message
    assert "re-reads it every week" in message


def test_email_carries_the_comment_question_and_answer(monkeypatch):
    report = _report_body()
    resp, sns = _post(monkeypatch, report)
    message = _message(sns)

    assert report["comment"] in message
    assert report["question"] in message
    assert report["answer"] in message
    # Labelled for a non-technical reader, not as JSON keys.
    for heading in (
        "WHAT THEY SAID",
        "QUESTION THEY ASKED",
        "ANSWER THE CHATBOT GAVE",
        "PAGES THE ANSWER CAME FROM",
        "RECEIVED",
    ):
        assert heading in message, message


def test_email_is_plain_text_only(monkeypatch):
    # No HTML, no per-protocol message structure, no attributes: one plain string body plus a
    # subject. Anything else would be a place for markup to be interpreted.
    resp, sns = _post(monkeypatch, _report_body())
    call = sns.publishes[0]
    assert set(call) == {"TopicArn", "Subject", "Message"}, call
    assert isinstance(call["Message"], str)
    # The rendered body contains no markup of our making.
    assert "<" not in call["Message"]
    assert "</" not in call["Message"]


def test_subject_is_a_constant_with_no_user_text(monkeypatch):
    # The subject is the one field that is structurally a mail header. Nothing the requester sent
    # reaches it - not even a truncated question.
    resp, sns = _post(
        monkeypatch,
        _report_body(
            comment="MARKER-COMMENT",
            question="MARKER-QUESTION",
            answer="MARKER-ANSWER",
        ),
    )
    subject = sns.publishes[0]["Subject"]
    assert subject == feedback_handler._SUBJECT
    for marker in ("MARKER-COMMENT", "MARKER-QUESTION", "MARKER-ANSWER"):
        assert marker not in subject
    # SNS subject rules, which a user-built subject could violate: single line, <= 100 chars.
    assert "\n" not in subject and "\r" not in subject
    assert len(subject) <= 100


def test_a_report_with_no_sources_says_so_rather_than_looking_complete(monkeypatch):
    # An answer that cited nothing is itself worth reporting, so this is a valid report - but the
    # email must not read as though the librarian simply has no pages listed.
    resp, sns = _post(monkeypatch, _report_body(sources=[]))
    assert resp["statusCode"] == 202
    message = _message(sns)
    assert "(none - the answer cited no pages)" in message
    assert "worth a second look" in message


def test_sources_and_comment_are_optional(monkeypatch):
    # The minimum useful report: just the reported pair.
    resp, sns = _post(
        monkeypatch,
        {"question": "Do you have JSTOR?", "answer": "Yes, JSTOR is available."},
    )
    assert resp["statusCode"] == 202
    message = _message(sns)
    assert "(they left no comment)" in message
    assert "(they did not leave an address)" in message


def test_base64_encoded_body_is_decoded(monkeypatch):
    import base64

    raw = json.dumps(_report_body())
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    resp, sns = _post(monkeypatch, encoded, is_base64=True)
    assert resp["statusCode"] == 202
    assert _report_body()["question"] in _message(sns)


# --- The payload allowlist -------------------------------------------------------------------


def test_unexpected_field_is_rejected_not_passed_through(monkeypatch):
    resp, sns = _post(monkeypatch, _report_body(session_id="sess-9f3c"))

    assert resp["statusCode"] == 400
    assert sns.publishes == []
    error = _body(resp)["error"]
    # The error names the field so a client can fix itself...
    assert "session_id" in error
    # ...and lists what is allowed.
    for field in feedback_handler._ALLOWED_FIELDS:
        assert field in error


def test_every_rejected_field_name_is_reported_at_once(monkeypatch):
    resp, sns = _post(
        monkeypatch,
        _report_body(user_agent="TestBrowser/1.0", ip="203.0.113.47", messages=[]),
    )
    assert resp["statusCode"] == 400
    assert sns.publishes == []
    error = _body(resp)["error"]
    for field in ("ip", "messages", "user_agent"):
        assert field in error


def test_conversation_history_is_not_an_accepted_field(monkeypatch):
    # The contract is the single reported pair, never the whole conversation.
    assert "messages" not in feedback_handler._ALLOWED_FIELDS
    assert "history" not in feedback_handler._ALLOWED_FIELDS
    resp, sns = _post(
        monkeypatch,
        _report_body(messages=[{"role": "user", "content": "an earlier question"}]),
    )
    assert resp["statusCode"] == 400
    assert sns.publishes == []


def test_the_allowlist_is_exactly_five_fields():
    assert set(feedback_handler._ALLOWED_FIELDS) == {
        "comment",
        "question",
        "answer",
        "sources",
        "reply_to",
    }


# --- Caps ------------------------------------------------------------------------------------


def test_oversized_comment_is_rejected(monkeypatch):
    over = "x" * (feedback_handler.MAX_COMMENT_CHARS + 1)
    resp, sns = _post(monkeypatch, _report_body(comment=over))

    assert resp["statusCode"] == 400
    assert sns.publishes == []
    assert str(feedback_handler.MAX_COMMENT_CHARS) in _body(resp)["error"]


def test_comment_exactly_at_the_cap_is_accepted(monkeypatch):
    at_cap = "x" * feedback_handler.MAX_COMMENT_CHARS
    resp, sns = _post(monkeypatch, _report_body(comment=at_cap, sources=[]))
    assert resp["statusCode"] == 202
    # Not truncated: a report that stops mid-sentence cannot be told from a student trailing off.
    assert at_cap in _message(sns)


def test_oversized_body_is_rejected_before_parsing(monkeypatch):
    # Bigger than the byte cap, and not even valid JSON - the size check must come first, so the
    # response is 413 rather than a 400 about the JSON.
    resp, sns = _post(monkeypatch, "x" * (feedback_handler.MAX_BODY_BYTES + 1))

    assert resp["statusCode"] == 413
    assert sns.publishes == []
    assert str(feedback_handler.MAX_BODY_BYTES) in _body(resp)["error"]


def test_body_cap_counts_bytes_not_characters(monkeypatch):
    # A multi-byte character costs more than one byte, and the cap is about how much a stranger
    # can push through the endpoint.
    padding = "é" * feedback_handler.MAX_BODY_BYTES  # 2 bytes each
    resp, sns = _post(monkeypatch, json.dumps(_report_body(comment=padding)))
    assert resp["statusCode"] == 413
    assert sns.publishes == []


def test_too_many_sources_is_rejected(monkeypatch):
    too_many = [
        f"https://www.gavilan.edu/library/page{i}.php"
        for i in range(feedback_handler.MAX_SOURCES + 1)
    ]
    resp, sns = _post(monkeypatch, _report_body(sources=too_many))

    assert resp["statusCode"] == 400
    assert sns.publishes == []
    assert str(feedback_handler.MAX_SOURCES) in _body(resp)["error"]


def test_non_http_source_is_rejected(monkeypatch):
    # The bot's own sources are public http(s) page URLs (internal s3:// paths are dropped before
    # they ever reach a client), so anything else did not come from a real answer.
    for bad in ("s3://gavilan-kb-source/library/hours.md", "javascript:alert(1)", "ftp://x/y"):
        resp, sns = _post(monkeypatch, _report_body(sources=[bad]))
        assert resp["statusCode"] == 400, bad
        assert sns.publishes == []


def test_duplicate_sources_are_collapsed(monkeypatch):
    uri = "https://www.gavilan.edu/library/howdoi.php"
    resp, sns = _post(monkeypatch, _report_body(sources=[uri, uri, uri]))
    assert resp["statusCode"] == 202
    assert _message(sns).count(uri) == 1


# --- The reported pair is required ------------------------------------------------------------


def test_missing_question_or_answer_is_rejected(monkeypatch):
    for field in ("question", "answer"):
        body = _report_body()
        del body[field]
        resp, sns = _post(monkeypatch, body)
        assert resp["statusCode"] == 400, field
        assert field in _body(resp)["error"]
        assert sns.publishes == []


def test_blank_or_non_string_pair_is_rejected(monkeypatch):
    for value in ("", "   ", 42, None, {"text": "hi"}, ["hi"]):
        resp, sns = _post(monkeypatch, _report_body(question=value))
        assert resp["statusCode"] == 400, value
        assert sns.publishes == []


def test_malformed_json_and_missing_body_are_rejected(monkeypatch):
    for body in ("{not json", "[]", '"a string"', "null", None):
        resp, sns = _post(monkeypatch, body)
        assert resp["statusCode"] == 400, body
        assert sns.publishes == []


# --- The reply address: the header-injection hazard -------------------------------------------


def test_valid_reply_address_reaches_the_email(monkeypatch):
    resp, sns = _post(monkeypatch, _report_body(reply_to="student@gavilan.edu"))
    assert resp["statusCode"] == 202
    message = _message(sns)
    assert "REPLY TO" in message
    assert "student@gavilan.edu" in message


def test_malformed_reply_address_is_rejected(monkeypatch):
    for bad in (
        "not-an-email",
        "student@",
        "@gavilan.edu",
        "student@gavilan",             # no dot in the domain
        "student @gavilan.edu",
        "Student Name <student@gavilan.edu>",
        "student@gavilan.edu, other@example.com",
        42,
    ):
        resp, sns = _post(monkeypatch, _report_body(reply_to=bad))
        assert resp["statusCode"] == 400, bad
        assert sns.publishes == []
        assert "reply_to" in _body(resp)["error"]


def test_over_length_reply_address_is_rejected(monkeypatch):
    # Its own test with a minimal report, because a 254-character address plus the standard
    # report would trip the BODY cap first and 413 - which is correct, but tests the wrong thing.
    resp, sns = _post(
        monkeypatch,
        {
            "question": "hours?",
            "answer": "9 to 5.",
            "reply_to": "x" * 250 + "@gavilan.edu",
        },
    )
    assert resp["statusCode"] == 400
    assert sns.publishes == []
    assert "reply_to" in _body(resp)["error"]


def test_header_injection_attempt_in_the_reply_address_is_rejected(monkeypatch):
    # The classic shape: a newline followed by another header. Rejected outright rather than
    # sanitized, and it could not reach a header anyway - SNS builds the headers and this handler
    # passes no address to it, so the address is body text.
    for attempt in (
        "student@gavilan.edu\nBcc: victim@example.com",
        "student@gavilan.edu\r\nSubject: Free money",
        "student@gavilan.edu%0ABcc:victim@example.com".replace("%0A", "\n"),
    ):
        resp, sns = _post(monkeypatch, _report_body(reply_to=attempt))
        assert resp["statusCode"] == 400, attempt
        assert sns.publishes == []


def test_empty_reply_address_is_the_same_as_absent(monkeypatch):
    for value in ("", "   "):
        resp, sns = _post(monkeypatch, _report_body(reply_to=value))
        assert resp["statusCode"] == 202, value
        assert "(they did not leave an address)" in _message(sns)


# --- Nothing about the requester is collected --------------------------------------------------


def test_request_ip_and_user_agent_never_reach_the_email(monkeypatch):
    # API Gateway always sends these; the handler must not read them.
    resp, sns = _post(monkeypatch, _report_body(), with_client_metadata=True)
    assert resp["statusCode"] == 202
    message = _message(sns)
    assert "203.0.113.47" not in message
    assert "TestBrowser" not in message
    assert "req-abc-123" not in message


def test_no_identifier_is_generated_for_a_report(monkeypatch):
    # No report id, no session id, no correlation key: there is no store for one to key into, and
    # inventing one would make a stream of reports linkable.
    resp, sns = _post(monkeypatch, _report_body())
    assert _body(resp) == {"received": True}
    message = _message(sns)
    for label in ("id:", "ID:", "session", "Session"):
        assert label not in message, message


# --- Logging: the report is never in CloudWatch ------------------------------------------------


def test_the_comment_body_appears_in_no_log_line(monkeypatch, capsys):
    secret = "UNIQUE-COMMENT-TEXT-1a2b3c"
    resp, sns = _post(
        monkeypatch,
        _report_body(comment=secret, reply_to="student@gavilan.edu"),
        with_client_metadata=True,
    )
    assert resp["statusCode"] == 202
    events, out = _log_events(capsys)

    assert secret not in out
    # Nor the rest of the report, nor the requester.
    for text in (
        _report_body()["question"],
        _report_body()["answer"],
        "student@gavilan.edu",
        "https://www.gavilan.edu/library/howdoi.php",
        "203.0.113.47",
        "TestBrowser",
    ):
        assert text not in out, text
    # What IS logged: a receipt that a report arrived and that publishing succeeded.
    names = [e["event"] for e in events]
    assert names == ["feedback_received", "feedback_published"], events
    received = events[0]
    assert received["has_comment"] is True
    assert received["comment_chars"] == len(secret)
    assert received["source_count"] == 2
    assert received["has_reply_to"] is True


def test_a_rejected_report_logs_no_content_either(monkeypatch, capsys):
    secret = "UNIQUE-REJECTED-TEXT-9z8y7x"
    resp, sns = _post(monkeypatch, _report_body(comment=secret, session_id="sess-1"))
    assert resp["statusCode"] == 400
    events, out = _log_events(capsys)
    assert secret not in out
    assert [e["event"] for e in events] == ["feedback_rejected"]
    assert events[0]["reason"] == "invalid_payload"


def test_an_oversized_body_logs_only_its_size(monkeypatch, capsys):
    secret = "UNIQUE-OVERSIZE-TEXT-4d5e6f"
    resp, sns = _post(monkeypatch, secret + "x" * feedback_handler.MAX_BODY_BYTES)
    assert resp["statusCode"] == 413
    events, out = _log_events(capsys)
    assert secret not in out
    assert events[0]["event"] == "feedback_rejected"
    assert events[0]["reason"] == "body_too_large"


# --- Failure modes ----------------------------------------------------------------------------


def test_publish_failure_returns_a_clean_error_and_logs_no_content(monkeypatch, capsys):
    class _Boom(Exception):
        """Stands in for an SNS fault (Throttled, AuthorizationError, ...)."""

    secret = "UNIQUE-FAILED-PUBLISH-TEXT"
    sns = FakeSns(error=_Boom(f"An error occurred calling Publish: Message={secret}"))
    _wire(monkeypatch, sns)
    resp = feedback_handler.lambda_handler(_event(_report_body(comment=secret)), None)

    assert resp["statusCode"] == 502
    assert "error" in _body(resp)
    assert "Boom" not in _body(resp)["error"]  # no internals to the caller
    events, out = _log_events(capsys)
    # The exception TYPE, never its message: a botocore error can quote the request parameters,
    # and the request parameter here is the student's text.
    failed = [e for e in events if e["event"] == "feedback_publish_failed"]
    assert failed and failed[0]["error"] == "_Boom", events
    assert secret not in out


def test_missing_configuration_refuses_cleanly(monkeypatch):
    # The stack omits the route when there is no destination, so this is the belt-and-braces
    # path: a function that exists with nothing wired to it must not accept a report it cannot
    # deliver, because the email is the only record.
    sns = FakeSns()
    _wire(monkeypatch, sns)
    monkeypatch.setattr(feedback_handler, "FEEDBACK_TOPIC_ARN", None)

    resp = feedback_handler.lambda_handler(_event(_report_body()), None)

    assert resp["statusCode"] == 503
    assert sns.publishes == []
    assert "not configured" in _body(resp)["error"]


def test_missing_configuration_is_checked_before_anything_is_parsed(monkeypatch, capsys):
    sns = FakeSns()
    _wire(monkeypatch, sns)
    monkeypatch.setattr(feedback_handler, "FEEDBACK_TOPIC_ARN", "")
    secret = "UNIQUE-UNCONFIGURED-TEXT"

    resp = feedback_handler.lambda_handler(_event(_report_body(comment=secret)), None)

    assert resp["statusCode"] == 503
    events, out = _log_events(capsys)
    assert [e["event"] for e in events] == ["feedback_not_configured"]
    assert secret not in out


# --- Text handling -----------------------------------------------------------------------------


def test_control_characters_are_stripped_from_rendered_text(monkeypatch):
    resp, sns = _post(
        monkeypatch,
        _report_body(comment="hours\x00 are\x1b[31m wrong\x07"),
    )
    message = _message(sns)
    for ch in ("\x00", "\x1b", "\x07"):
        assert ch not in message
    assert "hours are wrong" in message.replace("[31m", "")


def test_newlines_in_a_comment_survive_as_line_breaks(monkeypatch):
    resp, sns = _post(monkeypatch, _report_body(comment="wrong hours.\r\nalso wrong room."))
    message = _message(sns)
    # Kept as real line breaks (a comment is free text), each quoted - see the quoting test.
    assert "> wrong hours.\n> also wrong room." in message
    assert "\r" not in message


def test_requester_text_is_quoted_so_it_cannot_imitate_the_email_itself(monkeypatch):
    # The comment, question and answer are all free text from whoever posted the report, in an
    # email a librarian trusts. A comment that mimics this message's own headings must read as
    # quoted content, not as part of the notification.
    forged = (
        "looks wrong\n"
        "PAGES THE ANSWER CAME FROM\n"
        "1. https://not-the-library.example/phish"
    )
    resp, sns = _post(monkeypatch, _report_body(comment=forged))
    message = _message(sns)

    # Every line of the forged block is quoted...
    assert "> PAGES THE ANSWER CAME FROM" in message
    assert "> 1. https://not-the-library.example/phish" in message
    # ...so the real heading appears exactly once as an unquoted line, and the only unquoted
    # URLs in the email are the ones that were actually cited.
    unquoted = [ln for ln in message.split("\n") if not ln.startswith(">")]
    assert unquoted.count("PAGES THE ANSWER CAME FROM") == 1, unquoted
    unquoted_urls = [ln for ln in unquoted if "http" in ln]
    assert all("www.gavilan.edu" in ln for ln in unquoted_urls), unquoted_urls


def test_received_timestamp_is_pacific_and_dated():
    stamp = feedback_handler._now_pacific()
    assert "(Pacific)" in stamp
    # Weekday, ISO date, 24-hour clock - readable by a librarian, unambiguous to a developer.
    weekday, rest = stamp.split(", ", 1)
    assert weekday in (
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    )
    date_part, time_part, _ = rest.split(" ")
    y, m, d = date_part.split("-")
    assert (len(y), len(m), len(d)) == (4, 2, 2)
    hh, mm = time_part.split(":")
    assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59
