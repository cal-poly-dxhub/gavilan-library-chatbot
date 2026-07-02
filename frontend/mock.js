/*
 * Gavilan College Library chat widget — DEV-ONLY backend stand-in.
 * ---------------------------------------------------------------------------
 * NOT shipped to users. Loaded ONLY by the local demo harness
 * (frontend/demo.html) and the test suite (frontend/test/). Production ships
 * widget.js alone; widget.js has no knowledge of this file and never
 * references it. The dependency direction is one-way: demo + tests -> here.
 *
 * How it plugs in WITHOUT widget.js knowing:
 *   - In the browser, on load it finds the widget's <script data-api-url="...">
 *     and monkeypatches window.fetch so that requests to THAT url are answered
 *     locally with canned { answer, sources } payloads. Every other request
 *     falls through to the real fetch. widget.js just does its normal fetch to
 *     the configured URL and never actually reaches the network.
 *   - In Node (tests) it exports `mockQuery(question) -> Promise<{answer,
 *     sources}>` directly (no fetch, no window).
 *
 * Returns the same locked contract as the real backend:
 *     { "answer": "<text>", "sources": [ { "uri": "...", "excerpt": "..." } ] }
 *
 * Dev aid: a question containing "trigger error" makes the responder fail (an
 * HTTP 500 over the wire) so the widget's error/retry state can be exercised.
 * ===========================================================================
 */
