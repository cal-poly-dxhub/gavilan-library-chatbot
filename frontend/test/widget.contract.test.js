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

test("a delayed 'waking up' hint backs a slow first response (no fake progress)", () => {
  assert.ok(typeof widget.CONFIG.wakingHintDelayMs === "number", "hint delay is configurable");
  assert.ok(
    widget.CONFIG.wakingHintDelayMs > 0 &&
      widget.CONFIG.wakingHintDelayMs < widget.CONFIG.requestTimeoutMs,
    "the hint must appear before the request would time out"
  );
  // A real element revealed on a timer, not a fake progress bar.
  assert.ok(/typing__hint/.test(SOURCE), "the typing hint element exists");
  assert.ok(/Waking up/i.test(SOURCE), "honest 'waking up' copy is present");
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
