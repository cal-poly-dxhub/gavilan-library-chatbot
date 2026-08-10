"""Unit tests for the theme-save Lambda (app/theme_handler.py).

boto3 is stubbed in sys.modules and both client getters are monkeypatched, so no live AWS is
touched. Events use the API Gateway HTTP API payload format 2.0 shape.

What these tests are actually defending, beyond "it works":
  - the SERIALISATION pin. What the Lambda writes must be byte-identical to the repo's pinned
    theme.json form - the same bytes the editor's Download builds and the contract suite holds
    frontend/defaults/theme.json to. Saving the defaults file's own content must reproduce the
    file exactly.
  - the RULES pin. The handler is a Python copy of the widget's validation rules (the editor's
    JS copy is pinned by the contract suite); these tests read widget.js and hold the copies
    equal so neither can drift silently.
  - rejection over repair. The editor cannot produce a fifth question, an over-long one or an
    unknown key, so the handler treats them as a bypass (400), never a soft drop.
  - the blast radius. One object key, one invalidation path; an invalidation failure reports
    itself but does not unsave the file.
"""

import json
import re
import sys
import types
from pathlib import Path

# app/theme_handler.py lives at the repo root, outside the infra package.
_APP_DIR = Path(__file__).resolve().parents[3] / "app"
sys.path.insert(0, str(_APP_DIR))

# The handler does `import boto3`, which only exists in the Lambda runtime. Stub it; both
# client getters are monkeypatched in every test, so the stub is never called.
if "boto3" not in sys.modules:
    _fake_boto3 = types.ModuleType("boto3")
    _fake_boto3.client = lambda *args, **kwargs: None
    sys.modules["boto3"] = _fake_boto3

import theme_handler  # noqa: E402

_FRONTEND_DIR = Path(__file__).resolve().parents[3] / "frontend"
_DEFAULTS_PATH = _FRONTEND_DIR / "defaults" / "theme.json"
_WIDGET_JS = (_FRONTEND_DIR / "widget.js").read_text(encoding="utf-8")

_BUCKET = "gavilanchatbotstack-widgetbucket-test"
_DISTRIBUTION = "E2TESTDISTRIBUTION"


class FakeS3:
    def __init__(self, error=None):
        self.puts = []
        self._error = error

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        if self._error is not None:
            raise self._error
        return {}


class FakeCloudFront:
    def __init__(self, error=None):
        self.invalidations = []
        self._error = error

    def create_invalidation(self, **kwargs):
        self.invalidations.append(kwargs)
        if self._error is not None:
            raise self._error
        return {"Invalidation": {"Id": "I1"}}


def _wire(monkeypatch, s3=None, cloudfront=None):
    monkeypatch.setattr(theme_handler, "WIDGET_BUCKET", _BUCKET)
    monkeypatch.setattr(theme_handler, "WIDGET_THEME_KEY", "theme.json")
    monkeypatch.setattr(theme_handler, "WIDGET_DISTRIBUTION_ID", _DISTRIBUTION)
    s3 = s3 or FakeS3()
    cloudfront = cloudfront or FakeCloudFront()
    monkeypatch.setattr(theme_handler, "_s3_client", lambda: s3)
    monkeypatch.setattr(theme_handler, "_cloudfront_client", lambda: cloudfront)
    return s3, cloudfront


def _event(body):
    return {
        "version": "2.0",
        "routeKey": "PUT /theme",
        "rawPath": "/theme",
        "requestContext": {"http": {"method": "PUT", "path": "/theme"}},
        "isBase64Encoded": False,
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
    }


def _put(monkeypatch, body, **wire_kwargs):
    s3, cloudfront = _wire(monkeypatch, **wire_kwargs)
    resp = theme_handler.lambda_handler(_event(body), None)
    return resp, s3, cloudfront


def _body(resp):
    return json.loads(resp["body"])


def _theme(**overrides):
    theme = {"highlightColor": "#1e4b8f", "fontFamily": "serif"}
    theme.update(overrides)
    return theme


# --- the serialisation pin -------------------------------------------------------------


