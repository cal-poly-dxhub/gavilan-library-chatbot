"""Feedback-path Lambda for the Gavilan Library Chatbot.

Fronted by the same API Gateway HTTP API (payload format 2.0) as the query path, on its own
route and its own function:

  POST /feedback -> lambda_handler(): validate a narrow, allowlisted payload, render a PLAIN
      TEXT email, publish it to an SNS topic whose only subscriber is the librarian address in
      config.yaml. Returns 202 with {"received": true}.

WHY THIS EXISTS. The client's first question after "does it work" is "it said something wrong,
how do we fix it?", and until now nobody could even tell us an answer was wrong. The fix itself
is RAG-first (D-20260727-10): correct the library webpage and the next scheduled scrape of that
page's tier corrects the bot. So the ONLY thing that makes this notification worth sending is that it names the pages the
reported answer cited - the email is a work order for a webpage edit, not a complaint box.

NO SERVER-SIDE STORE, deliberately (see docs/architecture.md). There is no table, no bucket and
no logged copy of a report: the email IS the record. A publish failure therefore loses the
report, which is the accepted cost of not accumulating a database of student complaints.

WHAT THIS ACCEPTS is an allowlist, not a filter - five fields, listed in _ALLOWED_FIELDS, and an
unexpected sixth is a 400 rather than something quietly forwarded into a librarian's inbox.
Nothing about the requester is accepted, derived or recorded: no IP address, no user agent, no
session or generated id, and no conversation history beyond the single question/answer pair being
reported. The event carries a source IP and user agent (API Gateway puts them in
requestContext.http) and this module never reads them.

WHAT NEVER REACHES CLOUDWATCH is the report itself. Logging is a receipt - that a report arrived
and whether publishing it worked - and carries no comment, question, answer, address or URL. The
log is not the record and must not become one by accident.

WHAT SCREENS THE TEXT: nothing but the caps below. Feedback text is never sent to a model, so the
Bedrock guardrail that screens /query does not apply here (and it only screens that path's input
for prompt injection anyway - it was never a content filter on free text). The controls that exist are the
configured length caps, the body-size cap, plain-text-only rendering, and a subject line built
from a constant.
"""

import base64
import datetime
import json
import os
import re

import boto3

# The SNS topic the stack creates, with the librarian's address as its only subscription. UNSET
# means this deployment has no feedback destination, and every request is refused (see
# _not_configured): the stack normally omits the route entirely in that case, so this is the
# belt-and-braces path for a function that exists with nothing wired to it.
FEEDBACK_TOPIC_ARN = os.environ.get("FEEDBACK_TOPIC_ARN")
REGION = os.environ.get("AWS_REGION")

# Size caps, wired from config.yaml by the stack (feedback.*). The defaults are a local-run safety
# net and match config.py's defaults; config.yaml is the source of truth.
MAX_COMMENT_CHARS = int(os.environ.get("FEEDBACK_MAX_COMMENT_CHARS", "1000"))
MAX_BODY_BYTES = int(os.environ.get("FEEDBACK_MAX_BODY_BYTES", "8192"))
MAX_SOURCES = int(os.environ.get("FEEDBACK_MAX_SOURCES", "12"))

# Sanity cap on one source URL. Not a config knob: it exists so a single absurd string cannot
# dominate the email, and the body cap already bounds the total.
_MAX_SOURCE_CHARS = 2048

# The ENTIRE accepted payload. Anything else in the body is a 400 (see _validate): a field we do
# not recognize is either a client that has drifted from this contract or an attempt to smuggle
# something into the email, and both should fail loudly rather than pass through.
#   comment  - optional free text the student typed. Capped at MAX_COMMENT_CHARS.
#   question - the reported question (required). The pair being reported, not the conversation.
#   answer   - the answer the bot gave for it (required).
#   sources  - the source URIs that answer cited. THE POINT OF THE FEATURE: these are the pages a
#              librarian edits to fix the answer. May be empty - an answer that cited nothing is
#              itself the report.
#   reply_to - optional address, only if the student volunteered one.
_ALLOWED_FIELDS = ("comment", "question", "answer", "sources", "reply_to")

