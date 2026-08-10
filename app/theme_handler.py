"""Theme-save Lambda for the Gavilan Library Chatbot settings editor.

Fronted by the same API Gateway HTTP API (payload format 2.0) as the query path, on its own
route and its own function, behind the theme-admin Cognito pool's JWT authorizer:

  PUT /theme -> lambda_handler(): validate the parsed theme.json content against the SAME
      rules and caps the settings editor (and widget.js) enforce, rewrite the file in the
      repo's pinned serialisation, PutObject it to the widget bucket root, and invalidate
      /theme.json on the widget distribution. Returns 200 with {"saved": true}.

WHY THIS EXISTS. The editor page used to be download-only: the customer downloaded a
finished theme.json and dragged it into the S3 console. Save removes the console trip for a
signed-in librarian - the page PUTs here and this function performs the one write the
console upload used to be. The download path still works and is untouched.

WHO CAN CALL IT. Nobody unauthenticated: API Gateway validates a Cognito access token from
the theme-admin pool before this function runs. The pool holds named librarian accounts
(admin-created, no self sign-up), so this is not a public write path - but the payload is
still validated as if it were, because "the editor would never send that" is not a runtime
guarantee and the file this writes is served to every visitor of the library's site.

WHAT IT VALIDATES is a mirror of the editor's own rules (which mirror widget.js, pinned by
the contract suite): an allowlisted key set, the widget's hex pattern for the colour, the
five font keywords, and the starter-question count/length caps. Violations are a 400, not a
silent fix-up - the editor cannot produce them, so a violation is a bypass, not a typo. The
one transformation applied is canonicalisation: the file is re-serialised in the pinned two
space form (key order, normalized six digit lowercase colour, collapsed whitespace in
questions, trailing newline), so what lands in the bucket is byte-identical to what the
editor's own Download button would have produced.

WHAT IT TOUCHES: exactly one S3 object (the root theme.json - IAM is scoped to that key)
and one CloudFront invalidation path. A failed invalidation is reported, not fatal: the
theme behavior's 60-second TTL bounds the staleness either way.
"""

import base64
import json
import os
import re
import uuid

import boto3

# Wired by the stack. UNSET means this function exists with nothing behind it (the stack
# always wires them, but "the stack would never do that" is not a runtime guarantee).
WIDGET_BUCKET = os.environ.get("WIDGET_BUCKET")
WIDGET_THEME_KEY = os.environ.get("WIDGET_THEME_KEY", "theme.json")
WIDGET_DISTRIBUTION_ID = os.environ.get("WIDGET_DISTRIBUTION_ID")

# Hard cap on the request body, checked BEFORE parsing. Not a config knob: the largest
# legitimate file (full _readme, four questions per language at the character cap) is under
# 4 KB, so 16 KB accepts every real save with headroom and bounds a garbage payload.
MAX_BODY_BYTES = 16384

# --- The widget's rules, mirrored (pinned against widget.js by tests/unit/test_theme_handler) --

# widget.js normalizeHex: a hex colour and nothing else, six digits or three. The colour is
# concatenated into a stylesheet by the widget, so this pattern is a security boundary.
_HEX_RE = re.compile(r"^#(?:[0-9a-f]{3}|[0-9a-f]{6})$")
_FONT_KEYWORDS = ("system", "sans", "serif", "mono", "inherit")
_LANGUAGES = ("en", "es")
_MAX_STARTER_QUESTIONS = 4
_MAX_STARTER_CHARS = 120

# The saved file's key set and order - the pinned serialisation the contract suite holds
# defaults/theme.json and the editor's Download to.
_ALLOWED_KEYS = ("_readme", "highlightColor", "fontFamily", "starterQuestions")

# Sanity caps on the inert _readme block (the widget ignores it; the editor round-trips the
# shipped lines). Bounded so the one free-form key cannot become a dumping ground.
_MAX_README_LINES = 60
_MAX_README_CHARS = 200