(function () {
  "use strict";

  var MOCK_MIN_DELAY_MS = 600;
  var MOCK_MAX_DELAY_MS = 1300;

  var MOCK_ROUTES = [
    {
      test: /\b(hours?|opens?|opening|closed?|closing|times?|schedule)\b/i,
      answer:
        "The Gavilan College Library (Gilroy campus) is open Monday through " +
        "Thursday 8:00 AM to 7:00 PM, and Friday 8:00 AM to 1:00 PM. It's " +
        "closed on weekends. Hours can change on holidays and during breaks, " +
        "so it's worth checking the library hours page before you come in.",
      sources: [
        {
          uri: "https://www.gavilan.edu/library/hours.php",
          excerpt:
            "Fall hours: Mon-Thu 8am-7pm, Fri 8am-1pm, Sat-Sun closed. " +
            "Holiday and intersession hours are posted separately."
        }
      ]
    },
    {
      test: /\b(reserve|reserves|course reserve|on reserve)\b/i,
      answer:
        "Textbooks for a lot of classes are on course reserve at the front " +
        "desk. Reserve items are for in-library use and usually check out for " +
        "about two hours at a time, so everyone in the class gets a turn. " +
        "Bring the course number or your instructor's name to the circulation " +
        "desk and they'll grab it for you.",
      sources: [
        {
          uri: "https://www.gavilan.edu/library/reserves.php",
          excerpt:
            "Course reserves circulate for 2 hours, in-library use only. " +
            "Bring the course number or instructor name to the circulation desk."
        }
      ]
    },
    {
      // Textbook flow -> clarifying question, intentionally NO sources.
      test: /\b(textbook|text book|course material|book for (my )?class|assigned book)\b/i,
      answer:
        "Happy to help you track down a textbook! It depends a little on your " +
        "situation: do you need it for the whole semester, or just a short " +
        "time? And is a physical copy fine, or do you need online access? " +
        "Once I know that, I can point you the right way.",
      sources: []
    },
    {
      test: /\b(check ?out|checkout|borrow|renew|return|due date|loan|library card)\b/i,
      answer:
        "You can check out most books for three weeks with your Gavilan " +
        "student ID card, and renew once if no one else is waiting on them. " +
        "DVDs and equipment check out for shorter periods. Your account just " +
        "needs to be in good standing (no large fines) to borrow.",
      sources: [
        {
          uri: "https://www.gavilan.edu/library/borrowing.php",
          excerpt:
            "Currently enrolled students may borrow circulating books for 21 " +
            "days with a valid Gavilan ID. One renewal is allowed if there are " +
            "no holds."
        }
      ]
    },
    {
      test: /\b(laptop|chromebook|calculator|charger|equipment|device|hotspot)\b/i,
      answer:
        "The library lends laptops, calculators, and phone chargers for use " +
        "while you're on campus. They check out at the front desk with your " +
        "student ID and are due back the same day. It's first come, first " +
        "served, so earlier in the day is your best bet.",
      sources: [
        {
          uri: "https://www.gavilan.edu/library/borrowing.php",
          excerpt:
            "Laptops and calculators are available for in-library use and " +
            "check out for the day with a valid student ID."
        }
      ]
    },
    {
      test: /\b(research|cite|citation|sources for|find articles|database|scholarly|peer.reviewed)\b/i,
      answer:
        "I can help with hours, borrowing, and finding your way around the " +
        "library, but I'm not able to do research for you. A librarian can " +
        "help you find and evaluate sources during staffed hours, and the " +
        "library's research guides are a good place to start on your own.",
      sources: [
        {
          uri: "https://www.gavilan.edu/library/research.php",
          excerpt:
            "Research help is available in person and online during staffed " +
            "hours. Subject research guides are linked from the library home page."
        }
      ]
    },
    {
      // IT / account issues -> out of scope, no library source.
      test: /\b(password|log ?in|login|email|account|canvas|wifi|wi-fi|reset my)\b/i,
      answer:
        "That one's outside what the library handles. Email, passwords, and " +
        "campus account issues are looked after by the IT Help Desk, not the " +
        "library. I'm glad to help with anything about the library itself " +
        "though, like hours, checkouts, or finding a textbook.",
      sources: []
    },
    {
      test: /\b(offer|services|what.*do|what.*have|help with|about the library|provide)\b/i,
      answer:
        "Quite a lot! The Gavilan Library has study spaces and computers, " +
        "books and DVDs you can borrow, textbooks on course reserve, laptops " +
        "and calculators to check out, printing, research help from " +
        "librarians, and online databases you can use with your student login. " +
        "Ask me about any of those and I can tell you more.",
      sources: [
        {
          uri: "https://www.gavilan.edu/library/",
          excerpt:
            "The library provides study spaces, computers, circulating and " +
            "reserve collections, technology lending, printing, research " +
            "assistance, and online databases."
        }
      ]
    }
  ];

  var MOCK_FALLBACK = {
    answer:
      "I can help with things like library hours, checking out and returning " +
      "materials, finding textbooks and course reserves, borrowing laptops, " +
      "and what the library offers. What would you like to know?",
    sources: []
  };

  /**
   * Pure responder: resolve the canned { answer, sources } for a question, or
   * reject when the "trigger error" dev backdoor is present. Includes a small
   * simulated network delay so the widget's typing state is visible.
   */
  function mockQuery(question) {
    var q = String(question == null ? "" : question);
    return new Promise(function (resolve, reject) {
      var delay =
        MOCK_MIN_DELAY_MS +
        Math.random() * (MOCK_MAX_DELAY_MS - MOCK_MIN_DELAY_MS);
      setTimeout(function () {
        // Dev-only hook to exercise the widget's error state.
        if (/trigger error/i.test(q)) {
          reject(new Error("Simulated backend failure (mock)."));
          return;
        }
        var picked = MOCK_FALLBACK;
        for (var i = 0; i < MOCK_ROUTES.length; i++) {
          if (MOCK_ROUTES[i].test.test(q)) {
            picked = MOCK_ROUTES[i];
            break;
          }
        }
        // Return a fresh copy in the exact wire shape (what a real JSON
        // response body would look like).
        resolve({
          answer: picked.answer,
          sources: (picked.sources || []).map(function (s) {
            return { uri: s.uri, excerpt: s.excerpt };
          })
        });
      }, delay);
    });
  }

  // ---- browser: transparently intercept fetch to the widget's endpoint -----

  function jsonResponse(bodyObj, status) {
    return new Response(JSON.stringify(bodyObj), {
      status: status,
      headers: { "Content-Type": "application/json" }
    });
  }

  function readQuery(init) {
    if (!init || typeof init.body !== "string") return "";
    try {
      var parsed = JSON.parse(init.body);
      return parsed && typeof parsed.query === "string" ? parsed.query : "";
    } catch (e) {
      return "";
    }
  }

  /**
   * Patch window.fetch so POSTs to `targetUrl` are served from mockQuery().
   * All other requests pass through to the real fetch untouched. The widget is
   * unaware: it just fetches its configured URL and gets a normal Response.
   */
  function installMockFetch(targetUrl) {
    if (typeof window === "undefined" || typeof window.fetch !== "function") {
      return;
    }
    var realFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      var url =
        typeof input === "string"
          ? input
          : input && input.url
          ? input.url
          : "";
      if (url !== targetUrl) {
        return realFetch(input, init);
      }
      var q = readQuery(init);
      return mockQuery(q).then(
        function (data) {
          return jsonResponse(data, 200);
        },
        function () {
          // "trigger error" -> a real-looking 5xx so the widget shows its
          // error/retry UI exactly as it would against a failing backend.
          return jsonResponse(
            { error: "Simulated backend failure (mock)." },
            500
          );
        }
      );
    };
  }

  // Auto-install against whatever URL the widget script advertises, so the
  // data-api-url attribute stays the single source of truth for the endpoint.
  // (Runs as a deferred script, after the widget <script> tag is in the DOM.)
  if (typeof document !== "undefined") {
    var scriptEl = document.querySelector("script[data-api-url]");
    var target = scriptEl ? scriptEl.getAttribute("data-api-url") : null;
    if (target) installMockFetch(target);
  }

  // ---- Node (tests): export the pure responder ----------------------------
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { mockQuery: mockQuery, installMockFetch: installMockFetch };
  }
})();