# The OPTIONAL reply-to address. Kept strict on purpose: it is the one piece of user-supplied text
# that could be read as an instruction rather than as content, so it is validated to a shape with
# no room for a newline, a comma, a bracket or a display name. It is rendered in the message BODY
# and never as a mail header - SNS builds the headers itself and this handler passes no address to
# it - so this check is the second line of defense, not the only one. Same pattern as
# infra/infra/config.py's destination check (which cannot be imported here: different runtimes).
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_EMAIL_MAX_CHARS = 254

# The only schemes a cited source may use. The bot's own `sources` are public http(s) page URLs
# (handler._build_sources drops anything that resolves only to an s3:// path), so anything else
# here did not come from a real answer.
_ALLOWED_SOURCE_SCHEMES = ("https://", "http://")

# Subject line. A CONSTANT, with no user-supplied text interpolated into it at any point: a
# subject is the one field in an email that is structurally a header, so the safest version of
# "never interpolate user text into a header" is to have nothing to interpolate.
_SUBJECT = "Gavilan Library chatbot: a student reported an answer"

# Timezone for the "received" line. The librarian reading this works Pacific time, and the Lambda
# runs UTC - after 5pm Pacific a UTC stamp reads as tomorrow. Same approach as the query handler.
_LIBRARY_TZ = "America/Los_Angeles"

_sns = None


def _sns_client():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns", region_name=REGION)
    return _sns


def _response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _now_pacific():
    """Now, at the library, as 'Tuesday, 2026-07-29 14:32 (Pacific)'. Never raises: zoneinfo
    reads the OS tzdata (present on Amazon Linux 2023), and a missing database falls back to a
    fixed -08:00 rather than silently stamping UTC on a librarian's work order."""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.datetime.now(ZoneInfo(_LIBRARY_TZ))
    except Exception:  # noqa: BLE001 - missing tzdata must not lose a report
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-8)))
    return now.strftime("%A, %Y-%m-%d %H:%M (Pacific)")


