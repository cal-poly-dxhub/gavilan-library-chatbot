/*
 * Zero-dependency tests for the widget's pure logic + safety invariants.
 * Runs on plain Node (no jsdom, no npm install):
 *
 *     node frontend/test/widget.contract.test.js
 *
 * Covers the response-contract normalization and the URL sanitizer in the
 * production widget (widget.js), the routing/shape of the dev-only mock
 * (mock.js, which the shipped widget never references), and a static source
 * scan of widget.js for the "no storage / no innerHTML / correct request shape
 * / no mock code" rules. The full in-browser DOM round-trip is verified
 * separately with jsdom (see the build notes); this file intentionally stays
 * dependency-free so it can live in the repo.
 */
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const WIDGET_PATH = path.join(__dirname, "..", "widget.js");
const widget = require(WIDGET_PATH);
const SOURCE = fs.readFileSync(WIDGET_PATH, "utf8");

// The mock lives in its own dev-only file; widget.js must not reference it.
const mock = require(path.join(__dirname, "..", "mock.js"));

// --- tiny async test runner (no deps) -----------------------------------
const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

async function run() {
  let passed = 0;
  const failures = [];
  for (const t of tests) {
    try {
      await t.fn();
      passed++;
      console.log("  ✓ " + t.name);
    } catch (err) {
      failures.push({ name: t.name, err });
      console.log("  ✗ " + t.name);
      console.log("      " + (err && err.stack ? err.stack.split("\n").join("\n      ") : err));
    }
  }
  console.log("\n" + passed + "/" + tests.length + " passed, " + failures.length + " failed");
  if (failures.length) process.exitCode = 1;
}

// --- normalizeResponse: enforce the { answer, sources[] } contract ------
test("normalizeResponse keeps a well-formed payload intact", () => {
  const out = widget.normalizeResponse({
    answer: "Open 8-7.",
    sources: [{ uri: "https://x.test/a", excerpt: "hi" }]
  });
  assert.deepStrictEqual(out, {
    answer: "Open 8-7.",
    sources: [{ uri: "https://x.test/a", excerpt: "hi" }]
  });
});

test("normalizeResponse coerces missing/wrong-typed fields to safe defaults", () => {
  assert.deepStrictEqual(widget.normalizeResponse({}), { answer: "", sources: [] });
  assert.deepStrictEqual(widget.normalizeResponse(null), { answer: "", sources: [] });
  assert.deepStrictEqual(widget.normalizeResponse({ answer: 42, sources: "nope" }), {
    answer: "",
    sources: []
  });
});

test("normalizeResponse drops sources without a string uri, fills missing excerpt", () => {
  const out = widget.normalizeResponse({
    answer: "a",
    sources: [
      { uri: "https://ok.test/1" },      // missing excerpt -> ""
      { excerpt: "no uri" },              // dropped
      { uri: 5, excerpt: "bad uri" },     // dropped
      null                                 // dropped
    ]
  });
  assert.deepStrictEqual(out.sources, [{ uri: "https://ok.test/1", excerpt: "" }]);
});

// --- safeHttpUrl: block dangerous schemes -------------------------------
test("safeHttpUrl allows http and https", () => {
  assert.strictEqual(widget.safeHttpUrl("https://www.gavilan.edu/library/"), "https://www.gavilan.edu/library/");
  assert.strictEqual(widget.safeHttpUrl("http://x.test/p"), "http://x.test/p");
});

test("safeHttpUrl rejects javascript:, data:, file:, and relative URLs", () => {
  assert.strictEqual(widget.safeHttpUrl("javascript:alert(1)"), null);
  assert.strictEqual(widget.safeHttpUrl("JavaScript:alert(1)"), null);
  assert.strictEqual(widget.safeHttpUrl("data:text/html,<script>alert(1)</script>"), null);
  assert.strictEqual(widget.safeHttpUrl("file:///etc/passwd"), null);
  assert.strictEqual(widget.safeHttpUrl("/library/hours.php"), null);
  assert.strictEqual(widget.safeHttpUrl(""), null);
  assert.strictEqual(widget.safeHttpUrl(null), null);
});

// --- mock routing + contract shape --------------------------------------
async function mockAnswer(q) {
  const r = await mock.mockQuery(q);
  assert.ok(typeof r.answer === "string" && r.answer.length > 0, "answer is non-empty string");
  assert.ok(Array.isArray(r.sources), "sources is an array");
  for (const s of r.sources) {
    assert.ok(typeof s.uri === "string" && typeof s.excerpt === "string", "source shape");
  }
  return r;
}

test("mock: hours question returns an answer WITH a source link", async () => {
  const r = await mockAnswer("What are the library hours?");
  assert.match(r.answer, /Monday|8:00 AM|hours/i);
  assert.ok(r.sources.length >= 1, "expected a source");
  assert.match(r.sources[0].uri, /gavilan\.edu\/library\/hours/);
});

test("mock: checkout question returns an answer WITH a source link", async () => {
  const r = await mockAnswer("How do I check out a book?");
  assert.match(r.answer, /three weeks|renew|ID/i);
  assert.ok(r.sources.length >= 1);
});

test("mock: textbook question returns a clarifying answer with NO sources", async () => {
  const r = await mockAnswer("I need a textbook for my class");
  assert.match(r.answer, /whole semester|short time|online access/i);
  assert.deepStrictEqual(r.sources, [], "textbook clarifier must have no sources");
});

test("mock: research question routes to a librarian", async () => {
  const r = await mockAnswer("Can you help me find research articles?");
  assert.match(r.answer, /librarian|research guides|not able to do research/i);
});

test("mock: IT/account question is handled as out-of-scope, no library source", async () => {
  const r = await mockAnswer("I forgot my email password");
  assert.match(r.answer, /IT Help Desk|outside/i);
  assert.deepStrictEqual(r.sources, []);
});

test("mock: unmatched question falls back to a helpful menu", async () => {
  const r = await mockAnswer("asdf qwerty zxcv");
  assert.match(r.answer, /hours|check|textbook|offers/i);
  assert.deepStrictEqual(r.sources, []);
});

test("mock: 'trigger error' rejects (drives the error state)", async () => {
  await assert.rejects(() => mock.mockQuery("please trigger error now"), /Simulated backend failure/);
});