_s3 = None
_cloudfront = None


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _cloudfront_client():
    global _cloudfront
    if _cloudfront is None:
        _cloudfront = boto3.client("cloudfront")
    return _cloudfront


def _response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _log(event_name, **fields):
    """A structured receipt: event names, counts and booleans. The theme content is a public
    file, but the log is still not the record - values are never arguments here."""
    print(json.dumps({"event": event_name, **fields}))


def _raw_body(event):
    """The request body as a string plus its size in BYTES (same shape as feedback_handler:
    base64-decoded if API Gateway encoded it, (None, 0) when unusable)."""
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


def _has_control_chars(text):
    return any(ch < " " or ch == "\x7f" for ch in text)


def _normalize_hex(value):
    """widget.js normalizeHex: trim, lowercase, match the pattern, expand #abc to #aabbcc.
    Returns the canonical six digit lowercase form, or None."""
    if not isinstance(value, str):
        return None
    hex_value = value.strip().lower()
    if not _HEX_RE.match(hex_value):
        return None
    if len(hex_value) == 4:
        hex_value = "#" + "".join(ch * 2 for ch in hex_value[1:])
    return hex_value


def _clean_question_list(value, lang):
    """One language's starter questions, validated at the widget's caps.

    Unlike widget.js readQuestionList (which soft-drops, because it faces a hand-edited
    file), a violation HERE is a 400: the editor cannot produce a fifth question, an
    over-long one, or a non-string, so accepting-and-dropping would hide a bypass.
    Whitespace is collapsed exactly as the editor collapses it (canonicalisation, not
    repair). Returns (cleaned_list_or_None, error_message_or_None); an empty list cleans
    to None, the "omit this language" shape."""
    if not isinstance(value, list):
        return None, f"'starterQuestions.{lang}' must be a list of strings."
    if len(value) > _MAX_STARTER_QUESTIONS:
        return None, (
            f"'starterQuestions.{lang}' may hold at most {_MAX_STARTER_QUESTIONS} questions."
        )
    cleaned = []
    for item in value:
        if not isinstance(item, str):
            return None, f"'starterQuestions.{lang}' must be a list of strings."
        question = re.sub(r"\s+", " ", item).strip()
        if not question:
            return None, f"'starterQuestions.{lang}' contains an empty question."
        if len(question) > _MAX_STARTER_CHARS or _has_control_chars(question):
            return None, (
                f"'starterQuestions.{lang}' questions are capped at "
                f"{_MAX_STARTER_CHARS} characters of plain text."
            )
        cleaned.append(question)
    return (cleaned or None), None


