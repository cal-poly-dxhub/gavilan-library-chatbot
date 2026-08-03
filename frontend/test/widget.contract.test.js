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
// querySelectorAll + closest, the document-level Escape needs addEventListener on the
// document, and the first-launch description needs removeAttribute. This augments a
// fake doc for the tests below rather than changing the fake the rest of the file uses.
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
  el.removeAttribute = function (k) { delete el.attrs[k]; };
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

// Like withNetwork, but with a real attribute map on the script tag and a fetch that can tell
// the Cognito call apart from the /query call.
function withGate(attrs, fetchImpl) {
  var priorDoc = global.document;
  var priorFetch = global.fetch;
  var priorErr = console.error;
  var calls = [];
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
    cognito: function () {
      return calls.filter(function (c) { return /cognito-idp/.test(c.url); });
    },
    query: function () {
      return calls.filter(function (c) { return /\/query$/.test(c.url); });
    },
    restore: function () {
      global.document = priorDoc;
      global.fetch = priorFetch;
      console.error = priorErr;
      widget.signOut();
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
    var ok = await widget.signIn("someone@example.com", "hunter2-correct-horse");
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
    assert.strictEqual(body.AuthParameters.USERNAME, "someone@example.com");

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
      await widget.signIn("someone@example.com", "wrong");
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
      await widget.signIn("someone@example.com", "temp-password");
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
      await widget.signIn("someone@example.com", "whatever");
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
    await widget.signIn("someone@example.com", "pw");
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

test("the widget keeps no token or password anywhere a reload could find it", () => {
  // The storage scan above covers the API surface; this covers the handle. Nothing the widget
  // exposes can read a token back out, so nothing downstream can persist one by accident.
  assert.ok(!("accessToken" in widget), "no token getter is exported");
  assert.ok(!("session" in widget), "no session object is exported");
  assert.ok(typeof widget.signedIn === "function" && typeof widget.signOut === "function");
  // The refresh token is fetched and dropped: it appears nowhere in the source.
  assert.ok(!/RefreshToken/.test(SOURCE), "the refresh token is never read");
});

// --- the panel while gated ----------------------------------------------

test("a gated, signed-out panel shows sign-in in the composer's place and offers no dead buttons", () => {
  var g = withGate(GATE_ATTRS, function () { return Promise.reject(new Error("x")); });
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    var signin = findByClass(handle.shadow, "signin");
    var composer = findByClass(handle.shadow, "composer");
    assert.ok(signin, "the sign-in form is in the panel");
    assert.strictEqual(signin.hidden, false, "and it is the one on screen");
    assert.strictEqual(composer.hidden, true, "the composer is not");
    // The greeting still shows - it is what says WHAT you are signing in for - but the starter
    // questions do not, because each is a button that would submit nothing.
    assert.ok(/Gavilan College Library assistant/.test(allText(handle.shadow)), "greeting is present");
    assert.strictEqual(findByClass(handle.shadow, "suggestions"), null, "no starter questions yet");
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
    assert.strictEqual(fields[0].getAttribute("type"), "email");
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

test("signing in swaps the form for the composer and releases the starter questions", async () => {
  var g = withGate(GATE_ATTRS, routeGate(cognitoOk("TOKEN-A"), { answer: "hi", sources: [] }, []));
  try {
    var doc = makeDoc();
    var handle = widget.mount(doc);
    var fields = findAll(handle.shadow, "signin__input");
    fields[0].value = "someone@example.com";
    fields[1].value = "a-password";
    handle.signInForm.fire("submit");
    await new Promise(function (r) { setTimeout(r, 0); });

    assert.strictEqual(widget.signedIn(), true);
    assert.strictEqual(findByClass(handle.shadow, "signin").hidden, true);
    assert.strictEqual(findByClass(handle.shadow, "composer").hidden, false);
    assert.ok(findByClass(handle.shadow, "suggestions"), "the starter questions arrive with the session");
    // The password is cleared from the DOM the moment it is no longer needed.
    assert.strictEqual(fields[1].value, "", "the password field is emptied");
    assert.strictEqual(fields[0].value, "someone@example.com", "the email is not, so nobody retypes it");
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
    fields[0].value = "someone@example.com";
    fields[1].value = "wrong";
    handle.signInForm.fire("submit");
    await new Promise(function (r) { setTimeout(r, 0); });

    var err = findByClass(handle.shadow, "signin__error");
    assert.strictEqual(err.hidden, false, "the alert is shown");
    assert.strictEqual(err.textContent, widget.STRINGS.en.signInFailed);
    assert.strictEqual(fields[1].value, "", "the password does not sit in the form for the next person");
    assert.strictEqual(findByClass(handle.shadow, "signin").hidden, false, "still gated");
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
    assert.strictEqual(labels[0].textContent, widget.STRINGS.es.signInEmailLabel);
    assert.strictEqual(labels[1].textContent, widget.STRINGS.es.signInPasswordLabel);
  } finally {
    widget.resetLanguage();
    g.restore();
  }
});

run();