// --- static source scan: enforce the hard rules -------------------------
test("source keeps nothing that outlives the tab: no localStorage, no indexedDB, no cookie", () => {
  // sessionStorage is now allowed for ONE thing (the temporary gate's access token, so a reload
  // does not throw away a session with most of a day left on it) - and these three are still
  // banned outright, because each of them survives a browser restart on a shared library
  // terminal. The next test pins that the allowance did not widen.
  //
  // localStorage is matched as USAGE (a property read, a call, an assignment), not as the word:
  // the gate's comment has to be able to say what it deliberately did not use, and the same
  // rule the innerHTML scan below follows applies here.
  [
    /\blocalStorage\s*[.[]/,     // localStorage.setItem(...) / localStorage["k"]
    /[=(]\s*localStorage\b/,     // var s = localStorage / f(localStorage)
    /\breturn\s+localStorage\b/  // handed back from a helper
  ].forEach(function (pattern) {
    assert.ok(!pattern.test(SOURCE), "must not use localStorage (" + pattern + ")");
  });
  assert.ok(!/\bindexedDB\b/i.test(SOURCE), "must not use indexedDB");
  assert.ok(!/document\s*\.\s*cookie/.test(SOURCE), "must not use cookies");
});

test("the only thing ever written to storage is the gate's session key", () => {
  // The transcript and the language choice stay in memory: a widget that quietly started
  // persisting a student's questions would pass every other test in this file.
  var writes = SOURCE.match(/\.setItem\(\s*([A-Za-z_$][\w$]*|"[^"]*")/g) || [];
  assert.strictEqual(writes.length, 1, "exactly one setItem call in the whole widget");
  assert.ok(/AUTH_STORAGE_KEY/.test(writes[0]), "and it writes the auth session key");
  assert.strictEqual(widget.AUTH_STORAGE_KEY, "gavilan-chatbot-session");
});

test("source never uses unsafe HTML sinks (innerHTML/outerHTML/document.write/insertAdjacentHTML)", () => {
  // Match actual USAGE (assignment / call), not the words in explanatory comments.
  assert.ok(!/\.innerHTML\s*=/.test(SOURCE), "must not assign innerHTML");
  assert.ok(!/\.outerHTML\s*=/.test(SOURCE), "must not assign outerHTML");
  assert.ok(!/\.insertAdjacentHTML\s*\(/.test(SOURCE), "must not call insertAdjacentHTML");
  assert.ok(!/document\.write\s*\(/.test(SOURCE), "must not call document.write");
});

test("source sends the request shape the handler reads: JSON body { messages }", () => {
  // Single-session history: the widget POSTs the whole conversation as a `messages` array,
  // not a single { query }. The server trims/caps it.
  assert.ok(
    /JSON\.stringify\(\{\s*messages:\s*messages\s*\}\)/.test(SOURCE),
    "must POST { messages: [...] }"
  );
  assert.ok(!/JSON\.stringify\(\{\s*query:/.test(SOURCE), "must not POST the legacy { query } shape");
  assert.ok(/"Content-Type":\s*"application\/json"/.test(SOURCE), "must set JSON content-type");
  assert.ok(/method:\s*"POST"/.test(SOURCE), "must POST");
});

// --- usage events: opt-in, and invisible to the production embed --------
//
// The demo site meters what a conversation costs, which needs the backend's opt-in
// `include_usage` payload. The widget is what makes the request, so it has to ask -
// and these tests are the guarantee that asking is strictly opt-in: with the attribute
// absent (the library's embed, and the tag the CDK output prints) the request body and
// the rendered result are exactly what they were before the feature existed.

// Like withNetwork, but lets a test control what each script-tag attribute returns, so
// the presence of data-usage-events can actually be varied. `attrs` maps attribute name
// to value; anything unlisted reads as absent (null), which is the production case.
function withNetworkAttrs(fetchImpl, attrs) {
  var priorDoc = global.document;
  var priorFetch = global.fetch;
  var priorWindow = global.window;
  var priorCustomEvent = global.CustomEvent;
  var priorErr = console.error;
  var seen = [];
  global.document = {
    querySelector: function () {
      return {
        getAttribute: function (name) {
          return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null;
        }
      };
    }
  };
  global.fetch = fetchImpl;
  global.CustomEvent = function (type, init) {
    this.type = type;
    this.detail = init && init.detail;
  };
  global.window = {
    dispatchEvent: function (ev) { seen.push(ev); return true; }
  };
  console.error = function () {};
  return {
    events: seen,
    restore: function () {
      global.document = priorDoc;
      global.fetch = priorFetch;
      global.window = priorWindow;
      global.CustomEvent = priorCustomEvent;
      console.error = priorErr;
    }
  };
}

// NOTE the name: there is a second, simpler okJson() further down this file, and a plain
// `function okJson` here would be hoisted over by it (last declaration wins), silently
// dropping the capture argument and leaving every assertion below reading an empty array.
function okJsonCapturing(payload, capture) {
  return function (url, opts) {
    if (capture) capture.push(JSON.parse(opts.body));
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(payload); } });
  };
}

test("usage events are off unless the embed opts in", () => {
  // No document at all (the plain-Node case) must not throw and must read as off.
  assert.strictEqual(widget.usageEventsEnabled(), false);
  assert.strictEqual(widget.USAGE_ATTR, "data-usage-events");
});

test("the production embed sends no include_usage flag and emits no event", async () => {
  var bodies = [];
  var h = withNetworkAttrs(
    okJsonCapturing({ answer: "hi", sources: [], usage: { model_calls: 2 } }, bodies),
    { "data-api-url": "https://api.test/query" } // no data-usage-events
  );
  try {
    var res = await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.deepStrictEqual(bodies, [{ messages: [{ role: "user", content: "hours?" }] }]);
    assert.ok(!("include_usage" in bodies[0]), "production body must not carry the flag");
    assert.strictEqual(h.events.length, 0, "production embed must emit no usage event");
    // Even when the backend volunteers usage, the rendered contract is unchanged.
    assert.deepStrictEqual(res, { answer: "hi", sources: [] });
  } finally {
    h.restore();
  }
});

test("an embed with data-usage-events sends the flag and re-broadcasts usage", async () => {
  var bodies = [];
  var usage = { model_calls: 3, input_tokens: 41000, output_tokens: 260 };
  var h = withNetworkAttrs(
    okJsonCapturing({ answer: "hi", sources: [], usage: usage }, bodies),
    { "data-api-url": "https://api.test/query", "data-usage-events": "true" }
  );
  try {
    var res = await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.strictEqual(bodies[0].include_usage, true, "opted-in body must carry the flag");
    assert.strictEqual(h.events.length, 1, "one usage event per answer");
    assert.strictEqual(h.events[0].type, widget.USAGE_EVENT);
    assert.deepStrictEqual(h.events[0].detail.usage, usage);
    // Opting in must not change what the UI renders.
    assert.deepStrictEqual(res, { answer: "hi", sources: [] });
  } finally {
    h.restore();
  }
});

test("a response with no usage block emits nothing rather than an empty event", async () => {
  var h = withNetworkAttrs(
    okJsonCapturing({ answer: "hi", sources: [] }),
    { "data-api-url": "https://api.test/query", "data-usage-events": "true" }
  );
  try {
    await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.strictEqual(h.events.length, 0, "no usage in the payload means no event");
  } finally {
    h.restore();
  }
});

test("data-usage-events=\"false\" reads as off", async () => {
  var bodies = [];
  var h = withNetworkAttrs(
    okJsonCapturing({ answer: "hi", sources: [] }, bodies),
    { "data-api-url": "https://api.test/query", "data-usage-events": "false" }
  );
  try {
    await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.ok(!("include_usage" in bodies[0]), "explicit false must not opt in");
  } finally {
    h.restore();
  }
});

test("source builds the messages array from the in-memory transcript (role + content)", () => {
  // The transcript is mapped to { role, content } turns and the whole thing is sent on each
  // send - that IS the single-session memory (no storage).
  assert.ok(/conversationForRequest\s*\(/.test(SOURCE), "maps the transcript to a messages array");
  assert.ok(
    /role:\s*m\.role\s*===\s*"user"\s*\?\s*"user"\s*:\s*"assistant"/.test(SOURCE),
    "maps the internal 'bot' role to 'assistant'"
  );
  assert.ok(
    /sendQuery\(\s*conversationForRequest\(\)\s*\)/.test(SOURCE),
    "sends the full conversation, not just the latest text"
  );
});

test("widget.js reads its endpoint from the data-api-url attribute (the swap point)", () => {
  assert.ok(/data-api-url/.test(SOURCE), "must read the endpoint from data-api-url");
  assert.ok(/SWAP POINT/.test(SOURCE), "swap point is clearly marked");
});

test("widget.js is production-clean: no mock code, canned answers, or backdoors", () => {
  assert.ok(!/\bmock\b/i.test(SOURCE), "must not reference a mock");
  assert.ok(!/trigger error/i.test(SOURCE), "must not contain the trigger-error backdoor");
  assert.ok(!/\.php\b/.test(SOURCE), "must not embed canned answer source URLs");
  assert.ok(!/Simulated backend failure/i.test(SOURCE), "must not contain the mock failure text");
});

test("external source links open safely (noopener noreferrer)", () => {
  assert.ok(/noopener noreferrer/.test(SOURCE), "target=_blank links must be rel=noopener noreferrer");
});

// --- warm path: fire-and-forget pre-warm --------------------------------
test("warmUrl derives the sibling /warm route from the /query endpoint", () => {
  assert.strictEqual(
    widget.warmUrl("https://abc123.execute-api.us-west-2.amazonaws.com/query"),
    "https://abc123.execute-api.us-west-2.amazonaws.com/warm"
  );
  // A trailing slash on /query is tolerated.
  assert.strictEqual(
    widget.warmUrl("https://abc123.execute-api.us-west-2.amazonaws.com/query/"),
    "https://abc123.execute-api.us-west-2.amazonaws.com/warm"
  );
});

test("warmUrl falls back to appending /warm for a non-/query base", () => {
  assert.strictEqual(widget.warmUrl("https://x.test/api"), "https://x.test/api/warm");
  assert.strictEqual(widget.warmUrl("https://x.test/api/"), "https://x.test/api/warm");
});

test("warmUrl returns null for unusable input", () => {
  assert.strictEqual(widget.warmUrl(""), null);
  assert.strictEqual(widget.warmUrl(null), null);
  assert.strictEqual(widget.warmUrl(undefined), null);
});

test("widget fires a fire-and-forget GET /warm on load, derived from data-api-url", () => {
  assert.ok(/warmBackend\(\)/.test(SOURCE), "warmBackend must be invoked on load");
  assert.ok(
    /fetch\(\s*url\s*,\s*\{\s*method:\s*"GET"\s*\}\s*\)/.test(SOURCE),
    "the warm ping must be a GET"
  );
  assert.ok(
    /\.then\(\s*noop\s*,\s*noop\s*\)/.test(SOURCE),
    "warm result AND errors must be ignored (fire-and-forget)"
  );
  assert.ok(
    /warmUrl\(\s*apiUrl\(\)\s*\)/.test(SOURCE),
    "the /warm URL must derive from the same data-api-url base"
  );
});

// --- honest loading state -----------------------------------------------
test("request timeout is raised to the 30s API Gateway ceiling", () => {
  assert.strictEqual(widget.CONFIG.requestTimeoutMs, 30000);
});

test("a delayed 'still working' hint backs a slow response (no fake progress)", () => {
  assert.ok(typeof widget.CONFIG.wakingHintDelayMs === "number", "hint delay is configurable");
  assert.ok(
    widget.CONFIG.wakingHintDelayMs > 0 &&
      widget.CONFIG.wakingHintDelayMs < widget.CONFIG.requestTimeoutMs,
    "the hint must appear before the request would time out"
  );
  // A real element revealed on a timer, not a fake progress bar.
  assert.ok(/typing__hint/.test(SOURCE), "the typing hint element exists");
  // Short, neutral 'Working…' copy, not startup wording that reads like a cold boot each message.
  assert.ok(/Working…/i.test(SOURCE), "neutral 'Working…' copy is present");
  assert.ok(!/Waking up/i.test(SOURCE), "no startup-implying 'waking up' copy remains");
  assert.ok(
    /CONFIG\.wakingHintDelayMs/.test(SOURCE),
    "the hint reveal is driven by the configured delay"
  );
});

// --- advisory input length cap ------------------------------------------
test("input has an advisory maxlength cap", () => {
  assert.ok(
    /setAttribute\(\s*"maxlength"\s*,\s*"1000"\s*\)/.test(SOURCE),
    "the textarea sets an advisory maxlength (server-side is the real limit)"
  );
});

// --- scoped fallback selector -------------------------------------------
test("apiUrl fallback selector is scoped to the widget's own script tag", () => {
  // The currentScript fallback must require src*="widget.js" so it can never bind to a
  // foreign data-api-url tag on the host page.
  assert.ok(
    /querySelector\(\s*'script\[data-api-url\]\[src\*="widget\.js"\]'\s*\)/.test(SOURCE),
    "fallback selector is scoped to the widget's own tag"
  );
  // The unscoped selector must be gone.
  assert.ok(
    !/querySelector\(\s*"script\[data-api-url\]"\s*\)/.test(SOURCE),
    "no unscoped script[data-api-url] selector remains"
  );
});

// =========================================================================
// ===  tiny dependency-free DOM fake (jsdom is not available offline)  ====
// =========================================================================
// Enough of the DOM for mount() and the markdown renderer: elements with
// children, className/classList, attributes, textContent, and event
// listeners you can fire(). Text nodes are {nodeType:3}. This lets the
// contract test exercise real widget behavior without any dependency.

function makeEl(tagName) {
  var el = {
    tagName: tagName,
    nodeType: 1,
    children: [],
    _text: "",
    attrs: {},
    listeners: {},
    style: {},
    hidden: false,
    disabled: false,
    className: "",
    parentNode: null
  };
  el.classList = {
    _s: {},
    add: function (c) { this._s[c] = true; },
    remove: function (c) { delete this._s[c]; },
    contains: function (c) { return !!this._s[c]; }
  };
  el.appendChild = function (c) { c.parentNode = el; el.children.push(c); return c; };
  el.removeChild = function (c) {
    var i = el.children.indexOf(c);
    if (i >= 0) el.children.splice(i, 1);
    c.parentNode = null;
    return c;
  };
  el.setAttribute = function (k, v) { el.attrs[k] = String(v); };
  el.removeAttribute = function (k) { delete el.attrs[k]; };
  el.getAttribute = function (k) {
    return Object.prototype.hasOwnProperty.call(el.attrs, k) ? el.attrs[k] : null;
  };
  el.addEventListener = function (t, fn) { (el.listeners[t] = el.listeners[t] || []).push(fn); };
  el.attachShadow = function () { el.shadow = makeEl("#shadow"); return el.shadow; };
  el.focus = function () {};
  el.querySelector = function () { return null; };
  el.fire = function (t, ev) {
    ev = ev || {};
    ev.preventDefault = ev.preventDefault || function () {};
    ev.stopPropagation = ev.stopPropagation || function () {};
    (el.listeners[t] || []).slice().forEach(function (fn) { fn(ev); });
  };
  Object.defineProperty(el, "textContent", {
    get: function () {
      if (el.children.length === 0) return el._text;
      return el.children.map(function (c) { return c.textContent; }).join("");
    },
    set: function (v) { el._text = String(v); el.children = []; }
  });
  Object.defineProperty(el, "firstChild", { get: function () { return el.children[0] || null; } });
  Object.defineProperty(el, "lastChild", {
    get: function () { return el.children[el.children.length - 1] || null; }
  });
  Object.defineProperty(el, "scrollHeight", { get: function () { return 40; } });
  el.scrollTop = 0;
  return el;
}

function makeDoc() {
  var doc = {
    createElement: function (tag) { return makeEl(tag); },
    // SVG namespace element (icons); the fake ignores the namespace.
    createElementNS: function (ns, tag) { return makeEl(tag); },
    createTextNode: function (t) {
      var n = { nodeType: 3, tagName: "#text", children: [], _t: String(t) };
      Object.defineProperty(n, "textContent", { get: function () { return n._t; } });
      return n;
    },
    getElementById: function () { return null; }
  };
  doc.body = makeEl("body");
  return doc;
}

function collectByTag(node, tag) {
  var out = [];
  (node.children || []).forEach(function (c) {
    if (c.tagName === tag) out.push(c);
    out = out.concat(collectByTag(c, tag));
  });
  return out;
}

function allText(node) {
  if (node.nodeType === 3) return node.textContent;
  var s = node._text || "";
  (node.children || []).forEach(function (c) { s += allText(c); });
  return s;
}

function findByClass(node, cls) {
  if (!node) return null;
  var cn = node.className || "";
  if ((" " + cn + " ").indexOf(" " + cls + " ") >= 0) return node;
  if (node.classList && node.classList.contains(cls)) return node;
  var kids = node.children || [];
  for (var i = 0; i < kids.length; i++) {
    var f = findByClass(kids[i], cls);
    if (f) return f;
  }
  return node.shadow ? findByClass(node.shadow, cls) : null;
}

function findAll(node, cls) {
  var out = [];
  (function walk(n) {
    if (!n) return;
    var cn = n.className || "";
    if ((" " + cn + " ").indexOf(" " + cls + " ") >= 0) out.push(n);
    (n.children || []).forEach(walk);
    if (n.shadow) walk(n.shadow);
  })(node);
  return out;
}

// A fetch stub that resolves one canned {answer, sources} payload as an ok JSON response.
function okJson(payload) {
  return function () {
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(payload); } });
  };
}

function countUserTurns(state) {
  return state.messages.filter(function (m) { return m.role === "user"; }).length;
}

// Let one microtask/timer cycle drain so settled fetch promises run their handlers.
function flush() {
  return new Promise(function (r) { setTimeout(r, 0); });
}

// Install browser-ish globals (document for apiUrl, a fetch stub) for the duration of a
// test; returns a restore fn. `fetchImpl` decides success vs failure.
function withNetwork(fetchImpl) {
  var priorDoc = global.document;
  var priorFetch = global.fetch;
  var priorErr = console.error;
  global.document = {
    querySelector: function () {
      return { getAttribute: function () { return "https://api.test/query"; } };
    }
  };
  global.fetch = fetchImpl;
  console.error = function () {}; // silence the widget's expected failure log; not a test failure
  return function restore() {
    global.document = priorDoc;
    global.fetch = priorFetch;
    console.error = priorErr;
  };
}

// --- markdown rendering: safe DOM, correct structure --------------------
test("renderMarkdown renders bold, links, and lists as real DOM nodes", () => {
  var doc = makeDoc();
  var root = doc.createElement("div");
  widget.renderMarkdown(
    root,
    "See **hours** and [the page](https://gav.edu/x).\n\n- one\n- two",
    doc
  );
  var strong = collectByTag(root, "strong");
  assert.strictEqual(strong.length, 1);
  assert.strictEqual(strong[0].textContent, "hours");

  var links = collectByTag(root, "a");
  assert.strictEqual(links.length, 1);
  assert.strictEqual(links[0].href, "https://gav.edu/x");
  assert.strictEqual(links[0].target, "_blank");
  assert.match(links[0].rel, /noopener noreferrer/);
  assert.strictEqual(links[0].textContent, "the page");

  var lists = collectByTag(root, "ul");
  assert.strictEqual(lists.length, 1);
  var items = collectByTag(lists[0], "li");
  assert.deepStrictEqual(items.map(function (li) { return li.textContent; }), ["one", "two"]);
});

test("renderMarkdown renders numbered lists as <ol>", () => {
  var doc = makeDoc();
  var root = doc.createElement("div");
  widget.renderMarkdown(root, "1. first\n2. second", doc);
  var ol = collectByTag(root, "ol");
  assert.strictEqual(ol.length, 1);
  assert.strictEqual(collectByTag(ol[0], "li").length, 2);
});

test("renderMarkdown is injection-safe: no markup or scripts from message content", () => {
  var doc = makeDoc();
  var root = doc.createElement("div");
  widget.renderMarkdown(
    root,
    "<script>alert(1)</script> <b>x</b> [evil](javascript:alert(1)) [data](data:text/html,x)",
    doc
  );
  // Nothing from the content becomes an element: no <script>, no <b>, and dangerous
  // link schemes are NOT linkified (label kept as text).
  assert.strictEqual(collectByTag(root, "script").length, 0);
  assert.strictEqual(collectByTag(root, "b").length, 0);
  assert.strictEqual(collectByTag(root, "a").length, 0, "javascript:/data: URLs are never linkified");
  var text = allText(root);
  assert.ok(/<script>alert\(1\)<\/script>/.test(text), "angle brackets survive as literal text");
  assert.ok(/evil/.test(text) && /data/.test(text), "unsafe link labels kept as text");
});

// --- bare-URL autolinking -----------------------------------------------
//
// The model writes URLs as prose ("visit: https://...") as often as it writes
// [label](url); unlinked, they read as broken. These cases pin down where a
// URL ENDS, which is the part naive detection gets wrong.

// Render `md` and return { links: [{text, href, nested}], text }.
function renderInfo(md) {
  var doc = makeDoc();
  var root = doc.createElement("div");
  widget.renderMarkdown(root, md, doc);
  var links = [];
  (function walk(n) {
    if (n.tagName === "a") {
      links.push({
        text: n.textContent,
        href: n.href,
        nested: collectByTag(n, "a").length
      });
    }
    (n.children || []).forEach(walk);
  })(root);
  return { links: links, text: allText(root), root: root, doc: doc };
}

test("bare URLs become links with the same treatment as written links", () => {
  var r = renderInfo("For more info visit: https://www.gavilan.edu/library/index.php");
  assert.strictEqual(r.links.length, 1);
  assert.strictEqual(r.links[0].href, "https://www.gavilan.edu/library/index.php");
  assert.strictEqual(r.links[0].text, "https://www.gavilan.edu/library/index.php");
  // Identical affordances to a [label](url) link: same class, new tab, same rel.
  var a = collectByTag(r.root, "a")[0];
  assert.strictEqual(a.className, "md-link");
  assert.strictEqual(a.target, "_blank");
  assert.strictEqual(a.rel, "noopener noreferrer nofollow");
});

test("bare URLs are autolinked inside list items (the shape the model actually emits)", () => {
  var r = renderInfo(
    "- Static map: https://www.gavilan.edu/about/maps/main_map.php\n" +
    "- Interactive: https://www.gavilan.edu/about/maps/gilroy_interactive_map.php"
  );
  assert.strictEqual(r.links.length, 2);
  // Underscores in a URL must not be eaten as italics - the URL wins the scan.
  assert.strictEqual(
    r.links[1].href,
    "https://www.gavilan.edu/about/maps/gilroy_interactive_map.php"
  );
  assert.strictEqual(collectByTag(r.root, "em").length, 0, "no italics from URL underscores");
});

test("trailing sentence punctuation is not swallowed into the link", () => {
  [
    ["Visit https://www.gavilan.edu/library/.", "https://www.gavilan.edu/library/", "."],
    ["Books: https://x.test/a, and more.", "https://x.test/a", ","],
    ["Open now! https://x.test/faq!", "https://x.test/faq", "!"],
    ["Which one? https://x.test/b?", "https://x.test/b", "?"],
    ["Note: https://x.test/c; next", "https://x.test/c", ";"],
    ["Quote \"https://x.test/q\"", "https://x.test/q", "\""]
  ].forEach(function (row) {
    var r = renderInfo(row[0]);
    assert.strictEqual(r.links.length, 1, "one link for: " + row[0]);
    assert.strictEqual(r.links[0].href, row[1], "href for: " + row[0]);
    assert.strictEqual(r.links[0].text, row[1], "link text for: " + row[0]);
    // The punctuation survives as visible text - it is moved out of the link, not dropped.
    assert.ok(
      r.text.indexOf(row[1] + row[2]) >= 0,
      "punctuation kept as text for: " + row[0]
    );
  });
});

test("closing brackets go to the link only when the URL opened them", () => {
  // Sentence-owned paren: dropped from the href, kept as text.
  var wrapped = renderInfo("(see https://www.gavilan.edu/library/)");
  assert.strictEqual(wrapped.links[0].href, "https://www.gavilan.edu/library/");
  assert.strictEqual(wrapped.text, "(see https://www.gavilan.edu/library/)");

  // URL-owned paren: kept, because the URL opened it.
  var balanced = renderInfo("https://en.wikipedia.org/wiki/Library_(disambiguation)");
  assert.strictEqual(
    balanced.links[0].href,
    "https://en.wikipedia.org/wiki/Library_(disambiguation)"
  );

  // Both at once: keep the balanced paren, drop the sentence's period.
  var both = renderInfo("See https://en.wikipedia.org/wiki/Library_(disambiguation).");
  assert.strictEqual(
    both.links[0].href,
    "https://en.wikipedia.org/wiki/Library_(disambiguation)"
  );
  assert.ok(/\(disambiguation\)\.$/.test(both.text), "period stays as text");

  // Brackets and braces follow the same rule.
  assert.strictEqual(renderInfo("[x] {https://x.test/z}").links[0].href, "https://x.test/z");
});

test("trimUrlEnd unwinds a mixed punctuation tail", () => {
  assert.strictEqual(widget.trimUrlEnd("https://x.test/a"), "https://x.test/a");
  assert.strictEqual(widget.trimUrlEnd("https://x.test/a)."), "https://x.test/a");
  assert.strictEqual(widget.trimUrlEnd("https://x.test/a_(b)."), "https://x.test/a_(b)");
  assert.strictEqual(widget.trimUrlEnd("https://x.test/a!!!"), "https://x.test/a");
});

test("nothing is double-linked: an existing link is never re-linked", () => {
  // A URL inside [label](url) is consumed by the link rule, not autolinked.
  var written = renderInfo("Ask [the library](https://www.gavilan.edu/library/) today.");
  assert.strictEqual(written.links.length, 1);
  assert.strictEqual(written.links[0].text, "the library");
  assert.strictEqual(written.links[0].href, "https://www.gavilan.edu/library/");

  // A URL used as its OWN label must produce exactly one <a>, never a nested one.
  var selfLabelled = renderInfo("[https://a.test/x](https://b.test/y)");
  assert.strictEqual(selfLabelled.links.length, 1, "exactly one anchor");
  assert.strictEqual(selfLabelled.links[0].nested, 0, "no nested <a>");
  assert.strictEqual(selfLabelled.links[0].href, "https://b.test/y");
  assert.strictEqual(selfLabelled.links[0].text, "https://a.test/x");

  // Bold inside a link label keeps the no-autolink rule as it recurses.
  var bolded = renderInfo("[see **https://a.test**](https://b.test)");
  assert.strictEqual(bolded.links.length, 1);
  assert.strictEqual(bolded.links[0].nested, 0, "no nested <a> under <strong>");
});

test("autolinking respects the URL sanitizer and code spans", () => {
  // Dangerous / non-http schemes are never autolinked.
  var unsafe = renderInfo("Bad: javascript:alert(1) ftp://x.test/f mailto:a@b.test");
  assert.strictEqual(unsafe.links.length, 0);

  // A URL inside inline code stays literal - code is matched first and never re-parsed.
  var code = renderInfo("Run `https://x.test/incode` now.");
  assert.strictEqual(code.links.length, 0, "no link inside a code span");
  assert.strictEqual(collectByTag(code.root, "code")[0].textContent, "https://x.test/incode");

  // A schemeless www. host is linked over https, displayed as written.
  var www = renderInfo("Go to www.gavilan.edu/library now.");
  assert.strictEqual(www.links[0].href, "https://www.gavilan.edu/library");
  assert.strictEqual(www.links[0].text, "www.gavilan.edu/library");
});

// --- pipe tables ---------------------------------------------------------
//
// The model answers the hours question - the most-asked one - with a table,
// so this is the most-read answer in the product.

test("a markdown table renders as a real table with header and body rows", () => {
  var doc = makeDoc();
  var root = doc.createElement("div");
  // Verbatim from a live /query answer to "Show me the library hours for the whole week".
  widget.renderMarkdown(
    root,
    "**Gilroy Library - Summer 2026**\n\n" +
    "| Day | Hours |\n" +
    "|---|---|\n" +
    "| Monday - Thursday | 9:00 AM - 3:00 PM |\n" +
    "| Friday | Closed |\n",
    doc
  );
  var tables = collectByTag(root, "table");
  assert.strictEqual(tables.length, 1);
  assert.strictEqual(tables[0].className, "md-table");

  var th = collectByTag(tables[0], "th");
  assert.deepStrictEqual(th.map(function (c) { return c.textContent; }), ["Day", "Hours"]);

  var rows = collectByTag(collectByTag(tables[0], "tbody")[0], "tr");
  assert.strictEqual(rows.length, 2);
  assert.deepStrictEqual(
    collectByTag(rows[0], "td").map(function (c) { return c.textContent; }),
    ["Monday - Thursday", "9:00 AM - 3:00 PM"]
  );
  assert.deepStrictEqual(
    collectByTag(rows[1], "td").map(function (c) { return c.textContent; }),
    ["Friday", "Closed"]
  );
});

test("a table is wrapped in a scroll container so a wide one cannot burst the bubble", () => {
  var r = renderInfo("| A | B |\n|---|---|\n| 1 | 2 |");
  var wrap = findByClass(r.root, "md-table-wrap");
  assert.ok(wrap, "table has a .md-table-wrap parent");
  assert.strictEqual(wrap.children[0].tagName, "table");
  // The wrapper is what scrolls; the CSS must actually say so.
  assert.ok(
    /\.md-table-wrap \{[^}]*overflow-x: auto/.test(SOURCE),
    ".md-table-wrap scrolls horizontally"
  );
  // Cells need a width floor, and must NOT inherit the bubble's `anywhere`
  // breaking, which would let the browser squash columns to one character.
  assert.ok(/\.md-table th, \.md-table td \{[^}]*min-width:/.test(SOURCE), "cells have a min-width floor");
  assert.ok(
    /\.md-table th, \.md-table td \{[^}]*overflow-wrap: break-word/.test(SOURCE),
    "cells override the bubble's overflow-wrap: anywhere"
  );
});

test("table cells render inline markdown, including bare URLs", () => {
  var r = renderInfo(
    "| Day | Where |\n|---|---|\n| **Mon** | https://x.test/a |\n| Tue | [site](https://y.test/b) |"
  );
  assert.strictEqual(collectByTag(r.root, "strong")[0].textContent, "Mon");
  assert.deepStrictEqual(
    r.links.map(function (l) { return l.href; }),
    ["https://x.test/a", "https://y.test/b"]
  );
});

test("table parsing handles optional outer pipes, alignment, ragged rows, and escaped pipes", () => {
  // No outer pipes.
  var bare = renderInfo("Day | Hours\n--- | ---\nMon | 9-3");
  assert.strictEqual(collectByTag(bare.root, "table").length, 1);
  assert.deepStrictEqual(
    collectByTag(bare.root, "th").map(function (c) { return c.textContent; }),
    ["Day", "Hours"]
  );

  // Alignment markers become classes (never inline style).
  var aligned = renderInfo("| A | B | C |\n|:--|:-:|--:|\n| 1 | 2 | 3 |");
  assert.deepStrictEqual(
    collectByTag(aligned.root, "th").map(function (c) { return c.className; }),
    ["md-cell--left", "md-cell--center", "md-cell--right"]
  );

  // Ragged rows pad and truncate to the header width, so the grid stays square.
  var ragged = renderInfo("| A | B | C |\n|---|---|---|\n| 1 |\n| 1 | 2 | 3 | 4 |");
  collectByTag(collectByTag(ragged.root, "tbody")[0], "tr").forEach(function (tr) {
    assert.strictEqual(collectByTag(tr, "td").length, 3);
  });

  // An escaped pipe is cell content, not a boundary.
  assert.deepStrictEqual(widget.splitTableRow("| x \\| y | z |"), ["x | y", "z"]);
});

test("prose containing a pipe is NOT turned into a table", () => {
  // The delimiter row - not the pipe - is what makes a table.
  var r = renderInfo("Use the pipe | character here.\nIt is just prose.");
  assert.strictEqual(collectByTag(r.root, "table").length, 0);
  assert.strictEqual(collectByTag(r.root, "p").length, 1);

  // A table immediately after prose (no blank line) still starts a table.
  var tight = renderInfo("Here are hours:\n| Day | Hours |\n|---|---|\n| Mon | 9-3 |");
  assert.strictEqual(collectByTag(tight.root, "table").length, 1);
  assert.strictEqual(collectByTag(tight.root, "p")[0].textContent, "Here are hours:");
});

test("table content is injection-safe: cells are text nodes, never markup", () => {
  var r = renderInfo(
    "| A | B |\n|---|---|\n| <script>alert(1)</script> | [x](javascript:alert(1)) |"
  );
  assert.strictEqual(collectByTag(r.root, "script").length, 0);
  assert.strictEqual(collectByTag(r.root, "a").length, 0, "javascript: never linkified in a cell");
  assert.ok(/<script>alert\(1\)<\/script>/.test(r.text), "angle brackets stay literal text");
});

// --- double-append fix: a retry must not duplicate the user turn --------
test("a failed send followed by retry does NOT duplicate the user turn in the transcript", async () => {
  var restore = withNetwork(function () {
    return Promise.reject(new Error("network down"));
  });
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    assert.ok(handle, "widget mounted");

    handle.submit("what are the hours?");
    await flush();

    var state = handle.getState();
    // greeting(bot) + one user turn; the failed send appends no bot turn.
    assert.strictEqual(countUserTurns(state), 1, "one user turn after the first failed send");

    // Click the retry button in the rendered error bubble.
    var retryBtn = findByClass(handle.shadow, "retry");
    assert.ok(retryBtn, "an error/retry button is shown after a failed send");
    retryBtn.fire("click");
    await flush();

    // THE regression: retry resends the existing transcript; it must not append a 2nd user turn.
    assert.strictEqual(
      countUserTurns(handle.getState()),
      1,
      "retry must not add a phantom duplicate user turn"
    );
  } finally {
    restore();
  }
});

test("a successful send records exactly one user turn and one new bot turn", async () => {
  var restore = withNetwork(function () {
    return Promise.resolve({
      ok: true,
      json: function () {
        return Promise.resolve({ answer: "Open **9-5**.", sources: [] });
      }
    });
  });
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    var before = handle.getState().messages.length; // greeting only
    handle.submit("hours?");
    await flush();
    var msgs = handle.getState().messages;
    assert.strictEqual(countUserTurns(handle.getState()), 1);
    // greeting + user + bot answer
    assert.strictEqual(msgs.length, before + 2);
    assert.strictEqual(msgs[msgs.length - 1].role, "bot");
    // The bot turn stores the RAW markdown (what we echo back to the server), not rendered HTML.
    assert.strictEqual(msgs[msgs.length - 1].text, "Open **9-5**.");
  } finally {
    restore();
  }
});

// --- expandable window --------------------------------------------------
test("the header expand control toggles the expanded size class and aria-pressed", () => {
  var doc = makeDoc();
  var handle = widget.mount(doc);
  var panel = findByClass(handle.shadow, "panel");
  var expandBtn = findByClass(handle.shadow, "header__expand");
  assert.ok(panel && expandBtn, "panel and expand control exist");

  assert.ok(!panel.classList.contains("panel--expanded"), "starts at default size");
  assert.strictEqual(expandBtn.getAttribute("aria-pressed"), "false");

  expandBtn.fire("click");
  assert.ok(panel.classList.contains("panel--expanded"), "expands on click");
  assert.strictEqual(expandBtn.getAttribute("aria-pressed"), "true");

  expandBtn.fire("click");
  assert.ok(!panel.classList.contains("panel--expanded"), "collapses on second click");
  assert.strictEqual(expandBtn.getAttribute("aria-pressed"), "false");
});

test("expanded dimensions clamp to the viewport (usable on mobile)", () => {
  // The .panel--expanded rule sizes with min(...) against the viewport, so a phone can't
  // get an off-screen panel.
  assert.ok(/\.panel--expanded\s*\{[^}]*min\(/.test(SOURCE), "expanded width/height clamp with min()");
});

// --- mobile: focus-zoom floor, dynamic viewport, touch targets ----------
test("text inputs compute to 16px so iOS Safari does not zoom the page on focus", () => {
  var flat = SOURCE.replace(/\n/g, " ");
  ["\\.composer__input", "\\.signin__input"].forEach(function (sel) {
    var rule = new RegExp(sel + " \\{[^}]*\\}").exec(flat);
    assert.ok(rule, sel + " has a base rule");
    // AFTER `font: inherit`, which would otherwise reset the size back to the 15px root.
    assert.ok(/font: inherit; font-size: 16px/.test(rule[0]), sel + " sets 16px after the font shorthand");
  });
});

test("panel heights track the dynamic viewport, with plain vh kept first as the fallback", () => {
  // 100vh is the LARGE viewport in current browsers: while mobile browser chrome shows,
  // a vh-only panel is up to a toolbar's height too tall. Each height rule therefore
  // declares vh then dvh, in that order, so engines without dvh keep the vh value.
  var flat = SOURCE.replace(/\n/g, " ");
  // The two declarations are adjacent string literals in the STYLES array, so the
  // flattened source separates them with quotes and commas, not only whitespace.
  [
    /height: min\(560px, calc\(100vh - 64px\)\);[\s",]*height: min\(560px, calc\(100dvh - 64px\)\);/,
    /height: min\(860px, calc\(100vh - 32px\)\);[\s",]*height: min\(860px, calc\(100dvh - 32px\)\);/
  ].forEach(function (pair) {
    assert.ok(pair.test(flat), "vh-then-dvh pair present: " + pair);
  });
  // Count declarations, not the bare token (comments may say "100vh" in prose).
  assert.strictEqual((flat.match(/calc\(100vh/g) || []).length, 2, "no height rule is vh-only");
  assert.strictEqual((flat.match(/calc\(100dvh/g) || []).length, 2, "each vh height has its dvh partner");
});

test("the small header and sources controls declare the 24px WCAG 2.2 target floor", () => {
  var flat = SOURCE.replace(/\n/g, " ");
  // The four controls the mobile audit measured under 24x24 (2.5.8): the two language
  // buttons (one rule), the header expand button, and the sources disclosure.
  ["\\.header__lang-btn", "\\.header__expand", "\\.sources__toggle"].forEach(function (sel) {
    var rule = new RegExp(sel + " \\{[^}]*\\}").exec(flat);
    assert.ok(rule, sel + " has a base rule");
    assert.ok(/min-height: 24px/.test(rule[0]), sel + " declares min-height: 24px");
  });
  assert.ok(
    /\.header__expand \{[^}]*min-width: 24px/.test(flat),
    ".header__expand declares min-width: 24px (the only one that was also narrow-capable)"
  );
});

// --- per-message, collapsible sources UI --------------------------------
test("sources are per-message: each answer shows only its own sources, not a union", async () => {
  var calls = 0;
  var restore = withNetwork(function () {
    calls++;
    var payload = calls === 1
      ? { answer: "A1", sources: [{ uri: "https://gav.edu/hours", excerpt: "h" }] }
      : { answer: "A2", sources: [{ uri: "https://gav.edu/borrow", excerpt: "b" }] };
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve(payload); } });
  });
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    handle.submit("q1"); await flush();
    handle.submit("q2"); await flush();

    var bots = findAll(handle.shadow, "msg--bot"); // greeting + 2 answers
    var linksOf = function (msg) {
      var list = findByClass(msg, "sources__list");
      return list ? collectByTag(list, "a").map(function (a) { return a.href; }) : [];
    };
    // Each answer shows ONLY its own source (no accumulation across the conversation).
    assert.deepStrictEqual(linksOf(bots[bots.length - 2]), ["https://gav.edu/hours"]);
    assert.deepStrictEqual(linksOf(bots[bots.length - 1]), ["https://gav.edu/borrow"]);
  } finally {
    restore();
  }
});

test("sources are collapsed by default and expand on click, linking the public URL in a new tab", async () => {
  var restore = withNetwork(
    okJson({ answer: "A", sources: [{ uri: "https://www.gavilan.edu/library/hours.php", excerpt: "e" }] })
  );
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    handle.submit("hours?"); await flush();

    var bot = findAll(handle.shadow, "msg--bot").pop();
    var toggle = findByClass(bot, "sources__toggle");
    var list = findByClass(bot, "sources__list");
    assert.ok(toggle && list, "a sources toggle and list are rendered");

    // Collapsed by default.
    assert.strictEqual(toggle.getAttribute("aria-expanded"), "false");
    assert.strictEqual(list.hidden, true, "list is hidden until the user expands it");

    // Expands on click.
    toggle.fire("click");
    assert.strictEqual(toggle.getAttribute("aria-expanded"), "true");
    assert.strictEqual(list.hidden, false, "list is revealed after clicking the toggle");

    // Clean public link, opens in a new tab, safe rel.
    var a = collectByTag(list, "a")[0];
    assert.strictEqual(a.href, "https://www.gavilan.edu/library/hours.php");
    assert.strictEqual(a.target, "_blank");
    assert.match(a.rel, /noopener noreferrer/);

    // Collapses again on a second click.
    toggle.fire("click");
    assert.strictEqual(toggle.getAttribute("aria-expanded"), "false");
    assert.strictEqual(list.hidden, true);
  } finally {
    restore();
  }
});

test("expanding a source dropdown does NOT force the thread to scroll to the bottom", async () => {
  // Regression: toggling sources on a message that isn't the most recent used to jerk the whole
  // thread down. Expanding/collapsing must leave the scroll position alone.
  var restore = withNetwork(
    okJson({ answer: "A", sources: [{ uri: "https://gav.edu/x", excerpt: "e" }] })
  );
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    handle.submit("q1"); await flush();
    handle.submit("q2"); await flush(); // a later message exists, so q1's answer isn't the most recent

    var thread = findByClass(handle.shadow, "thread");
    var firstAnswer = findAll(handle.shadow, "msg--bot")[1]; // greeting[0], q1 answer[1]
    var toggle = findByClass(firstAnswer, "sources__toggle");
    assert.ok(thread && toggle, "thread and an older message's sources toggle exist");

    // Simulate the user having scrolled up to that older message.
    thread.scrollTop = 5;
    toggle.fire("click"); // expand
    assert.strictEqual(thread.scrollTop, 5, "expanding must not move the scroll position");
    toggle.fire("click"); // collapse
    assert.strictEqual(thread.scrollTop, 5, "collapsing must not move the scroll position");
    // And it still actually toggled.
    assert.strictEqual(toggle.getAttribute("aria-expanded"), "false");
  } finally {
    restore();
  }
});

