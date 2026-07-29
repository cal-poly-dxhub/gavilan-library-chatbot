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
 * session only. On every send it posts the WHOLE in-memory transcript as a
 * `messages` array ({ role: "user"|"assistant", content }) so the bot remembers
 * earlier turns within the session; closing the tab discards it. The server caps
 * and trims the history, so the widget just sends what it has.
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
    // backend right up to that ceiling rather than aborting early. A cold first query
    // (OpenSearch scale-to-zero wake + generation) can take 15-25s.
    requestTimeoutMs: 30000,
    // After this long with no response, the typing indicator gains an honest "still working"
    // note so a slow turn doesn't look frozen. Kept generous so it only shows on genuinely slow
    // responses, not routine ones (it must not read like a startup message every message).
    wakingHintDelayMs: 6000,
    title: "Library Help",
    launcherLabel: "Ask the Library",
    greeting:
      "Hi! I'm the Gavilan College Library assistant. I can help with hours, " +
      "checking out materials, textbooks, and what the library offers. " +
      "What can I help you find?",
    // Starter questions shown as clickable buttons on first launch, under the greeting.
    // They disappear as soon as the user sends any message. Scoped to what the bot handles.
    suggestedQuestions: [
      "What are the library hours?",
      "How do I check out a book?",
      "Where do I find my textbook?",
      "What research databases are available?"
    ]
  };

  // Capture the script element at load time (before the deferred mount runs),
  // so `document.currentScript` still resolves; fall back to a DOM query.
  var CURRENT_SCRIPT =
    (typeof document !== "undefined" && document.currentScript) || null;

  /** This widget's own <script> tag, or null. */
  function scriptEl() {
    // The fallback query is scoped to this widget's own script tag (its src ends in
    // widget.js), so it can never bind to another embed's data-api-url tag on the host page.
    return (
      CURRENT_SCRIPT ||
      (typeof document !== "undefined"
        ? document.querySelector('script[data-api-url][src*="widget.js"]')
        : null)
    );
  }

  /** The configured backend endpoint, or null if `data-api-url` is unset. */
  function apiUrl() {
    var el = scriptEl();
    var url = el && el.getAttribute ? el.getAttribute("data-api-url") : null;
    return url && url.trim() ? url.trim() : null;
  }

  // ---- usage events: opt-in, off by default -------------------------------
  //
  // The demo site shows what a conversation cost, which needs the token counts
  // the backend reports behind its `include_usage` flag. The widget is the only
  // thing that makes the request, so it has to be the thing that asks - but a
  // library page must not pay for a payload it never reads, and its responses
  // must stay exactly { answer, sources }.
  //
  // So this is a SECOND opt-in attribute on the same script tag, absent from the
  // production embed. With it unset (the library's tag, and the tag in the CDK
  // output), the request body and every response path are byte-identical to what
  // they were before this existed. With it set, the widget adds the flag and
  // re-broadcasts what came back as a DOM event. Nothing about rendering changes
  // either way: the answer is drawn from { answer, sources } exactly as before.
  var USAGE_ATTR = "data-usage-events";
  var USAGE_EVENT = "gavilan-widget:usage";

  /** Whether this embed asked for usage events. Any non-"false" value counts. */
  function usageEventsEnabled() {
    var el = scriptEl();
    if (!el || !el.getAttribute) return false;
    var raw = el.getAttribute(USAGE_ATTR);
    if (raw === null) return false;
    return String(raw).trim().toLowerCase() !== "false";
  }

  /**
   * Re-broadcast a response's `usage` object as a window CustomEvent so a host
   * page can meter it. Swallows everything: a page with no listener, an old
   * browser with no CustomEvent constructor, or a backend that returned no
   * usage must all be no-ops, never an error in the answer path.
   */
  function emitUsage(data) {
    // Gate on the embed's own opt-in, not just on the payload. A backend that
    // volunteered a usage block must not make a page that never asked start
    // emitting events - opting in is the host page's decision, not the server's.
    if (!usageEventsEnabled()) return;
    if (!data || typeof data.usage !== "object" || data.usage === null) return;
    try {
      if (typeof window === "undefined" || typeof CustomEvent !== "function") return;
      window.dispatchEvent(
        new CustomEvent(USAGE_EVENT, { detail: { usage: data.usage } })
      );
    } catch (e) {
      /* ignore: metering is never allowed to break a reply */
    }
  }

  /**
   * The single entry point the UI calls. Takes the full conversation so far as a
   * `messages` array ({ role, content }, oldest first, newest user turn last) and
   * returns a Promise for the locked { answer, sources } contract. Always a normal
   * fetch to the configured URL.
   */
  function sendQuery(messages) {
    var url = apiUrl();
    if (!url) {
      return Promise.resolve({
        answer:
          "The library assistant isn't connected yet. Please try again later.",
        sources: []
      });
    }
    return realQuery(url, messages);
  }

  /**
   * Real backend call. Matches app/handler.py: POST JSON { "messages": [...] } to
   * `/query` (the whole session transcript; the server trims it) and expects
   * { "answer", "sources": [{ uri, excerpt }] } back.
   */
  function realQuery(url, messages) {
    var controller =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = null;
    if (controller) {
      timer = setTimeout(function () {
        controller.abort();
      }, CONFIG.requestTimeoutMs);
    }
    // Two bodies, written out side by side rather than one object mutated by a
    // conditional: the production request stays the literal { messages } shape,
    // visible as such in the source and pinned by the contract test. The flag is
    // added only for an embed that set data-usage-events.
    var body = usageEventsEnabled()
      ? JSON.stringify({ messages: messages, include_usage: true })
      : JSON.stringify({ messages: messages });
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body,
      signal: controller ? controller.signal : undefined
    })
      .then(function (res) {
        if (!res.ok) {
          throw new Error("Backend returned HTTP " + res.status);
        }
        return res.json();
      })
      .then(function (data) {
        // Broadcast before normalizing: normalizeResponse deliberately narrows the
        // payload to { answer, sources }, so `usage` is gone by the time the UI sees it.
        emitUsage(data);
        return normalizeResponse(data);
      })
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

  // ---- minimal, safe markdown renderer ------------------------------------
  //
  // Renders a small, well-defined markdown subset (bold, italic, inline code,
  // links, bare URLs, bullet/numbered lists, headings, pipe tables, paragraphs
  // with soft breaks) into real DOM nodes. It builds every node with
  // createElement + createTextNode ONLY - never innerHTML - so message content
  // can never inject markup or run scripts, and every link target (written or
  // autolinked) is passed through safeHttpUrl (javascript:/data:/relative URLs
  // degrade to plain text). Anything unrecognized renders as literal text. The
  // model's answers are the only input; user messages stay plain text.

  // ---- bare-URL autolinking -----------------------------------------------
  //
  // The model regularly writes a URL as prose ("visit: https://...") rather
  // than as [label](url), and an unlinked URL is worse than none - it looks
  // clickable, so a student reads it as broken. The whole difficulty is
  // deciding where the URL ENDS, because prose butts punctuation straight up
  // against it and a link that swallows the sentence's period points at a 404.

  // Candidate span: a scheme, or a bare `www.` host, run out to the first
  // whitespace or angle bracket. Deliberately greedy - trimUrlEnd picks the
  // real boundary, since that decision needs the whole candidate in hand.
  var MD_AUTOLINK = /(?:https?:\/\/|www\.)[^\s<>]+/i;

  // Punctuation that ends a sentence, never a URL.
  var URL_TAIL_PUNCT = /[.,;:!?'"]+$/;

  // Closing brackets, mapped to the opener that would justify keeping one.
  var URL_CLOSERS = { ")": "(", "]": "[", "}": "{" };

  function countChar(s, ch) {
    var n = 0;
    for (var i = 0; i < s.length; i++) if (s.charAt(i) === ch) n++;
    return n;
  }

  /**
   * Given a greedy bare-URL candidate, return only the URL part; whatever
   * punctuation trailed it is left behind for the caller to render as text.
   * Two rules, applied until neither fires:
   *   - trailing sentence punctuation (. , ; : ! ? ' ") is never part of a URL;
   *   - a trailing bracket belongs to the URL only if the URL itself opened it,
   *     so `/wiki/Library_(disambiguation)` keeps its paren while the paren in
   *     `(see https://x.test/a)` goes back to the sentence.
   * Applied in a loop so a mixed tail like `...(a).` unwinds fully.
   */
  function trimUrlEnd(candidate) {
    var url = String(candidate == null ? "" : candidate);
    for (var guard = 0; guard <= url.length; guard++) {
      var before = url;
      url = url.replace(URL_TAIL_PUNCT, "");
      var last = url.charAt(url.length - 1);
      var opener = URL_CLOSERS[last];
      if (opener && countChar(url, opener) < countChar(url, last)) {
        url = url.slice(0, -1);
      }
      if (url === before) break;
    }
    return url;
  }

  /** Resolve a bare-URL candidate to a safe href, or null. `www.` implies https. */
  function autoLinkHref(url) {
    return safeHttpUrl(/^www\./i.test(url) ? "https://" + url : url);
  }

  // Inline rules, in scan-precedence order: code first (its contents are never
  // re-parsed), then links, then bare URLs, then bold, then italic. The scan
  // below picks the EARLIEST match, so an explicit [label](url) - which always
  // starts at the `[` before its URL - beats the autolink rule and the URL is
  // never linked twice.
  var MD_INLINE = [
    { re: /`([^`]+)`/, kind: "code" },
    { re: /\[([^\]]+)\]\(([^)\s]+)\)/, kind: "link" },
    { re: MD_AUTOLINK, kind: "autolink" },
    { re: /\*\*([^*]+)\*\*/, kind: "strong" },
    { re: /__([^_]+)__/, kind: "strong" },
    { re: /\*([^*]+)\*/, kind: "em" },
    { re: /_([^_]+)_/, kind: "em" }
  ];

  /** Build an anchor with the widget's standard link treatment + safety attrs. */
  function makeLink(doc, href) {
    var a = doc.createElement("a");
    a.className = "md-link";
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer nofollow";
    return a;
  }

  /**
   * Render inline markdown in `text` as child nodes appended to `parent`.
   * `inLink` is true while rendering the label of an existing link; it
   * suppresses autolinking so a URL used as its own label ([https://a](https://b))
   * cannot produce a nested <a>. It propagates through bold/italic, which can
   * legally sit inside a link.
   */
  function renderInline(parent, text, doc, inLink) {
    var remaining = String(text == null ? "" : text);
    while (remaining) {
      var best = null;
      for (var i = 0; i < MD_INLINE.length; i++) {
        if (inLink && MD_INLINE[i].kind === "autolink") continue;
        var m = MD_INLINE[i].re.exec(remaining);
        if (m && (best === null || m.index < best.match.index)) {
          best = { spec: MD_INLINE[i], match: m };
        }
      }
      if (!best) {
        parent.appendChild(doc.createTextNode(remaining));
        break;
      }
      var match = best.match;
      if (match.index > 0) {
        parent.appendChild(doc.createTextNode(remaining.slice(0, match.index)));
      }
      var kind = best.spec.kind;
      var consumed = match[0].length;
      if (kind === "link") {
        var href = safeHttpUrl(match[2]);
        if (href) {
          var a = makeLink(doc, href);
          renderInline(a, match[1], doc, true);
          parent.appendChild(a);
        } else {
          // Unsafe/relative URL: never linkify; keep the label as plain text.
          parent.appendChild(doc.createTextNode(match[1]));
        }
      } else if (kind === "autolink") {
        // Consume only the URL, never the punctuation the sentence put after it.
        var bare = trimUrlEnd(match[0]);
        var bareHref = bare ? autoLinkHref(bare) : null;
        if (bareHref) {
          var auto = makeLink(doc, bareHref);
          auto.textContent = bare; // shown exactly as the model wrote it
          parent.appendChild(auto);
        } else {
          parent.appendChild(doc.createTextNode(bare || match[0]));
        }
        // `bare` is non-empty for every match this regex can produce, but fall
        // back to the full match rather than risk a zero-length loop step.
        consumed = bare ? bare.length : match[0].length;
      } else if (kind === "code") {
        var code = doc.createElement("code");
        code.className = "md-code";
        code.textContent = match[1]; // literal, never re-parsed
        parent.appendChild(code);
      } else {
        var el = doc.createElement(kind === "strong" ? "strong" : "em");
        renderInline(el, match[1], doc, inLink);
        parent.appendChild(el);
      }
      remaining = remaining.slice(match.index + consumed);
    }
  }

  var MD_BULLET = /^\s*[-*+]\s+(.*)$/;
  var MD_ORDERED = /^\s*\d+[.)]\s+(.*)$/;
  // The `#` run is captured so a markdown heading can become a real h3-h6 element
  // rather than a bold paragraph: a user who navigates by heading gets nothing from
  // a <p>. Offset by two because the widget is embedded in a host page that owns the
  // h1/h2 levels, and clamped at h6.
  var MD_HEADING = /^\s*(#{1,6})\s+(.*)$/;
  var MD_HEADING_BASE_LEVEL = 2;

  // ---- pipe tables ---------------------------------------------------------
  //
  // GFM pipe tables. The model answers "what are the hours" with one, which
  // makes this the most-read answer in the product; unrendered it is a wall of
  // pipes and dashes. A table is a header row plus a delimiter row of matching
  // cell count - the delimiter row is what distinguishes a table from a
  // paragraph that happens to contain a pipe.

  var MD_TABLE_DELIM_CELL = /^:?-+:?$/;

  /**
   * Split one pipe-table row into trimmed cells. Outer pipes are optional (both
   * `| a | b |` and `a | b` are valid GFM) and `\|` is an escaped literal pipe,
   * not a cell boundary.
   */
  function splitTableRow(line) {
    var s = String(line == null ? "" : line).trim();
    var cells = [];
    var buf = "";
    var i = s.charAt(0) === "|" ? 1 : 0;
    for (; i < s.length; i++) {
      var ch = s.charAt(i);
      if (ch === "\\" && s.charAt(i + 1) === "|") { buf += "|"; i++; continue; }
      if (ch === "|") { cells.push(buf.trim()); buf = ""; continue; }
      buf += ch;
    }
    // A closing pipe leaves an empty tail: that is the row's end, not a cell.
    if (buf.trim() !== "" || cells.length === 0) cells.push(buf.trim());
    return cells;
  }

  /**
   * Read a delimiter row's cells as per-column alignments, or null if this is
   * not a delimiter row. `""` means "no explicit alignment".
   */
  function tableAlignments(cells) {
    var aligns = [];
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      if (!MD_TABLE_DELIM_CELL.test(c)) return null;
      var left = c.charAt(0) === ":";
      var right = c.charAt(c.length - 1) === ":";
      aligns.push(left && right ? "center" : right ? "right" : left ? "left" : "");
    }
    return aligns;
  }

  /**
   * If `lines[i]` starts a pipe table, return its header cells + column
   * alignments; otherwise null. A table needs a header row AND a delimiter row
   * of the same width - that pairing, not the mere presence of a pipe, is the
   * signal, so prose containing a `|` stays a paragraph.
   */
  function tableHeaderAt(lines, i) {
    if (i + 1 >= lines.length || lines[i].indexOf("|") < 0) return null;
    var headCells = splitTableRow(lines[i]);
    if (headCells.length < 2) return null;
    var aligns = tableAlignments(splitTableRow(lines[i + 1]));
    if (!aligns || aligns.length !== headCells.length) return null;
    return { cells: headCells, aligns: aligns };
  }

  /** Append one row of cells (`th` or `td`), padded/truncated to the header width. */
  function appendTableRow(parent, cells, aligns, tag, doc) {
    var tr = doc.createElement("tr");
    for (var c = 0; c < aligns.length; c++) {
      var cell = doc.createElement(tag);
      // A single header row inside <thead> is usually inferred correctly, but saying
      // it costs one attribute and removes the guess (1.3.1).
      if (tag === "th") cell.setAttribute("scope", "col");
      if (aligns[c]) cell.className = "md-cell--" + aligns[c];
      renderInline(cell, c < cells.length ? cells[c] : "", doc);
      tr.appendChild(cell);
    }
    parent.appendChild(tr);
  }

  /** Render block-level markdown from `md` as child nodes appended to `parent`. */
  function renderMarkdown(parent, md, doc) {
    var lines = String(md == null ? "" : md).replace(/\r\n?/g, "\n").split("\n");
    var i = 0;
    while (i < lines.length) {
      if (/^\s*$/.test(lines[i])) { i++; continue; } // skip blank lines between blocks

      // Table: a header row + a delimiter row of the same width. Checked first
      // because it is the only block needing lookahead, and it is unambiguous.
      var head = tableHeaderAt(lines, i);
      if (head) {
        var table = doc.createElement("table");
        table.className = "md-table";
        var thead = doc.createElement("thead");
        appendTableRow(thead, head.cells, head.aligns, "th", doc);
        table.appendChild(thead);

        var tbody = doc.createElement("tbody");
        i += 2;
        while (
          i < lines.length &&
          !/^\s*$/.test(lines[i]) &&
          lines[i].indexOf("|") >= 0
        ) {
          appendTableRow(tbody, splitTableRow(lines[i]), head.aligns, "td", doc);
          i++;
        }
        table.appendChild(tbody);

        // Scroll container: a table too wide for the bubble scrolls inside this
        // wrapper instead of bursting the bubble or squashing its columns.
        var tableWrap = doc.createElement("div");
        tableWrap.className = "md-table-wrap";
        tableWrap.appendChild(table);
        parent.appendChild(tableWrap);
        continue;
      }

      var ordered = MD_ORDERED.test(lines[i]);
      var bullet = MD_BULLET.test(lines[i]);
      if (ordered || bullet) {
        var list = doc.createElement(ordered ? "ol" : "ul");
        list.className = "md-list";
        var itemRe = ordered ? MD_ORDERED : MD_BULLET;
        while (i < lines.length) {
          var im = itemRe.exec(lines[i]);
          if (!im) break;
          var li = doc.createElement("li");
          renderInline(li, im[1], doc);
          list.appendChild(li);
          i++;
        }
        parent.appendChild(list);
        continue;
      }

      var hm = MD_HEADING.exec(lines[i]);
      if (hm) {
        var level = Math.min(6, hm[1].length + MD_HEADING_BASE_LEVEL);
        var heading = doc.createElement("h" + level);
        heading.className = "md-heading";
        renderInline(heading, hm[2], doc);
        parent.appendChild(heading);
        i++;
        continue;
      }

      // Paragraph: consecutive non-blank lines that aren't a list/heading; a single
      // newline inside becomes a soft <br> break.
      var para = [];
      while (
        i < lines.length &&
        !/^\s*$/.test(lines[i]) &&
        !MD_BULLET.test(lines[i]) &&
        !MD_ORDERED.test(lines[i]) &&
        !MD_HEADING.test(lines[i]) &&
        !tableHeaderAt(lines, i) // a table right after prose still starts a table
      ) {
        para.push(lines[i]);
        i++;
      }
      var p = doc.createElement("p");
      p.className = "md-p";
      for (var k = 0; k < para.length; k++) {
        if (k > 0) p.appendChild(doc.createElement("br"));
        renderInline(p, para[k], doc);
      }
      parent.appendChild(p);
    }
  }

  // ---- inline SVG icons ---------------------------------------------------
  //
  // Icons are built as real SVG nodes (createElementNS), not emoji or font
  // glyphs, so they render identically everywhere and inherit color via
  // `currentColor`. Feather-style single-stroke paths, sized to 1em.

  var SVG_NS = "http://www.w3.org/2000/svg";

  function svgIcon(doc, shapes) {
    var svg = doc.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "1em");
    svg.setAttribute("height", "1em");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    for (var i = 0; i < shapes.length; i++) {
      var node = doc.createElementNS(SVG_NS, shapes[i][0]);
      var attrs = shapes[i][1];
      for (var k in attrs) {
        if (Object.prototype.hasOwnProperty.call(attrs, k)) node.setAttribute(k, attrs[k]);
      }
      svg.appendChild(node);
    }
    return svg;
  }

  // Chat bubble (launcher).
  function iconChat(doc) {
    return svgIcon(doc, [
      ["path", { d: "M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" }]
    ]);
  }

  // Diagonal expand / collapse (header size toggle).
  function iconExpand(doc) {
    return svgIcon(doc, [
      ["polyline", { points: "15 3 21 3 21 9" }],
      ["polyline", { points: "9 21 3 21 3 15" }],
      ["line", { x1: "21", y1: "3", x2: "14", y2: "10" }],
      ["line", { x1: "3", y1: "21", x2: "10", y2: "14" }]
    ]);
  }
  function iconCollapse(doc) {
    return svgIcon(doc, [
      ["polyline", { points: "4 14 10 14 10 20" }],
      ["polyline", { points: "20 10 14 10 14 4" }],
      ["line", { x1: "14", y1: "10", x2: "21", y2: "3" }],
      ["line", { x1: "3", y1: "21", x2: "10", y2: "14" }]
    ]);
  }

  // Down chevron (sources disclosure); CSS rotates it when expanded.
  function iconChevron(doc) {
    return svgIcon(doc, [["polyline", { points: "6 9 12 15 18 9" }]]);
  }

  /** Replace an element's children with a single icon node. */
  function setIcon(el, icon) {
    while (el.firstChild) el.removeChild(el.firstChild);
    el.appendChild(icon);
  }

  // ---- title font ---------------------------------------------------------
  //
  // The title uses Bitter, loaded once from Google Fonts. The <link> goes in the HOST document
  // head (not the shadow root) because @font-face registered at the document level is usable
  // inside the shadow tree, which a shadow-scoped link cannot guarantee across browsers. Purely
  // decorative: if the host site's CSP blocks the font, the title just falls back to serif.
  var FONT_LINK_ID = "gavilan-chatbot-font";
  var FONT_HREF =
    "https://fonts.googleapis.com/css2?family=Bitter:wght@600;700&display=swap";

  function ensureTitleFont(doc) {
    try {
      if (!doc || !doc.head || doc.getElementById(FONT_LINK_ID)) return;
      var link = doc.createElement("link");
      link.id = FONT_LINK_ID;
      link.rel = "stylesheet";
      link.href = FONT_HREF;
      doc.head.appendChild(link);
    } catch (e) {
      /* font is decorative; never let it break the widget */
    }
  }

  // ---- styles (scoped inside the shadow root) -----------------------------

  var STYLES = [
    ":host { all: initial; }",
    "*, *::before, *::after { box-sizing: border-box; }",
    ".root {",
    // PRIMARY BRAND COLOR - the single source of truth. Everything that reads as
    // "brand" (header, launcher, buttons, user bubbles, links) derives from --brand,
    // so swapping this one value re-skins the widget.
    "  --brand: #8a1c30;",
    "  --accent: var(--brand); --accent-ink: #ffffff;",
    "  --bg: #ffffff; --panel-border: #d9dee5;",
    "  --user-bg: var(--brand); --user-ink: #ffffff;",
    "  --bot-bg: #f1f3f6; --bot-ink: #1a1d21;",
    "  --muted: #5b6570; --error-bg: #fdecea; --error-ink: #8a1c12;",
    // Two-tone focus ring (WCAG 1.4.11). No single flat colour reaches 3:1 against
    // every surface the ring can land on - a white host page, the thread, and the
    // maroon launcher/Send fill pull in opposite directions - so the ring is a dark
    // outline PLUS a light halo filling the outline-offset gap. Whichever background
    // it lands on, one of the two carries the 3:1 contrast, and the pair contrasts
    // with itself (16.9:1), so it also survives a dark host page.
    "  --focus-ring: #1a1d21; --focus-halo: #ffffff;",
    // Boundary/indicator grey that clears the 1.4.11 3:1 floor on all three light
    // surfaces it is drawn on (white composer, #fafbfc thread, #f1f3f6 bot bubble).
    // The decorative --panel-border stays light: container edges and table rules are
    // exempt, and the panel's drop shadow does its separating work.
    "  --line: #7d8894;",
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
    // The halo is a box-shadow ring, so the launcher's drop shadow is re-declared
    // here (a second box-shadow declaration would replace it, not add to it).
    ".launcher:focus-visible {",
    "  outline: 3px solid var(--focus-ring); outline-offset: 2px;",
    "  box-shadow: 0 0 0 2px var(--focus-halo), 0 6px 20px rgba(0,0,0,0.22);",
    "}",
    ".launcher__icon { display: inline-flex; font-size: 18px; line-height: 1; }",
    // SVG icons: size to 1em of their control, inherit color via currentColor.
    ".launcher__icon svg, .header__expand svg, .sources__caret svg { display: block; width: 1em; height: 1em; }",
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
    // Expanded size, toggled from the header. width/height both clamp to the viewport
    // via min(), so on a phone this collapses to the same near-full-width panel and the
    // gain is mostly extra height - usable on desktop and mobile alike.
    ".panel--expanded {",
    "  width: min(720px, calc(100vw - 24px));",
    "  height: min(860px, calc(100vh - 32px));",
    "}",
    ".panel[hidden], .launcher[hidden] { display: none !important; }",
    // header
    ".header {",
    "  display: flex; align-items: center; justify-content: space-between;",
    "  padding: 12px 14px; background: var(--accent); color: var(--accent-ink);",
    "}",
    // Title uses Bitter (loaded from Google Fonts into the host document head at mount);
    // falls back to a serif until/if it loads. Only the title uses it - body/UI stays default.
    ".header__title { font-family: 'Bitter', Georgia, 'Times New Roman', serif; font-size: 15px; font-weight: 700; }",
    ".header__close {",
    "  appearance: none; border: none; background: transparent;",
    "  color: var(--accent-ink); font-size: 22px; line-height: 1;",
    "  cursor: pointer; padding: 2px 6px; border-radius: 6px;",
    "}",
    ".header__close:hover { background: rgba(255,255,255,0.18); }",
    ".header__close:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }",
    ".header__actions { display: inline-flex; align-items: center; gap: 2px; }",
    ".header__expand {",
    "  appearance: none; border: none; background: transparent;",
    "  color: var(--accent-ink); font-size: 17px; line-height: 1;",
    "  cursor: pointer; padding: 2px 6px; border-radius: 6px;",
    "}",
    ".header__expand:hover { background: rgba(255,255,255,0.18); }",
    ".header__expand:focus-visible { outline: 2px solid #fff; outline-offset: 1px; }",
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
    // Two classes deep on purpose: `.msg--bot .bubble` (0,2,0) beat a bare
    // `.bubble--error` (0,1,0) regardless of source order, so the error palette
    // never rendered and a failed send looked exactly like an answer.
    ".msg--bot .bubble.bubble--error { background: var(--error-bg); color: var(--error-ink); }",
    // sources - per message, collapsed behind a disclosure toggle
    ".sources { margin-top: 8px; padding-top: 8px; border-top: 1px solid #dfe3e8; }",
    ".sources__toggle {",
    "  display: inline-flex; align-items: center; gap: 5px;",
    "  appearance: none; border: none; background: transparent; cursor: pointer;",
    "  padding: 2px 0; color: var(--muted); font: inherit;",
    "  font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;",
    "}",
    ".sources__toggle:hover { color: var(--accent); }",
    ".sources__toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }",
    ".sources__caret { display: inline-flex; transition: transform .15s ease; }",
    ".sources__toggle[aria-expanded=\"true\"] .sources__caret { transform: rotate(180deg); }",
    "@media (prefers-reduced-motion: reduce) { .sources__caret { transition: none; } }",
    ".sources__list { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }",
    ".sources__list[hidden] { display: none; }",
    ".sources__link { color: var(--accent); font-weight: 600; font-size: 13px; text-decoration: none; word-break: break-word; }",
    ".sources__link:hover { text-decoration: underline; }",
    ".sources__link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }",
    ".sources__excerpt { color: var(--muted); font-size: 12.5px; margin-top: 2px; }",
    // rendered markdown (assistant messages). Block spacing is tight; first/last
    // children lose their outer margin so the bubble stays snug.
    ".md { white-space: normal; }",
    ".md > :first-child { margin-top: 0; }",
    ".md > :last-child { margin-bottom: 0; }",
    ".md-p { margin: 0 0 8px; }",
    // Real h3-h6 elements now (1.3.1), so font-size is pinned to the body size:
    // otherwise the user-agent's heading sizes would make an h5/h6 SMALLER than
    // the surrounding text. Visual result is byte-identical to the old <p>.
    ".md-heading { margin: 0 0 8px; font-size: 1em; font-weight: 700; }",
    ".md-list { margin: 0 0 8px; padding-left: 20px; }",
    ".md-list li { margin: 2px 0; }",
    ".md-code {",
    "  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;",
    "  font-size: 0.9em; background: rgba(0,0,0,0.06); padding: 1px 4px; border-radius: 4px;",
    "}",
    // Tables. The bubble is ~220px wide on a phone, so the goal is "wrap, don't
    // squash, and scroll only as a last resort". Three pieces do that:
    //   - min-width floors a column so it can never squash to one letter a line;
    //   - overflow-wrap:break-word OVERRIDES the bubble's `anywhere`, which would
    //     otherwise let the browser compute a 1-character min-content width and
    //     shred every column to fit;
    //   - the wrapper scrolls horizontally when those floors genuinely exceed the
    //     bubble (wide tables), so the table never bursts out of the bubble.
    ".md-table-wrap { margin: 0 0 8px; max-width: 100%; overflow-x: auto; }",
    ".md-table {",
    "  border-collapse: collapse; width: 100%;",
    "  font-size: 0.92em; line-height: 1.35;",
    "}",
    ".md-table th, .md-table td {",
    "  border: 1px solid #dfe3e8; padding: 4px 7px;",
    "  text-align: left; vertical-align: top;",
    "  overflow-wrap: break-word; word-break: normal; min-width: 5.5em;",
    "}",
    ".md-table th { background: rgba(0,0,0,0.045); font-weight: 700; }",
    ".md-cell--left { text-align: left; }",
    ".md-cell--center { text-align: center; }",
    ".md-cell--right { text-align: right; }",
    ".md-link { color: var(--accent); font-weight: 600; text-decoration: underline; word-break: break-word; }",
    ".md-link:hover { text-decoration: none; }",
    ".md-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }",
    // typing indicator
    ".typing { display: inline-flex; gap: 4px; align-items: center; padding: 4px 2px; }",
    ".typing .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--line); animation: gv-blink 1.2s infinite ease-in-out; }",
    ".typing .dot:nth-child(2) { animation-delay: .2s; }",
    ".typing .dot:nth-child(3) { animation-delay: .4s; }",
    "@keyframes gv-blink { 0%, 80%, 100% { opacity: .3; } 40% { opacity: 1; } }",
    "@media (prefers-reduced-motion: reduce) { .typing .dot { animation: none; } }",
    // honest slow-start hint (shown only after a delay under the typing dots)
    ".typing__hint { margin-top: 6px; font-size: 12.5px; color: var(--muted); }",
    // retry
    ".retry {",
    "  margin-top: 8px; appearance: none; border: 1px solid currentColor;",
    "  background: transparent; color: var(--error-ink); cursor: pointer;",
    "  font-size: 13px; font-weight: 600; padding: 4px 10px; border-radius: 8px;",
    "}",
    ".retry:hover { background: rgba(138,28,18,0.08); }",
    // Retry was the one control with no widget-defined ring, so it fell back to
    // whatever the browser ships. Same two-tone treatment as every other control.
    ".retry:focus-visible {",
    "  outline: 2px solid var(--focus-ring); outline-offset: 2px;",
    "  box-shadow: 0 0 0 2px var(--focus-halo);",
    "}",
    // composer
    ".composer {",
    "  display: flex; gap: 8px; align-items: flex-end;",
    "  padding: 10px; border-top: 1px solid var(--panel-border); background: #fff;",
    "}",
    ".composer__input {",
    "  flex: 1 1 auto; resize: none; max-height: 120px; min-height: 40px;",
    "  padding: 9px 11px; border: 1px solid var(--line); border-radius: 10px;",
    "  font: inherit; color: inherit; background: #fff;",
    "}",
    // Two-tone ring, thinner than the buttons' so the text box still reads gently. The
    // brand-tinted border-color is kept from the old softened treatment: at 3.09:1 it
    // already cleared 1.4.11 on its own, and it is what makes a focused field look like
    // this widget rather than like the browser default.
    ".composer__input:focus-visible {",
    "  outline: 2px solid var(--focus-ring);",
    "  outline-offset: 1px;",
    "  box-shadow: 0 0 0 1px var(--focus-halo);",
    "  border-color: color-mix(in srgb, var(--brand) 55%, transparent);",
    "}",
    ".composer__send {",
    "  flex: 0 0 auto; appearance: none; border: none; border-radius: 10px;",
    "  background: var(--accent); color: var(--accent-ink); cursor: pointer;",
    "  font-weight: 600; font-size: 14px; padding: 0 16px; height: 40px;",
    "}",
    ".composer__send:hover:not(:disabled) { filter: brightness(1.07); }",
    ".composer__send:disabled { opacity: .5; cursor: default; }",
    ".composer__send:focus-visible {",
    "  outline: 3px solid var(--focus-ring); outline-offset: 2px;",
    "  box-shadow: 0 0 0 2px var(--focus-halo);",
    "}",
    // first-launch example questions (removed after the first message)
    ".suggestions { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; margin: 2px 0 2px; }",
    ".suggestions__label { font-size: 12px; color: var(--muted); margin-bottom: 2px; }",
    ".suggestion {",
    "  appearance: none; cursor: pointer; text-align: left; max-width: 100%;",
    "  background: #fff; border: 1px solid var(--line); color: var(--accent);",
    "  font: inherit; font-size: 13px; font-weight: 600; line-height: 1.3;",
    "  padding: 8px 12px; border-radius: 12px;",
    "}",
    ".suggestion:hover { border-color: var(--accent); background: color-mix(in srgb, var(--brand) 6%, #fff); }",
    ".suggestion:focus-visible {",
    "  outline: 2px solid var(--focus-ring); outline-offset: 2px;",
    "  box-shadow: 0 0 0 2px var(--focus-halo);",
    "}",
    // visually-hidden (for a11y live region labels)
    ".sr-only {",
    "  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;",
    "  overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;",
    "}"
  ].join("\n");

  // ---- widget construction ------------------------------------------------

  var HOST_ID = "gavilan-chatbot-widget-host";
  // Only has to be unique inside the widget's own shadow root, which is also the only
  // place an ARIA id reference can resolve from - one pointing at the host document
  // would fail silently.
  var GREETING_ID = "gavilan-chatbot-greeting";

  // Everything that can hold focus inside the panel, in DOM order. `:not([disabled])`
  // matters for Send, which disables itself while a request is pending.
  var FOCUSABLE_SELECTOR = [
    "button:not([disabled])",
    "a[href]",
    "textarea:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    '[tabindex]:not([tabindex="-1"])'
  ].join(", ");

  /**
   * Build and wire the widget into `doc` (defaults to the global document).
   * Returns a small handle for tests. Idempotent: a second call is a no-op.
   */
  function mount(doc) {
    doc = doc || (typeof document !== "undefined" ? document : null);
    if (!doc || !doc.body) return null;
    if (doc.getElementById(HOST_ID)) return null; // already mounted

    ensureTitleFont(doc); // load Bitter for the title (host document head; safe no-op if blocked)

    var host = doc.createElement("div");
    host.id = HOST_ID;
    doc.body.appendChild(host);
    var shadow = host.attachShadow({ mode: "open" });

    var style = doc.createElement("style");
    style.textContent = STYLES;
    shadow.appendChild(style);

    var root = doc.createElement("div");
    root.className = "root";
    // Declare the chrome's own language instead of inheriting whatever the host page
    // says (3.1.2). Every string the widget itself renders is English; the ANSWERS are
    // not covered by this - the widget is not told what language the model replied in,
    // so a per-bubble `lang` needs a field on the /query response contract first.
    root.lang = "en";
    shadow.appendChild(root);

    // in-memory session state (no storage)
    var state = { open: false, pending: false, expanded: false, messages: [], lastQuestion: null };

    // launcher
    var launcher = doc.createElement("button");
    launcher.type = "button";
    launcher.className = "launcher";
    // NO aria-label. The old one named opening the chat, which OVERRODE the visible
    // "Ask the Library" as the accessible name and left a speech-input user unable to
    // activate the button by the words on screen (2.5.3). The button's own text is the
    // name now; aria-haspopup carries what that label used to hint at - it opens a dialog.
    launcher.setAttribute("aria-haspopup", "dialog");
    var lIcon = doc.createElement("span");
    lIcon.className = "launcher__icon";
    lIcon.setAttribute("aria-hidden", "true");
    lIcon.appendChild(iconChat(doc)); // inline SVG chat bubble
    var lText = doc.createElement("span");
    lText.textContent = CONFIG.launcherLabel;
    launcher.appendChild(lIcon);
    launcher.appendChild(lText);
    root.appendChild(launcher);

    // panel
    var panel = doc.createElement("section");
    panel.className = "panel";
    panel.setAttribute("role", "dialog");
    // The panel already behaves like a modal - fixed overlay covering page content,
    // launcher hidden while it is open - so it declares itself one and contains focus
    // (see the Tab handling below). Previously it declared modal=false to assistive
    // technology while visually acting modal, and let Tab wander invisibly behind it.
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", "Gavilan College Library chat");
    panel.hidden = true;

    var header = doc.createElement("div");
    header.className = "header";
    var hTitle = doc.createElement("span");
    hTitle.className = "header__title";
    hTitle.textContent = CONFIG.title;
    var hExpand = doc.createElement("button");
    hExpand.type = "button";
    hExpand.className = "header__expand";
    hExpand.setAttribute("aria-label", "Expand chat");
    hExpand.setAttribute("aria-pressed", "false");
    hExpand.appendChild(iconExpand(doc)); // inline SVG, swapped on toggle
    var hClose = doc.createElement("button");
    hClose.type = "button";
    hClose.className = "header__close";
    hClose.setAttribute("aria-label", "Close chat");
    hClose.textContent = "×"; // ×
    var hActions = doc.createElement("div");
    hActions.className = "header__actions";
    hActions.appendChild(hExpand);
    hActions.appendChild(hClose);
    header.appendChild(hTitle);
    header.appendChild(hActions);

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
    // Advisory input cap so a user gets feedback instead of typing a wall of text. The real
    // limit is the server-side length check; this is intentionally lower and UX-only.
    input.setAttribute("maxlength", "1000");
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

    /**
     * A visually-hidden speaker label for one turn. Who said what was carried only by
     * the `msg--user` / `msg--bot` class - alignment and bubble colour for a sighted
     * user, nothing at all in the accessibility tree (1.3.1). It goes FIRST in the
     * wrapper so it is read before the message it introduces, and it is out of flow
     * (`.sr-only` is absolutely positioned), so the flex layout is untouched.
     */
    function speakerLabel(who) {
      var label = doc.createElement("span");
      label.className = "sr-only";
      label.textContent = who;
      return label;
    }

    function appendUserMessage(text) {
      state.messages.push({ role: "user", text: text });
      var wrap = doc.createElement("div");
      wrap.className = "msg msg--user";
      var bubble = doc.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = text; // text node only — never innerHTML
      wrap.appendChild(speakerLabel("You said:"));
      wrap.appendChild(bubble);
      thread.appendChild(wrap);
      scrollToBottom();
    }

    // Build the collapsible sources disclosure for ONE message. Caller only invokes this when
    // that message has at least one source, so there is never an empty "Sources" affordance.
    // Collapsed by default: a small toggle reveals this message's own list of public links.
    function buildSources(sources) {
      var container = doc.createElement("div");
      container.className = "sources";

      var list = doc.createElement("ul");
      list.className = "sources__list";
      list.hidden = true; // collapsed by default

      var toggle = doc.createElement("button");
      toggle.type = "button";
      toggle.className = "sources__toggle";
      toggle.setAttribute("aria-expanded", "false");
      var caret = doc.createElement("span");
      caret.className = "sources__caret";
      caret.setAttribute("aria-hidden", "true");
      caret.appendChild(iconChevron(doc));
      var toggleText = doc.createElement("span");
      toggleText.className = "sources__toggle-text";
      toggleText.textContent =
        sources.length > 1 ? "Sources (" + sources.length + ")" : "Source";
      toggle.appendChild(caret);
      toggle.appendChild(toggleText);
      toggle.addEventListener("click", function () {
        var expanded = toggle.getAttribute("aria-expanded") === "true";
        toggle.setAttribute("aria-expanded", expanded ? "false" : "true");
        list.hidden = expanded; // was open -> collapse; was closed -> reveal
        // No scroll: the list appears right under the toggle the user clicked, which is already
        // in view. Forcing scrollToBottom() here jerked the thread down when expanding sources on
        // an older (not-most-recent) message.
      });

      for (var i = 0; i < sources.length; i++) {
        var src = sources[i];
        var href = safeHttpUrl(src.uri);
        var li = doc.createElement("li");
        li.className = "sources__item";
        if (href) {
          var a = doc.createElement("a");
          a.className = "sources__link";
          a.href = href;
          a.target = "_blank"; // open the public library page in a new tab
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

      container.appendChild(toggle);
      container.appendChild(list);
      return container;
    }

    /** Append one assistant turn; returns its bubble so the caller can reference it. */
    function appendBotMessage(answer, sources) {
      state.messages.push({ role: "bot", text: answer, sources: sources });
      var wrap = doc.createElement("div");
      wrap.className = "msg msg--bot";
      var bubble = doc.createElement("div");
      bubble.className = "bubble";
      var textEl = doc.createElement("div");
      textEl.className = "md";
      // Render the assistant answer as markdown (safe DOM, no innerHTML). The
      // no-response fallback is plain text and renders as a single paragraph.
      renderMarkdown(
        textEl,
        answer && answer.trim()
          ? answer
          : "Sorry, I didn't get a response. Please try again.",
        doc
      );
      bubble.appendChild(textEl);
      if (sources && sources.length) {
        bubble.appendChild(buildSources(sources));
      }
      wrap.appendChild(speakerLabel("Library assistant said:"));
      wrap.appendChild(bubble);
      thread.appendChild(wrap);
      scrollToBottom();
      return bubble;
    }

    function showTyping() {
      var wrap = doc.createElement("div");
      wrap.className = "msg msg--bot";
      wrap.setAttribute("data-typing", "1");
      var bubble = doc.createElement("div");
      bubble.className = "bubble";
      // The BUBBLE is the status region, not the dots row, for two reasons (4.1.3):
      // a live region announces its CONTENT, and the dots are decorative and
      // aria-hidden, so the old role="status" element had literally nothing to say -
      // its message sat in an aria-label. And the region has to be the same one the
      // slow-response note is appended to below, so that note is a change INSIDE a
      // status region rather than a sibling of it.
      bubble.setAttribute("role", "status");
      var statusText = doc.createElement("span");
      statusText.className = "sr-only";
      statusText.textContent = "Assistant is typing";
      bubble.appendChild(statusText);
      var typing = doc.createElement("div");
      typing.className = "typing";
      for (var i = 0; i < 3; i++) {
        var dot = doc.createElement("span");
        dot.className = "dot";
        dot.setAttribute("aria-hidden", "true");
        typing.appendChild(dot);
      }
      bubble.appendChild(typing);

      wrap.appendChild(bubble);
      thread.appendChild(wrap);
      scrollToBottom();

      // Honest slow-response note: a query occasionally takes long enough (a big generation,
      // a cold Lambda) that a bare typing indicator looks frozen. After a short delay, add a
      // plain "Working…" line - no fake progress bar. Short, neutral wording on purpose: this can
      // fire on any slow turn, so it must not imply the assistant is starting up each time.
      // CREATED HERE, not pre-inserted hidden and revealed: un-hiding an existing node is
      // neither an insertion (which is all the thread's aria-relevant="additions" reports)
      // nor a text change, so the old version changed nothing any live region was watching.
      var hintTimer = setTimeout(function () {
        var hint = doc.createElement("div");
        hint.className = "typing__hint";
        hint.textContent = "Working…";
        bubble.appendChild(hint);
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

    /** Append the failure bubble; returns its retry button, or null if there is none. */
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
      var retry = null;
      if (question) {
        retry = doc.createElement("button");
        retry.type = "button";
        retry.className = "retry";
        retry.textContent = "Try again";
        retry.addEventListener("click", function () {
          wrap.parentNode && wrap.parentNode.removeChild(wrap);
          // Resend the existing transcript; do NOT re-append the user turn (it's already there).
          deliver(question);
        });
        bubble.appendChild(retry);
      }
      wrap.appendChild(speakerLabel("Library assistant said:"));
      wrap.appendChild(bubble);
      thread.appendChild(wrap);
      scrollToBottom();
      return retry;
    }

    function setPending(pending) {
      state.pending = pending;
      send.disabled = pending;
    }

    // ---- interaction ----

    /**
     * The in-memory transcript as the backend's { role, content } messages array:
     * oldest first, newest user turn last. The widget's internal "bot" role maps to
     * "assistant"; the canned greeting rides along as the leading assistant turn (the
     * server drops a leading assistant turn and trims the rest). This is the whole of
     * the single-session memory - no storage, gone when the tab closes.
     */
    function conversationForRequest() {
      var out = [];
      for (var i = 0; i < state.messages.length; i++) {
        var m = state.messages[i];
        if (!m || typeof m.text !== "string" || !m.text) continue;
        out.push({
          role: m.role === "user" ? "user" : "assistant",
          content: m.text
        });
      }
      return out;
    }

    // First-launch example questions. Rendered under the greeting; each is a button that submits
    // that question. They are removed on the first message (typed or clicked), never to return.
    var suggestionsWrap = null;

    function renderSuggestions() {
      var qs = CONFIG.suggestedQuestions;
      if (!qs || !qs.length) return;
      var wrap = doc.createElement("div");
      wrap.className = "suggestions";
      var label = doc.createElement("div");
      label.className = "suggestions__label";
      label.textContent = "Try asking:";
      wrap.appendChild(label);
      qs.forEach(function (q) {
        var btn = doc.createElement("button");
        btn.type = "button";
        btn.className = "suggestion";
        btn.textContent = q;
        btn.addEventListener("click", function () { submitQuestion(q); });
        wrap.appendChild(btn);
      });
      thread.appendChild(wrap);
      suggestionsWrap = wrap;
      scrollToBottom();
    }

    function removeSuggestions() {
      if (suggestionsWrap && suggestionsWrap.parentNode) {
        suggestionsWrap.parentNode.removeChild(suggestionsWrap);
      }
      suggestionsWrap = null;
      // The greeting stops describing the composer at the same moment the starter
      // questions disappear: from here on it is the newest answer that matters, and a
      // fixed description would be re-read every time focus returns to the text box.
      // Guarded like the focus() calls: this must never throw on a partial DOM.
      if (typeof input.removeAttribute === "function") {
        input.removeAttribute("aria-describedby");
      }
    }

    function submitQuestion(question) {
      var text = String(question == null ? "" : question).trim();
      if (!text || state.pending) return;
      removeSuggestions(); // the starter questions go away once any message is sent
      state.lastQuestion = text;
      appendUserMessage(text); // the user turn is appended to the transcript exactly ONCE, here
      deliver(text);
    }

    /**
     * Send the current transcript to the backend and render the reply. `question` is used only
     * to re-arm the retry button on failure; it is NOT re-appended, so retrying a failed send
     * never duplicates the user turn in the transcript (the transcript already holds it).
     */
    function deliver(question) {
      if (state.pending) return;
      setPending(true);
      var typing = showTyping();

      sendQuery(conversationForRequest()).then(
        function (result) {
          typing.done();
          appendBotMessage(result.answer, result.sources);
          setPending(false);
          focusInput();
        },
        function (err) {
          typing.done();
          var retry = appendError(question);
          setPending(false);
          // Focus the retry button rather than the composer: the button lives in the
          // thread, which is EARLIER in DOM order than the composer, so from the
          // composer it was unreachable by Tab and only findable by Shift+Tab (2.4.3).
          if (retry && typeof retry.focus === "function") {
            try { retry.focus(); } catch (e) { focusInput(); }
          } else {
            focusInput();
          }
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

    /**
     * The panel's focusable controls, in DOM order, skipping any that sit in a hidden
     * subtree - a collapsed sources list is still full of links, and they are not
     * tabbable while its <ul> carries `hidden`. `closest()` stops at the shadow root,
     * so this never looks at the host page.
     */
    function focusablesInPanel() {
      if (typeof panel.querySelectorAll !== "function") return [];
      var found = panel.querySelectorAll(FOCUSABLE_SELECTOR);
      var out = [];
      for (var i = 0; i < found.length; i++) {
        var el = found[i];
        if (el.hidden) continue;
        if (typeof el.closest === "function" && el.closest("[hidden]")) continue;
        out.push(el);
      }
      return out;
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

    // Toggle the panel between its default and expanded size. A CSS class drives the
    // size (both dimensions clamp to the viewport), so this stays usable on mobile.
    function toggleExpand() {
      state.expanded = !state.expanded;
      if (state.expanded) {
        panel.classList.add("panel--expanded");
      } else {
        panel.classList.remove("panel--expanded");
      }
      hExpand.setAttribute("aria-pressed", state.expanded ? "true" : "false");
      hExpand.setAttribute("aria-label", state.expanded ? "Shrink chat" : "Expand chat");
      setIcon(hExpand, state.expanded ? iconCollapse(doc) : iconExpand(doc));
      scrollToBottom();
    }

    launcher.addEventListener("click", openPanel);
    hClose.addEventListener("click", closePanel);
    hExpand.addEventListener("click", toggleExpand);

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
        return;
      }
      if (e.key !== "Tab") return;
      // Focus containment. Recomputed on every Tab rather than cached at open, because
      // the panel's focusable set changes as the conversation runs: the starter chips
      // go away, a sources disclosure or a retry button arrives, Send disables itself
      // while a request is pending.
      var items = focusablesInPanel();
      if (!items.length) return;
      var first = items[0];
      var last = items[items.length - 1];
      // shadow.activeElement, not document.activeElement: from outside the shadow tree
      // the focused control reads as the host element, so the edge test would never hit.
      var active = shadow.activeElement;
      if (e.shiftKey ? active === first : active === last) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      }
    });

    // Escape from ANYWHERE while the panel is open, not just from inside it: the panel
    // covers host-page content that is still tabbable by mouse-click, and a user who
    // got out there had no way back and no way to dismiss the overlay. Capture phase, so
    // a host page that stops keydown propagation on its own controls cannot trap the
    // panel open. The panel's own handler above still stops propagation for the
    // focus-is-inside case, so Escape typed in the composer stays out of the host page.
    if (typeof doc.addEventListener === "function") {
      doc.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && state.open) closePanel();
      }, true);
    }

    // seed the greeting + first-launch example questions so they're present when the panel opens
    var greetingBubble = appendBotMessage(CONFIG.greeting, []);
    renderSuggestions();
    // On first launch, focus lands in the composer with the greeting and the starter
    // questions behind it in the tab order, so a screen-reader user met an empty text
    // box. Describing the composer with the greeting offers that content instead. It is
    // dropped with the suggestions on the first message, so later turns are not prefixed
    // by a stale description every time focus returns here. Both ends of the reference
    // are inside this shadow root, which is the only way an id reference resolves here.
    if (greetingBubble) {
      greetingBubble.id = GREETING_ID;
      input.setAttribute("aria-describedby", GREETING_ID);
    }

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
    // with the user reading the page rather than their first query.
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
      renderMarkdown: renderMarkdown,
      trimUrlEnd: trimUrlEnd,
      splitTableRow: splitTableRow,
      usageEventsEnabled: usageEventsEnabled,
      USAGE_ATTR: USAGE_ATTR,
      USAGE_EVENT: USAGE_EVENT,
      CONFIG: CONFIG,
      HOST_ID: HOST_ID
    };
  }
})();
