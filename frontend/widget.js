/*
 * Gavilan College Library — embeddable chat widget (v1)
 * ---------------------------------------------------------------------------
 * Vanilla JS, dependency-free, self-contained. The library embeds it with a
 * single tag on their existing site:
 *
 *     <script src="https://.../widget.js" defer></script>
 *
 * The whole UI lives inside an open Shadow DOM, so the host page's CSS cannot
 * break the widget and the widget's CSS cannot leak into the host page.
 *
 * It talks to the backend HTTP API's `POST /query` route and renders the
 * locked response contract:
 *     { "answer": "<text>", "sources": [ { "uri": "...", "excerpt": "..." } ] }
 *
 * No browser storage is used; conversation state is kept in memory for the
 * session only.
 * ===========================================================================
 */
(function () {
  "use strict";

  // =========================================================================
  // ============================  API LAYER  ================================
  // =========================================================================
  //
  // THE ONE SWAP POINT: the endpoint is read from this script tag's
  // `data-api-url` attribute. To go live, point it at the deployed API Gateway
  // `/query` URL:
  //
  //   <script src="https://.../widget.js"
  //           data-api-url="https://abc123.execute-api.us-west-2.amazonaws.com/query"
  //           defer></script>
  //
  // Until `data-api-url` is set, sendQuery() returns a graceful
  // "not connected yet" message. In local development the demo page transparently
  // intercepts fetch() to this same URL to serve canned answers; widget.js has
  // no knowledge of that and ships only the real path.
  // -------------------------------------------------------------------------
  var CONFIG = {
    // Abort a hung request after this long. Aligned to API Gateway's hard 30s integration
    // cap: past 30s the gateway kills the request anyway, so the browser should outlive the
    // backend right up to that ceiling rather than aborting early (finding 1.3). A cold
    // first query (OpenSearch scale-to-zero wake + generation) can take 15-25s.
    requestTimeoutMs: 30000,
    // After this long with no response, the typing indicator gains an honest "waking up"
    // note so a slow first query doesn't look frozen.
    wakingHintDelayMs: 4000,
    title: "Library Help",
    launcherLabel: "Ask the Library",
    greeting:
      "Hi! I'm the Gavilan College Library assistant. I can help with hours, " +
      "checking out materials, textbooks, and what the library offers. " +
      "What can I help you find?"
  };

  // Capture the script element at load time (before the deferred mount runs),
  // so `document.currentScript` still resolves; fall back to a DOM query.
  var CURRENT_SCRIPT =
    (typeof document !== "undefined" && document.currentScript) || null;

  /** The configured backend endpoint, or null if `data-api-url` is unset. */
  function apiUrl() {
    var el =
      CURRENT_SCRIPT ||
      (typeof document !== "undefined"
        ? document.querySelector("script[data-api-url]")
        : null);
    var url = el && el.getAttribute ? el.getAttribute("data-api-url") : null;
    return url && url.trim() ? url.trim() : null;
  }

  /**
   * The single entry point the UI calls. Returns a Promise for the locked
   * { answer, sources } contract. Always a normal fetch to the configured URL.
   */
  function sendQuery(question) {
    var url = apiUrl();
    if (!url) {
      return Promise.resolve({
        answer:
          "The library assistant isn't connected yet. Please try again later.",
        sources: []
      });
    }
    return realQuery(url, question);
  }

  /**
   * Real backend call. Matches app/handler.py: POST JSON { "query": <text> }
   * to `/query`; expects { "answer", "sources": [{ uri, excerpt }] } back.
   */
  function realQuery(url, question) {
    var controller =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = null;
    if (controller) {
      timer = setTimeout(function () {
        controller.abort();
      }, CONFIG.requestTimeoutMs);
    }
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: question }),
      signal: controller ? controller.signal : undefined
    })
      .then(function (res) {
        if (!res.ok) {
          throw new Error("Backend returned HTTP " + res.status);
        }
        return res.json();
      })
      .then(normalizeResponse)
      .finally(function () {
        if (timer) clearTimeout(timer);
      });
  }

  /**
   * Coerce any backend payload into the exact { answer, sources } shape the UI
   * renders, dropping malformed entries. Defensive: never trust the wire shape.
   */
  function normalizeResponse(data) {
    var answer =
      data && typeof data.answer === "string" ? data.answer : "";
    var rawSources =
      data && Array.isArray(data.sources) ? data.sources : [];
    var sources = [];
    for (var i = 0; i < rawSources.length; i++) {
      var s = rawSources[i];
      if (s && typeof s.uri === "string" && s.uri) {
        sources.push({
          uri: s.uri,
          excerpt: typeof s.excerpt === "string" ? s.excerpt : ""
        });
      }
    }
    return { answer: answer, sources: sources };
  }

  /**
   * Derive the sibling /warm URL from the configured /query endpoint. data-api-url points
   * at the /query route, so /warm is its sibling. Returns null for an unusable input.
   */
  function warmUrl(url) {
    if (typeof url !== "string" || !url) return null;
    if (/\/query\/?$/.test(url)) return url.replace(/\/query\/?$/, "/warm");
    // Fallback: treat the value as a base and hang /warm off it.
    return url.replace(/\/+$/, "") + "/warm";
  }

  /**
   * Fire-and-forget pre-warm. Pings GET /warm on load to wake the retrieval/OpenSearch path
   * (which scales to zero after idle) before the student's first real query. We never read
   * the result and swallow every failure (network, CORS, abort): warming is best-effort and
   * must never affect the UI. Deliberately warms ONLY retrieval, not generation.
   */
  function warmBackend() {
    var url = warmUrl(apiUrl());
    if (!url || typeof fetch === "undefined") return;
    try {
      fetch(url, { method: "GET" }).then(noop, noop);
    } catch (e) {
      /* ignore: best-effort only */
    }
  }

  function noop() {}

  // =========================================================================
  // ==========================  END API LAYER  ==============================
  // =========================================================================

  // ---- small helpers (no dependencies) ------------------------------------

  /**
   * Return a safe absolute http(s) URL string, or null. Blocks javascript:,
   * data:, and other schemes so a crawled/model-supplied uri can never become
   * a dangerous href. Relative URLs are rejected (crawler sources are absolute).
   */
  function safeHttpUrl(uri) {
    if (typeof uri !== "string" || !uri) return null;
    try {
      var u = new URL(uri);
      if (u.protocol === "http:" || u.protocol === "https:") return u.href;
    } catch (e) {
      /* not a parseable absolute URL */
    }
    return null;
  }

  /** Compact, human-readable label for a source link (host + path). */
  function displayUrl(uri) {
    try {
      var u = new URL(uri);
      var path = u.pathname.replace(/\/$/, "");
      var label = u.host + path;
      return label.length > 48 ? label.slice(0, 47) + "…" : label;
    } catch (e) {
      return uri;
    }
  }

  // ---- styles (scoped inside the shadow root) -----------------------------

  var STYLES = [
    ":host { all: initial; }",
    "*, *::before, *::after { box-sizing: border-box; }",
    ".root {",
    "  --accent: #1f4e79; --accent-ink: #ffffff;",
    "  --bg: #ffffff; --panel-border: #d9dee5;",
    "  --user-bg: #1f4e79; --user-ink: #ffffff;",
    "  --bot-bg: #f1f3f6; --bot-ink: #1a1d21;",
    "  --muted: #5b6570; --error-bg: #fdecea; --error-ink: #8a1c12;",
    "  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;",
    "  font-size: 15px; line-height: 1.45; color: var(--bot-ink);",
    // Inherited properties (text-transform, letter-spacing) cross the shadow
    // boundary. `:host { all: initial }` loses to the outer page's normal
    // declarations on the host element, so re-neutralize them here on .root
    // (the outer page cannot target .root, so this wins and stops inheritance).
    "  text-transform: none; letter-spacing: normal;",
    "}",
    // launcher
    ".launcher {",
    "  position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;",
    "  display: inline-flex; align-items: center; gap: 8px;",
    "  padding: 12px 18px; border: none; border-radius: 999px;",
    "  background: var(--accent); color: var(--accent-ink);",
    "  font-size: 15px; font-weight: 600; cursor: pointer;",
    "  box-shadow: 0 6px 20px rgba(0,0,0,0.22);",
    "}",
    ".launcher:hover { filter: brightness(1.07); }",
    ".launcher:focus-visible { outline: 3px solid #9ec5ff; outline-offset: 2px; }",
    ".launcher__icon { font-size: 18px; line-height: 1; }",
    // panel
    ".panel {",
    "  position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;",
    "  display: flex; flex-direction: column;",
    "  width: min(384px, calc(100vw - 32px));",
    "  height: min(560px, calc(100vh - 64px));",
    "  background: var(--bg); border: 1px solid var(--panel-border);",
    "  border-radius: 14px; overflow: hidden;",
    "  box-shadow: 0 12px 40px rgba(0,0,0,0.24);",
    "}",
    ".panel[hidden], .launcher[hidden] { display: none !important; }",
    // header
    ".header {",
    "  display: flex; align-items: center; justify-content: space-between;",
    "  padding: 12px 14px; background: var(--accent); color: var(--accent-ink);",
    "}",
    ".header__title { font-size: 15px; font-weight: 600; }",
    ".header__close {",
    "  appearance: none; border: none; background: transparent;",
    "  color: var(--accent-ink); font-size: 22px; line-height: 1;",
    "  cursor: pointer; padding: 2px 6px; border-radius: 6px;",
    "}",
    ".header__close:hover { background: rgba(255,255,255,0.18); }",
    ".header__close:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }",
    // thread
    ".thread {",
    "  flex: 1 1 auto; overflow-y: auto; padding: 14px;",
    "  display: flex; flex-direction: column; gap: 10px; background: #fafbfc;",
    "}",
    ".msg { display: flex; }",
    ".msg--user { justify-content: flex-end; }",
    ".msg--bot { justify-content: flex-start; }",
    ".bubble {",
    "  max-width: 82%; padding: 9px 12px; border-radius: 14px;",
    "  white-space: pre-wrap; word-wrap: break-word; overflow-wrap: anywhere;",
    "}",
    ".msg--user .bubble { background: var(--user-bg); color: var(--user-ink); border-bottom-right-radius: 4px; }",
    ".msg--bot .bubble { background: var(--bot-bg); color: var(--bot-ink); border-bottom-left-radius: 4px; }",
    ".bubble--error { background: var(--error-bg); color: var(--error-ink); }",
    // sources
    ".sources { margin-top: 8px; padding-top: 8px; border-top: 1px solid #dfe3e8; }",
    ".sources__label {",
    "  font-size: 11px; font-weight: 700; letter-spacing: .04em;",
    "  text-transform: uppercase; color: var(--muted); margin-bottom: 6px;",
    "}",
    ".sources__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }",
    ".sources__link { color: var(--accent); font-weight: 600; font-size: 13px; text-decoration: none; word-break: break-word; }",
    ".sources__link:hover { text-decoration: underline; }",
    ".sources__link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }",
    ".sources__excerpt { color: var(--muted); font-size: 12.5px; margin-top: 2px; }",
    // typing indicator
    ".typing { display: inline-flex; gap: 4px; align-items: center; padding: 4px 2px; }",
    ".typing .dot { width: 7px; height: 7px; border-radius: 50%; background: #9aa4b0; animation: gv-blink 1.2s infinite ease-in-out; }",
    ".typing .dot:nth-child(2) { animation-delay: .2s; }",
    ".typing .dot:nth-child(3) { animation-delay: .4s; }",
    "@keyframes gv-blink { 0%, 80%, 100% { opacity: .3; } 40% { opacity: 1; } }",
    "@media (prefers-reduced-motion: reduce) { .typing .dot { animation: none; } }",
    // honest slow-start hint (shown only after a delay under the typing dots)
    ".typing__hint { margin-top: 6px; font-size: 12.5px; color: var(--muted); }",
    ".typing__hint[hidden] { display: none; }",
    // retry
    ".retry {",
    "  margin-top: 8px; appearance: none; border: 1px solid currentColor;",
    "  background: transparent; color: var(--error-ink); cursor: pointer;",
    "  font-size: 13px; font-weight: 600; padding: 4px 10px; border-radius: 8px;",
    "}",
    ".retry:hover { background: rgba(138,28,18,0.08); }",
    // composer
    ".composer {",
    "  display: flex; gap: 8px; align-items: flex-end;",
    "  padding: 10px; border-top: 1px solid var(--panel-border); background: #fff;",
    "}",
    ".composer__input {",
    "  flex: 1 1 auto; resize: none; max-height: 120px; min-height: 40px;",
    "  padding: 9px 11px; border: 1px solid #c8cfd8; border-radius: 10px;",
    "  font: inherit; color: inherit; background: #fff;",
    "}",
    ".composer__input:focus-visible { outline: 2px solid var(--accent); outline-offset: 0; border-color: var(--accent); }",
    ".composer__send {",
    "  flex: 0 0 auto; appearance: none; border: none; border-radius: 10px;",
    "  background: var(--accent); color: var(--accent-ink); cursor: pointer;",
    "  font-weight: 600; font-size: 14px; padding: 0 16px; height: 40px;",
    "}",
    ".composer__send:hover:not(:disabled) { filter: brightness(1.07); }",
    ".composer__send:disabled { opacity: .5; cursor: default; }",
    ".composer__send:focus-visible { outline: 3px solid #9ec5ff; outline-offset: 2px; }",
    // visually-hidden (for a11y live region labels)
    ".sr-only {",
    "  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;",
    "  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;",
    "}"
  ].join("\n");

  // ---- widget construction ------------------------------------------------

  var HOST_ID = "gavilan-chatbot-widget-host";

  /**
   * Build and wire the widget into `doc` (defaults to the global document).
   * Returns a small handle for tests. Idempotent: a second call is a no-op.
   */
  function mount(doc) {
    doc = doc || (typeof document !== "undefined" ? document : null);
    if (!doc || !doc.body) return null;
    if (doc.getElementById(HOST_ID)) return null; // already mounted

    var host = doc.createElement("div");
    host.id = HOST_ID;
    doc.body.appendChild(host);
    var shadow = host.attachShadow({ mode: "open" });

    var style = doc.createElement("style");
    style.textContent = STYLES;
    shadow.appendChild(style);

    var root = doc.createElement("div");
    root.className = "root";
    shadow.appendChild(root);

    // in-memory session state (no storage)
    var state = { open: false, pending: false, messages: [], lastQuestion: null };

    // launcher
    var launcher = doc.createElement("button");
    launcher.type = "button";
    launcher.className = "launcher";
    launcher.setAttribute("aria-label", "Open the library chat");
    var lIcon = doc.createElement("span");
    lIcon.className = "launcher__icon";
    lIcon.setAttribute("aria-hidden", "true");
    lIcon.textContent = "💬"; // speech balloon
    var lText = doc.createElement("span");
    lText.textContent = CONFIG.launcherLabel;
    launcher.appendChild(lIcon);
    launcher.appendChild(lText);
    root.appendChild(launcher);

    // panel
    var panel = doc.createElement("section");
    panel.className = "panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Gavilan College Library chat");
    panel.hidden = true;

    var header = doc.createElement("div");
    header.className = "header";
    var hTitle = doc.createElement("span");
    hTitle.className = "header__title";
    hTitle.textContent = CONFIG.title;
    var hClose = doc.createElement("button");
    hClose.type = "button";
    hClose.className = "header__close";
    hClose.setAttribute("aria-label", "Close chat");
    hClose.textContent = "×"; // ×
    header.appendChild(hTitle);
    header.appendChild(hClose);

    var thread = doc.createElement("div");
    thread.className = "thread";
    thread.setAttribute("role", "log");
    thread.setAttribute("aria-live", "polite");
    thread.setAttribute("aria-relevant", "additions");
    thread.setAttribute("aria-label", "Conversation");

    var form = doc.createElement("form");
    form.className = "composer";
    var input = doc.createElement("textarea");
    input.className = "composer__input";
    input.setAttribute("rows", "1");
    input.setAttribute("aria-label", "Type your question");
    input.setAttribute("placeholder", "Ask a question…");
    var send = doc.createElement("button");
    send.type = "submit";
    send.className = "composer__send";
    send.textContent = "Send";
    send.setAttribute("aria-label", "Send message");
    form.appendChild(input);
    form.appendChild(send);

    panel.appendChild(header);
    panel.appendChild(thread);
    panel.appendChild(form);
    root.appendChild(panel);

    // ---- rendering ----

    function scrollToBottom() {
      thread.scrollTop = thread.scrollHeight;
    }

    function appendUserMessage(text) {
      state.messages.push({ role: "user", text: text });
      var wrap = doc.createElement("div");
      wrap.className = "msg msg--user";
      var bubble = doc.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = text; // text node only — never innerHTML
      wrap.appendChild(bubble);
      thread.appendChild(wrap);
      scrollToBottom();
    }

    function buildSources(sources) {
      var container = doc.createElement("div");
      container.className = "sources";
      var label = doc.createElement("div");
      label.className = "sources__label";
      label.textContent = sources.length > 1 ? "Sources" : "Source";
      container.appendChild(label);
      var list = doc.createElement("ul");
      list.className = "sources__list";
      for (var i = 0; i < sources.length; i++) {
        var src = sources[i];
        var href = safeHttpUrl(src.uri);
        var li = doc.createElement("li");
        if (href) {
          var a = doc.createElement("a");
          a.className = "sources__link";
          a.href = href;
          a.target = "_blank";
          a.rel = "noopener noreferrer nofollow";
          a.textContent = displayUrl(href);
          li.appendChild(a);
        } else {
          // Non-http(s) uri: never linkify; show as plain text.
          var span = doc.createElement("span");
          span.className = "sources__link";
          span.textContent = displayUrl(src.uri);
          li.appendChild(span);
        }
        if (src.excerpt) {
          var ex = doc.createElement("div");
          ex.className = "sources__excerpt";
          ex.textContent = src.excerpt; // text node only
          li.appendChild(ex);
        }
        list.appendChild(li);
      }
      container.appendChild(list);
      return container;
    }

    function appendBotMessage(answer, sources) {
      state.messages.push({ role: "bot", text: answer, sources: sources });
      var wrap = doc.createElement("div");
      wrap.className = "msg msg--bot";
      var bubble = doc.createElement("div");
      bubble.className = "bubble";
      var textEl = doc.createElement("div");
      textEl.textContent =
        answer && answer.trim()
          ? answer
          : "Sorry, I didn't get a response. Please try again.";
      bubble.appendChild(textEl);
      if (sources && sources.length) {
        bubble.appendChild(buildSources(sources));
      }
      wrap.appendChild(bubble);
      thread.appendChild(wrap);
      scrollToBottom();
    }

    function showTyping() {
      var wrap = doc.createElement("div");
      wrap.className = "msg msg--bot";
      wrap.setAttribute("data-typing", "1");
      var bubble = doc.createElement("div");
      bubble.className = "bubble";
      var typing = doc.createElement("div");
      typing.className = "typing";
      typing.setAttribute("aria-label", "Assistant is typing");
      typing.setAttribute("role", "status");
      for (var i = 0; i < 3; i++) {
        var dot = doc.createElement("span");
        dot.className = "dot";
        dot.setAttribute("aria-hidden", "true");
        typing.appendChild(dot);
      }
      bubble.appendChild(typing);

      // Honest slow-start note: a first query after idle can spend 15-25s waking the
      // backend (OpenSearch scale-to-zero), which would otherwise look frozen. After a
      // short delay, reveal a plain "waking up" line - no fake progress bar.
      var hint = doc.createElement("div");
      hint.className = "typing__hint";
      hint.hidden = true;
      hint.textContent = "Waking up the library assistant. This can take a moment…";
      bubble.appendChild(hint);

      wrap.appendChild(bubble);
      thread.appendChild(wrap);
      scrollToBottom();

      var hintTimer = setTimeout(function () {
        hint.hidden = false;
        scrollToBottom();
      }, CONFIG.wakingHintDelayMs);

      // Handle: remove the indicator and cancel the pending hint in one call.
      return {
        el: wrap,
        done: function () {
          if (hintTimer) { clearTimeout(hintTimer); hintTimer = null; }
          if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
        }
      };
    }

    function appendError(question) {
      var wrap = doc.createElement("div");
      wrap.className = "msg msg--bot";
      var bubble = doc.createElement("div");
      bubble.className = "bubble bubble--error";
      var msg = doc.createElement("div");
      msg.textContent =
        "Sorry, I couldn't reach the library assistant just now. " +
        "Please try again in a moment.";
      bubble.appendChild(msg);
      if (question) {
        var retry = doc.createElement("button");
        retry.type = "button";
        retry.className = "retry";
        retry.textContent = "Try again";
        retry.addEventListener("click", function () {
          wrap.parentNode && wrap.parentNode.removeChild(wrap);
          submitQuestion(question);
        });
        bubble.appendChild(retry);
      }
      wrap.appendChild(bubble);
      thread.appendChild(wrap);
      scrollToBottom();
    }

    function setPending(pending) {
      state.pending = pending;
      send.disabled = pending;
    }

    // ---- interaction ----

    function submitQuestion(question) {
      var text = String(question == null ? "" : question).trim();
      if (!text || state.pending) return;
      state.lastQuestion = text;
      appendUserMessage(text);
      setPending(true);
      var typing = showTyping();

      sendQuery(text).then(
        function (result) {
          typing.done();
          appendBotMessage(result.answer, result.sources);
          setPending(false);
          focusInput();
        },
        function (err) {
          typing.done();
          appendError(text);
          setPending(false);
          focusInput();
          if (typeof console !== "undefined" && console.error) {
            console.error("[gavilan-widget] query failed:", err);
          }
        }
      );
    }

    function autosize() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 120) + "px";
    }

    function focusInput() {
      if (typeof input.focus === "function") {
        try { input.focus(); } catch (e) { /* ignore */ }
      }
    }

    function openPanel() {
      if (state.open) return;
      state.open = true;
      panel.hidden = false;
      launcher.hidden = true;
      focusInput();
      scrollToBottom();
    }

    function closePanel() {
      if (!state.open) return;
      state.open = false;
      panel.hidden = true;
      launcher.hidden = false;
      if (typeof launcher.focus === "function") {
        try { launcher.focus(); } catch (e) { /* ignore */ }
      }
    }

    launcher.addEventListener("click", openPanel);
    hClose.addEventListener("click", closePanel);

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var text = input.value;
      input.value = "";
      autosize();
      submitQuestion(text);
    });

    input.addEventListener("input", autosize);

    input.addEventListener("keydown", function (e) {
      // Enter submits; Shift+Enter inserts a newline.
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        var text = input.value;
        input.value = "";
        autosize();
        submitQuestion(text);
      }
    });

    panel.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        e.stopPropagation();
        closePanel();
      }
    });

    // seed the greeting so it's present when the panel opens
    appendBotMessage(CONFIG.greeting, []);

    return {
      host: host,
      shadow: shadow,
      open: openPanel,
      close: closePanel,
      submit: submitQuestion,
      getState: function () { return state; }
    };
  }

  // ---- auto-mount (browser only) / Node export (tests) --------------------

  var IS_COMMONJS = typeof module !== "undefined" && module.exports;

  if (!IS_COMMONJS && typeof document !== "undefined") {
    // Fire the pre-warm as early as the deferred script runs, so the OSS cold start overlaps
    // with the user reading the page rather than their first query (finding 1.3).
    warmBackend();
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { mount(); });
    } else {
      mount();
    }
  }

  if (IS_COMMONJS) {
    // Exposed for offline Node/jsdom tests. Not used by the browser bundle.
    module.exports = {
      mount: mount,
      sendQuery: sendQuery,
      normalizeResponse: normalizeResponse,
      warmUrl: warmUrl,
      safeHttpUrl: safeHttpUrl,
      displayUrl: displayUrl,
      CONFIG: CONFIG,
      HOST_ID: HOST_ID
    };
  }
})();