test("an answer with zero sources shows no sources affordance at all", async () => {
  var restore = withNetwork(okJson({ answer: "Just a greeting reply.", sources: [] }));
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    handle.submit("hello"); await flush();

    var bot = findAll(handle.shadow, "msg--bot").pop();
    assert.strictEqual(findByClass(bot, "sources"), null, "no .sources container");
    assert.strictEqual(findByClass(bot, "sources__toggle"), null, "no empty Sources label");
  } finally {
    restore();
  }
});

// --- SVG icons (no emoji glyphs) ----------------------------------------
test("launcher and resize controls use inline SVG icons, not emoji glyphs", () => {
  assert.ok(!/💬/.test(SOURCE), "no chat speech-balloon emoji");
  assert.ok(!/[⤢⤡⬌⬍]/.test(SOURCE), "no resize arrow glyphs");
  assert.ok(!/⤢|⤡/.test(SOURCE), "no diagonal resize glyphs");
  assert.ok(/createElementNS\(/.test(SOURCE), "icons are built as real SVG nodes");
});

test("launcher renders an SVG icon and the expand control swaps SVG icons on toggle", () => {
  var doc = makeDoc();
  var handle = widget.mount(doc);
  var launcherIcon = findByClass(handle.shadow, "launcher__icon");
  assert.ok(collectByTag(launcherIcon, "svg").length >= 1, "launcher shows an svg icon");

  var expandBtn = findByClass(handle.shadow, "header__expand");
  assert.strictEqual(collectByTag(expandBtn, "svg").length, 1, "expand shows one svg icon");
  expandBtn.fire("click");
  assert.strictEqual(collectByTag(expandBtn, "svg").length, 1, "still exactly one svg after toggling to collapse");
});

// --- brand color: one swappable token -----------------------------------
test("the primary color is a single --brand token, reused (not hardcoded per spot)", () => {
  assert.ok(/--brand:\s*#[0-9a-fA-F]{3,8}\s*;/.test(SOURCE), "defines a single --brand token");
  assert.ok(/--accent:\s*var\(--brand\)/.test(SOURCE), "accent derives from --brand");
  assert.ok(/--user-bg:\s*var\(--brand\)/.test(SOURCE), "user bubble derives from --brand");
  // The old hardcoded blue is fully gone.
  assert.ok(!/#1f4e79/i.test(SOURCE), "old blue accent value removed");
});

test("the brand red is the exact Claude Design value", () => {
  assert.ok(/--brand:\s*#8a1c30\s*;/i.test(SOURCE), "--brand is #8a1c30");
});

// --- title font ----------------------------------------------------------
test("the title uses the Bitter font, loaded from Google Fonts", () => {
  // Header title font-family is Bitter (body/UI is untouched).
  assert.ok(
    /\.header__title\s*\{[^}]*font-family:\s*'Bitter'/.test(SOURCE),
    "header title uses Bitter"
  );
  // The font is loaded via a Google Fonts link for Bitter.
  assert.ok(
    /fonts\.googleapis\.com\/css2\?family=Bitter/.test(SOURCE),
    "loads the Bitter font stylesheet"
  );
  assert.ok(/ensureTitleFont\(/.test(SOURCE), "font link is injected at mount");
});

// --- two-tone focus ring ------------------------------------------------
//
// REPLACES the test that pinned the composer's softened brand-tinted OUTLINE. That tint
// measured 2.09:1 against the page behind it and failed WCAG 1.4.11, which wants 3:1 -
// and no single flat colour clears 3:1 on every surface this ring lands on, because the
// widget cannot know its host page's background and the launcher's own fill is a dark
// maroon. So the outline is now a dark ink ring PLUS a light halo filling the
// outline-offset gap: whichever background it lands on, one of the two carries the
// contrast. The composer keeps its brand-tinted BORDER, which already measured 3.09:1
// and is what makes a focused field still look like this widget rather than the browser.
test("focus rings are two-tone (dark ring + light halo) so they hold 3:1 on any background", () => {
  var flat = SOURCE.replace(/\n/g, " ");
  assert.ok(
    /--focus-ring:\s*#[0-9a-fA-F]{6};\s*--focus-halo:\s*#[0-9a-fA-F]{6};/.test(SOURCE),
    "the ring's two colours are defined as tokens"
  );

  // Every control whose ring the audit measured below 3:1 now uses both halves.
  ["\\.launcher", "\\.composer__send", "\\.composer__input", "\\.suggestion", "\\.retry"].forEach(
    function (sel) {
      var rule = new RegExp(sel + ":focus-visible \\{[^}]*\\}").exec(flat);
      assert.ok(rule, sel + " has a :focus-visible rule");
      assert.ok(/outline: \dpx solid var\(--focus-ring\)/.test(rule[0]), sel + " outlines with --focus-ring");
      assert.ok(/box-shadow: 0 0 0 \dpx var\(--focus-halo\)/.test(rule[0]), sel + " draws the halo");
    }
  );

  // The pale blue that failed at 1.77:1 is gone from the file entirely.
  assert.ok(!/#9ec5ff/i.test(SOURCE), "the old pale-blue ring colour is removed");
  // The launcher's drop shadow has to be re-declared alongside its halo, or focusing it
  // would silently delete the shadow (one box-shadow declaration replaces the other).
  assert.ok(
    /\.launcher:focus-visible \{[^}]*box-shadow: 0 0 0 2px var\(--focus-halo\), 0 6px 20px/.test(flat),
    "the launcher keeps its drop shadow while focused"
  );
  // The composer's brand-tinted border survives the change.
  assert.ok(
    /\.composer__input:focus-visible \{[^}]*border-color: color-mix\(in srgb, var\(--brand\)/.test(flat),
    "the focused text box still tints its border with the brand"
  );
});

// --- first-launch example questions -------------------------------------
test("example questions appear on first launch and each is clickable", () => {
  var doc = makeDoc();
  var handle = widget.mount(doc);
  var suggestions = findByClass(handle.shadow, "suggestions");
  assert.ok(suggestions, "a suggestions block is shown on first launch");
  var btns = findAll(handle.shadow, "suggestion");
  assert.ok(btns.length >= 3, "several starter question buttons are shown");
  // Each button carries a non-empty question string.
  btns.forEach(function (b) {
    assert.ok(typeof b.textContent === "string" && b.textContent.length > 0);
  });
});

test("clicking an example question submits it and removes the suggestions", async () => {
  var asked = null;
  var restore = withNetwork(function (url, init) {
    asked = JSON.parse(init.body); // capture what got sent
    return okJson({ answer: "A", sources: [] })();
  });
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    var firstBtn = findAll(handle.shadow, "suggestion")[0];
    var questionText = firstBtn.textContent;
    firstBtn.fire("click");
    await flush();

    // The clicked question was submitted...
    var lastMsg = asked.messages[asked.messages.length - 1];
    assert.strictEqual(lastMsg.role, "user");
    assert.strictEqual(lastMsg.content, questionText);
    // ...and the suggestions are gone.
    assert.strictEqual(findByClass(handle.shadow, "suggestions"), null, "suggestions removed after use");
    assert.strictEqual(handle.getState().messages.filter(function (m) { return m.role === "user"; }).length, 1);
  } finally {
    restore();
  }
});

// =========================================================================
// ===================  bilingual UI (English / Español)  ==================
// =========================================================================
//
// The bot already answers Spanish questions correctly; what was missing was the shell
// around the conversation. So these tests are about the SHELL: that a Spanish speaker can
// see and reach their language before typing anything, that switching swaps every string
// and the `lang` attribute, that it does NOT rewrite what was already said, and that a
// visitor who never touches the control sends exactly the request they sent before.
//
// Deliberately invariant-based rather than copy-based: they assert that every English key
// has a Spanish counterpart and that no user-visible literal is left inline in render code,
// so the copy can be corrected by a native speaker without touching the tests.

// The render half of the file: everything after the localization table. User-visible
// literals are allowed above this point and nowhere below it.
const RENDER_SOURCE = SOURCE.slice(SOURCE.indexOf("END LOCALIZATION"));

// Switch language the way a person does - by clicking the control - and hand the module's
// default back afterwards (it is loaded once per process, so language leaks between tests).
function clickLanguage(handle, code) {
  const btn = findAll(handle.shadow, "header__lang-btn").filter(function (b) {
    return b.getAttribute("data-lang") === code;
  })[0];
  assert.ok(btn, "a control button exists for " + code);
  btn.fire("click");
  return btn;
}

function chromeText(handle) {
  return {
    title: findByClass(handle.shadow, "header__title").textContent,
    placeholder: findByClass(handle.shadow, "composer__input").getAttribute("placeholder"),
    send: findByClass(handle.shadow, "composer__send").textContent,
    launcher: findByClass(handle.shadow, "launcher").textContent,
    closeAria: findByClass(handle.shadow, "header__close").getAttribute("aria-label")
  };
}

// --- the one string table ------------------------------------------------
test("every language table carries the same keys, and none is empty", () => {
  const codes = widget.LANGUAGES;
  assert.ok(codes.length >= 2, "at least English and one other language are offered");
  assert.ok(codes.indexOf(widget.DEFAULT_LANG) >= 0, "the default language is one of them");

  const reference = Object.keys(widget.STRINGS[widget.DEFAULT_LANG]).sort();
  codes.forEach(function (code) {
    const table = widget.STRINGS[code];
    assert.ok(table, "a table exists for " + code);
    assert.deepStrictEqual(
      Object.keys(table).sort(),
      reference,
      "table for " + code + " has exactly the default language's keys"
    );
    reference.forEach(function (key) {
      const value = table[key];
      const ref = widget.STRINGS[widget.DEFAULT_LANG][key];
      assert.strictEqual(typeof value, typeof ref, code + "." + key + " has the same type");
      if (Array.isArray(ref)) {
        assert.strictEqual(value.length, ref.length, code + "." + key + " has the same length");
        value.forEach(function (v) {
          assert.ok(typeof v === "string" && v.trim(), code + "." + key + " entries are non-empty");
        });
      } else {
        assert.ok(typeof value === "string" && value.trim(), code + "." + key + " is non-empty");
      }
    });
  });
});

test("a language is offered under its own name, identically in both UIs", () => {
  // The whole point of a visible control is that someone looking for Spanish sees the word
  // "Español" - so the language NAMES are the one thing that must not be translated.
  assert.strictEqual(widget.STRINGS.en.languageName, "English");
  assert.strictEqual(widget.STRINGS.es.languageName, "Español");
});

test("CONFIG carries knobs, not copy", () => {
  // All user-visible strings moved to STRINGS; a copy change must never have to touch CONFIG.
  Object.keys(widget.CONFIG).forEach(function (key) {
    assert.strictEqual(typeof widget.CONFIG[key], "number", "CONFIG." + key + " is a number");
  });
});

test("no user-visible literal is left inline in the render code", () => {
  // Two sinks account for every visible string in this widget: a text node, and a labelling
  // attribute. Below the localization table, neither may take a word-bearing literal.
  const inlineText = /textContent\s*=\s*"[^"]*[A-Za-z]{3,}/.exec(RENDER_SOURCE);
  assert.strictEqual(inlineText, null, "inline textContent literal: " + (inlineText && inlineText[0]));
  const inlineAttr =
    /setAttribute\(\s*"(?:aria-label|placeholder|title)"\s*,\s*"[^"]*[A-Za-z]{3,}/.exec(RENDER_SOURCE);
  assert.strictEqual(inlineAttr, null, "inline label literal: " + (inlineAttr && inlineAttr[0]));
  // ...and it really does read from the table.
  assert.ok(/textContent\s*=\s*t\(/.test(RENDER_SOURCE), "render code reads copy from t()");
  assert.ok(/setAttribute\("aria-label",\s*t\(/.test(RENDER_SOURCE), "labels read from t()");
});

// --- the control ---------------------------------------------------------
test("the language control is a labelled group of real, named, keyboard-operable buttons", () => {
  const doc = makeDoc();
  const handle = widget.mount(doc);
  try {
    const group = findByClass(handle.shadow, "header__lang");
    assert.ok(group, "the control is present in the panel header");
    assert.strictEqual(group.getAttribute("role"), "group");
    // The group names itself: `aria-labelledby`/`for` cannot cross the shadow boundary.
    assert.ok(group.getAttribute("aria-label"), "the group has an accessible name");

    const btns = findAll(handle.shadow, "header__lang-btn");
    assert.strictEqual(btns.length, widget.LANGUAGES.length, "one button per offered language");
    btns.forEach(function (b) {
      assert.strictEqual(b.tagName, "button", "a real button, so it is focusable and keyboard-operable");
      assert.strictEqual(b.type, "button", "type=button: it never submits the composer form");
      // Accessible name from its own text content - no aria-label to drift from the label.
      assert.ok(b.textContent && b.textContent.trim(), "the button names itself");
      assert.ok(b.getAttribute("aria-pressed"), "state is exposed to assistive tech");
    });
    // English is the shipped default and reads as pressed.
    assert.strictEqual(btns[0].getAttribute("aria-pressed"), "true");
    assert.strictEqual(btns[1].getAttribute("aria-pressed"), "false");
  } finally {
    widget.resetLanguage();
  }
});

test("the control has a visible focus indicator and does not signal state by color alone", () => {
  assert.ok(
    /\.header__lang-btn:focus-visible \{[^}]*outline:/.test(SOURCE),
    "focus-visible draws an outline"
  );
  // The pressed state is a filled pill (background change), not just a different text color.
  assert.ok(
    /\.header__lang-btn\[aria-pressed=\\"true\\"\] \{[^}]*background:/.test(SOURCE),
    "the pressed state changes the background, driven by aria-pressed itself"
  );
});

test("choosing Español swaps every piece of chrome and the lang attribute", () => {
  const doc = makeDoc();
  const handle = widget.mount(doc);
  try {
    const before = chromeText(handle);
    assert.strictEqual(handle.host.getAttribute("lang"), "en");

    clickLanguage(handle, "es");

    const after = chromeText(handle);
    Object.keys(before).forEach(function (key) {
      assert.notStrictEqual(after[key], before[key], key + " switched language");
    });
    assert.strictEqual(after.title, widget.STRINGS.es.title);
    assert.strictEqual(after.placeholder, widget.STRINGS.es.inputPlaceholder);
    assert.strictEqual(after.send, widget.STRINGS.es.sendLabel);
    // Both the host element and the shadow root's container carry the language.
    assert.strictEqual(handle.host.getAttribute("lang"), "es");
    assert.strictEqual(findByClass(handle.shadow, "root").getAttribute("lang"), "es");
    // aria-pressed follows.
    const btns = findAll(handle.shadow, "header__lang-btn");
    assert.strictEqual(btns[0].getAttribute("aria-pressed"), "false");
    assert.strictEqual(btns[1].getAttribute("aria-pressed"), "true");
  } finally {
    widget.resetLanguage();
  }
});

test("switching before the first message re-renders the greeting and starter questions", () => {
  // The greeting is not a turn anyone took - it is what the panel says before anyone speaks -
  // and leaving it in the other language is what would make the switch look broken. It is
  // canned in both languages, so this costs no model call.
  const doc = makeDoc();
  const handle = widget.mount(doc);
  try {
    clickLanguage(handle, "es");
    const bots = findAll(handle.shadow, "msg--bot");
    assert.strictEqual(bots.length, 1, "still exactly one greeting, not two");
    assert.ok(
      allText(bots[0]).indexOf("Biblioteca de Gavilan College") >= 0,
      "the greeting is the Spanish one"
    );
    assert.strictEqual(handle.getState().messages.length, 1, "the transcript still holds one turn");
    assert.strictEqual(handle.getState().messages[0].text, widget.STRINGS.es.greeting);

    const suggestions = findAll(handle.shadow, "suggestion").map(function (b) { return b.textContent; });
    assert.deepStrictEqual(suggestions, widget.STRINGS.es.suggestedQuestions);
  } finally {
    widget.resetLanguage();
  }
});

// --- where the bilingual UI meets the accessibility remediation ----------
//
// The two features touch the same nodes from opposite directions: the audit added text
// that only a screen reader ever reads, and the toggle says every string a person can
// perceive comes out of the table. These pin the overlap, which neither feature's own
// tests cover.

test("the visually-hidden speaker labels are localized too", async () => {
  // A label nobody can see is still text that gets read aloud - so a Spanish UI reading
  // "Library assistant said:" over a Spanish answer is exactly the gap the toggle exists
  // to close, and it is invisible to every check that looks at the rendered page.
  const restore = withNetwork(okJson({ answer: "Abierto de 9 a 5.", sources: [] }));
  try {
    const doc = makeDoc();
    const handle = widget.mount(doc);
    clickLanguage(handle, "es");
    handle.submit("¿horario?");
    await flush();

    const labels = findAll(handle.shadow, "msg").map(function (m) {
      return findByClass(m, "sr-only").textContent;
    });
    assert.deepStrictEqual(labels, [
      widget.STRINGS.es.speakerBot,   // the re-seeded Spanish greeting
      widget.STRINGS.es.speakerYou,
      widget.STRINGS.es.speakerBot
    ]);
    // English is what dev's speaker-label test asserts by literal; keep the tables honest
    // about it so that test and this one cannot drift apart.
    assert.strictEqual(widget.STRINGS.en.speakerYou, "You said:");
    assert.strictEqual(widget.STRINGS.en.speakerBot, "Library assistant said:");
  } finally {
    widget.resetLanguage();
    restore();
  }
});

test("the pending status region announces in the chosen language", async () => {
  const restore = withNetwork(function () { return new Promise(function () {}); }); // never settles
  const priorDelay = widget.CONFIG.wakingHintDelayMs;
  widget.CONFIG.wakingHintDelayMs = 10;
  try {
    const doc = makeDoc();
    const handle = widget.mount(doc);
    clickLanguage(handle, "es");
    handle.submit("¿horario?");

    const bubble = findTypingBubble(handle.shadow);
    assert.ok(bubble, "a pending indicator is rendered");
    assert.strictEqual(bubble.getAttribute("role"), "status");
    // One status region, not two: the dots row must not carry a nested role/aria-label,
    // or the pending state is announced twice.
    const dots = findByClass(bubble, "typing");
    assert.strictEqual(dots.getAttribute("role"), null, "the dots row is not a second region");
    assert.strictEqual(dots.getAttribute("aria-label"), null, "and carries no label of its own");
    assert.ok(bubble.textContent.indexOf(widget.STRINGS.es.typingStatus) >= 0);

    await sleep(40);
    assert.ok(
      bubble.textContent.indexOf(widget.STRINGS.es.workingHint) >= 0,
      "the slow-response note is Spanish as well"
    );
  } finally {
    widget.CONFIG.wakingHintDelayMs = priorDelay;
    widget.resetLanguage();
    restore();
  }
});

test("switching language before the first message keeps the composer described", async () => {
  // The greeting describes the composer (F3), and a switch tears that greeting down and
  // builds a new one. Wired at mount only, the description would point at a removed node
  // and the Spanish opening state would arrive undescribed - a silent regression, since
  // both features' own tests still pass.
  const doc = makeFocusDoc();
  const handle = widget.mount(doc);
  try {
    const input = findByClass(handle.shadow, "composer__input");
    const before = input.getAttribute("aria-describedby");
    assert.ok(before, "described on first launch");

    clickLanguage(handle, "es");

    const id = input.getAttribute("aria-describedby");
    assert.ok(id, "still described after the switch");
    const described = findAll(handle.shadow, "bubble").filter(function (b) { return b.id === id; });
    assert.strictEqual(described.length, 1, "exactly one live node answers to the id");
    assert.ok(
      described[0].textContent.indexOf("Biblioteca de Gavilan College") >= 0,
      "and it is the SPANISH greeting, not the removed English one"
    );
  } finally {
    widget.resetLanguage();
  }
});

test("switching mid-conversation leaves every past turn exactly as it was said", async () => {
  const restore = withNetwork(okJson({ answer: "Open **9-5**.", sources: [] }));
  try {
    const doc = makeDoc();
    const handle = widget.mount(doc);
    handle.submit("what are the hours?");
    await flush();

    const msgsBefore = findAll(handle.shadow, "msg").map(function (m) { return allText(m); });
    const transcriptBefore = handle.getState().messages.map(function (m) { return m.text; });

    clickLanguage(handle, "es");

    assert.deepStrictEqual(
      findAll(handle.shadow, "msg").map(function (m) { return allText(m); }),
      msgsBefore,
      "no rendered message changed"
    );
    assert.deepStrictEqual(
      handle.getState().messages.map(function (m) { return m.text; }),
      transcriptBefore,
      "no transcript entry changed"
    );
    // The English greeting/answer keep their own lang, so a screen reader does not read
    // English text with a Spanish voice after the switch.
    findAll(handle.shadow, "msg").forEach(function (m) {
      assert.strictEqual(m.getAttribute("lang"), "en", "past turns keep the language they were said in");
    });
    // Chrome DID switch.
    assert.strictEqual(chromeText(handle).title, widget.STRINGS.es.title);
  } finally {
    restore();
    widget.resetLanguage();
  }
});

test("switching after the first message does not resurrect the starter questions", async () => {
  const restore = withNetwork(okJson({ answer: "A", sources: [] }));
  try {
    const doc = makeDoc();
    const handle = widget.mount(doc);
    handle.submit("hours?");
    await flush();
    clickLanguage(handle, "es");
    assert.strictEqual(findByClass(handle.shadow, "suggestions"), null, "suggestions stay gone");
  } finally {
    restore();
    widget.resetLanguage();
  }
});

// --- the preference on the wire -----------------------------------------
test("with no language chosen the request body is unchanged: exactly { messages }", async () => {
  const bodies = [];
  const h = withNetworkAttrs(
    okJsonCapturing({ answer: "hi", sources: [] }, bodies),
    { "data-api-url": "https://api.test/query" }
  );
  try {
    assert.deepStrictEqual(widget.getLanguage(), { code: "en", chosen: false });
    await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.deepStrictEqual(bodies, [{ messages: [{ role: "user", content: "hours?" }] }]);
    assert.ok(!("language" in bodies[0]), "no preference means no field, so the backend behaves as before");
  } finally {
    h.restore();
  }
});

test("an explicit choice rides along as one extra field", async () => {
  const bodies = [];
  const h = withNetworkAttrs(
    okJsonCapturing({ answer: "hola", sources: [] }, bodies),
    { "data-api-url": "https://api.test/query" }
  );
  try {
    const doc = makeDoc();
    const handle = widget.mount(doc);
    clickLanguage(handle, "es");
    await widget.sendQuery([{ role: "user", content: "what are the hours?" }]);
    assert.deepStrictEqual(bodies[0], {
      messages: [{ role: "user", content: "what are the hours?" }],
      language: "es"
    });
    // Explicitly choosing English is also a choice: it must pin the reply language too,
    // rather than falling back to auto-detection from a Spanish-looking question.
    clickLanguage(handle, "en");
    await widget.sendQuery([{ role: "user", content: "¿horario?" }]);
    assert.strictEqual(bodies[1].language, "en");
    assert.strictEqual(widget.getLanguage().chosen, true);
  } finally {
    h.restore();
    widget.resetLanguage();
  }
});

test("the demo page's metering and a language choice coexist without disturbing each other", async () => {
  const bodies = [];
  const h = withNetworkAttrs(
    okJsonCapturing({ answer: "hola", sources: [], usage: { model_calls: 1 } }, bodies),
    { "data-api-url": "https://api.test/query", "data-usage-events": "true" }
  );
  try {
    widget.setLanguage("es", true);
    await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.strictEqual(bodies[0].include_usage, true);
    assert.strictEqual(bodies[0].language, "es");
    assert.strictEqual(h.events.length, 1, "metering still fires exactly once");
  } finally {
    h.restore();
    widget.resetLanguage();
  }
});

// --- localized runtime states -------------------------------------------
test("the sources disclosure heading is localized, with the count filled in", async () => {
  const restore = withNetwork(
    okJson({
      answer: "A",
      sources: [
        { uri: "https://gav.edu/a", excerpt: "a" },
        { uri: "https://gav.edu/b", excerpt: "b" }
      ]
    })
  );
  try {
    const doc = makeDoc();
    const handle = widget.mount(doc);
    clickLanguage(handle, "es");
    handle.submit("¿horario?");
    await flush();

    const toggle = findByClass(findAll(handle.shadow, "msg--bot").pop(), "sources__toggle");
    assert.strictEqual(toggle.textContent, widget.STRINGS.es.sourcesMany.replace("{n}", "2"));
    assert.ok(toggle.textContent.indexOf("2") >= 0, "the count is substituted");
  } finally {
    restore();
    widget.resetLanguage();
  }
});

test("the network-failure bubble and its retry button are localized", async () => {
  const restore = withNetwork(function () {
    return Promise.reject(new Error("network down"));
  });
  try {
    const doc = makeDoc();
    const handle = widget.mount(doc);
    clickLanguage(handle, "es");
    handle.submit("¿horario?");
    await flush();

    const bubble = findByClass(handle.shadow, "bubble--error");
    assert.ok(bubble, "an error bubble is shown");
    assert.ok(allText(bubble).indexOf(widget.STRINGS.es.networkError) >= 0, "the error text is Spanish");
    assert.strictEqual(findByClass(handle.shadow, "retry").textContent, widget.STRINGS.es.retryLabel);
  } finally {
    restore();
    widget.resetLanguage();
  }
});

test("the not-connected fallback is localized (no data-api-url)", async () => {
  const priorDoc = global.document;
  global.document = { querySelector: function () { return null; } };
  try {
    widget.setLanguage("es", true);
    const res = await widget.sendQuery([{ role: "user", content: "hola" }]);
    assert.strictEqual(res.answer, widget.STRINGS.es.notConnected);
    assert.deepStrictEqual(res.sources, []);
  } finally {
    global.document = priorDoc;
    widget.resetLanguage();
  }
});

test("suggestions disappear after a typed message and do not come back", async () => {
  var restore = withNetwork(okJson({ answer: "A", sources: [] }));
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    assert.ok(findByClass(handle.shadow, "suggestions"), "present before any message");
    handle.submit("a typed question"); await flush();
    assert.strictEqual(findByClass(handle.shadow, "suggestions"), null, "gone after first message");
    handle.submit("a second question"); await flush();
    assert.strictEqual(findByClass(handle.shadow, "suggestions"), null, "still gone after later messages");
  } finally {
    restore();
  }
});

// =========================================================================
// ===  accessibility: WCAG 2.1 AA remediation (docs/accessibility-audit.md) ==
// =========================================================================
//
// One test per audit finding that changed behaviour, so a later edit that undoes one
// fails by name. Announcement itself is NOT asserted anywhere here - no screen reader
// runs in CI - so these pin the STRUCTURE the audit found missing: a speaker label in
// the tree, text inside the status region, focus that cannot leave an open panel.

// The shared DOM fake predates these behaviours: focus containment needs
// querySelectorAll + closest, and the document-level Escape needs addEventListener on the
// document. This augments a fake doc for the tests below rather than changing the fake the
// rest of the file uses.
function matchesFocusable(el, sel) {
  var tag = String(el.tagName || "").toLowerCase();
  var m = /^([a-z]+):not\(\[disabled\]\)$/.exec(sel);
  if (m) return tag === m[1] && !el.disabled && el.getAttribute("disabled") === null;
  if (sel === "a[href]") return tag === "a" && !!el.href;
  if (sel === '[tabindex]:not([tabindex="-1"])') {
    var t = el.getAttribute("tabindex");
    return t !== null && t !== "-1";
  }
  throw new Error("the fake selector matcher does not understand: " + sel);
}

function augmentEl(el, doc) {
  el.focus = function () { doc.focused = el; };
  el.querySelectorAll = function (selector) {
    var parts = selector.split(",").map(function (s) { return s.trim(); });
    var out = [];
    (function walk(n) {
      (n.children || []).forEach(function (c) {
        if (c.nodeType !== 1) return;
        var hit = parts.some(function (p) { return matchesFocusable(c, p); });
        if (hit) out.push(c);   // pre-order, so the result is in DOM order
        walk(c);
      });
    })(el);
    return out;
  };
  el.closest = function (selector) {
    if (selector !== "[hidden]") throw new Error("fake closest only handles [hidden]");
    var n = el;
    while (n && n.nodeType === 1) {
      if (n.hidden === true) return n;
      n = n.parentNode;
    }
    return null;
  };
  return el;
}

function makeFocusDoc() {
  var doc = makeDoc();
  var docListeners = {};
  doc.focused = null;
  doc.addEventListener = function (t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); };
  doc.fire = function (t, ev) {
    ev = ev || {};
    ev.preventDefault = ev.preventDefault || function () { ev.prevented = true; };
    ev.stopPropagation = ev.stopPropagation || function () { ev.propagationStopped = true; };
    (docListeners[t] || []).slice().forEach(function (fn) { fn(ev); });
    return ev;
  };
  var create = doc.createElement;
  doc.createElement = function (tag) { return augmentEl(create(tag), doc); };
  var createNS = doc.createElementNS;
  doc.createElementNS = function (ns, tag) { return augmentEl(createNS(ns, tag), doc); };
  return doc;
}

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

function findTypingBubble(shadow) {
  var msgs = findAll(shadow, "msg");
  for (var i = 0; i < msgs.length; i++) {
    if (msgs[i].getAttribute("data-typing") === "1") return findByClass(msgs[i], "bubble");
  }
  return null;
}

// --- F1: speaker attribution (1.3.1) ------------------------------------
test("every message turn carries a visually-hidden speaker label", async () => {
  var restore = withNetwork(okJson({ answer: "Open 9-5.", sources: [] }));
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    handle.submit("hours?");
    await flush();

    var msgs = findAll(handle.shadow, "msg");
    assert.strictEqual(msgs.length, 3, "greeting, the question, the answer");
    msgs.forEach(function (m) {
      var label = findByClass(m, "sr-only");
      assert.ok(label, "each turn has an .sr-only label: " + m.className);
      // It has to be FIRST, so it is read before the message it introduces.
      assert.strictEqual(m.children[0], label, "the label leads the turn");
      var expected = /msg--user/.test(m.className) ? "You said:" : "Library assistant said:";
      assert.strictEqual(label.textContent, expected);
    });

    // The bubble itself is untouched: the label is a sibling, not a prefix on the text.
    var userBubble = findByClass(findAll(handle.shadow, "msg--user")[0], "bubble");
    assert.strictEqual(userBubble.textContent, "hours?");

    // ...and .sr-only really is visually hidden, not just named that.
    assert.ok(/\.sr-only \{[^}]*position: absolute;[^}]*clip: rect\(0 0 0 0\)/.test(SOURCE.replace(/\n/g, " ")),
      ".sr-only is clipped out of the visual layout");
  } finally {
    restore();
  }
});

// --- F2: the pending state's live region (4.1.3) ------------------------
test("the pending state's status region carries text, and the slow note is INSERTED into it", async () => {
  var restore = withNetwork(function () { return new Promise(function () {}); }); // never settles
  var priorDelay = widget.CONFIG.wakingHintDelayMs;
  widget.CONFIG.wakingHintDelayMs = 10; // 6s is the shipped value; too slow for a test
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    handle.submit("hold this open");

    var bubble = findTypingBubble(handle.shadow);
    assert.ok(bubble, "a pending indicator is rendered");
    // The message is CONTENT, not an aria-label on an element whose only children are
    // aria-hidden dots - that was the failure: a live region with nothing to announce.
    assert.strictEqual(bubble.getAttribute("role"), "status");
    assert.strictEqual(bubble.getAttribute("aria-label"), null, "no aria-label to override the content");
    assert.ok(bubble.textContent.indexOf("Assistant is typing") >= 0, "the region has text");
    assert.ok(bubble.textContent.length > 0);
    findAll(bubble, "dot").forEach(function (d) {
      assert.strictEqual(d.getAttribute("aria-hidden"), "true", "the dots stay decorative");
    });
    assert.strictEqual(findByClass(bubble, "typing__hint"), null, "the slow note does not pre-exist");

    await sleep(40);
    var hint = findByClass(bubble, "typing__hint");
    assert.ok(hint, "the slow note is created and appended INSIDE the status region");
    assert.strictEqual(hint.hidden, false, "it is a fresh node, not an un-hidden one");
    assert.ok(bubble.textContent.indexOf("Working…") >= 0, "the region's text changed");
  } finally {
    widget.CONFIG.wakingHintDelayMs = priorDelay;
    restore();
  }
});

test("the thread's live region is untouched: polite log, additions only, aria-atomic unset", () => {
  var doc = makeDoc();
  var handle = widget.mount(doc);
  var thread = findByClass(handle.shadow, "thread");
  assert.strictEqual(thread.getAttribute("role"), "log");
  assert.strictEqual(thread.getAttribute("aria-live"), "polite");
  assert.strictEqual(thread.getAttribute("aria-relevant"), "additions");
  // If this is ever set, every update re-reads the whole conversation.
  assert.strictEqual(thread.getAttribute("aria-atomic"), null, "aria-atomic must stay unset");
});

// --- F4: focus containment and Escape (2.4.3) ---------------------------
test("Escape closes the panel from OUTSIDE it, and from inside still stops at the widget", () => {
  var doc = makeFocusDoc();
  var handle = widget.mount(doc);
  var panel = findByClass(handle.shadow, "panel");
  var launcher = findByClass(handle.shadow, "launcher");
  assert.strictEqual(panel.getAttribute("aria-modal"), "true", "the panel declares itself modal");

  // Closed: the document listener must not fire on a stray Escape.
  doc.focused = null;
  doc.fire("keydown", { key: "Escape" });
  assert.strictEqual(doc.focused, null, "Escape does nothing while the panel is closed");

  // Open, with focus on a host-page control: the panel's own handler never sees the key,
  // which is exactly the case that used to leave the panel stuck open.
  handle.open();
  assert.strictEqual(panel.hidden, false);
  var outside = doc.fire("keydown", { key: "Escape" });
  assert.strictEqual(panel.hidden, true, "Escape from outside the panel closes it");
  assert.strictEqual(doc.focused, launcher, "closing returns focus to the launcher");
  assert.ok(!outside.propagationStopped, "a host-page Escape is not swallowed by the widget");

  // From inside, the panel's own handler closes it AND keeps the key off the host page.
  handle.open();
  var stopped = 0;
  panel.fire("keydown", { key: "Escape", stopPropagation: function () { stopped++; } });
  assert.strictEqual(panel.hidden, true, "Escape from inside still closes");
  assert.strictEqual(stopped, 1, "Escape typed in the panel does not reach host-page handlers");
});

test("Tab and Shift+Tab wrap at the open panel's edges instead of walking into the host page", async () => {
  var restore = withNetwork(okJson({ answer: "A", sources: [{ uri: "https://gav.edu/x", excerpt: "e" }] }));
  try {
    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    var panel = findByClass(handle.shadow, "panel");
    var send = findByClass(handle.shadow, "composer__send");
    var input = findByClass(handle.shadow, "composer__input");
    // The panel's first control is the language toggle's leading button - it is the first
    // focusable thing in the header, ahead of the expand control. Named from the DOM rather
    // than hardcoded, so adding chrome moves the edge instead of breaking this test.
    var first = findAll(handle.shadow, "header__lang-btn")[0];
    assert.ok(first, "the language control leads the panel's tab order");
    handle.open();

    // The shared fake's default preventDefault is a no-op, so bring a spy.
    function tab(shift) {
      var ev = { key: "Tab", shiftKey: !!shift, preventDefault: function () { ev.prevented = true; } };
      panel.fire("keydown", ev);
      return ev;
    }

    // At the last control, Tab wraps to the first instead of leaving the panel.
    handle.shadow.activeElement = send;
    doc.focused = null;
    assert.ok(tab().prevented, "Tab at the last control is intercepted");
    assert.strictEqual(doc.focused, first, "...and wraps to the first control");

    // At the first control, Shift+Tab wraps to the last.
    handle.shadow.activeElement = first;
    doc.focused = null;
    assert.ok(tab(true).prevented, "Shift+Tab at the first control is intercepted");
    assert.strictEqual(doc.focused, send, "...and wraps to the last control");

    // Away from an edge, the browser's own focus order is left alone.
    handle.shadow.activeElement = input;
    doc.focused = null;
    assert.ok(!tab().prevented, "a mid-panel Tab is not intercepted");
    assert.strictEqual(doc.focused, null);

    // The edges are recomputed per keypress, not cached: an answer arrives with a
    // collapsed sources list (full of links that are NOT tabbable while hidden), and
    // Send disables itself while the next request is pending, which moves the last edge.
    handle.submit("hours?");
    await flush();
    handle.shadow.activeElement = send;
    doc.focused = null;
    tab();
    assert.strictEqual(doc.focused, first, "a collapsed disclosure's links are not panel edges");

    var pending = withNetwork(function () { return new Promise(function () {}); });
    try {
      handle.submit("now hold");
      assert.strictEqual(send.disabled, true, "Send is disabled while pending");
      handle.shadow.activeElement = input; // the textarea is the last enabled control now
      doc.focused = null;
      assert.ok(tab().prevented, "the last edge followed the disabled Send");
      assert.strictEqual(doc.focused, first);
    } finally {
      pending();
    }
  } finally {
    restore();
  }
});

// --- F14: label in name (2.5.3) -----------------------------------------
test("the launcher's accessible name contains its visible label", () => {
  var doc = makeDoc();
  var handle = widget.mount(doc);
  var launcher = findByClass(handle.shadow, "launcher");
  // No aria-label: the button's own text IS the name, so "click Ask the Library" works.
  assert.strictEqual(launcher.getAttribute("aria-label"), null, "no aria-label overriding the text");
  // The label now comes from the string table (CONFIG carries knobs, not copy), and the
  // rule holds in every language: whatever is printed on the button is its whole name.
  widget.LANGUAGES.forEach(function (code) {
    clickLanguage(handle, code);
    assert.strictEqual(launcher.getAttribute("aria-label"), null, "still no aria-label in " + code);
    assert.ok(
      launcher.textContent.indexOf(widget.STRINGS[code].launcherLabel) >= 0,
      "the visible label is the accessible name in " + code
    );
  });
  widget.resetLanguage();
  assert.ok(!/Open the library chat/.test(SOURCE), "the overriding label string is gone");
  // What that label used to hint at is now carried by a standard attribute instead.
  assert.strictEqual(launcher.getAttribute("aria-haspopup"), "dialog");
});

// --- F7: the error bubble's palette actually applies --------------------
test("the error bubble's styling wins the specificity fight it used to lose", async () => {
  var flat = SOURCE.replace(/\n/g, " ");
  // `.msg--bot .bubble` is (0,2,0); a bare `.bubble--error` is (0,1,0) and lost every time.
  assert.ok(
    /\.msg--bot \.bubble\.bubble--error \{[^}]*background: var\(--error-bg\)[^}]*color: var\(--error-ink\)/.test(flat),
    "the error rule is scoped two classes deep so it outranks .msg--bot .bubble"
  );
  assert.ok(
    !/(^|[^.\w])\.bubble--error \{/.test(flat),
    "no single-class .bubble--error rule remains to be silently overridden"
  );

  var restore = withNetwork(function () { return Promise.reject(new Error("network down")); });
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    handle.submit("hours?");
    await flush();
    var bubble = findByClass(handle.shadow, "bubble--error");
    assert.ok(bubble, "a failed send renders an error bubble");
    assert.ok(/\bbubble\b/.test(bubble.className) && /bubble--error/.test(bubble.className),
      "it carries both classes the selector needs");
  } finally {
    restore();
  }
});

// --- F16: the retry button is where focus goes ---------------------------
test("a failed send moves focus to Try again, which sits BEHIND the composer in tab order", async () => {
  var restore = withNetwork(function () { return Promise.reject(new Error("network down")); });
  try {
    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    handle.open();
    handle.submit("hours?");
    await flush();
    var retry = findByClass(handle.shadow, "retry");
    assert.ok(retry, "a retry button is rendered");
    assert.strictEqual(doc.focused, retry, "focus lands on retry, not back in the composer");
  } finally {
    restore();
  }
});

// --- F9 / F10: heading and table semantics (1.3.1) ----------------------
test("markdown headings render as real h3-h6 elements, sized like body text", () => {
  var r = renderInfo("# One\n\n## Two\n\n### Three\n\n#### Four\n\n##### Five");
  var tags = [];
  (function walk(n) {
    if (/^h[1-6]$/.test(String(n.tagName))) tags.push(n.tagName + ":" + n.textContent);
    (n.children || []).forEach(walk);
  })(r.root);
  // Offset by two: the host page owns h1/h2. Deeper levels clamp at h6.
  assert.deepStrictEqual(tags, ["h3:One", "h4:Two", "h5:Three", "h6:Four", "h6:Five"]);
  assert.strictEqual(collectByTag(r.root, "p").length, 0, "no heading renders as a paragraph");
  // The class stays, and the rule pins font-size so a real h5/h6 does not shrink.
  assert.ok(/\.md-heading \{[^}]*font-size: 1em/.test(SOURCE.replace(/\n/g, " ")),
    "heading font-size is pinned to the body size");
});

test("table header cells declare scope=col", () => {
  var r = renderInfo("| Day | Hours |\n|---|---|\n| Mon | 9-5 |");
  var th = collectByTag(r.root, "th");
  assert.deepStrictEqual(th.map(function (c) { return c.getAttribute("scope"); }), ["col", "col"]);
  collectByTag(r.root, "td").forEach(function (c) {
    assert.strictEqual(c.getAttribute("scope"), null, "data cells get no scope");
  });
});

// --- F3: the greeting is offered on first launch ------------------------
test("the greeting describes the composer on first launch, and stops after the first message", async () => {
  var restore = withNetwork(okJson({ answer: "A", sources: [] }));
  try {
    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    var input = findByClass(handle.shadow, "composer__input");
    var id = input.getAttribute("aria-describedby");
    assert.ok(id, "the composer is described on first launch");
    // Both ends of the reference are inside the shadow root, which is the only way an
    // id-based ARIA reference resolves here.
    var greeting = findAll(handle.shadow, "bubble").filter(function (b) { return b.id === id; })[0];
    assert.ok(greeting, "the reference resolves inside the same shadow root");
    assert.ok(greeting.textContent.indexOf("Gavilan College Library assistant") >= 0);

    handle.submit("hours?");
    await flush();
    assert.strictEqual(
      input.getAttribute("aria-describedby"), null,
      "a fixed description would be re-read on every refocus, so it is dropped"
    );
  } finally {
    restore();
  }
});

// --- F8: the widget declares its own language ---------------------------
test("the widget declares its own language rather than inheriting the host page's", () => {
  var doc = makeDoc();
  var handle = widget.mount(doc);
  assert.strictEqual(findByClass(handle.shadow, "root").lang, "en");
});

// --- 1.4.11 / 1.4.3: the numbers, computed from the shipped values ------
//
// The browser measurement lives in the audit doc; this recomputes the same WCAG
// arithmetic from the colour values actually in the file, so a later "just darken it a
// bit" edit that drops one below its floor fails here instead of in an external audit.
function relLum(hex) {
  var h = hex.replace("#", "");
  var ch = [0, 2, 4].map(function (i) {
    var v = parseInt(h.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
}
function contrast(a, b) {
  var la = relLum(a), lb = relLum(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
function token(name) {
  var m = new RegExp("--" + name + ":\\s*(#[0-9a-fA-F]{6})").exec(SOURCE);
  assert.ok(m, "--" + name + " is defined in the stylesheet");
  return m[1].toLowerCase();
}

test("the focus ring clears 3:1 on every background it can land on (1.4.11)", () => {
  var ring = token("focus-ring");
  var halo = token("focus-halo");
  var brand = token("brand");
  // White host page, the demo page, and the widget's own thread: the dark half carries it.
  ["#ffffff", "#f6f4f2", "#fafbfc"].forEach(function (bg) {
    assert.ok(contrast(ring, bg) >= 3, "ring vs " + bg + " = " + contrast(ring, bg).toFixed(2) + ":1");
  });
  // Against the maroon launcher/Send fill the dark half cannot, so the halo does.
  assert.ok(contrast(ring, brand) < 3, "the dark half alone does NOT clear the maroon fill");
  assert.ok(contrast(halo, brand) >= 3, "halo vs the brand fill = " + contrast(halo, brand).toFixed(2) + ":1");
  // The two halves contrast with each other, which is what makes this robust on a host
  // page whose background the widget cannot know (including a dark one).
  assert.ok(contrast(ring, halo) >= 3, "the ring contrasts with its own halo");
});

test("control boundaries and the typing dots clear 3:1 (1.4.11)", () => {
  var line = token("line");
  [
    ["#ffffff", "composer strip"],
    ["#fafbfc", "thread behind a suggestion chip"],
    ["#f1f3f6", "bot bubble behind the typing dots"]
  ].forEach(function (row) {
    assert.ok(
      contrast(line, row[0]) >= 3,
      "--line vs " + row[1] + " = " + contrast(line, row[0]).toFixed(2) + ":1"
    );
  });
  // The old values that failed are gone from the file.
  assert.ok(!/#9aa4b0/i.test(SOURCE), "the 2.27:1 typing-dot grey is removed");
  assert.ok(!/#c8cfd8/i.test(SOURCE), "the 1.57:1 composer border grey is removed");
  // ...and the three controls that needed it actually use the token.
  var flat = SOURCE.replace(/\n/g, " ");
  assert.ok(/\.typing \.dot \{[^}]*background: var\(--line\)/.test(flat), "typing dots use --line");
  assert.ok(/\.composer__input \{[^}]*border: 1px solid var\(--line\)/.test(flat), "composer border uses --line");
  assert.ok(/\.suggestion \{[^}]*border: 1px solid var\(--line\)/.test(flat), "suggestion chips use --line");
});

test("the error palette that now renders still passes 4.5:1 (1.4.3)", () => {
  var bg = token("error-bg");
  var ink = token("error-ink");
  assert.ok(contrast(ink, bg) >= 4.5, "error text = " + contrast(ink, bg).toFixed(2) + ":1");
  // The retry button borrows the same ink for its text and border.
  assert.ok(/\.retry \{[^}]*color: var\(--error-ink\)/.test(SOURCE.replace(/\n/g, " ")));
});

// =========================================================================
// ===  sign-in gate (TEMPORARY: these tests leave with the feature)  ======
// =========================================================================
//
// The gate that matters is the API Gateway JWT authorizer on POST /query, which no test here
// can reach. What IS testable in this file is everything the widget must get right around it:
// that an ungated embed is completely untouched, that the token rides in a header and never in
// a body, that the password is never kept, and that a person who cannot send anything is not
// shown controls that pretend otherwise.

// A sessionStorage stand-in. Node has no Web Storage, so the widget's `typeof sessionStorage`
// guard reads as "unavailable" unless a test puts one here - which also means every test that
// does NOT install one is exercising the storage-blocked path for free.
function makeStore(seed) {
  var data = {};
  if (seed !== undefined && seed !== null) {
    data[widget.AUTH_STORAGE_KEY] = typeof seed === "string" ? seed : JSON.stringify(seed);
  }
  return {
    getItem: function (k) {
      return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null;
    },
    setItem: function (k, v) { data[k] = String(v); },
    removeItem: function (k) { delete data[k]; },
    keys: function () { return Object.keys(data); },
    dump: function () { return JSON.stringify(data); }
  };
}

// What a previous page load in this tab would have left behind.
function storedSession(token, msFromNow, clientId) {
  return {
    clientId: clientId === undefined ? GATE_ATTRS["data-client-id"] : clientId,
    accessToken: token,
    expiresAt: Date.now() + msFromNow
  };
}

// Like withNetwork, but with a real attribute map on the script tag, a fetch that can tell the
// Cognito call apart from the /query call, and a sessionStorage stub. `store` is the stub
// itself when the caller wants a custom one (e.g. one that throws); anything else is seeded as
// the stored session record, and `undefined` means an empty store.
function withGate(attrs, fetchImpl, store) {
  var priorDoc = global.document;
  var priorFetch = global.fetch;
  var priorStore = global.sessionStorage;
  var priorErr = console.error;
  var calls = [];
  var storage = store && typeof store.getItem === "function" ? store : makeStore(store);
  global.sessionStorage = storage;
  global.document = {
    querySelector: function () {
      return {
        getAttribute: function (name) {
          return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null;
        }
      };
    }
  };
  global.fetch = function (url, opts) {
    calls.push({ url: url, opts: opts });
    return fetchImpl(url, opts);
  };
  console.error = function () {};
  return {
    calls: calls,
    store: storage,
    cognito: function () {
      return calls.filter(function (c) { return /cognito-idp/.test(c.url); });
    },
    query: function () {
      return calls.filter(function (c) { return /\/query$/.test(c.url); });
    },
    restore: function () {
      // Sign out BEFORE the stubs come down, or the token stays in a store nobody can reach and
      // the next test inherits a signed-in module.
      widget.signOut();
      global.document = priorDoc;
      global.fetch = priorFetch;
      global.sessionStorage = priorStore;
      console.error = priorErr;
    }
  };
}

var GATE_ATTRS = {
  "data-api-url": "https://api.test/query",
  "data-user-pool-id": "us-west-2_TESTPOOL",
  "data-client-id": "testclientid123"
};

// Cognito's real success shape, trimmed to the fields the widget reads.
function cognitoOk(token, expiresIn) {
  return {
    ok: true,
    json: function () {
      return Promise.resolve({
        AuthenticationResult: {
          AccessToken: token,
          ExpiresIn: expiresIn === undefined ? 86400 : expiresIn,
          TokenType: "Bearer",
          RefreshToken: "refresh-token-the-widget-must-ignore",
          IdToken: "id-token-the-widget-must-ignore"
        }
      });
    }
  };
}

function routeGate(cognitoRes, queryPayload, seenBodies) {
  return function (url, opts) {
    if (/cognito-idp/.test(url)) return Promise.resolve(cognitoRes);
    if (seenBodies) seenBodies.push({ body: JSON.parse(opts.body), headers: opts.headers });
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve(queryPayload); }
    });
  };
}

test("an embed with no sign-in ids is the ungated widget: no config, nothing required", () => {
  // The production tag today, the offline harness, and the tag left after go-live.
  assert.strictEqual(widget.authConfig(), null, "no document at all reads as ungated");
  assert.strictEqual(widget.authRequired(), false);
  var g = withGate({ "data-api-url": "https://api.test/query" }, function () {
    return Promise.reject(new Error("no call expected"));
  });
  try {
    assert.strictEqual(widget.authConfig(), null, "data-api-url alone is not a gate");
    assert.strictEqual(widget.authRequired(), false);
  } finally {
    g.restore();
  }
});

test("both ids are required, and the region comes from the pool id rather than a third attribute", () => {
  var half = withGate(
    { "data-api-url": "https://api.test/query", "data-user-pool-id": "us-west-2_TESTPOOL" },
    function () { return Promise.reject(new Error("no call expected")); }
  );
  try {
    assert.strictEqual(widget.authConfig(), null, "a pool id with no client id is not a gate");
  } finally {
    half.restore();
  }

  var g = withGate(GATE_ATTRS, function () { return Promise.reject(new Error("x")); });
  try {
    var cfg = widget.authConfig();
    assert.deepStrictEqual(cfg, {
      poolId: "us-west-2_TESTPOOL",
      clientId: "testclientid123",
      region: "us-west-2"
    });
  } finally {
    g.restore();
  }

  // A pool id that is not "<region>_<suffix>" is a misconfiguration, and reading it as
  // region "" would build a nonsense hostname and fail as an opaque network error.
  var bad = withGate(
    {
      "data-api-url": "https://api.test/query",
      "data-user-pool-id": "no-underscore-here",
      "data-client-id": "testclientid123"
    },
    function () { return Promise.reject(new Error("x")); }
  );
  try {
    assert.strictEqual(widget.authConfig(), null);
  } finally {
    bad.restore();
  }
});

test("the ungated request is byte-identical: no Authorization header at all", async () => {
  var seen = [];
  var g = withGate({ "data-api-url": "https://api.test/query" },
    routeGate(null, { answer: "hi", sources: [] }, seen));
  try {
    await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.strictEqual(seen.length, 1);
    assert.deepStrictEqual(seen[0].headers, { "Content-Type": "application/json" });
    assert.deepStrictEqual(Object.keys(seen[0].body), ["messages"]);
  } finally {
    g.restore();
  }
});

test("a gated widget will not spend a request without a token", async () => {
  var g = withGate(GATE_ATTRS, routeGate(null, { answer: "hi", sources: [] }, []));
  try {
    var err = null;
    try {
      await widget.sendQuery([{ role: "user", content: "hours?" }]);
    } catch (e) {
      err = e;
    }
    assert.ok(err, "sendQuery rejects rather than resolving an answer");
    assert.strictEqual(err.reason, "expired");
    assert.strictEqual(g.query().length, 0, "no /query request was made");
  } finally {
    g.restore();
  }
});

test("signIn posts InitiateAuth to the pool's region, and the password goes nowhere else", async () => {
  var seen = [];
  var g = withGate(GATE_ATTRS, routeGate(cognitoOk("ACCESS-TOKEN-1"), { answer: "hi", sources: [] }, seen));
  try {
    var ok = await widget.signIn("gavtesting", "hunter2-correct-horse");
    assert.strictEqual(ok, true);
    assert.strictEqual(widget.signedIn(), true);

    var call = g.cognito()[0];
    assert.strictEqual(call.url, "https://cognito-idp.us-west-2.amazonaws.com/");
    assert.strictEqual(call.opts.method, "POST");
    assert.strictEqual(call.opts.headers["Content-Type"], "application/x-amz-json-1.1");
    assert.strictEqual(
      call.opts.headers["X-Amz-Target"],
      "AWSCognitoIdentityProviderService.InitiateAuth"
    );
    var body = JSON.parse(call.opts.body);
    assert.strictEqual(body.AuthFlow, "USER_PASSWORD_AUTH");
    assert.strictEqual(body.ClientId, "testclientid123");
    assert.strictEqual(body.AuthParameters.USERNAME, "gavtesting");

    // ...and now the question. The password must appear in the sign-in call and NOWHERE else.
    await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.strictEqual(seen.length, 1);
    var wire = JSON.stringify(seen[0]);
    assert.ok(!/hunter2/.test(wire), "the password never reaches /query, header or body");
    assert.ok(!/ACCESS-TOKEN-1/.test(JSON.stringify(seen[0].body)), "the token is not in the body");
    assert.strictEqual(seen[0].headers.Authorization, "Bearer ACCESS-TOKEN-1");
    assert.deepStrictEqual(Object.keys(seen[0].body), ["messages"]);
  } finally {
    g.restore();
  }
});

test("bad credentials reject as 'credentials' and leave the widget signed out", async () => {
  var denied = {
    ok: false,
    json: function () {
      return Promise.resolve({
        __type: "NotAuthorizedException",
        message: "Incorrect username or password."
      });
    }
  };
  var g = withGate(GATE_ATTRS, routeGate(denied, null, []));
  try {
    var err = null;
    try {
      await widget.signIn("gavtesting", "wrong");
    } catch (e) {
      err = e;
    }
    assert.ok(err && err.reason === "credentials", "reason is 'credentials'");
    // AWS's own wording is never carried forward: the pool is configured not to say whether the
    // address exists, and echoing its message would undo that.
    assert.ok(!/Incorrect username/.test(String(err.message)), "AWS's message is not passed through");
    assert.strictEqual(widget.signedIn(), false);
  } finally {
    g.restore();
  }
});

test("a challenge instead of a token is a failed sign-in, not a half-signed-in widget", async () => {
  // The shape when the account was created but its password was never made permanent.
  var challenge = {
    ok: true,
    json: function () {
      return Promise.resolve({ ChallengeName: "NEW_PASSWORD_REQUIRED", Session: "abc" });
    }
  };
  var g = withGate(GATE_ATTRS, routeGate(challenge, null, []));
  try {
    var err = null;
    try {
      await widget.signIn("gavtesting", "temp-password");
    } catch (e) {
      err = e;
    }
    assert.ok(err && err.reason === "credentials");
    assert.strictEqual(widget.signedIn(), false);
  } finally {
    g.restore();
  }
});

test("an unreachable Cognito is 'unavailable', a distinct outcome from a wrong password", async () => {
  var g = withGate(GATE_ATTRS, function (url) {
    if (/cognito-idp/.test(url)) return Promise.reject(new TypeError("Failed to fetch"));
    return Promise.reject(new Error("unexpected"));
  });
  try {
    var err = null;
    try {
      await widget.signIn("gavtesting", "whatever");
    } catch (e) {
      err = e;
    }
    assert.ok(err && err.reason === "unavailable", "network failure is not reported as bad credentials");
  } finally {
    g.restore();
  }
});

test("an expired token is caught before the request, not read off a 401", async () => {
  // The 401 from an API Gateway authorizer carries no CORS headers, so a browser turns it into
  // an opaque network error - the status is unreadable. Expiry is therefore tracked locally
  // from ExpiresIn, which is what makes "your session ended" distinguishable at all.
  var g = withGate(GATE_ATTRS, routeGate(cognitoOk("SHORT-LIVED", 1), { answer: "hi", sources: [] }, []));
  try {
    await widget.signIn("gavtesting", "pw");
    // ExpiresIn 1s, minus the 60s safety margin, is already in the past.
    assert.strictEqual(widget.signedIn(), false, "a token inside its safety margin is already gone");
    var err = null;
    try {
      await widget.sendQuery([{ role: "user", content: "hours?" }]);
    } catch (e) {
      err = e;
    }
    assert.strictEqual(err && err.reason, "expired");
    assert.strictEqual(g.query().length, 0, "no doomed request was sent");
  } finally {
    g.restore();
  }
});

test("nothing exposed can read the token back out, and the refresh token is never read", () => {
  // The API surface is deliberately actions only: sign in, ask whether we are signed in, sign
  // out. Nothing downstream can copy the token somewhere longer-lived by accident.
  assert.ok(!("accessToken" in widget), "no token getter is exported");
  assert.ok(!("session" in widget), "no session object is exported");
  assert.ok(typeof widget.signedIn === "function" && typeof widget.signOut === "function");
  // Cognito hands back a refresh token; the widget never reads it, so the stored record cannot
  // outlive the one day the access token was minted for.
  assert.ok(!/RefreshToken/.test(SOURCE), "the refresh token is never read");
});

// --- the session survives a reload (D-20260803-1) ------------------------
//
// "Reload" here is: memory empty, storage kept - which is exactly what a fresh page load looks
// like to this module. Seeding the stub and NOT signing in is therefore the real thing, not an
// approximation of it.

test("signing in writes the token to sessionStorage - and only what a reload needs", async () => {
  var g = withGate(GATE_ATTRS, routeGate(cognitoOk("STORED-TOKEN"), { answer: "hi", sources: [] }, []));
  try {
    await widget.signIn("gavtesting", "hunter2-correct-horse");
    assert.deepStrictEqual(g.store.keys(), [widget.AUTH_STORAGE_KEY], "one key, namespaced");
    var saved = JSON.parse(g.store.getItem(widget.AUTH_STORAGE_KEY));
    assert.deepStrictEqual(
      Object.keys(saved).sort(),
      ["accessToken", "clientId", "expiresAt"],
      "three fields and no more"
    );
    assert.strictEqual(saved.accessToken, "STORED-TOKEN");
    assert.strictEqual(saved.clientId, "testclientid123");
    assert.ok(saved.expiresAt > Date.now(), "with an absolute expiry, not a duration");
    // The credentials are not part of "what a reload needs".
    var dump = g.store.dump();
    assert.ok(!/hunter2/.test(dump), "the password is not stored");
    assert.ok(!/gavtesting/.test(dump), "nor the username");
    assert.ok(!/refresh-token/.test(dump), "nor the refresh token");
  } finally {
    g.restore();
  }
});

test("a reload with a live stored token goes straight to the composer, signing in again for nothing", async () => {
  var seen = [];
  widget.signOut(); // memory empty, as it is on a fresh page load
  var g = withGate(
    GATE_ATTRS,
    routeGate(cognitoOk("SHOULD-NOT-BE-CALLED"), { answer: "Open 9-5.", sources: [] }, seen),
    storedSession("RELOADED-TOKEN", 20 * 60 * 60 * 1000)
  );
  try {
    assert.strictEqual(widget.signedIn(), true, "the stored session is picked up");

    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    assert.strictEqual(findByClass(handle.shadow, "signin-overlay"), null, "no overlay");
    assert.strictEqual(findByClass(handle.shadow, "composer").hidden, false, "the composer is live");
    assert.ok(findByClass(handle.shadow, "suggestions"), "and the starter questions are real offers");

    handle.submit("hours?");
    await flush();
    assert.strictEqual(seen.length, 1, "the question went out");
    assert.strictEqual(seen[0].headers.Authorization, "Bearer RELOADED-TOKEN", "on the stored token");
    assert.strictEqual(g.cognito().length, 0, "and nobody was made to sign in again");
  } finally {
    g.restore();
  }
});

test("a stored token past its expiry draws the overlay, and is dropped rather than sent", async () => {
  // The case nobody hits by hand: you signed in yesterday, the tab is still open, you reload.
  // Restoring it blindly would send a request the widget already knows is doomed - and a
  // browser cannot read the 401 that comes back (no CORS headers on an authorizer rejection),
  // so it would surface as "couldn't reach the assistant" rather than "sign in again".
  widget.signOut();
  var g = withGate(
    GATE_ATTRS,
    function () { return Promise.reject(new Error("no call expected")); },
    storedSession("YESTERDAYS-TOKEN", -1000)
  );
  try {
    assert.strictEqual(widget.signedIn(), false, "an expired record is not a session");
    assert.deepStrictEqual(g.store.keys(), [], "and it is removed, not left readable");

    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    var overlay = findByClass(handle.shadow, "signin-overlay");
    assert.ok(overlay, "the overlay draws");
    assert.strictEqual(findByClass(handle.shadow, "composer").hidden, true);
    assert.strictEqual(findByClass(handle.shadow, "suggestions"), null, "no dead buttons");

    handle.submit("hours?");
    await flush();
    assert.strictEqual(g.calls.length, 0, "no request was fired on the dead token");
  } finally {
    g.restore();
  }
});

test("a stored token that expires while the tab sits open stops being one, and clears itself", async () => {
  widget.signOut();
  var g = withGate(
    GATE_ATTRS,
    function () { return Promise.reject(new Error("no call expected")); },
    storedSession("HALF-A-SECOND", 500)
  );
  try {
    assert.strictEqual(widget.signedIn(), true);
    await sleep(700);
    assert.strictEqual(widget.signedIn(), false, "expiry is re-checked, not read once at load");
    assert.deepStrictEqual(g.store.keys(), [], "and the dead record is removed");
  } finally {
    g.restore();
  }
});

test("a record the widget cannot trust is dropped rather than used", () => {
  // Each of these would otherwise wedge the tab: the sign-in form is hidden while `signedIn()`
  // is true, so a bad record that reads as a session is a panel that can only fail.
  var bad = {
    "not JSON at all": "{{{",
    "no token": JSON.stringify({ clientId: "testclientid123", expiresAt: Date.now() + 60000 }),
    "a token that is not a string": JSON.stringify(
      storedSession(12345, 60000)
    ),
    "an expiry that is not a number": JSON.stringify({
      clientId: "testclientid123", accessToken: "T", expiresAt: "tomorrow"
    }),
    // The embed was repointed at a different app client: the old token cannot satisfy the new
    // authorizer, so signing in again is the only correct outcome.
    "a token for another app client": JSON.stringify(
      storedSession("OTHER-POOLS-TOKEN", 60000, "some-other-client")
    )
  };
  Object.keys(bad).forEach(function (label) {
    widget.signOut();
    var g = withGate(GATE_ATTRS, function () { return Promise.reject(new Error("x")); }, bad[label]);
    try {
      assert.strictEqual(widget.signedIn(), false, label + " must not read as a session");
      assert.deepStrictEqual(g.store.keys(), [], label + " must be cleared out");
    } finally {
      g.restore();
    }
  });
});

test("a readable 401 clears storage too, so a reload cannot resurrect a refused token", async () => {
  var g = withGate(GATE_ATTRS, function (url) {
    if (/cognito-idp/.test(url)) return Promise.resolve(cognitoOk("DOOMED-TOKEN"));
    return Promise.resolve({ ok: false, status: 401, json: function () { return Promise.resolve({}); } });
  });
  try {
    await widget.signIn("gavtesting", "pw");
    assert.deepStrictEqual(g.store.keys(), [widget.AUTH_STORAGE_KEY], "stored on the way in");
    var err = null;
    try {
      await widget.sendQuery([{ role: "user", content: "hours?" }]);
    } catch (e) {
      err = e;
    }
    assert.strictEqual(err && err.reason, "expired");
    assert.strictEqual(widget.signedIn(), false);
    assert.deepStrictEqual(g.store.keys(), [], "nothing readable is left behind");
  } finally {
    g.restore();
  }
});

test("a failed sign-in leaves nothing in storage, including from an earlier attempt", async () => {
  var stage = { deny: false };
  var g = withGate(GATE_ATTRS, function (url) {
    if (!/cognito-idp/.test(url)) return Promise.reject(new Error("no query expected"));
    if (stage.deny) {
      return Promise.resolve({
        ok: false,
        json: function () { return Promise.resolve({ __type: "NotAuthorizedException" }); }
      });
    }
    return Promise.resolve(cognitoOk("FIRST-TOKEN"));
  });
  try {
    await widget.signIn("gavtesting", "right");
    assert.deepStrictEqual(g.store.keys(), [widget.AUTH_STORAGE_KEY]);
    stage.deny = true;
    var err = null;
    try {
      await widget.signIn("gavtesting", "wrong");
    } catch (e) {
      err = e;
    }
    assert.strictEqual(err && err.reason, "credentials");
    assert.strictEqual(widget.signedIn(), false);
    assert.deepStrictEqual(g.store.keys(), [], "the earlier token does not survive a failed attempt");
  } finally {
    g.restore();
  }
});

test("blocked storage degrades to the memory-only gate rather than failing to sign in", async () => {
  // Safari's private mode and a sandboxed iframe both throw on the storage access itself, which
  // is why the widget guards the lookup and not just the write. The cost of it is the old
  // behaviour - a reload signs in again - and nothing worse.
  var hostile = {
    getItem: function () { throw new Error("SecurityError"); },
    setItem: function () { throw new Error("QuotaExceededError"); },
    removeItem: function () { throw new Error("SecurityError"); },
    keys: function () { return []; }
  };
  var seen = [];
  var g = withGate(GATE_ATTRS, routeGate(cognitoOk("MEMORY-ONLY"), { answer: "hi", sources: [] }, seen), hostile);
  try {
    var ok = await widget.signIn("gavtesting", "pw");
    assert.strictEqual(ok, true, "the sign-in still succeeds");
    assert.strictEqual(widget.signedIn(), true);
    await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.strictEqual(seen[0].headers.Authorization, "Bearer MEMORY-ONLY");
  } finally {
    g.restore();
  }
});

test("an ungated embed never touches storage: same request, header and all", async () => {
  var seen = [];
  widget.signOut();
  var g = withGate(
    { "data-api-url": "https://api.test/query" },
    routeGate(null, { answer: "hi", sources: [] }, seen),
    storedSession("LEFTOVER-FROM-A-GATED-EMBED", 60 * 60 * 1000, "testclientid123")
  );
  try {
    assert.strictEqual(widget.signedIn(), false, "no gate means no session, whatever is stored");
    await widget.sendQuery([{ role: "user", content: "hours?" }]);
    assert.deepStrictEqual(seen[0].headers, { "Content-Type": "application/json" });
    assert.deepStrictEqual(g.store.keys(), [widget.AUTH_STORAGE_KEY], "and it left the key alone");
  } finally {
    g.restore();
  }
});

// --- the panel while gated ----------------------------------------------

test("a gated, signed-out panel puts the overlay over its own body and offers no dead buttons", () => {
  var g = withGate(GATE_ATTRS, function () { return Promise.reject(new Error("x")); });
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    var overlay = findByClass(handle.shadow, "signin-overlay");
    var composer = findByClass(handle.shadow, "composer");
    var thread = findByClass(handle.shadow, "thread");
    assert.ok(overlay, "the overlay is up");
    assert.ok(findByClass(overlay, "signin"), "and it carries the sign-in form");
    // Over the PANEL'S BODY and nothing wider. This widget is a guest on somebody else's
    // page, so the covered region stops at the panel: no host-page scrim, ever.
    assert.strictEqual(overlay.parentNode.className, "panel__body", "scoped to the panel body");
    assert.strictEqual(composer.hidden, true, "the composer is out of reach beneath it");
    // What the overlay covers cannot be read or acted on, so it is not narrated either.
    assert.strictEqual(thread.getAttribute("aria-hidden"), "true");
    // The starter questions do not show, because each is a button that would submit nothing.
    assert.strictEqual(findByClass(handle.shadow, "suggestions"), null, "no starter questions yet");
  } finally {
    g.restore();
  }
});

test("hiding the composer actually hides it: its class display does not beat the attribute", () => {
  // `hidden` is a UA `display: none`, which a class-level `display: flex` outranks. The panel
  // and the launcher always carried the override; the composer did not, so `form.hidden = true`
  // took it out of the tab order and left it painted - a dead text box under the gate.
  var flat = SOURCE.replace(/\n/g, " ");
  var m = /([^"]*)\[hidden\][^{]*\{ display: none !important; \}/.exec(flat);
  assert.ok(m, "there is a [hidden] display override rule");
  var rule = m[0];
  ["\\.panel\\[hidden\\]", "\\.launcher\\[hidden\\]", "\\.composer\\[hidden\\]"].forEach(function (sel) {
    assert.ok(new RegExp(sel).test(rule), sel.replace(/\\/g, "") + " is covered by it");
  });
});

test("while the overlay is up, Tab reaches the header and the overlay and nothing else", async () => {
  var g = withGate(GATE_ATTRS, routeGate(cognitoOk("TOKEN-TRAP"), { answer: "hi", sources: [] }, []));
  try {
    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    var panel = findByClass(handle.shadow, "panel");
    var fields = findAll(handle.shadow, "signin__input");

    // Opening puts focus INTO the overlay rather than on a composer nobody can use.
    handle.open();
    assert.strictEqual(doc.focused, fields[0], "focus lands in the username field");

    // The last thing in the cycle is the overlay's submit button, and Tab there wraps back to
    // the header's language control - it does not fall through to the composer or the thread.
    var firstLang = findAll(handle.shadow, "header__lang-btn")[0];
    var submit = findByClass(handle.shadow, "signin__submit");
    function tab(shift) {
      var ev = { key: "Tab", shiftKey: !!shift, preventDefault: function () { ev.prevented = true; } };
      panel.fire("keydown", ev);
      return ev;
    }
    handle.shadow.activeElement = submit;
    doc.focused = null;
    assert.ok(tab().prevented, "Tab at the submit button is intercepted");
    assert.strictEqual(doc.focused, firstLang, "...and wraps to the language control");

    // Shift+Tab from the header's first control wraps to the overlay's last, so the cycle is
    // exactly [header, overlay]. The language buttons being IN it is the point: the sign-in
    // prompt has to be readable in Spanish before anyone signs in.
    handle.shadow.activeElement = firstLang;
    doc.focused = null;
    assert.ok(tab(true).prevented);
    assert.strictEqual(doc.focused, submit, "the cycle ends at the overlay, not at Send");

    // Signing in hands the whole panel back.
    fields[0].value = "gavtesting";
    fields[1].value = "pw";
    handle.signInForm.fire("submit");
    await new Promise(function (r) { setTimeout(r, 0); });
    handle.shadow.activeElement = findByClass(handle.shadow, "composer__send");
    doc.focused = null;
    assert.ok(tab().prevented);
    assert.strictEqual(doc.focused, firstLang, "Send is the last edge again");
  } finally {
    g.restore();
  }
});

test("Escape does not dismiss the overlay - it closes the panel, and the gate is still there", () => {
  var g = withGate(GATE_ATTRS, function () { return Promise.reject(new Error("x")); });
  try {
    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    var panel = findByClass(handle.shadow, "panel");
    handle.open();
    panel.fire("keydown", { key: "Escape" });
    assert.strictEqual(panel.hidden, true, "Escape closes the panel");
    // Nothing behind the overlay is usable, so there is no state where the panel is open and
    // the overlay is gone. Reopening returns to the same gate, with focus back inside it.
    assert.ok(findByClass(handle.shadow, "signin-overlay"), "the overlay was not dismissed");
    doc.focused = null;
    handle.open();
    assert.strictEqual(panel.hidden, false);
    assert.ok(findByClass(handle.shadow, "signin-overlay"), "still gated");
    assert.strictEqual(doc.focused, findAll(handle.shadow, "signin__input")[0]);
  } finally {
    g.restore();
  }
});

test("the sign-in fields are real labelled inputs, typed, and reachable by keyboard", () => {
  var g = withGate(GATE_ATTRS, function () { return Promise.reject(new Error("x")); });
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    var labels = findAll(handle.shadow, "signin__label");
    var fields = findAll(handle.shadow, "signin__input");
    assert.strictEqual(labels.length, 2);
    assert.strictEqual(fields.length, 2);
    labels.forEach(function (l, i) {
      assert.strictEqual(l.tagName, "label");
      assert.ok(l.textContent && l.textContent.trim(), "the label carries visible text");
      // for/id resolves only because both ends are in this shadow root.
      assert.strictEqual(l.getAttribute("for"), fields[i].getAttribute("id"));
    });
    // type=text, NOT type=email. The pool signs in by plain username, and `type=email` alongside
    // the `required` attribute is browser-enforced validation: it would refuse to submit
    // "gavtesting" before any widget code ran, and the message would be the browser's, not ours.
    assert.strictEqual(fields[0].getAttribute("type"), "text");
    assert.strictEqual(fields[1].getAttribute("type"), "password");
    assert.strictEqual(fields[0].getAttribute("autocomplete"), "username");
    assert.strictEqual(fields[1].getAttribute("autocomplete"), "current-password");
    // A failure has to be announced, not just drawn.
    var err = findByClass(handle.shadow, "signin__error");
    assert.strictEqual(err.getAttribute("role"), "alert");
    assert.strictEqual(err.hidden, true, "and it is absent until there is something to say");
  } finally {
    g.restore();
  }
});

test("signing in leaves no trace of the gate and puts focus in the composer", async () => {
  var g = withGate(GATE_ATTRS, routeGate(cognitoOk("TOKEN-A"), { answer: "hi", sources: [] }, []));
  try {
    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    var fields = findAll(handle.shadow, "signin__input");
    fields[0].value = "gavtesting";
    fields[1].value = "a-password";
    handle.signInForm.fire("submit");
    await new Promise(function (r) { setTimeout(r, 0); });

    assert.strictEqual(widget.signedIn(), true);
    // REMOVED, not hidden: nothing on screen and nothing in the tree says there was a gate.
    assert.strictEqual(findByClass(handle.shadow, "signin-overlay"), null, "the overlay is gone");
    assert.strictEqual(findByClass(handle.shadow, "signin"), null, "and so is the form inside it");
    assert.strictEqual(findByClass(handle.shadow, "composer").hidden, false);
    assert.strictEqual(
      findByClass(handle.shadow, "thread").getAttribute("aria-hidden"), null,
      "the transcript is narrated again"
    );
    assert.strictEqual(doc.focused, findByClass(handle.shadow, "composer__input"), "focus lands on the composer");
    assert.ok(findByClass(handle.shadow, "suggestions"), "the starter questions arrive with the session");
    // The password is cleared from the DOM the moment it is no longer needed.
    assert.strictEqual(fields[1].value, "", "the password field is emptied");
    assert.strictEqual(fields[0].value, "gavtesting", "the username is not, so nobody retypes it");
  } finally {
    g.restore();
  }
});

test("a 401 mid-conversation brings the overlay back, with a reason", async () => {
  // The readable-401 path. In a browser it is mostly unreachable (an authorizer rejection
  // carries no CORS headers), which is why the expiry check below exists too - but this is
  // the branch that fires wherever the status IS readable.
  var stage = { rejecting: false };
  var g = withGate(GATE_ATTRS, function (url) {
    if (/cognito-idp/.test(url)) return Promise.resolve(cognitoOk("TOKEN-401"));
    if (stage.rejecting) return Promise.resolve({ ok: false, status: 401, json: function () { return Promise.resolve({}); } });
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ answer: "Open 9-5.", sources: [] }); } });
  });
  try {
    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    var fields = findAll(handle.shadow, "signin__input");
    fields[0].value = "gavtesting";
    fields[1].value = "pw";
    handle.signInForm.fire("submit");
    await flush();
    handle.submit("hours?");
    await flush();
    assert.strictEqual(findByClass(handle.shadow, "signin-overlay"), null, "signed in, no gate");

    stage.rejecting = true;
    doc.focused = null;
    handle.submit("and tomorrow?");
    await flush();

    assert.strictEqual(widget.signedIn(), false, "the dead token was dropped");
    var overlay = findByClass(handle.shadow, "signin-overlay");
    assert.ok(overlay, "the overlay is back over the transcript");
    assert.strictEqual(findByClass(handle.shadow, "composer").hidden, true);
    assert.strictEqual(
      findByClass(overlay, "signin__error").textContent, widget.STRINGS.en.signInExpired,
      "and it says why, rather than reappearing unexplained"
    );
    // The password field, not the username: the name is still filled in from the first
    // sign-in, and the password is the only thing this person has to type again.
    assert.strictEqual(doc.focused, findAll(handle.shadow, "signin__input")[1], "focus is in the overlay");
  } finally {
    g.restore();
  }
});

test("a token the widget already knows is expired brings the overlay back before any request", async () => {
  var seen = [];
  // 60.5s, minus the 60s safety margin, is half a second of validity: enough for the first
  // question, gone by the second. Expiry is tracked locally precisely so the second question
  // is never sent - a browser cannot read the 401 it would come back as.
  var g = withGate(GATE_ATTRS, routeGate(cognitoOk("SHORT", 60.5), { answer: "Open 9-5.", sources: [] }, seen));
  try {
    var doc = makeFocusDoc();
    var handle = widget.mount(doc);
    var fields = findAll(handle.shadow, "signin__input");
    fields[0].value = "gavtesting";
    fields[1].value = "pw";
    handle.signInForm.fire("submit");
    await flush();
    handle.submit("hours?");
    await flush();
    assert.strictEqual(seen.length, 1, "the first question went out");
    assert.strictEqual(findByClass(handle.shadow, "signin-overlay"), null);

    await sleep(700);
    handle.submit("and tomorrow?");
    await flush();

    assert.strictEqual(seen.length, 1, "no doomed request was sent");
    var overlay = findByClass(handle.shadow, "signin-overlay");
    assert.ok(overlay, "the overlay is back");
    assert.strictEqual(findByClass(overlay, "signin__error").textContent, widget.STRINGS.en.signInExpired);
  } finally {
    g.restore();
  }
});

test("a rejected sign-in says so, clears the password, and stays gated", async () => {
  var denied = { ok: false, json: function () { return Promise.resolve({ __type: "NotAuthorizedException" }); } };
  var g = withGate(GATE_ATTRS, routeGate(denied, null, []));
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    var fields = findAll(handle.shadow, "signin__input");
    fields[0].value = "gavtesting";
    fields[1].value = "wrong";
    handle.signInForm.fire("submit");
    await new Promise(function (r) { setTimeout(r, 0); });

    var err = findByClass(handle.shadow, "signin__error");
    assert.strictEqual(err.hidden, false, "the alert is shown");
    assert.strictEqual(err.textContent, widget.STRINGS.en.signInFailed);
    assert.strictEqual(fields[1].value, "", "the password does not sit in the form for the next person");
    assert.ok(findByClass(handle.shadow, "signin-overlay"), "still gated: the overlay stays up");
    assert.strictEqual(findByClass(handle.shadow, "composer").hidden, true);
    assert.strictEqual(findByClass(handle.shadow, "suggestions"), null, "still no dead buttons");
  } finally {
    g.restore();
  }
});