def test_saving_the_defaults_content_reproduces_the_shipped_file_byte_for_byte(monkeypatch):
    # THE cross-language pin: the handler's canonical serialisation is the same pinned
    # form the contract suite holds defaults/theme.json and the editor's Download to. If
    # json.dumps and JSON.stringify ever disagree, this is the test that says so.
    defaults_text = _DEFAULTS_PATH.read_text(encoding="utf-8")
    resp, s3, cloudfront = _put(monkeypatch, json.loads(defaults_text))
    assert resp["statusCode"] == 200, resp
    assert _body(resp) == {"saved": True, "invalidated": True}
    (put,) = s3.puts
    assert put["Bucket"] == _BUCKET
    assert put["Key"] == "theme.json"
    assert put["ContentType"] == "application/json"
    assert put["Body"].decode("utf-8") == defaults_text
    (invalidation,) = cloudfront.invalidations
    assert invalidation["DistributionId"] == _DISTRIBUTION
    assert invalidation["InvalidationBatch"]["Paths"]["Items"] == ["/theme.json"]


def test_the_saved_file_is_canonicalised_not_echoed(monkeypatch):
    # Uppercase three-digit hex, keyword case, stray whitespace and an empty language all
    # normalise to the same form the editor itself would have produced.
    resp, s3, _ = _put(
        monkeypatch,
        {
            "fontFamily": "  Serif ",
            "highlightColor": "#A13",
            "starterQuestions": {"en": ["  Where   is the    makerspace? "], "es": []},
        },
    )
    assert resp["statusCode"] == 200, resp
    (put,) = s3.puts
    saved = put["Body"].decode("utf-8")
    assert saved == (
        '{\n  "highlightColor": "#aa1133",\n  "fontFamily": "serif",\n'
        '  "starterQuestions": {\n    "en": [\n      "Where is the makerspace?"\n    ]\n'
        "  }\n}\n"
    )
    # Key order is the pinned one even though the payload led with fontFamily.
    assert list(json.loads(saved)) == ["highlightColor", "fontFamily", "starterQuestions"]


def test_no_usable_questions_means_no_starter_questions_block(monkeypatch):
    resp, s3, _ = _put(monkeypatch, _theme(starterQuestions={"en": [], "es": []}))
    assert resp["statusCode"] == 200, resp
    assert "starterQuestions" not in json.loads(s3.puts[0]["Body"].decode("utf-8"))


# --- the rules pin: the handler's copies against widget.js -----------------------------


def test_handler_rules_match_widget_js():
    fonts = re.search(r"var FONT_KEYWORDS = (\[[^\]]*\]);", _WIDGET_JS)
    assert fonts, "widget.js FONT_KEYWORDS not found"
    assert list(theme_handler._FONT_KEYWORDS) == json.loads(fonts.group(1))
    count = re.search(r"var MAX_STARTER_QUESTIONS = (\d+);", _WIDGET_JS)
    chars = re.search(r"var MAX_STARTER_CHARS = (\d+);", _WIDGET_JS)
    assert theme_handler._MAX_STARTER_QUESTIONS == int(count.group(1))
    assert theme_handler._MAX_STARTER_CHARS == int(chars.group(1))
    langs = re.search(r"var LANGUAGES = (\[[^\]]*\]);", _WIDGET_JS)
    assert list(theme_handler._LANGUAGES) == json.loads(langs.group(1))


def test_hex_normalisation_matches_the_widgets_accepts_and_rejects():
    # The same value table the contract suite runs against widget.js applyTheme.
    assert theme_handler._normalize_hex("#1E4B8F") == "#1e4b8f"
    assert theme_handler._normalize_hex("#a13") == "#aa1133"
    assert theme_handler._normalize_hex("#A13") == "#aa1133"
    assert theme_handler._normalize_hex(" #ffd400 ") == "#ffd400"
    for rejected in (
        "maroon",
        "rgb(138, 28, 48)",
        "8a1c30",
        "#8a1c3",
        "#gggggg",
        "",
        "#fff; } .panel { display: none } .x {",
        "var(--anything)",
        42,
        None,
    ):
        assert theme_handler._normalize_hex(rejected) is None, rejected