def _clean_text(value):
    """User text, safe to drop into a plain-text email body.

    Normalizes newlines and strips C0/C1 control characters except tab and newline. A comment is
    free text, so real line breaks are kept; a stray carriage return, NUL or escape sequence is
    not content and is the shape of an attempt to control a terminal, a log line or a header."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text if ch in "\n\t" or (ch >= " " and ch != "\x7f"))


def _single_line(value):
    """Same as _clean_text but with every newline and tab removed - for a value that must occupy
    exactly one line of the email (the reply-to address, a source URL)."""
    return " ".join(_clean_text(value).split())


def _quoted(text):
    """Every line of requester-supplied text prefixed with '> '.

    The comment, the question and the answer all arrive as free text from whoever posted the
    report, and they land in an email a librarian trusts. Quoting them makes the notification's
    own structure unambiguous: the only unquoted lines are ours. Without it, a comment can mimic
    this message's own headings - a convincing 'PAGES THE ANSWER CAME FROM' list pointing
    somewhere else - and a librarian has no way to tell which lines the system wrote. Standard
    mail-quoting convention, so it costs nothing in readability."""
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def _raw_body(event):
    """The request body as a string, base64-decoded if API Gateway encoded it, plus its size in
    BYTES. Returns (body, byte_length), or (None, 0) when there is no usable body.

    Bytes rather than characters because the cap is about how much a stranger can push through
    the endpoint, and a multi-byte character costs more than one."""
    body = event.get("body")
    if body is None:
        return None, 0
    if event.get("isBase64Encoded"):
        try:
            decoded = base64.b64decode(body)
        except Exception:  # noqa: BLE001 - a malformed body is a 400, not a 500
            return None, 0
        return decoded.decode("utf-8", errors="replace"), len(decoded)
    if not isinstance(body, str):
        return None, 0
    return body, len(body.encode("utf-8"))


def _validate(data):
    """Enforce the payload contract. Returns (report, error) where exactly one is None.

    `report` is the cleaned, allowlisted content the email is rendered from. `error` is a
    (status_code, message) pair for a clean rejection. Rejection messages name FIELDS, never
    values: the point of the endpoint is to move a student's text to one mailbox, so it should
    not also echo that text back to whoever posted it."""
    if not isinstance(data, dict):
        return None, (400, "Request body must be a JSON object.")

    # Allowlist first, before anything is read. An unexpected field is refused rather than
    # ignored: silently dropping it lets a client believe it sent something we act on.
    unexpected = sorted(k for k in data if k not in _ALLOWED_FIELDS)
    if unexpected:
        return None, (
            400,
            "Unexpected field(s) in request body: "
            + ", ".join(unexpected)
            + ". Allowed fields: "
            + ", ".join(_ALLOWED_FIELDS)
            + ".",
        )

    # The reported pair. Required: a report with no answer in it names nothing to fix.
    pair = {}
    for field in ("question", "answer"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            return None, (400, f"'{field}' is required and must be a non-empty string.")
        pair[field] = _clean_text(value.strip())

    # The comment: optional, hard-capped. Over the cap is a rejection rather than a silent
    # truncation - a librarian reading a report that stops mid-sentence cannot tell whether the
    # student trailed off or the server ate the rest.
    comment = data.get("comment")
    if comment is None:
        comment = ""
    if not isinstance(comment, str):
        return None, (400, "'comment' must be a string.")
    comment = _clean_text(comment.strip())
    if len(comment) > MAX_COMMENT_CHARS:
        return None, (
            400,
            f"'comment' exceeds the maximum length of {MAX_COMMENT_CHARS} characters.",
        )

    # The cited sources - the payload's reason to exist.
    raw_sources = data.get("sources")
    if raw_sources is None:
        raw_sources = []
    if not isinstance(raw_sources, list):
        return None, (400, "'sources' must be a list of source URLs.")
    if len(raw_sources) > MAX_SOURCES:
        return None, (400, f"'sources' may contain at most {MAX_SOURCES} URLs.")
    sources = []
    for item in raw_sources:
        if not isinstance(item, str):
            return None, (400, "'sources' must be a list of source URLs.")
        uri = _single_line(item.strip())
        if not uri:
            continue
        if len(uri) > _MAX_SOURCE_CHARS or not uri.startswith(_ALLOWED_SOURCE_SCHEMES):
            return None, (400, "'sources' entries must be http(s) URLs.")
        if uri not in sources:
            sources.append(uri)

    # The optional reply address. Absent and empty are the same thing (a student who left the
    # field blank); present-but-malformed is a rejection, so a client cannot discover that a
    # weird address was accepted and then silently dropped.
    reply_to = data.get("reply_to")
    if reply_to is None:
        reply_to = ""
    if not isinstance(reply_to, str):
        return None, (400, "'reply_to' must be an email address string.")
    reply_to = _single_line(reply_to.strip())
    if reply_to and (len(reply_to) > _EMAIL_MAX_CHARS or not _EMAIL_RE.match(reply_to)):
        return None, (400, "'reply_to' is not a valid email address.")

    return {
        "comment": comment,
        "question": pair["question"],
        "answer": pair["answer"],
        "sources": sources,
        "reply_to": reply_to,
    }, None


def _render_email(report):
    """The PLAIN TEXT notification a librarian receives. No HTML, no markup, no attachment.

    Written for a reader who does not work on the chatbot: it leads with what the student said,
    shows the exchange being reported, and then names the pages to edit and says plainly that
    editing one of them is the fix. Nothing else is in here - no request metadata, no ids, no
    conversation history, no internal identifiers.

    The three free-text fields are quoted (see _quoted), so every unquoted line is ours."""
    lines = [
        "A student reported an answer from the Gavilan Library chatbot.",
        "",
        "WHAT THEY SAID",
        _quoted(report["comment"]) if report["comment"] else "(they left no comment)",
        "",
        "QUESTION THEY ASKED",
        _quoted(report["question"]),
        "",
        "ANSWER THE CHATBOT GAVE",
        _quoted(report["answer"]),
        "",
        "PAGES THE ANSWER CAME FROM",
    ]
    if report["sources"]:
        lines.extend(f"{i}. {uri}" for i, uri in enumerate(report["sources"], start=1))
        lines.extend(
            [
                "",
                "These are the pages to check. The chatbot answers from the library",
                "website and re-reads it every week, so correcting a page corrects the",
                "chatbot - no code change and no ticket needed.",
            ]
        )
    else:
        lines.extend(
            [
                "(none - the answer cited no pages)",
                "",
                "An answer with no pages behind it is worth a second look: the chatbot",
                "may have answered from something other than the library website.",
            ]
        )
    lines.extend(
        [
            "",
            "REPLY TO",
            report["reply_to"] or "(they did not leave an address)",
            "",
            "RECEIVED",
            _now_pacific(),
            "",
            "This email is the only record of this report - nothing is stored.",
        ]
    )
    return "\n".join(lines)


def _log(event_name, **fields):
    """A structured receipt. Callers pass counts and booleans only: the comment, question,
    answer, reply address and source URLs are never arguments to this function, because the log
    is not the record and must not become one."""
    print(json.dumps({"event": event_name, **fields}))


def _not_configured():
    """No topic wired: refuse cleanly. The stack omits the route when there is no destination, so
    reaching this means a function exists with nothing behind it - which must not look like a
    delivered report."""
    _log("feedback_not_configured")
    return _response(
        503,
        {"error": "Feedback is not configured for this deployment."},
    )


def lambda_handler(event, context):
    """POST /feedback: validate -> render plain text -> SNS publish. No storage, no retry."""
    if not FEEDBACK_TOPIC_ARN:
        return _not_configured()

    body, size = _raw_body(event)
    # Size cap BEFORE parsing: a hard ceiling on top of stage throttling, which is the volume
    # control. The Lambda is still invoked for an oversized body (nothing but WAF could prevent
    # that, and WAF cannot attach to an HTTP API), but nothing is parsed, rendered or published.
    if size > MAX_BODY_BYTES:
        _log("feedback_rejected", reason="body_too_large", bytes=size)
        return _response(
            413, {"error": f"Request body exceeds the maximum size of {MAX_BODY_BYTES} bytes."}
        )
    try:
        data = json.loads(body) if body else None
    except (ValueError, TypeError):
        data = None

    report, error = _validate(data)
    if error:
        status, message = error
        # The reason is a field-level code, never the rejected value.
        _log("feedback_rejected", reason="invalid_payload", status=status)
        return _response(status, {"error": message})

    # Receipt of a valid report: counts and booleans only, no content.
    _log(
        "feedback_received",
        has_comment=bool(report["comment"]),
        comment_chars=len(report["comment"]),
        source_count=len(report["sources"]),
        has_reply_to=bool(report["reply_to"]),
    )

    try:
        _sns_client().publish(
            TopicArn=FEEDBACK_TOPIC_ARN,
            # Constant subject; the report is entirely in the body.
            Subject=_SUBJECT,
            Message=_render_email(report),
        )
    except Exception as exc:  # noqa: BLE001 - any SNS/runtime fault, reported without the report
        # The exception TYPE only. A botocore error message can quote the offending request
        # parameters, and the request parameter here is the student's text.
        _log("feedback_publish_failed", error=type(exc).__name__)
        return _response(
            502,
            {"error": "Could not send your feedback right now. Please try again in a moment."},
        )

    _log("feedback_published")
    return _response(202, {"received": True})