test("a gated panel will not submit a question, however it is asked to", async () => {
  var seen = [];
  var g = withGate(GATE_ATTRS, routeGate(cognitoOk("T"), { answer: "hi", sources: [] }, seen));
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    handle.submit("what are the hours?");
    await new Promise(function (r) { setTimeout(r, 0); });
    assert.strictEqual(seen.length, 0, "no /query request escaped");
    assert.strictEqual(handle.getState().started, false, "and the conversation did not start");
  } finally {
    g.restore();
  }
});

test("the sign-in form is localized, and a switch does not strand it in the other language", () => {
  var g = withGate(GATE_ATTRS, function () { return Promise.reject(new Error("x")); });
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    var heading = findByClass(handle.shadow, "signin__heading");
    var submit = findByClass(handle.shadow, "signin__submit");
    assert.strictEqual(heading.textContent, widget.STRINGS.en.signInHeading);
    assert.strictEqual(submit.textContent, widget.STRINGS.en.signInSubmit);

    handle.chooseLanguage("es");
    assert.strictEqual(heading.textContent, widget.STRINGS.es.signInHeading);
    assert.strictEqual(submit.textContent, widget.STRINGS.es.signInSubmit);
    assert.strictEqual(
      findByClass(handle.shadow, "signin__intro").textContent,
      widget.STRINGS.es.signInIntro
    );
    var labels = findAll(handle.shadow, "signin__label");
    assert.strictEqual(labels[0].textContent, widget.STRINGS.es.signInUsernameLabel);
    assert.strictEqual(labels[1].textContent, widget.STRINGS.es.signInPasswordLabel);
  } finally {
    widget.resetLanguage();
    g.restore();
  }
});

