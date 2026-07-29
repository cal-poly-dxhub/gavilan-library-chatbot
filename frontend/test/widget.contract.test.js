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
test("source uses NO browser storage (localStorage/sessionStorage)", () => {
  assert.ok(!/\blocalStorage\b/.test(SOURCE), "must not use localStorage");
  assert.ok(!/\bsessionStorage\b/.test(SOURCE), "must not use sessionStorage");
  assert.ok(!/\bindexedDB\b/i.test(SOURCE), "must not use indexedDB");
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

// --- softened focus ring on the composer --------------------------------
test("the text box focus ring is a softened tint of the brand, not full-strength", () => {
  assert.ok(
    /\.composer__input:focus-visible\s*\{[\s\S]*color-mix\(in srgb, var\(--brand\)/.test(SOURCE),
    "composer focus outline uses a translucent color-mix of --brand"
  );
  // It must NOT be the old full-strength solid accent outline.
  assert.ok(
    !/\.composer__input:focus-visible\s*\{\s*outline:\s*2px solid var\(--accent\)/.test(SOURCE),
    "no full-strength solid accent outline remains"
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

run();