# --- rejection over repair -------------------------------------------------------------


def _rejects(monkeypatch, body, fragment):
    resp, s3, cloudfront = _put(monkeypatch, body)
    assert resp["statusCode"] == 400, (body, resp)
    assert fragment in _body(resp)["error"], resp
    assert s3.puts == [] and cloudfront.invalidations == []


def test_unexpected_keys_and_shapes_are_rejected(monkeypatch):
    _rejects(monkeypatch, _theme(uploadedBy="me"), "Unexpected key")
    _rejects(monkeypatch, [1, 2, 3], "JSON object")
    _rejects(monkeypatch, None, "JSON object")
    _rejects(monkeypatch, "not json {", "JSON object")
    _rejects(monkeypatch, _theme(starterQuestions={"fr": ["Où?"]}), "language")
    _rejects(monkeypatch, _theme(starterQuestions=["flat list"]), "starterQuestions")


def test_the_widget_caps_are_enforced_not_applied(monkeypatch):
    _rejects(monkeypatch, _theme(highlightColor="maroon"), "hex colour")
    _rejects(monkeypatch, {"fontFamily": "serif"}, "hex colour")  # colour is required
    _rejects(monkeypatch, _theme(fontFamily="comic sans"), "fontFamily")
    _rejects(monkeypatch, {"highlightColor": "#1e4b8f"}, "fontFamily")  # font is required
    five = [f"Question {i}?" for i in range(5)]
    _rejects(monkeypatch, _theme(starterQuestions={"en": five}), "at most 4")
    long_q = "x" * (theme_handler._MAX_STARTER_CHARS + 1)
    _rejects(monkeypatch, _theme(starterQuestions={"en": [long_q]}), "capped")
    _rejects(monkeypatch, _theme(starterQuestions={"en": ["ok", 3]}), "list of strings")
    _rejects(monkeypatch, _theme(starterQuestions={"en": ["   "]}), "empty question")
    _rejects(monkeypatch, _theme(_readme="not a list"), "_readme")
    _rejects(monkeypatch, _theme(_readme=["ok", "bad\x07line"]), "_readme")


def test_an_oversized_body_is_refused_before_parsing(monkeypatch):
    s3, cloudfront = _wire(monkeypatch)
    big = "x" * (theme_handler.MAX_BODY_BYTES + 1)
    resp = theme_handler.lambda_handler(_event(big), None)
    assert resp["statusCode"] == 413, resp
    assert s3.puts == [] and cloudfront.invalidations == []


# --- blast radius ----------------------------------------------------------------------


def test_an_invalidation_failure_does_not_unsave_the_file(monkeypatch):
    cloudfront = FakeCloudFront(error=RuntimeError("boom"))
    resp, s3, _ = _put(monkeypatch, _theme(), cloudfront=cloudfront)
    assert resp["statusCode"] == 200, resp
    assert _body(resp) == {"saved": True, "invalidated": False}
    assert len(s3.puts) == 1


def test_an_s3_failure_is_a_clean_502(monkeypatch):
    s3 = FakeS3(error=RuntimeError("boom"))
    resp, _, cloudfront = _put(monkeypatch, _theme(), s3=s3)
    assert resp["statusCode"] == 502, resp
    assert cloudfront.invalidations == []


def test_missing_wiring_is_a_503_not_a_write(monkeypatch):
    s3, cloudfront = _wire(monkeypatch)
    monkeypatch.setattr(theme_handler, "WIDGET_BUCKET", None)
    resp = theme_handler.lambda_handler(_event(_theme()), None)
    assert resp["statusCode"] == 503, resp
    assert s3.puts == [] and cloudfront.invalidations == []


def test_rejection_logs_carry_no_values(monkeypatch, capsys):
    _rejects(monkeypatch, _theme(highlightColor="#fff; } .panel {"), "hex colour")
    out = capsys.readouterr().out
    assert ".panel" not in out, out
    events = [json.loads(ln) for ln in out.splitlines() if ln.startswith("{")]
    assert any(e["event"] == "theme_save_rejected" for e in events), events