// ==========================================================================
// theme.json - the runtime customisation file
// ==========================================================================
//
// The customer edits this file by hand, in an S3 console, with no way to redeploy and no
// way to see a stack trace. So these are mostly tests about WRONG input: what the widget
// does with a colour that is not a colour, a font it has never heard of, five starter
// questions, and a file that is not JSON. The answer is always "ignore that one key and
// render the rest", and the default install has to stay byte-identical.

// defaults/theme.json is what the customer downloads from the stack's output link and edits
// in place, so it is tested as code: it has to BE the defaults, and it has to document
// itself, because once downloaded it travels alone and its reader has no GitHub access.
// The file is its own source of truth; there is no separate authored copy.
const THEME_DEFAULTS_PATH = path.join(__dirname, "..", "defaults", "theme.json");
const THEME_DEFAULTS_TEXT = fs.readFileSync(THEME_DEFAULTS_PATH, "utf8");
const THEME_DEFAULTS = JSON.parse(THEME_DEFAULTS_TEXT);
// The hosted guide page, served at the widget bucket root next to widget.js. The page is
// its own source of truth as well.
const THEME_GUIDE_PAGE = fs.readFileSync(
  path.join(__dirname, "..", "theme-guide.html"),
  "utf8"
);

// mount() reads the module-level theme, so a themed mount has to hand the default back.
function withTheme(data, fn) {
  widget.applyTheme(data);
  try {
    return fn();
  } finally {
    widget.resetTheme();
  }
}