def _validate(data):
    """Enforce the theme.json contract. Returns (canonical_file_dict, error) where exactly
    one is None; `error` is a (status_code, message) pair. The dict's insertion order IS the
    pinned key order - json.dumps preserves it."""
    if not isinstance(data, dict):
        return None, (400, "Request body must be a JSON object (the theme.json content).")

    unexpected = sorted(k for k in data if k not in _ALLOWED_KEYS)
    if unexpected:
        return None, (
            400,
            "Unexpected key(s) in theme: "
            + ", ".join(unexpected)
            + ". Allowed keys: "
            + ", ".join(_ALLOWED_KEYS)
            + ".",
        )

    canonical = {}

    # _readme: optional, inert (the widget ignores it), bounded. The editor round-trips the
    # shipped instruction lines so a saved file still explains itself when downloaded later.
    readme = data.get("_readme")
    if readme is not None:
        if (
            not isinstance(readme, list)
            or len(readme) > _MAX_README_LINES
            or not all(
                isinstance(line, str)
                and len(line) <= _MAX_README_CHARS
                and not _has_control_chars(line)
                for line in readme
            )
        ):
            return None, (
                400,
                f"'_readme' must be a list of at most {_MAX_README_LINES} plain-text lines "
                f"of at most {_MAX_README_CHARS} characters.",
            )
        canonical["_readme"] = list(readme)

    # The two always-written settings. Required: the editor writes both into every file, so
    # their absence means the payload did not come from a theme file at all.
    highlight = _normalize_hex(data.get("highlightColor"))
    if highlight is None:
        return None, (
            400,
            "'highlightColor' must be a hex colour: six digits like #1e4b8f, or three.",
        )
    canonical["highlightColor"] = highlight

    font = data.get("fontFamily")
    font = font.strip().lower() if isinstance(font, str) else None
    if font not in _FONT_KEYWORDS:
        return None, (
            400,
            "'fontFamily' must be one of: " + ", ".join(_FONT_KEYWORDS) + ".",
        )
    canonical["fontFamily"] = font

    # starterQuestions: optional; language keys only; each list validated above. A language
    # that cleans to nothing is omitted, and a block with no languages left is omitted
    # entirely - the file shapes that mean "fall back" to the widget.
    block = data.get("starterQuestions")
    if block is not None:
        if not isinstance(block, dict):
            return None, (400, "'starterQuestions' must be an object with 'en'/'es' lists.")
        unknown = sorted(k for k in block if k not in _LANGUAGES)
        if unknown:
            return None, (
                400,
                "Unexpected language(s) in starterQuestions: "
                + ", ".join(unknown)
                + ". Allowed: "
                + ", ".join(_LANGUAGES)
                + ".",
            )
        questions = {}
        for lang in _LANGUAGES:
            if lang not in block:
                continue
            cleaned, error = _clean_question_list(block[lang], lang)
            if error:
                return None, (400, error)
            if cleaned:
                questions[lang] = cleaned
        if questions:
            canonical["starterQuestions"] = questions

    return canonical, None


def _serialize(canonical):
    """The pinned serialisation: two space indent, key order as inserted, one trailing
    newline, non-ASCII kept literal - byte-identical to the editor's Download and to
    frontend/defaults/theme.json's own form."""
    return json.dumps(canonical, indent=2, ensure_ascii=False) + "\n"


def _not_configured():
    _log("theme_save_not_configured")
    return _response(503, {"error": "Theme saving is not configured for this deployment."})


def lambda_handler(event, context):
    """PUT /theme: validate -> canonicalise -> PutObject -> invalidate. One object, ever."""
    if not WIDGET_BUCKET or not WIDGET_DISTRIBUTION_ID:
        return _not_configured()

    body, size = _raw_body(event)
    if size > MAX_BODY_BYTES:
        _log("theme_save_rejected", reason="body_too_large", bytes=size)
        return _response(
            413, {"error": f"Request body exceeds the maximum size of {MAX_BODY_BYTES} bytes."}
        )
    try:
        data = json.loads(body) if body else None
    except (ValueError, TypeError):
        data = None

    canonical, error = _validate(data)
    if error:
        status, message = error
        _log("theme_save_rejected", reason="invalid_theme", status=status)
        return _response(status, {"error": message})

    text = _serialize(canonical)
    try:
        _s3_client().put_object(
            Bucket=WIDGET_BUCKET,
            Key=WIDGET_THEME_KEY,
            Body=text.encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001 - any S3/runtime fault is a clean 502
        _log("theme_save_failed", error=type(exc).__name__)
        return _response(
            502, {"error": "Could not save the settings right now. Please try again."}
        )

    # Invalidate so the change reads as immediate rather than up-to-60s stale. Soft-fail:
    # the file IS saved, and the theme behavior's TTL delivers it within a minute anyway.
    invalidated = True
    try:
        _cloudfront_client().create_invalidation(
            DistributionId=WIDGET_DISTRIBUTION_ID,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": [f"/{WIDGET_THEME_KEY}"]},
                "CallerReference": str(uuid.uuid4()),
            },
        )
    except Exception as exc:  # noqa: BLE001
        invalidated = False
        _log("theme_invalidation_failed", error=type(exc).__name__)

    _log("theme_saved", bytes=len(text.encode("utf-8")), invalidated=invalidated)
    return _response(200, {"saved": True, "invalidated": invalidated})