// WCAG 2.x contrast ratio between two six-digit hex colours.
function contrastRatio(a, b) {
  function lum(hex) {
    const parts = [1, 3, 5].map((i) => {
      const c = parseInt(hex.slice(i, i + 2), 16) / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2];
  }
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

test("an install with no theme.json renders exactly as it always has", () => {
  // The whole safety argument for shipping this: absent or unreadable, the theme emits no
  // CSS at all, so there is nothing for it to override and nothing to regress.
  assert.strictEqual(widget.themeCss(), "");
  assert.strictEqual(widget.applyTheme(null), false);
  assert.strictEqual(widget.applyTheme(undefined), false);
  assert.strictEqual(widget.applyTheme("nope"), false);   // JSON.parse of a bare string
  assert.strictEqual(widget.applyTheme([1, 2, 3]), false);
  assert.strictEqual(widget.themeCss(), "");
  assert.deepStrictEqual(widget.starterQuestions(), widget.STRINGS.en.suggestedQuestions);
});

test("a highlight colour moves --brand and derives its own ink", () => {
  withTheme({ highlightColor: "#1E4B8F" }, () => {
    const css = widget.themeCss();
    assert.ok(/--brand:\s*#1e4b8f/.test(css), css);
    // Dark blue -> white text.
    assert.ok(/--accent-ink:\s*#ffffff/.test(css), css);
    assert.ok(/--user-ink:\s*#ffffff/.test(css), css);
  });
  withTheme({ highlightColor: "#FFD400" }, () => {
    // Bright yellow -> black text, which is the case the old hardcoded #fff got wrong.
    const css = widget.themeCss();
    assert.ok(/--accent-ink:\s*#000000/.test(css), css);
    assert.ok(/--user-ink:\s*#000000/.test(css), css);
  });
  // Three-digit hex is accepted and expanded, so a customer copying "#a13" from a brand
  // sheet is not silently ignored.
  withTheme({ highlightColor: "#A13" }, () => {
    assert.ok(/--brand:\s*#aa1133/.test(widget.themeCss()), widget.themeCss());
  });
});

test("the derived ink clears 4.5:1 on every colour a customer can type", () => {
  // This is why there is no contrast validation to fail: picking the better of black and
  // white bottoms out at 4.58:1, above the 1.4.3 floor, so no hex needs rejecting. Swept
  // rather than spot-checked, and the sweep straddles the crossover point.
  let worst = Infinity;
  let worstHex = null;
  for (let r = 0; r < 256; r += 15) {
    for (let g = 0; g < 256; g += 15) {
      for (let b = 0; b < 256; b += 15) {
        const hex =
          "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
        const ratio = contrastRatio(hex, widget.inkFor(hex));
        if (ratio < worst) { worst = ratio; worstHex = hex; }
      }
    }
  }
  assert.ok(worst >= 4.5, "worst pair " + worstHex + " at " + worst.toFixed(2) + ":1");
  // ...and the derivation really is picking the better one, not just a passing one.
  assert.ok(worst >= 4.57 && worst <= 4.6, "worst is the crossover, got " + worst);
});

test("a colour that is not a hex colour leaves the colour alone", () => {
  // Each of these is a plausible thing to type, and each has to cost the customer their
  // colour and nothing else.
  [
    "maroon",                      // a CSS colour name
    "rgb(138, 28, 48)",
    "8a1c30",                      // no hash
    "#8a1c3",                      // five digits
    "#gggggg",
    "",
    42,
    null,
    ["#8a1c30"]
  ].forEach((value) => {
    withTheme({ highlightColor: value }, () => {
      assert.strictEqual(
        widget.themeCss(),
        "",
        "no CSS emitted for highlightColor " + JSON.stringify(value)
      );
    });
  });
});

test("nothing from theme.json can write CSS of its own", () => {
  // The colour is the one value that reaches a stylesheet, so the pattern that validates it
  // is a security boundary, not tidiness: anything carrying a ; or a } would let a theme
  // file restyle - or hide - the whole widget.
  [
    "#fff; } .panel { display: none } .x {",
    "red; background-image: url(https://evil.test/x)",
    "var(--anything)",
    "#fff/**/;",
    "expression(alert(1))"
  ].forEach((value) => {
    withTheme({ highlightColor: value }, () => {
      assert.strictEqual(widget.themeCss(), "", "rejected: " + value);
    });
  });
  // And the shape that IS accepted cannot contain either character by construction.
  withTheme({ highlightColor: "#123abc" }, () => {
    const declarations = widget.themeCss().split("{")[1];
    assert.strictEqual(declarations.split(";").length, 4, declarations); // 3 decls + tail
  });
});

test("the font is an enumerated keyword, resolved from a table the widget owns", () => {
  widget.FONT_KEYWORDS.forEach((keyword) => {
    withTheme({ fontFamily: keyword }, () => {
      const css = widget.themeCss();
      if (keyword === widget.DEFAULT_FONT) {
        assert.strictEqual(css, "", "the default font emits nothing");
        return;
      }
      assert.ok(/\.root, \.header__title \{ font-family:/.test(css), css);
      if (keyword === "inherit") {
        // Inheriting the host page's family needs the host element too: `:host { all:
        // initial }` resets font-family to the browser default, not the page's.
        assert.ok(/:host \{ font-family: inherit; \}/.test(css), css);
      } else {
        assert.ok(css.indexOf(widget.FONT_STACKS[keyword]) >= 0, css);
      }
    });
  });
  // Every stack is a list of families that ship on macOS AND Windows - no @font-face, no
  // download, and no bare family name that would resolve on one desk and nowhere else.
  widget.FONT_KEYWORDS.forEach((keyword) => {
    const stack = widget.FONT_STACKS[keyword];
    if (keyword === "inherit") {
      assert.strictEqual(stack, null, "inherit is handled separately, not as a stack");
      return;
    }
    assert.ok(/,\s*(sans-serif|serif|monospace)$/.test(stack), keyword + ": " + stack);
    assert.ok(stack.indexOf("@font-face") < 0 && stack.indexOf("url(") < 0, stack);
  });
});

test("an unknown font keyword leaves the font alone, including inherited property names", () => {
  ["Comic Sans MS", "helvetica neue", "", "  ", 7, null, "toString", "constructor"].forEach(
    (value) => {
      withTheme({ fontFamily: value }, () => {
        assert.strictEqual(
          widget.themeCss(),
          "",
          "no CSS emitted for fontFamily " + JSON.stringify(value)
        );
      });
    }
  );
  // Case and stray whitespace are forgiven, because they are typing, not intent.
  withTheme({ fontFamily: "  SERIF " }, () => {
    assert.ok(widget.themeCss().indexOf(widget.FONT_STACKS.serif) >= 0, widget.themeCss());
  });
});

test("starter questions are per-language, capped in count and length", () => {
  withTheme(
    {
      starterQuestions: {
        en: ["One?", "Two?", "Three?", "Four?", "Five?"],   // one too many
        es: ["Uno?", "x".repeat(widget.MAX_STARTER_CHARS + 1), "  Dos  ?  "]
      }
    },
    () => {
      assert.deepStrictEqual(widget.starterQuestions(), ["One?", "Two?", "Three?", "Four?"]);
      assert.strictEqual(widget.setLanguage("es", true), true);
      try {
        // The over-long one is DROPPED rather than truncated (a half question reads as a
        // bug), and whitespace is collapsed.
        assert.deepStrictEqual(widget.starterQuestions(), ["Uno?", "Dos ?"]);
      } finally {
        widget.resetLanguage();
      }
    }
  );
});

test("Spanish falls back to the customer's English list, never to a translation", () => {
  withTheme({ starterQuestions: { en: ["Where is the makerspace?"] } }, () => {
    assert.strictEqual(widget.setLanguage("es", true), true);
    try {
      // NOT the built-in Spanish questions: those may now ask about something the customer
      // removed. Their English wording is worse copy but honest, and nothing is machine
      // translated.
      assert.deepStrictEqual(widget.starterQuestions(), ["Where is the makerspace?"]);
      assert.notDeepStrictEqual(
        widget.starterQuestions(),
        widget.STRINGS.es.suggestedQuestions
      );
    } finally {
      widget.resetLanguage();
    }
  });
});

test("unusable starter questions fall all the way back to the built-in table", () => {
  [
    { starterQuestions: [] },
    { starterQuestions: ["a", "b"] },              // not keyed by language
    { starterQuestions: { en: "not a list" } },
    { starterQuestions: { en: [] } },
    { starterQuestions: { en: [null, 3, ""] } },
    { starterQuestions: { fr: ["Bonjour?"] } }     // a language the widget does not offer
  ].forEach((data) => {
    withTheme(data, () => {
      assert.deepStrictEqual(
        widget.starterQuestions(),
        widget.STRINGS.en.suggestedQuestions,
        JSON.stringify(data)
      );
    });
  });
});

test("one bad key does not take the good ones down with it", () => {
  // The realistic failure: they get the colour right and fat-finger the font.
  withTheme({ highlightColor: "#2f6f4f", fontFamily: "papyrus", starterQuestions: 5 }, () => {
    const css = widget.themeCss();
    assert.ok(/--brand:\s*#2f6f4f/.test(css), css);
    assert.ok(css.indexOf("font-family") < 0, css);
    assert.deepStrictEqual(widget.starterQuestions(), widget.STRINGS.en.suggestedQuestions);
  });
});

test("the themed panel is painted themed, in one stylesheet, on the first frame", () => {
  // No flash of the default colours: the override lands in the SAME <style> element as the
  // shipped rules, appended after them, before anything is in the document.
  withTheme({ highlightColor: "#0b5d1e" }, () => {
    const doc = makeDoc();
    const handle = widget.mount(doc);
    const styles = collectByTag(handle.shadow, "style");
    assert.strictEqual(styles.length, 1, "exactly one stylesheet");
    const css = styles[0].textContent;
    const base = css.indexOf("--brand: #8a1c30");
    const override = css.indexOf("--brand: #0b5d1e");
    assert.ok(base >= 0 && override > base, "the override comes after the shipped rule");
  });
});

test("the header's own rings and washes follow the derived ink, not a hardcoded white", () => {
  // A white focus ring on a pale highlight is invisible. Everything drawn ON the header
  // derives from --accent-ink (directly, or via currentColor for the translucent washes),
  // which is the same decision as the text colour rather than a second one.
  const RENDERED = SOURCE.slice(SOURCE.indexOf('".header {"'), SOURCE.indexOf('// thread'));
  assert.ok(RENDERED, "found the header block");
  assert.ok(
    !/rgba\(255,\s*255,\s*255/.test(RENDERED),
    "no hardcoded white wash left in the header: " + RENDERED
  );
  assert.ok(
    !/outline:\s*\d+px solid #fff/.test(RENDERED),
    "no hardcoded white ring left in the header"
  );
  assert.ok(/outline: 2px solid var\(--accent-ink\)/.test(RENDERED), RENDERED);
  // The two-tone focus ring is NOT themeable and stays exactly where it was.
  assert.ok(/--focus-ring: #1a1d21; --focus-halo: #ffffff;/.test(SOURCE));
});

test("the Google Fonts fetch happens only for the default font", () => {
  function mountWithHead() {
    const doc = makeDoc();
    doc.head = makeEl("head");
    doc.getElementById = function () { return null; };
    widget.mount(doc);
    return collectByTag(doc.head, "link").filter(function (l) {
      return (l.href || "").indexOf("fonts.googleapis.com") >= 0;
    });
  }
  // Bitter is the DEFAULT theme's title face, so any other choice replaces the title
  // family and there is nothing left to download - the request should not be made.
  assert.strictEqual(mountWithHead().length, 1, "default theme loads Bitter");
  withTheme({ fontFamily: "serif" }, () => {
    assert.strictEqual(mountWithHead().length, 0, "a themed font fetches no webfont");
  });
  withTheme({ fontFamily: "inherit" }, () => {
    assert.strictEqual(mountWithHead().length, 0, "inherit fetches no webfont");
  });
});

test("theme.json is fetched next to widget.js, with a bounded wait", () => {
  const priorDoc = global.document;
  global.document = {
    querySelector: function () {
      return { src: "https://d123.cloudfront.net/widget.js", getAttribute: function () { return null; } };
    }
  };
  try {
    assert.strictEqual(widget.themeUrl(), "https://d123.cloudfront.net/" + widget.THEME_FILE);
  } finally {
    global.document = priorDoc;
  }
  // A dead or slow CDN delays the widget appearing by this and no more.
  assert.strictEqual(typeof widget.CONFIG.themeTimeoutMs, "number");
  assert.ok(widget.CONFIG.themeTimeoutMs <= 2000, widget.CONFIG.themeTimeoutMs);
  // The mount waits for the theme, so it cannot be the plain DOMContentLoaded call it was.
  assert.ok(/Promise\.all\(\[themeSettled, domSettled\]\)/.test(SOURCE), "boot waits on both");
});

test("the theme adds no storage and no second request path", () => {
  // Same rule as the rest of the widget: nothing about a visit is written down, and the
  // theme is one GET of a static file - it never reaches /query.
  assert.ok(/fetch\(url, \{ method: "GET" \}\)/.test(SOURCE));
  const themeBlock = SOURCE.slice(
    SOURCE.indexOf("==============================  THEME"),
    SOURCE.indexOf("============================  END THEME")
  );
  assert.ok(themeBlock.length > 500, "found the theme block");
  ["localStorage", "sessionStorage", "document.cookie", "indexedDB"].forEach((sink) => {
    assert.ok(themeBlock.indexOf(sink) < 0, "theme code must not touch " + sink);
  });
});

test("defaults/theme.json is canonical, byte for byte", () => {
  // The shipped file is the source of truth for its own wording, spacing and key order,
  // so its form is pinned directly. Byte equality against its canonical serialisation:
  // two space indent, LF line endings, no BOM, one trailing newline. A hand edit that
  // reformats the file cannot land silently.
  assert.notStrictEqual(THEME_DEFAULTS_TEXT.charCodeAt(0), 0xfeff, "no BOM");
  assert.ok(THEME_DEFAULTS_TEXT.indexOf("\r") < 0, "LF only");
  assert.strictEqual(
    THEME_DEFAULTS_TEXT,
    JSON.stringify(THEME_DEFAULTS, null, 2) + "\n"
  );
  // Key order is part of the file's teaching: the _readme comes first so the
  // documentation is the first thing its reader sees, then the three settings in the
  // order the _readme explains them.
  assert.deepStrictEqual(Object.keys(THEME_DEFAULTS), [
    "_readme",
    "highlightColor",
    "fontFamily",
    "starterQuestions",
  ]);
  assert.deepStrictEqual(Object.keys(THEME_DEFAULTS.starterQuestions), ["en", "es"]);
});

test("defaults/theme.json is valid, is the shipped default, and documents itself", () => {
  // It is downloaded, edited and re-uploaded under the name it already has, so it must be
  // a plain theme.json the widget reads, not a variant with a different name or shape.
  assert.strictEqual(path.basename(THEME_DEFAULTS_PATH), "theme.json");

  // Once it is in someone's Downloads folder it is on its own, and JSON takes no
  // comments, so the _readme array IS the documentation: every key, every font keyword
  // and both caps, in the file itself. No URL: the old pointer went to GitHub, which the
  // customer cannot open, and a file that explains itself needs no pointer at all.
  assert.ok(Array.isArray(THEME_DEFAULTS._readme), "the _readme is an array of lines");
  THEME_DEFAULTS._readme.forEach((line) => assert.strictEqual(typeof line, "string"));
  const readme = THEME_DEFAULTS._readme.join("\n");
  assert.ok(readme.indexOf("theme.json") >= 0, readme);
  assert.ok(!/https?:\/\//.test(readme), "the readme must not depend on any external URL");
  ["highlightColor", "fontFamily", "starterQuestions"].forEach((key) => {
    assert.ok(readme.indexOf(key) >= 0, "the readme documents " + key);
  });
  widget.FONT_KEYWORDS.forEach((keyword) => {
    assert.ok(
      readme.indexOf('\\"' + keyword + '\\"') >= 0 || readme.indexOf('"' + keyword + '"') >= 0,
      "the readme lists the font keyword " + keyword
    );
  });
  assert.ok(
    readme.indexOf(String(widget.MAX_STARTER_CHARS)) >= 0,
    "the readme states the length cap"
  );
  assert.ok(/FOUR/.test(readme), "the readme states the count cap");

  // Every value in it equals a built-in default, so uploading it unchanged is a no-op...
  assert.strictEqual(THEME_DEFAULTS.highlightColor, widget.DEFAULT_HIGHLIGHT);
  assert.strictEqual(THEME_DEFAULTS.fontFamily, widget.DEFAULT_FONT);
  assert.deepStrictEqual(
    THEME_DEFAULTS.starterQuestions.en,
    widget.STRINGS.en.suggestedQuestions
  );
  assert.deepStrictEqual(
    THEME_DEFAULTS.starterQuestions.es,
    widget.STRINGS.es.suggestedQuestions
  );
  // ...and the widget reads it without tripping on anything, the _readme array included
  // (unknown keys are ignored whatever their type, so the docs can live in the file).
  withTheme(THEME_DEFAULTS, () => {
    assert.strictEqual(widget.themeCss(), "", "the shipped defaults are the default theme");
    assert.deepStrictEqual(widget.starterQuestions(), widget.STRINGS.en.suggestedQuestions);
  });
});

test("the theme guide page is self-contained and names only the output that exists", () => {
  // One file at the bucket root, rendered in a browser tab straight off the widget CDN.
  // Nothing external: no scripts, no fonts, no stylesheets, no URLs at all, so it can
  // never break, phone home, or depend on a host the customer's network blocks.
  assert.ok(/<html lang="en">/.test(THEME_GUIDE_PAGE), "the page declares its language");
  assert.ok(!/<script\b/i.test(THEME_GUIDE_PAGE), "no scripts");
  assert.ok(!/<link\b/i.test(THEME_GUIDE_PAGE), "no external stylesheets");
  assert.ok(!/\bsrc=/i.test(THEME_GUIDE_PAGE), "no embedded resources");
  assert.ok(!/@import|url\(/i.test(THEME_GUIDE_PAGE), "no CSS fetches");
  assert.ok(!/https?:/i.test(THEME_GUIDE_PAGE), "no absolute URLs anywhere");

  // Every link on the page is a relative path to a sibling object - the settings file,
  // and the settings editor page - which is what lets the page work on any distribution
  // domain without being stamped at deploy.
  const hrefs = THEME_GUIDE_PAGE.match(/href="[^"]*"/g) || [];
  const allowed = ['href="defaults/theme.json"', 'href="theme-editor.html"'];
  hrefs.forEach((href) => assert.ok(allowed.includes(href), href));
  allowed.forEach((href) => assert.ok(hrefs.includes(href), "missing link: " + href));

  // It teaches the CloudFormation Outputs tab, so the console link is named by its output
  // name and never described in other words. WidgetThemeUpload is the ONLY theming output
  // left besides the editor's own, and quoting a name the stack no longer emits would send
  // the reader hunting a row that is not there.
  assert.ok(THEME_GUIDE_PAGE.indexOf("WidgetThemeUpload") >= 0);
  ["WidgetThemeGuide", "WidgetThemeDownload"].forEach((gone) => {
    assert.strictEqual(
      THEME_GUIDE_PAGE.indexOf(gone), -1,
      "the guide names a deleted output: " + gone
    );
  });
  assert.ok(
    THEME_GUIDE_PAGE.indexOf("Changing the chatbot's colours and questions") >= 0,
    "the page carries its own title"
  );

  // The guide is the MANUAL version of the editor and the editor is the only route to it,
  // so it has to say so and link back. Nothing else in the product points here.
  assert.ok(
    /manual version/i.test(THEME_GUIDE_PAGE),
    "the guide frames itself as the manual route"
  );
  assert.ok(
    (THEME_GUIDE_PAGE.match(/href="theme-editor\.html"/g) || []).length >= 2,
    "the guide links back to the editor from the top and the bottom"
  );
});

// ==========================================================================
// theme-editor.html - the settings editor page
// ==========================================================================
//
// The editor is a form that writes theme.json so nobody has to hand-edit JSON, which
// means it holds COPIES of the widget's validation rules and of the shipped defaults.
// A copy is a drift hazard, so everything copied is pinned here against widget.js and
// defaults/theme.json directly: the rule tables must be equal, the embedded defaults
// must be the shipped file byte for byte, and the file the download builds must be the
// pinned serialisation and a file applyTheme accepts. The page's inline script exposes
// its rules on window.__themeEditor, and these tests run it against a stub document
// (readyState "loading", so init() never fires and no DOM is needed).

const THEME_EDITOR_PATH = path.join(__dirname, "..", "theme-editor.html");
const THEME_EDITOR_PAGE = fs.readFileSync(THEME_EDITOR_PATH, "utf8");

let cachedEditor = null;
function editorRules() {
  if (cachedEditor) return cachedEditor;
  // The page's one attribute-less <script> is the editor's own code; the JSON block
  // above it carries attributes, so this match cannot land on it.
  const m = THEME_EDITOR_PAGE.match(/<script>\n?([\s\S]*?)<\/script>/);
  assert.ok(m, "found the editor's inline script");
  const win = {};
  const doc = {
    readyState: "loading",
    addEventListener() {},
    getElementById() { return null; }
  };
  new Function("window", "document", m[1])(win, doc);
  assert.ok(win.__themeEditor, "the editor exposes its rules for pinning");
  cachedEditor = win.__themeEditor;
  return cachedEditor;
}

// What the widget actually does with a highlightColor value, read back out of the CSS it
// emits - the behavioural truth the editor's copy of normalizeHex has to match.
function widgetNormalizedHighlight(value) {
  return withTheme({ highlightColor: value }, () => {
    const m2 = widget.themeCss().match(/--brand:\s*(#[0-9a-f]{6})/);
    return m2 ? m2[1] : null;
  });
}

test("the settings editor page is self-contained; its save path is deploy-stamped", () => {
  assert.ok(/<html lang="en">/.test(THEME_EDITOR_PAGE), "the page declares its language");
  assert.ok(/<title>/.test(THEME_EDITOR_PAGE), "the page carries a title");
  // Nothing external, same rule as the guide: no embedded resources, no absolute URLs,
  // so it renders on any distribution domain and can never phone home. The save
  // endpoint and the sign-in configuration are NOT an exception: they reach the page as
  // deploy-time substitution markers in the save-config block, so the committed file
  // still names no host anywhere.
  assert.ok(!/\bsrc=/i.test(THEME_EDITOR_PAGE), "no embedded resources");
  assert.ok(!/<link\b/i.test(THEME_EDITOR_PAGE), "no external stylesheets");
  // The CSS-fetch scan is scoped to the stylesheet: the page's script legitimately says
  // URL.createObjectURL( for the download, which a page-wide /url\(/i would match.
  const styleBlock = THEME_EDITOR_PAGE.match(/<style>([\s\S]*?)<\/style>/);
  assert.ok(styleBlock, "found the stylesheet");
  assert.ok(!/@import|url\(/i.test(styleBlock[1]), "no CSS fetches");
  assert.ok(!/@import/i.test(THEME_EDITOR_PAGE), "no @import anywhere");
  assert.ok(!/https?:/i.test(THEME_EDITOR_PAGE), "no absolute URLs anywhere");

  // The save-config block: exactly the four stamped values, each marker appearing
  // exactly once in the whole page (Source.data substitutes EVERY occurrence, so a
  // second one - a comment spelling a marker out - would get stamped too). The infra
  // suite pins the other end: what each marker resolves to at deploy.
  const cfgBlock = THEME_EDITOR_PAGE.match(
    /<script type="application\/json" id="save-config">([\s\S]*?)<\/script>/
  );
  assert.ok(cfgBlock, "found the save-config block");
  const cfg = JSON.parse(cfgBlock[1]);
  assert.deepStrictEqual(
    Object.keys(cfg).sort(), ["authBase", "clientId", "cognitoIdp", "saveUrl"]
  );
  [
    "__THEME_SAVE_URL__", "__THEME_AUTH_BASE__", "__THEME_CLIENT_ID__",
    "__THEME_COGNITO_IDP__"
  ].forEach((token) => {
    assert.strictEqual(
      THEME_EDITOR_PAGE.split(token).length - 1, 1,
      token + " must appear exactly once, in the save-config block"
    );
  });

  // Storage: nothing persistent and no cookies. The one tab-scoped exception is the
  // in-flight PKCE handshake (managed login is a full-page redirect, so the verifier
  // has to survive one navigation): a single session-store key, removed on return, and
  // no token may ever be a stored value - tokens live in variables and die with the tab.
  ["localStorage", "document.cookie", "indexedDB"].forEach((sink) => {
    assert.ok(THEME_EDITOR_PAGE.indexOf(sink) < 0, "editor must not touch " + sink);
  });
  const storageCalls = THEME_EDITOR_PAGE.match(/sessionStorage\.\w+\([^),]*/g) || [];
  assert.ok(storageCalls.length > 0, "the PKCE handshake uses the session store");
  storageCalls.forEach((call) => {
    assert.ok(
      /sessionStorage\.(setItem|getItem|removeItem)\(\s*OAUTH_STORAGE_KEY/.test(call),
      "session store access outside the handshake key: " + call
    );
  });
  assert.ok(/OAUTH_STORAGE_KEY = "gavtheme-oauth"/.test(THEME_EDITOR_PAGE));
  assert.ok(
    /sessionStorage\.removeItem\(OAUTH_STORAGE_KEY\)/.test(THEME_EDITOR_PAGE),
    "the handshake is removed on return"
  );
  assert.ok(
    !/sessionStorage\.setItem\([^)]*[Tt]oken/.test(THEME_EDITOR_PAGE),
    "tokens never reach storage"
  );

  assert.ok(
    !/\.innerHTML|\.outerHTML|insertAdjacentHTML|document\.write\b/.test(THEME_EDITOR_PAGE),
    "no HTML-from-strings"
  );
  // Its links are relative paths to siblings, and the only one is the guide. Sign-in
  // navigation goes through location.assign to the stamped managed-login URL, never an
  // anchor, so no href can name a host.
  const hrefs = THEME_EDITOR_PAGE.match(/href="[^"]*"/g) || [];
  hrefs.forEach((href) => assert.strictEqual(href, 'href="theme-guide.html"'));
  // That link is the guide's ONLY route in the whole product: its stack output is gone,
  // so an editor that stopped linking it would strand the page entirely.
  assert.ok(hrefs.length >= 1, "the editor links the guide");
  // The console procedure is the GUIDE's job now, named there by its output name. The
  // editor points at the guide instead of quoting output names of its own.
  assert.strictEqual(
    THEME_EDITOR_PAGE.indexOf("WidgetThemeUpload"), -1,
    "the editor defers the console procedure to the guide"
  );
  // The no-sign-in path is intact: the committed Save button ships hidden, reading
  // "Sign in to save" - the unsigned page is the download-only editor it always was.
  assert.ok(
    /<button type="button" id="save" hidden>Sign in to save<\/button>/.test(
      THEME_EDITOR_PAGE
    ),
    "Save ships hidden and unsigned"
  );
});

test("the editor offers every setting in the shipped file, at the widget's caps", () => {
  // THE contract this page exists under: a key added to defaults/theme.json without a
  // control here would silently miss the editor, and a control without a key would edit
  // nothing. data-theme-key markers make the correspondence checkable both ways.
  const fileKeys = Object.keys(THEME_DEFAULTS).filter((k) => k !== "_readme").sort();
  const pageKeys = (THEME_EDITOR_PAGE.match(/data-theme-key="[^"]*"/g) || [])
    .map((m2) => m2.slice('data-theme-key="'.length, -1))
    .sort();
  assert.deepStrictEqual(pageKeys, fileKeys);

  // The form cannot produce what the widget would drop: exactly one input per allowed
  // question (so a fifth cannot be typed), each capped at the widget's character limit
  // (so an over-long one cannot be typed either).
  const slots = THEME_EDITOR_PAGE.match(/data-question-slot="[^"]*"/g) || [];
  assert.strictEqual(slots.length, widget.MAX_STARTER_QUESTIONS * widget.LANGUAGES.length);
  const caps = THEME_EDITOR_PAGE.match(/maxlength="120"/g) || [];
  assert.strictEqual(caps.length, slots.length, "every slot carries the widget's cap");
  assert.strictEqual(String(widget.MAX_STARTER_CHARS), "120");

  // Every font keyword is offered, and nothing else is.
  const fontValues = (THEME_EDITOR_PAGE.match(/name="fontFamily" value="[^"]*"/g) || [])
    .map((m2) => m2.match(/value="([^"]*)"/)[1]);
  assert.deepStrictEqual(fontValues.sort(), widget.FONT_KEYWORDS.slice().sort());
});

test("the editor's embedded defaults are the shipped file, byte for byte", () => {
  // The _readme of every downloaded file and the form's built-in fallback values both
  // come from this block, so it must BE frontend/defaults/theme.json, not a paraphrase.
  const m = THEME_EDITOR_PAGE.match(
    /<script type="application\/json" id="theme-defaults">([\s\S]*?)<\/script>/
  );
  assert.ok(m, "found the embedded defaults block");
  assert.strictEqual(m[1], "\n" + THEME_DEFAULTS_TEXT);
});

test("the editor validates with the widget's own rules", () => {
  const editor = editorRules();
  // The tables are copies and must stay equal - a keyword or stack that moves in
  // widget.js without moving here would preview one thing and ship another.
  assert.deepStrictEqual(editor.FONT_KEYWORDS, widget.FONT_KEYWORDS);
  assert.deepStrictEqual(editor.FONT_STACKS, widget.FONT_STACKS);
  assert.deepStrictEqual(editor.LANGUAGES, widget.LANGUAGES);
  assert.strictEqual(editor.DEFAULT_HIGHLIGHT, widget.DEFAULT_HIGHLIGHT);
  assert.strictEqual(editor.DEFAULT_FONT, widget.DEFAULT_FONT);
  assert.strictEqual(editor.MAX_STARTER_QUESTIONS, widget.MAX_STARTER_QUESTIONS);
  assert.strictEqual(editor.MAX_STARTER_CHARS, widget.MAX_STARTER_CHARS);

  // The colour rule, compared behaviourally against what applyTheme actually accepts.
  // The one value the widget reads but emits no CSS for is the default itself.
  assert.strictEqual(editor.normalizeHex("#8A1C30"), widget.DEFAULT_HIGHLIGHT);
  [
    "#1E4B8F", "#a13", "#A13", " #ffd400 ",
    "maroon", "rgb(138, 28, 48)", "8a1c30", "#8a1c3", "#gggggg", "",
    "#fff; } .panel { display: none } .x {", "var(--anything)", 42, null
  ].forEach((value) => {
    assert.strictEqual(
      editor.normalizeHex(value),
      widgetNormalizedHighlight(value),
      "normalizeHex diverges on " + JSON.stringify(value)
    );
  });

  // The ink derivation, swept across the sRGB cube like the widget's own test.
  for (let r = 0; r < 256; r += 15) {
    for (let g = 0; g < 256; g += 15) {
      for (let b = 0; b < 256; b += 15) {
        const hex = "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
        assert.strictEqual(editor.inkFor(hex), widget.inkFor(hex), hex);
      }
    }
  }

  // The question cleaning, compared behaviourally against what the widget renders.
  [
    ["One?", "Two?", "Three?", "Four?", "Five?"],
    ["  Dos  ?  ", ""],
    ["x".repeat(widget.MAX_STARTER_CHARS + 1)],
    [null, 3, ""],
    ["ok", "x".repeat(widget.MAX_STARTER_CHARS + 1), "also ok"]
  ].forEach((list) => {
    const widgetSees = withTheme({ starterQuestions: { en: list } }, () =>
      widget.starterQuestions()
    );
    const editorSees =
      editor.cleanQuestionList(list) || widget.STRINGS.en.suggestedQuestions;
    assert.deepStrictEqual(editorSees, widgetSees, JSON.stringify(list));
  });
});

test("the editor's download is the pinned serialisation, and the widget accepts it", () => {
  const editor = editorRules();
  // An untouched form downloads the shipped defaults file, byte for byte - the same
  // canonical form (key order, two-space indent, trailing newline) this suite pins on
  // defaults/theme.json itself.
  const defaultState = {
    highlight: THEME_DEFAULTS.highlightColor,
    font: THEME_DEFAULTS.fontFamily,
    questions: {
      en: THEME_DEFAULTS.starterQuestions.en.slice(),
      es: THEME_DEFAULTS.starterQuestions.es.slice()
    }
  };
  assert.strictEqual(
    editor.buildFileText(defaultState, THEME_DEFAULTS._readme),
    THEME_DEFAULTS_TEXT
  );

  // An edited form downloads a file the widget reads back EXACTLY as edited.
  const edited = editor.buildFileText(
    { highlight: "#1e4b8f", font: "serif", questions: { en: ["Where is the makerspace?"], es: [] } },
    THEME_DEFAULTS._readme
  );
  assert.ok(edited.endsWith("}\n"), "one trailing newline");
  const parsed = JSON.parse(edited);
  assert.deepStrictEqual(Object.keys(parsed), [
    "_readme", "highlightColor", "fontFamily", "starterQuestions"
  ]);
  assert.deepStrictEqual(parsed._readme, THEME_DEFAULTS._readme);
  // A language with no questions is OMITTED, never written as an empty list: an absent
  // "es" is what makes the widget fall back to the customer's English questions.
  assert.deepStrictEqual(Object.keys(parsed.starterQuestions), ["en"]);
  withTheme(parsed, () => {
    const css = widget.themeCss();
    assert.ok(/--brand:\s*#1e4b8f/.test(css), css);
    assert.ok(css.indexOf(widget.FONT_STACKS.serif) >= 0, css);
    assert.deepStrictEqual(widget.starterQuestions(), ["Where is the makerspace?"]);
  });

  // No questions at all means NO starterQuestions block - the file shape that keeps the
  // widget's built-in questions in both languages.
  const noQuestions = JSON.parse(
    editor.buildFileText(
      { highlight: "#1e4b8f", font: "system", questions: { en: ["", "   "], es: [] } },
      THEME_DEFAULTS._readme
    )
  );
  assert.ok(!("starterQuestions" in noQuestions), JSON.stringify(noQuestions));
  withTheme(noQuestions, () => {
    assert.deepStrictEqual(widget.starterQuestions(), widget.STRINGS.en.suggestedQuestions);
  });
});

test("the editor seeds its form the way the widget reads a live file", () => {
  const editor = editorRules();
  const base = {
    highlight: widget.DEFAULT_HIGHLIGHT,
    font: widget.DEFAULT_FONT,
    questions: {
      en: THEME_DEFAULTS.starterQuestions.en.slice(),
      es: THEME_DEFAULTS.starterQuestions.es.slice()
    }
  };

  // A file that customises one key keeps the live behaviour for the rest.
  const colourOnly = editor.stateFrom({ highlightColor: "#1E4B8F" }, base);
  assert.strictEqual(colourOnly.changed, true);
  assert.strictEqual(colourOnly.state.highlight, "#1e4b8f");
  assert.strictEqual(colourOnly.state.font, widget.DEFAULT_FONT);
  assert.deepStrictEqual(colourOnly.state.questions.en, base.questions.en);

  // A usable starterQuestions block replaces BOTH language lists: the widget shows a
  // missing language by falling back to English at render time, so seeding its fields
  // with built-in Spanish would download a file that CHANGES the live behaviour.
  const enOnly = editor.stateFrom(
    { starterQuestions: { en: ["Where is the makerspace?"] } }, base
  );
  assert.deepStrictEqual(enOnly.state.questions.en, ["Where is the makerspace?"]);
  assert.deepStrictEqual(enOnly.state.questions.es, []);

  // Per-key soft-fail, same as applyTheme: a bad value costs that value and nothing else.
  const fatFingered = editor.stateFrom(
    { highlightColor: "maroon", fontFamily: "serif", starterQuestions: 5 }, base
  );
  assert.strictEqual(fatFingered.state.highlight, widget.DEFAULT_HIGHLIGHT);
  assert.strictEqual(fatFingered.state.font, "serif");
  // ...and a file that is not an object at all changes nothing.
  assert.strictEqual(editor.stateFrom([1, 2, 3], base).changed, false);
  assert.strictEqual(editor.stateFrom(null, base).changed, false);
});

// The main view is the everyday job - the fields, the preview, Save - and everything else
// is one control away. Once Save wrote the live file the console upload stopped being the
// workflow, so the sentences teaching it are gone rather than reworded.

// Everything the page renders before the panel, comments stripped: the scans below are
// about the copy a customer reads, and a comment is neither rendered nor read aloud.
const EDITOR_MAIN_VIEW = THEME_EDITOR_PAGE
  .split("<body>")[1]
  .split("<dialog")[0]
  .replace(/<!--[\s\S]*?-->/g, "");
const EDITOR_PANEL = (THEME_EDITOR_PAGE.match(
  /<dialog id="settings-panel"[^>]*>([\s\S]*?)<\/dialog>/
) || [])[1];

test("the main view carries the form, the preview and Save, and no file handling", () => {
  ["No uploaded settings file found",
   "Save puts your settings on the live widget",
   "walks through that step",
   "upload it through the same"].forEach((gone) => {
    assert.ok(THEME_EDITOR_PAGE.indexOf(gone) < 0, "still on the page: " + gone);
  });

  // Nothing in the main view names the file or teaches moving it around. ("downloaded"
  // survives in the font hint, which is about fonts, not files.)
  assert.ok(EDITOR_MAIN_VIEW, "found the main view");
  assert.ok(!/theme\.json/i.test(EDITOR_MAIN_VIEW), "the main view names no file");
  assert.ok(!/upload/i.test(EDITOR_MAIN_VIEW), "no upload text in the main view");
  assert.ok(!/\bdownload\b/i.test(EDITOR_MAIN_VIEW), "no download text in the main view");

  // What stays: the three settings, the preview, Save, and the unsigned indication.
  ["data-theme-key=", "id=\"pv-root\"", "id=\"load-status\"",
   '<button type="button" id="save" hidden>Sign in to save</button>'].forEach((kept) => {
    assert.ok(EDITOR_MAIN_VIEW.indexOf(kept) >= 0, "missing from the main view: " + kept);
  });
});

test("the download and account controls are reachable only through the settings control", () => {
  assert.ok(EDITOR_PANEL, "found the settings panel");
  // The panel ships closed - a <dialog> with no open attribute renders nothing and
  // holds nothing focusable - and one control opens it.
  assert.ok(
    !/<dialog id="settings-panel"[^>]*\sopen[\s>]/.test(THEME_EDITOR_PAGE),
    "the panel must ship closed"
  );
  assert.ok(
    /<button type="button" class="minor" id="settings-open"[\s\S]*?aria-controls="settings-panel">/
      .test(EDITOR_MAIN_VIEW),
    "the opener is a real button in the main view, pointed at the panel"
  );
  assert.ok(/aria-expanded="false"/.test(EDITOR_MAIN_VIEW), "the opener ships collapsed");

  // Every control the main view lost is in the panel, and each id exists exactly once on
  // the page - so there is no second route to any of them.
  ["download", "use-defaults", "account", "sign-out", "pw-change", "pw-current", "pw-new",
   "email-change", "email-confirm", "email-new", "email-code"].forEach((id) => {
    const marker = 'id="' + id + '"';
    assert.ok(EDITOR_PANEL.indexOf(marker) >= 0, id + " must live in the panel");
    assert.strictEqual(
      THEME_EDITOR_PAGE.split(marker).length - 1, 1, id + " appears more than once"
    );
  });

  // Keyboard: showModal is what gives Escape and the focus trap; the close handler puts
  // focus back on the opener, whichever way the panel was dismissed.
  assert.ok(/panel\.showModal\(\)/.test(THEME_EDITOR_PAGE), "opened modally");
  assert.ok(
    /panel\.addEventListener\("close", function \(\) \{[\s\S]*?panelOpener\.focus\(\);/
      .test(THEME_EDITOR_PAGE),
    "closing returns focus to the opener"
  );
  assert.ok(/id="settings-close"/.test(EDITOR_PANEL), "and there is a visible Close");
});

test("Choose defaults refills the form from the shipped file and saves nothing", () => {
  const editor = editorRules();
  const handler = THEME_EDITOR_PAGE.match(
    /getElementById\("use-defaults"\)\.addEventListener\("click", function \(\) \{([\s\S]*?)\n    \}\);/
  );
  assert.ok(handler, "found the Choose defaults handler");
  // It seeds from builtinState, which is read out of the embedded defaults block above -
  // so the page holds no second copy of the shipped values to drift.
  assert.ok(/seedForm\(builtinState\)/.test(handler[1]), handler[1]);
  assert.ok(!/fetch\(|saveUrl/.test(handler[1]), "refilling must not save");

  // seedForm writes every field there is, so a refill leaves nothing of the old theme.
  const seed = THEME_EDITOR_PAGE.match(/function seedForm\(state\) \{([\s\S]*?)\n    \}/);
  assert.ok(seed, "found seedForm");
  ["picker.value = state.highlight", "hexInput.value = state.highlight",
   'input[name="fontFamily"]', "slots[lang][i].value = state.questions[lang][i]"]
    .forEach((piece) => assert.ok(seed[1].indexOf(piece) >= 0, "seedForm misses " + piece));

  // And the state it seeds IS the shipped file, read through the widget's own rules.
  const shipped = editor.stateFrom(THEME_DEFAULTS, {
    highlight: editor.DEFAULT_HIGHLIGHT,
    font: editor.DEFAULT_FONT,
    questions: { en: [], es: [] }
  }).state;
  assert.deepStrictEqual(shipped, {
    highlight: THEME_DEFAULTS.highlightColor,
    font: THEME_DEFAULTS.fontFamily,
    questions: {
      en: THEME_DEFAULTS.starterQuestions.en,
      es: THEME_DEFAULTS.starterQuestions.es
    }
  });
});

test("an unsigned visitor still downloads the shipped defaults, byte for byte", () => {
  const editor = editorRules();
  // Unsigned, the committed page hides Save and the account group, so the panel's
  // download is the only way out - it moved, it did not disappear, and it is never
  // hidden or disabled in the markup.
  assert.ok(
    /<button type="button" id="save" hidden>Sign in to save<\/button>/.test(THEME_EDITOR_PAGE)
  );
  assert.ok(/<section class="panel-group" id="account" hidden>/.test(THEME_EDITOR_PAGE));
  assert.ok(
    /<button type="button" id="download">Download theme\.json<\/button>/.test(EDITOR_PANEL),
    "the download button is plain, visible markup"
  );

  // The whole unsigned path end to end: an untouched load seeds from the shipped
  // defaults file and downloading it returns those bytes unchanged.
  const seeded = editor.stateFrom(THEME_DEFAULTS, {
    highlight: editor.DEFAULT_HIGHLIGHT,
    font: editor.DEFAULT_FONT,
    questions: { en: [], es: [] }
  }).state;
  assert.strictEqual(
    editor.buildFileText(seeded, THEME_DEFAULTS._readme), THEME_DEFAULTS_TEXT
  );
});

run();
