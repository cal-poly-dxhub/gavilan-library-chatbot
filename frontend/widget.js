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
 * Conversation state is kept in memory for the session only - no storage of any
 * kind. On every send it posts the WHOLE in-memory transcript as a `messages`
 * array ({ role: "user"|"assistant", content }) so the bot remembers earlier
 * turns within the session; closing the tab discards it. The server caps and
 * trims the history, so the widget just sends what it has.
 *
 * The UI is bilingual (English + Spanish). Every user-visible string lives in the
 * STRINGS table below, keyed by language code, and a control in the panel header
 * switches between them. The language choice is session-only too, for the same
 * reason as the transcript: nothing about a conversation is written down.
 *
 * Three things are customer-changeable AFTER handover without a redeploy - the highlight
 * colour, the font family and the starter questions - by uploading a theme.json next to
 * this file in the widget bucket. See the THEME section below and docs/widget-theming.md.
 *
 * The ONE exception is the TEMPORARY sign-in gate's access token, which is kept
 * in `sessionStorage` so a reload does not throw away a session that is still
 * valid. That key leaves with the gate at go-live - see the gate block below for
 * why it is sessionStorage and nothing else.
 * ===========================================================================
 */
(function () {
  "use strict";

  // =========================================================================
  // =========================  LOCALIZATION  ================================
  // =========================================================================
  //
  // ONE table, keyed by language code. Render code below never holds a user-visible
  // literal - it calls t("key") - so adding a language is adding a table, not hunting
  // for strings in DOM-building code.
  //
  // WHY A VISIBLE CONTROL AND NOT AUTO-DETECTION. Gavilan serves a large Spanish-speaking
  // community, and the bot already answers Spanish questions correctly from the English
  // knowledge base. What was missing is the SHELL: every piece of chrome was English, so
  // before typing anything a Spanish speaker had no signal that the bot speaks Spanish.
  // Auto-detection cannot fix that, because it needs a message first. The control's primary
  // job is discovery in the pre-first-message state; switching is the secondary one.
  //
  // Each language is offered under its OWN name (`languageName`), in both UIs: someone
  // looking for Spanish scans for "Español", not for the word "Spanish".
  var DEFAULT_LANG = "en";

  // Offered languages, in the order the control shows them.
  var LANGUAGES = ["en", "es"];

  var STRINGS = {
    en: {
      languageName: "English",
      languageGroupLabel: "Language",
      title: "Library Help",
      // The launcher's VISIBLE text, and deliberately its whole accessible name: it carries
      // no aria-label, so speech input can activate it by the words on screen (2.5.3).
      launcherLabel: "Ask the Library",
      panelAria: "Gavilan College Library chat",
      expandAria: "Expand chat",
      shrinkAria: "Shrink chat",
      closeAria: "Close chat",
      threadAria: "Conversation",
      inputAria: "Type your question",
      inputPlaceholder: "Ask a question…",
      sendLabel: "Send",
      sendAria: "Send message",
      // Visually-hidden speaker labels. Who said what is otherwise carried only by bubble
      // colour and alignment, which is nothing at all in the accessibility tree (1.3.1) -
      // so these are read aloud, and a Spanish UI has to read them in Spanish.
      speakerYou: "You said:",
      speakerBot: "Library assistant said:",
      // The TEXT of the pending bubble's status region, not a label on it: a live region
      // announces its content, and the dots beside it are decorative.
      typingStatus: "Assistant is typing",
      workingHint: "Working…",
      greeting:
        "Hi! I'm the Gavilan College Library assistant. I can help with hours, " +
        "checking out materials, textbooks, and what the library offers. " +
        "What can I help you find?",
      suggestionsLabel: "Try asking:",
      // Starter questions shown as clickable buttons on first launch, under the greeting.
      // They disappear as soon as the user sends any message. Scoped to what the bot handles.
      suggestedQuestions: [
        "What are the library hours?",
        "How do I check out a book?",
        "Where do I find my textbook?",
        "What research databases are available?"
      ],
      sourceOne: "Source",
      // {n} is replaced with the count; substituted into a text node, never markup.
      sourcesMany: "Sources ({n})",
      noAnswer: "Sorry, I didn't get a response. Please try again.",
      networkError:
        "Sorry, I couldn't reach the library assistant just now. " +
        "Please try again in a moment.",
      retryLabel: "Try again",
      notConnected: "The library assistant isn't connected yet. Please try again later.",
      // ---- sign-in gate (temporary; removed at go-live) ----
      // The panel says WHY it is asking. "Sign in" with no reason reads like a data grab on a
      // library page; "this preview is private" is the honest version and sets expectations.
      signInHeading: "Sign in to try the assistant",
      signInIntro: "This preview is private. Use the account you were given.",
      signInUsernameLabel: "Username",
      signInPasswordLabel: "Password",
      signInSubmit: "Sign in",
      signInPending: "Signing in…",
      // Cognito is configured not to distinguish a wrong name from a wrong password, and
      // neither does this copy.
      signInFailed: "That username and password didn't work. Please check them and try again.",
      signInUnavailable:
        "Sorry, sign-in isn't reachable just now. Please try again in a moment.",
      signInExpired: "Your session ended. Please sign in again."
    },
    es: {
      languageName: "Español",
      languageGroupLabel: "Idioma",
      title: "Ayuda de la biblioteca",
      launcherLabel: "Pregunta a la biblioteca",
      panelAria: "Chat de la Biblioteca de Gavilan College",
      expandAria: "Ampliar el chat",
      shrinkAria: "Reducir el chat",
      closeAria: "Cerrar el chat",
      threadAria: "Conversación",
      inputAria: "Escribe tu pregunta",
      inputPlaceholder: "Haz una pregunta…",
      sendLabel: "Enviar",
      sendAria: "Enviar mensaje",
      speakerYou: "Tú dijiste:",
      speakerBot: "El asistente de la biblioteca dijo:",
      typingStatus: "El asistente está escribiendo",
      workingHint: "Sigo trabajando…",
      greeting:
        "¡Hola! Soy el asistente de la Biblioteca de Gavilan College. Te puedo ayudar " +
        "con los horarios, cómo pedir prestados materiales, los libros de texto y lo " +
        "que ofrece la biblioteca. ¿Qué buscas?",
      suggestionsLabel: "Prueba preguntando:",
      suggestedQuestions: [
        "¿Cuál es el horario de la biblioteca?",
        "¿Cómo pido prestado un libro?",
        "¿Dónde encuentro mi libro de texto?",
        "¿Qué bases de datos de investigación hay?"
      ],
      sourceOne: "Fuente",
      sourcesMany: "Fuentes ({n})",
      noAnswer: "Lo siento, no recibí una respuesta. Vuelve a intentarlo.",
      networkError:
        "Lo siento, ahora mismo no pude conectarme con el asistente de la biblioteca. " +
        "Vuelve a intentarlo en unos momentos.",
      retryLabel: "Reintentar",
      notConnected:
        "El asistente de la biblioteca aún no está conectado. Vuelve a intentarlo más tarde.",
      signInHeading: "Inicia sesión para probar el asistente",
      signInIntro: "Esta versión de prueba es privada. Usa la cuenta que te dieron.",
      signInUsernameLabel: "Nombre de usuario",
      signInPasswordLabel: "Contraseña",
      signInSubmit: "Iniciar sesión",
      signInPending: "Iniciando sesión…",
      signInFailed:
        "Ese nombre de usuario y esa contraseña no funcionaron. Revísalos e inténtalo de nuevo.",
      signInUnavailable:
        "Lo siento, ahora mismo no se puede iniciar sesión. Vuelve a intentarlo en unos momentos.",
      signInExpired: "Tu sesión terminó. Vuelve a iniciar sesión."
    }
  };

  // The active language, and whether the PERSON chose it. That second flag is load-bearing:
  // only an explicit choice is sent to the backend (see requestBody), so a visitor who never
  // touches the control gets exactly the request - and exactly the auto-detected reply
  // language - that they got before this feature existed.
  var lang = { code: DEFAULT_LANG, chosen: false };

  /** One localized string. Falls back to the default language for a key a table is missing. */
  function t(key) {
    var table = STRINGS[lang.code] || STRINGS[DEFAULT_LANG];
    var value = table[key];
    return value === undefined ? STRINGS[DEFAULT_LANG][key] : value;
  }

  /** t() with {n} filled in. The result is always used as text, never as markup. */
  function tCount(key, n) {
    return String(t(key)).replace("{n}", String(n));
  }

  /** The chosen language code, or null if nobody chose one. */
  function chosenLanguage() {
    return lang.chosen ? lang.code : null;
  }

  /**
   * Set the language. `chosen` records that a PERSON picked it (which is what reaches the
   * backend); the UI only ever calls this with chosen = true. Exported for test isolation:
   * the module is loaded once per process, so a test that clicks "Español" must be able to
   * put the module back to its shipped default for the tests after it.
   */
  function setLanguage(code, chosen) {
    if (!STRINGS[code]) return false;
    lang.code = code;
    lang.chosen = !!chosen;
    return true;
  }

  function resetLanguage() {
    setLanguage(DEFAULT_LANG, false);
  }

  function getLanguage() {
    return { code: lang.code, chosen: lang.chosen };
  }

  // =========================================================================
  // ========================  END LOCALIZATION  =============================
  // =========================================================================
  //
  // Everything below builds DOM. No user-visible literal belongs past this line -
  // it comes from t() / STRINGS above, and the contract test scans this half of the
  // file to keep it that way.

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
  // Timing knobs only. Every user-visible string lives in STRINGS above, so this object
  // carries numbers and nothing else - a copy change never touches it.
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
    // Sign-in is one small unsigned call to Cognito with no model behind it, so it has no
    // reason to sit near the query timeout. Short enough that a dead network reads as dead.
    signInTimeoutMs: 10000,
    // How long the boot sequence will wait for theme.json before mounting with the built-in
    // defaults. Mounting is DEFERRED until this settles (see the boot block at the bottom),
    // because a widget that paints maroon and then repaints in the library's colour is worse
    // than one that appears a beat later - so this is the whole of the delay a broken or slow
    // theme file can add to the widget appearing, and it is capped here.
    themeTimeoutMs: 1500
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

  // ---- sign-in gate: TEMPORARY, removed at go-live ------------------------
  //
  // WHY. The bot is not on gavilan.edu yet; the only thing driving the billable /query is a
  // demo link, and a link travels. The real gate is the API Gateway JWT authorizer on POST
  // /query - this code is only how a person obtains a token that satisfies it. Nothing here
  // can weaken it: an embed that skipped the sign-in ids does not get an ungated backend, it
  // gets a 401 on every question.
  //
  // TWO ATTRIBUTES, and their absence is the ungated widget. The offline dev harness, the
  // pre-gate embeds, and the post-go-live tag all carry neither, and for them every path below
  // is a no-op - which is the same opt-in-by-attribute shape as data-usage-events.
  //
  // WHAT IS STORED, AND WHERE. Exactly one thing: { clientId, accessToken, expiresAt } under
  // one sessionStorage key. Not the password, not the username, and not the refresh token -
  // that last one is fetched and dropped unread, so the stored record cannot outlive the one
  // day Cognito gave the access token.
  //
  // sessionStorage AND NOT localStorage, deliberately. Gavilan has shared library terminals,
  // and a day-long bearer token that survives a browser restart on a public machine is a
  // password left on the desk. sessionStorage is scoped to the tab and dies with it, which
  // buys back the reload (the thing people actually hit) without buying the borrowed laptop.
  // Do not "improve" the persistence here.
  //
  // The record is READ BACK through the same expiry check a live token goes through, so a
  // stored token past its expiry raises the overlay instead of sending a request the widget
  // already knows is doomed - and it is removed on sign-out, on a readable 401, on expiry, and
  // on a failed sign-in.
  var AUTH_POOL_ATTR = "data-user-pool-id";
  var AUTH_CLIENT_ATTR = "data-client-id";
  // The one Cognito operation this widget calls. Unsigned and public: it is what every browser
  // sign-in does, and it is why the app client must have no secret.
  var AUTH_TARGET = "AWSCognitoIdentityProviderService.InitiateAuth";
  // Namespaced, because sessionStorage is shared with whatever else the host page runs.
  var AUTH_STORAGE_KEY = "gavilan-chatbot-session";

  // { accessToken, expiresAt } while signed in, null otherwise. The in-memory copy of what
  // sessionStorage holds; currentSession() below is what keeps the two in step.
  var session = null;

  function trimmedAttr(el, name) {
    var raw = el && el.getAttribute ? el.getAttribute(name) : null;
    return raw && String(raw).trim() ? String(raw).trim() : null;
  }

  /**
   * This embed's sign-in configuration, or null if the tag carries none (the ungated case).
   * The region is DERIVED from the pool id, whose documented format is `<region>_<suffix>`:
   * a third attribute would be a third thing that can drift out of step with the other two.
   */
  function authConfig() {
    var el = scriptEl();
    if (!el) return null;
    var poolId = trimmedAttr(el, AUTH_POOL_ATTR);
    var clientId = trimmedAttr(el, AUTH_CLIENT_ATTR);
    if (!poolId || !clientId) return null;
    var cut = poolId.indexOf("_");
    if (cut <= 0) return null;
    return { poolId: poolId, clientId: clientId, region: poolId.slice(0, cut) };
  }

  /** Whether this embed is gated at all. */
  function authRequired() {
    return authConfig() !== null;
  }

  /**
   * sessionStorage, or null if it cannot be used. Access itself can THROW (a sandboxed iframe,
   * or a browser set to block site data), which is why this is a function with a try around the
   * lookup rather than a variable read once. Null means the gate degrades to exactly what it
   * did before: a token held in memory, and a reload that signs in again.
   */
  function sessionStore() {
    try {
      return typeof sessionStorage !== "undefined" && sessionStorage ? sessionStorage : null;
    } catch (e) {
      return null;
    }
  }

  /**
   * The stored session, if there is a usable one. Anything else - absent, unparseable, the
   * wrong shape, a token minted for a DIFFERENT app client (the embed was repointed), or one
   * already past its expiry - is removed rather than trusted, so a bad record cannot wedge a
   * tab into a state the sign-in form is hidden behind.
   */
  function readStoredSession() {
    var cfg = authConfig();
    var store = sessionStore();
    // An ungated embed never reads a token: its request has to stay byte-identical to the
    // pre-gate one, header and all.
    if (!cfg || !store) return null;
    var raw = null;
    try {
      raw = store.getItem(AUTH_STORAGE_KEY);
    } catch (e) {
      return null;
    }
    if (!raw) return null;
    var saved = null;
    try {
      saved = JSON.parse(raw);
    } catch (e) {
      saved = null;
    }
    var usable =
      saved &&
      typeof saved.accessToken === "string" && saved.accessToken &&
      typeof saved.expiresAt === "number" && isFinite(saved.expiresAt) &&
      saved.clientId === cfg.clientId &&
      saved.expiresAt > Date.now();
    if (!usable) {
      setSession(null);
      return null;
    }
    return { accessToken: saved.accessToken, expiresAt: saved.expiresAt };
  }

  /**
   * The one writer. Memory and storage move together here and nowhere else, so there is no path
   * that drops the token but leaves the record - which is the failure that would matter, a tab
   * showing the sign-in form while a readable token sits in storage.
   */
  function setSession(next) {
    session = next;
    var store = sessionStore();
    if (!store) return;
    try {
      if (!next) {
        store.removeItem(AUTH_STORAGE_KEY);
        return;
      }
      var cfg = authConfig();
      if (!cfg) return;
      store.setItem(
        AUTH_STORAGE_KEY,
        JSON.stringify({
          clientId: cfg.clientId,
          accessToken: next.accessToken,
          expiresAt: next.expiresAt
        })
      );
    } catch (e) {
      /* blocked or full: memory-only, which is where this feature started */
    }
  }

  /**
   * The live session, or null. Restores from storage when memory has none, which is the whole
   * reload story: a fresh page has an empty `session` and a stored record that is still good.
   * Expiry is applied on the way out, so a restored token gets the same check a freshly minted
   * one does and no caller has to know where the session came from.
   */
  function currentSession() {
    if (!session) session = readStoredSession();
    if (session && session.expiresAt <= Date.now()) {
      setSession(null);
      return null;
    }
    return session;
  }

  /** The held access token if it is still valid, else null (dropping the dead session). */
  function accessToken() {
    var live = currentSession();
    return live ? live.accessToken : null;
  }

  function signedIn() {
    return accessToken() !== null;
  }

  function signOut() {
    setSession(null);
  }

  /** A rejection the UI can branch on without matching message text. */
  function authError(reason) {
    var err = new Error("sign-in: " + reason);
    err.reason = reason;
    return err;
  }

  /**
   * Exchange a username and password for an access token: ONE unsigned fetch to Cognito's public
   * InitiateAuth, no SDK and no build step.
   *
   * USER_PASSWORD_AUTH rather than SRP because SRP needs big-integer crypto no dependency-free
   * widget is going to carry. The password crosses the wire inside TLS instead of never leaving
   * the browser - the right trade for one shared demo account, and not a pattern for real
   * student accounts.
   *
   * The password is a parameter and a request body field and nothing else: never stored, never
   * logged, and never sent to /query. Resolves true; rejects with `.reason`:
   *   "credentials"  - Cognito said no, or answered with a challenge instead of a token
   *   "unavailable"  - the call never completed (network, DNS, CORS, timeout)
   */
  function signIn(username, password) {
    var cfg = authConfig();
    if (!cfg || typeof fetch === "undefined") {
      return Promise.reject(authError("unavailable"));
    }
    var controller =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = null;
    if (controller) {
      timer = setTimeout(function () {
        controller.abort();
      }, CONFIG.signInTimeoutMs);
    }
    return fetch("https://cognito-idp." + cfg.region + ".amazonaws.com/", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": AUTH_TARGET
      },
      body: JSON.stringify({
        AuthFlow: "USER_PASSWORD_AUTH",
        ClientId: cfg.clientId,
        AuthParameters: { USERNAME: username, PASSWORD: password }
      }),
      signal: controller ? controller.signal : undefined
    })
      .then(function (res) {
        // Cognito reports a bad password as a 4xx with a typed JSON body, so the body is worth
        // reading either way - but its `message` is never surfaced. It is AWS's wording, in
        // AWS's language, and the pool is configured not to distinguish a wrong name from a
        // wrong password, which is only worth doing if the UI does not undo it.
        return res.json().then(
          function (data) { return { ok: res.ok, data: data }; },
          function () { return { ok: res.ok, data: null }; }
        );
      })
      .then(function (r) {
        var result = r.data && r.data.AuthenticationResult;
        var token =
          result && typeof result.AccessToken === "string" ? result.AccessToken : null;
        if (!r.ok || !token) {
          // A challenge is a 200 with no token, and the likely one here is
          // NEW_PASSWORD_REQUIRED: the account exists but its password was never made
          // permanent. That is a setup mistake nobody at the keyboard can fix, so it reads to
          // them as a failed sign-in and the name goes to the console for whoever set it up.
          if (r.data && r.data.ChallengeName && typeof console !== "undefined" && console.error) {
            console.error("[gavilan-widget] sign-in challenge:", r.data.ChallengeName);
          }
          throw authError("credentials");
        }
        // ExpiresIn is seconds. Expire a minute early, so a token that would die mid-flight is
        // treated as gone BEFORE the request rather than as an unreadable failure after it -
        // see the 401 note in realQuery for why after is not a usable signal.
        var ttl = typeof result.ExpiresIn === "number" ? result.ExpiresIn : 3600;
        setSession({
          accessToken: token,
          expiresAt: Date.now() + Math.max(0, ttl - 60) * 1000
        });
        return true;
      })
      .catch(function (err) {
        // A failed attempt leaves nothing behind, including anything an earlier attempt in this
        // tab stored: whoever is at the keyboard now could not prove they are the same person.
        setSession(null);
        if (err && err.reason) throw err;
        throw authError("unavailable");
      })
      .finally(function () {
        if (timer) clearTimeout(timer);
      });
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
      return Promise.resolve({ answer: t("notConnected"), sources: [] });
    }
    // A gated embed with no live token must not spend a request to discover that. This is also
    // the only expiry signal a browser reliably gives us (again: see realQuery).
    if (authRequired() && !signedIn()) {
      return Promise.reject(authError("expired"));
    }
    return realQuery(url, messages);
  }

  /**
   * The POST body. Three shapes, written out rather than assembled blindly, because the
   * DEFAULT one is a contract:
   *   - nothing opted in     -> the literal { messages } this widget has always sent;
   *   - a chosen language    -> + `language`, so the backend can tell the model which
   *                             language to answer in even when the question is typed in
   *                             the other one (an unset field leaves the backend's existing
   *                             auto-detection alone);
   *   - data-usage-events    -> + `include_usage` (demo-only metering, see above).
   */
  function requestBody(messages) {
    var language = chosenLanguage();
    var usage = usageEventsEnabled();
    if (!language && !usage) return JSON.stringify({ messages: messages });
    var body = { messages: messages };
    if (usage) body.include_usage = true;
    if (language) body.language = language;
    return JSON.stringify(body);
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
    // The token rides in the Authorization HEADER and never in the body - it must not end up in
    // a request log, an eval capture, or anything that echoes the payload back. An ungated
    // embed adds no header at all, so its request stays byte-identical to the pre-gate one.
    var headers = { "Content-Type": "application/json" };
    var token = accessToken();
    if (token) headers.Authorization = "Bearer " + token;
    return fetch(url, {
      method: "POST",
      headers: headers,
      body: requestBody(messages),
      signal: controller ? controller.signal : undefined
    })
      .then(function (res) {
        if (res.status === 401 || res.status === 403) {
          // The authorizer rejected the token. MOSTLY UNREACHABLE FROM A BROWSER, and worth
          // saying why: API Gateway "adds the configured CORS headers to the response from an
          // integration", and an authorizer rejection never reaches the integration - so this
          // 401 arrives with no Access-Control-Allow-Origin and the browser converts it into an
          // opaque network failure before any status is readable here. That is why expiry is
          // caught BEFORE the request (see sendQuery) rather than from the response. Kept
          // anyway: it is correct wherever the response IS readable, and costs one comparison.
          // signOut clears the stored record too, so a reload cannot resurrect a token the
          // authorizer has already refused.
          signOut();
          throw authError("expired");
        }
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

  // =========================================================================
  // ==============================  THEME  ==================================
  // =========================================================================
  //
  // The library owns the bucket and the distribution after handover, so the three things
  // a customer actually asks to change - the highlight colour, the typeface, and the
  // starter questions - are read at runtime from ONE file uploaded next to widget.js:
  //
  //     https://<the widget CDN>/theme.json
  //
  // No redeploy, no settings page, and nothing hand-edited inside a shipped file. See
  // docs/widget-theming.md for the upload steps; frontend/theme.example.json is the
  // annotated copy that ships beside this one.
  //
  // JSON, never JS. A .js config would be executable code running on the library's page
  // with the widget's privileges; a JSON file is data that this module parses and
  // ALLOWLISTS. Every value below is either matched against a pattern (the colour), looked
  // up in a table we own (the font), or rendered as a text node (the questions) - nothing
  // from the file is ever concatenated into CSS without passing one of those.
  //
  // Failure is per key and silent: an unreadable colour leaves the colour alone, a
  // misspelled font keyword leaves the font alone, and malformed JSON (or a 404, which is
  // what every install serves until the first upload) leaves everything alone. Soft-fail
  // is the point - the file is edited by hand, by someone who cannot redeploy, so a typo
  // has to cost them one wrong setting rather than a blank widget.

  var THEME_FILE = "theme.json";

  // The shipped defaults. `themeCss` emits nothing for a value still equal to its default,
  // so an install with no theme.json renders byte-identically to the pre-theme widget.
  var DEFAULT_HIGHLIGHT = "#8a1c30";
  var DEFAULT_FONT = "system";

  // Family only - never size, weight or line-height. Those are wired to the layout (the
  // panel clamps, the 1.4.4 resize behaviour, the two-tone focus rings), and handing them
  // to a text field turns a colour change into an accessibility regression.
  //
  // Enumerated keywords rather than a free-text font stack, and every stack here resolves
  // on both macOS and Windows with no download and no @font-face: a customer typing a
  // family name they have installed locally would ship a widget that falls back to Times
  // for everyone else, and they would never see it.
  var FONT_KEYWORDS = ["system", "sans", "serif", "mono", "inherit"];
  var FONT_STACKS = {
    // The OS UI font - what the widget has always used.
    system:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    sans: "Arial, Helvetica, 'Helvetica Neue', sans-serif",
    serif: "Georgia, 'Times New Roman', Times, serif",
    mono: "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Courier New', monospace",
    // "inherit" is not a stack: it hands the widget over to whatever the host page is set
    // in, which is the one option that cannot be enumerated. Handled separately in themeCss
    // because it also has to defeat `:host { all: initial }`.
    inherit: null
  };

  // Four is what the panel shows without pushing the greeting off screen on a phone, and
  // the character cap is roughly one line of a chip at the widget's width. Over either
  // limit the extra entries are dropped rather than truncated - a half-sentence question
  // reads as a bug, a missing one reads as a shorter list.
  var MAX_STARTER_QUESTIONS = 4;
  var MAX_STARTER_CHARS = 120;

  function defaultTheme() {
    return {
      highlight: DEFAULT_HIGHLIGHT,
      font: DEFAULT_FONT,
      // null (not a table of built-ins) so `starterQuestions` can tell "the customer said
      // nothing" from "the customer said something", which is what drives the Spanish
      // fallback below.
      starterQuestions: null
    };
  }

  var theme = defaultTheme();

  function resetTheme() {
    theme = defaultTheme();
  }

  /**
   * `#abc` / `#aabbcc` (any case) expanded to a lowercase six-digit hex, or null.
   *
   * Deliberately narrow, for two independent reasons. It is a security boundary: this
   * string is concatenated into a stylesheet, so anything that could carry a `;` or a `}`
   * would let theme.json write arbitrary CSS into the widget. And the ink derivation below
   * needs actual channel values, which a named colour or a `var()` cannot give us.
   */
  function normalizeHex(value) {
    if (typeof value !== "string") return null;
    var hex = value.trim().toLowerCase();
    if (!/^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/.test(hex)) return null;
    if (hex.length === 4) {
      hex = "#" + hex[1] + hex[1] + hex[2] + hex[2] + hex[3] + hex[3];
    }
    return hex;
  }

  /** WCAG relative luminance of a normalized six-digit hex. */
  function relativeLuminance(hex) {
    var channels = [
      parseInt(hex.slice(1, 3), 16),
      parseInt(hex.slice(3, 5), 16),
      parseInt(hex.slice(5, 7), 16)
    ].map(function (v) {
      var c = v / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  }

  /**
   * Black or white text for a given highlight - whichever contrasts better with it.
   *
   * There is deliberately NO validation of the customer's colour against a contrast floor,
   * because this derivation makes one unnecessary: the worst case over every colour in the
   * sRGB cube is the crossover point where black and white contrast equally, at luminance
   * 0.1791, and that gives 4.58:1 - above the 4.5:1 that 1.4.3 asks for. So text on the
   * highlight passes for ANY colour a customer can type, and there is nothing to reject.
   */
  function inkFor(hex) {
    return relativeLuminance(hex) > 0.1791 ? "#000000" : "#ffffff";
  }

  /** One language's starter questions from the file, capped and cleaned, or null. */
  function readQuestionList(value) {
    if (!Array.isArray(value)) return null;
    var out = [];
    for (var i = 0; i < value.length && out.length < MAX_STARTER_QUESTIONS; i++) {
      if (typeof value[i] !== "string") continue;
      var q = value[i].replace(/\s+/g, " ").trim();
      if (!q || q.length > MAX_STARTER_CHARS) continue;
      out.push(q);
    }
    return out.length ? out : null;
  }

  /**
   * The per-language starter questions from the file, or null if it offered none usable.
   * Only the languages the widget actually offers are read, so an unknown key is ignored
   * rather than becoming a language nobody can select.
   */
  function readStarterQuestions(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    var out = null;
    for (var i = 0; i < LANGUAGES.length; i++) {
      var list = readQuestionList(value[LANGUAGES[i]]);
      if (!list) continue;
      out = out || {};
      out[LANGUAGES[i]] = list;
    }
    return out;
  }

  /**
   * Merge a parsed theme.json over the built-in defaults. Returns true if anything moved.
   * Always starts from a fresh default set, so this is idempotent and a re-parse of a
   * broken file cannot leave half of a previous one behind.
   */
  function applyTheme(data) {
    theme = defaultTheme();
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    var changed = false;

    var hex = normalizeHex(data.highlightColor);
    if (hex) {
      theme.highlight = hex;
      changed = true;
    }

    // indexOf against our own list, not a property lookup on FONT_STACKS: `"constructor"`
    // is a truthy property of every object literal, and a lookup would happily resolve it.
    if (typeof data.fontFamily === "string") {
      var font = data.fontFamily.trim().toLowerCase();
      if (FONT_KEYWORDS.indexOf(font) >= 0) {
        theme.font = font;
        changed = true;
      }
    }

    var questions = readStarterQuestions(data.starterQuestions);
    if (questions) {
      theme.starterQuestions = questions;
      changed = true;
    }

    return changed;
  }

  /**
   * The starter questions to show, in the active language.
   *
   * Spanish is optional in the file and falls back to the customer's ENGLISH list rather
   * than to our built-in Spanish one. That looks backwards until you consider what a
   * customised English list means: they have replaced our questions with theirs, and the
   * shipped Spanish questions may now ask about a service they removed. Showing their
   * English wording to a Spanish reader is worse copy but honest; machine-translating it
   * would be neither, so it is not done.
   */
  function starterQuestions() {
    if (theme.starterQuestions) {
      var list = theme.starterQuestions[lang.code] || theme.starterQuestions[DEFAULT_LANG];
      if (list && list.length) return list;
    }
    return t("suggestedQuestions");
  }

  /**
   * The theme's CSS, appended AFTER the shipped stylesheet inside the same <style> element.
   * Same-specificity later rules win, so this overrides without the base stylesheet having
   * to know a theme exists - and it is empty for an unthemed install, which is what keeps
   * the default rendering provably unchanged.
   */
  function themeCss() {
    var rules = [];
    if (theme.highlight !== DEFAULT_HIGHLIGHT) {
      var ink = inkFor(theme.highlight);
      // --brand is the single source of truth the base stylesheet already derives the
      // header, launcher, buttons, user bubbles and links from; the two ink variables are
      // everything drawn ON the highlight.
      rules.push(
        ".root { --brand: " + theme.highlight +
          "; --accent-ink: " + ink +
          "; --user-ink: " + ink + "; }"
      );
    }
    if (theme.font === "inherit") {
      // `:host { all: initial }` resets font-family to the browser default, which is not
      // the host page's family - so inheriting takes an explicit later declaration on the
      // host element, where `inherit` means "the page element this widget was appended to".
      rules.push(":host { font-family: inherit; }");
      rules.push(".root, .header__title { font-family: inherit; }");
    } else if (theme.font !== DEFAULT_FONT) {
      rules.push(".root, .header__title { font-family: " + FONT_STACKS[theme.font] + "; }");
    }
    return rules.join("\n");
  }

  /** Absolute URL of theme.json, resolved next to this script's own src. */
  function themeUrl() {
    var el = scriptEl();
    if (!el) return null;
    var src = el.src || (el.getAttribute ? el.getAttribute("src") : null);
    if (!src) return null;
    try {
      return new URL(THEME_FILE, src).href;
    } catch (e) {
      return null;
    }
  }

  /**
   * Fetch and apply theme.json. NEVER rejects and never waits longer than
   * CONFIG.themeTimeoutMs: the boot sequence holds the mount on this promise, so a hung
   * CDN must degrade to the default theme rather than to no widget at all.
   *
   * Cross-origin by construction (the CDN is not the library's domain), so the widget
   * distribution serves this path with CORS headers - see the theme behavior in
   * infra/infra/infra_stack.py.
   */
  function loadTheme() {
    return new Promise(function (resolve) {
      var url = themeUrl();
      if (!url || typeof fetch === "undefined") return resolve(false);
      var settled = false;
      function finish(applied) {
        if (settled) return;
        settled = true;
        resolve(applied);
      }
      var timer = setTimeout(function () { finish(false); }, CONFIG.themeTimeoutMs);
      function done(applied) {
        clearTimeout(timer);
        finish(applied);
      }
      try {
        fetch(url, { method: "GET" })
          .then(function (res) {
            // A 404 is the NORMAL state of a fresh install - nobody has uploaded a theme
            // yet - so it is not an error path, just an empty one.
            return res && res.ok ? res.json() : null;
          })
          .then(
            function (data) { done(applyTheme(data)); },
            function () { done(false); }
          );
      } catch (e) {
        done(false);
      }
    });
  }

  // =========================================================================
  // ============================  END THEME  ================================
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
  //
  // Bitter belongs to the DEFAULT theme, not to the widget: any other `fontFamily` keyword
  // replaces the title's family too, which would leave this fetching a webfont nothing uses.
  // So mount only calls this when the theme is the default one - see the guard there.
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
    // `hidden` is a UA-stylesheet `display: none`, and every one of these sets `display`
    // from a class - which wins on specificity and paints the element anyway. That is why
    // .panel and .launcher always carried this rule; .composer needs it for the same reason
    // (its `display: flex` was beating its own `hidden` attribute, so hiding it behind the
    // sign-in gate did nothing visible and the dead text box stayed on screen).
    ".panel[hidden], .launcher[hidden], .composer[hidden] { display: none !important; }",
    // Everything under the header, in one positioned box. Its only job is to be the
    // containing block for the sign-in overlay, so the overlay covers the transcript and
    // the composer and stops at the header - never the host page, which this widget is a
    // guest on. (The wrapper outlives the gate; an absolutely-positioned child needs a
    // positioned ancestor and `.panel` is the wrong one.)
    ".panel__body {",
    "  position: relative; flex: 1 1 auto; min-height: 0;",
    "  display: flex; flex-direction: column;",
    "}",
    // header
    //
    // WRAPS on purpose. The header now holds a title AND the language control, and a translated
    // title is longer than the English one ("Ayuda de la biblioteca" vs "Library Help"), so on a
    // narrow panel the two genuinely do not fit on one line. Wrapping drops the controls onto
    // their own right-aligned row instead of truncating the title or pushing the close button
    // off the edge - and it needs no per-language width tuning, so a third language cannot
    // break the layout.
    ".header {",
    "  display: flex; align-items: center; justify-content: space-between;",
    "  flex-wrap: wrap; row-gap: 8px;",
    "  padding: 12px 14px; background: var(--accent); color: var(--accent-ink);",
    "}",
    // Title uses Bitter (loaded from Google Fonts into the host document head at mount);
    // falls back to a serif until/if it loads. Only the title uses it - body/UI stays default.
    ".header__title {",
    "  font-family: 'Bitter', Georgia, 'Times New Roman', serif; font-size: 15px; font-weight: 700;",
    // Takes the row it is on, and ellipsises only as a last resort (one unbreakably long word),
    // since the header wraps before it comes to that.
    "  flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;",
    "}",
    ".header__close {",
    "  appearance: none; border: none; background: transparent;",
    "  color: var(--accent-ink); font-size: 22px; line-height: 1;",
    "  cursor: pointer; padding: 2px 6px; border-radius: 6px;",
    "}",
    // The header's own overlays and rings are white because the ink on the highlight is
    // white - they are the SAME decision, not two. So they track --accent-ink (via
    // currentColor for the washes), which the theme derives from the highlight colour.
    // A themed install with a light highlight gets black ink and black washes here, and a
    // white ring on pale yellow - which is what these used to hardcode - never happens.
    // This is not the two-tone --focus-ring/--focus-halo pair, which stays fixed.
    ".header__close:hover { background: color-mix(in srgb, currentColor 18%, transparent); }",
    ".header__close:focus-visible { outline: 2px solid var(--accent-ink); outline-offset: 1px; }",
    ".header__actions { display: inline-flex; align-items: center; gap: 2px; flex: 0 0 auto; margin-left: auto; }",
    // language control: a segmented pair of real buttons in the header. The ACTIVE one is a
    // filled white pill, not merely a different text color, so the state reads without relying
    // on color perception - and it is exposed to assistive tech as aria-pressed, which is also
    // what drives the styling (one source of truth, no parallel class to fall out of sync).
    ".header__lang {",
    "  display: inline-flex; align-items: center; gap: 2px; margin-right: 4px;",
    "  background: color-mix(in srgb, currentColor 16%, transparent); border-radius: 999px; padding: 2px;",
    "}",
    ".header__lang-btn {",
    "  appearance: none; border: none; background: transparent; color: var(--accent-ink);",
    "  font: inherit; font-size: 11px; font-weight: 700; line-height: 1; white-space: nowrap;",
    "  cursor: pointer; padding: 4px 8px; border-radius: 999px;",
    "}",
    ".header__lang-btn:hover { background: color-mix(in srgb, currentColor 20%, transparent); }",
    // The active pill is the ink/highlight pair inverted, so it carries exactly the ratio
    // the un-inverted pair does - at worst the 4.58:1 floor that picking the better of
    // black and white guarantees.
    ".header__lang-btn[aria-pressed=\"true\"] { background: var(--accent-ink); color: var(--brand); }",
    ".header__lang-btn:focus-visible { outline: 2px solid var(--accent-ink); outline-offset: 2px; }",
    ".header__expand {",
    "  appearance: none; border: none; background: transparent;",
    "  color: var(--accent-ink); font-size: 17px; line-height: 1;",
    "  cursor: pointer; padding: 2px 6px; border-radius: 6px;",
    "}",
    ".header__expand:hover { background: color-mix(in srgb, currentColor 18%, transparent); }",
    ".header__expand:focus-visible { outline: 2px solid var(--accent-ink); outline-offset: 1px; }",
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
    // sign-in gate (TEMPORARY - this block goes at go-live with the rest of the gate).
    // A gate you pass THROUGH, not furniture that stays: it covers the panel's body while it
    // is up and the whole node leaves the DOM once you are through it. Scoped to `.panel__body`
    // and nothing wider - this is a third-party embed, so darkening the page it sits on is not
    // ours to do. The card borrows the composer's tokens throughout (same border, same two-tone
    // focus ring, same brand button) so it reads as part of the widget, not a bolted-on form.
    ".signin-overlay {",
    "  position: absolute; inset: 0; z-index: 1;",
    "  display: flex; align-items: center; justify-content: center;",
    "  padding: 16px; overflow-y: auto;",
    // Nearly opaque rather than a light wash: what is behind it cannot be read or reached, and
    // saying so honestly beats a teasing 50% scrim. Every piece of text in here sits on the
    // white card above, so the wash carries no contrast requirement of its own.
    "  background: rgba(250,251,252,0.94);",
    "}",
    ".signin {",
    "  display: flex; flex-direction: column; gap: 8px;",
    "  width: 100%; max-width: 300px; margin: auto;",
    "  padding: 16px; background: #fff;",
    "  border: 1px solid var(--panel-border); border-radius: 12px;",
    "  box-shadow: 0 6px 20px rgba(0,0,0,0.14);",
    "}",
    ".signin__heading { font-weight: 600; font-size: 14px; }",
    ".signin__intro { font-size: 12px; color: var(--muted); margin: 0; }",
    ".signin__field { display: flex; flex-direction: column; gap: 3px; }",
    ".signin__label { font-size: 12px; color: var(--muted); }",
    ".signin__input {",
    "  padding: 9px 11px; border: 1px solid var(--line); border-radius: 10px;",
    "  font: inherit; color: inherit; background: #fff; min-height: 40px;",
    "}",
    ".signin__input:focus-visible {",
    "  outline: 2px solid var(--focus-ring);",
    "  outline-offset: 1px;",
    "  box-shadow: 0 0 0 1px var(--focus-halo);",
    "  border-color: color-mix(in srgb, var(--brand) 55%, transparent);",
    "}",
    ".signin__submit {",
    "  appearance: none; border: none; border-radius: 10px; cursor: pointer;",
    "  background: var(--accent); color: var(--accent-ink);",
    "  font: inherit; font-weight: 600; font-size: 14px; height: 40px;",
    "}",
    ".signin__submit:hover:not(:disabled) { filter: brightness(1.07); }",
    ".signin__submit:disabled { opacity: .5; cursor: default; }",
    ".signin__submit:focus-visible {",
    "  outline: 3px solid var(--focus-ring); outline-offset: 2px;",
    "  box-shadow: 0 0 0 2px var(--focus-halo);",
    "}",
    // Same palette as the failed-send bubble, which the contrast suite already measures at
    // 4.5:1 - a sign-in failure is not a different kind of error.
    ".signin__error {",
    "  font-size: 13px; border-radius: 8px; padding: 7px 9px;",
    "  background: var(--error-bg); color: var(--error-ink);",
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
  // Same rule for the sign-in fields: a <label for> resolves only inside the shadow root that
  // holds both ends of it. TEMPORARY, with the rest of the gate.
  var SIGNIN_USERNAME_ID = "gavilan-chatbot-signin-username";
  var SIGNIN_PASSWORD_ID = "gavilan-chatbot-signin-password";

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

    // Bitter is the default theme's title face, so a themed font keyword means there is no
    // webfont to fetch at all (see ensureTitleFont).
    if (theme.font === DEFAULT_FONT) {
      ensureTitleFont(doc); // host document head; safe no-op if blocked
    }

    var host = doc.createElement("div");
    host.id = HOST_ID;
    doc.body.appendChild(host);
    var shadow = host.attachShadow({ mode: "open" });

    var style = doc.createElement("style");
    // The theme's overrides go in the SAME element, after the shipped rules, so the first
    // paint is already themed: there is no second stylesheet to arrive and no frame in
    // which the default colours are on screen. Empty string for an unthemed install.
    var overrides = themeCss();
    style.textContent = overrides ? STYLES + "\n" + overrides : STYLES;
    shadow.appendChild(style);

    var root = doc.createElement("div");
    root.className = "root";
    // The chrome declares its own language rather than inheriting whatever the host page
    // says (3.1.2). applyLanguage() sets it, here and on every switch, so the declaration
    // tracks the language the chrome is actually in. Individual turns carry their own
    // `lang` on top of it (see appendBotMessage), because a switch does not retranslate
    // what was already said.
    shadow.appendChild(root);

    // in-memory session state (no storage). `started` flips on the first real message and
    // freezes the opening state (greeting + starter questions) against language switches.
    var state = {
      open: false,
      pending: false,
      expanded: false,
      started: false,
      messages: [],
      lastQuestion: null,
      // A sign-in call is in flight (TEMPORARY, with the gate). Separate from `pending`, which
      // is about a question: the two forms are never on screen together, so they never race.
      signingIn: false
    };

    // launcher
    var launcher = doc.createElement("button");
    launcher.type = "button";
    launcher.className = "launcher";
    // NO aria-label, in either language. The old one named opening the chat, which
    // OVERRODE the visible "Ask the Library" as the accessible name and left a
    // speech-input user unable to activate the button by the words on screen (2.5.3).
    // The button's own text is the name now - localized, so the spoken words and the
    // printed ones still match in Spanish - and aria-haspopup carries what that label
    // used to hint at: it opens a dialog.
    launcher.setAttribute("aria-haspopup", "dialog");
    var lIcon = doc.createElement("span");
    lIcon.className = "launcher__icon";
    lIcon.setAttribute("aria-hidden", "true");
    lIcon.appendChild(iconChat(doc)); // inline SVG chat bubble
    var lText = doc.createElement("span");
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
    // Its aria-label is localized, so applyLanguage() sets it.
    panel.setAttribute("aria-modal", "true");
    panel.hidden = true;

    var header = doc.createElement("div");
    header.className = "header";
    var hTitle = doc.createElement("span");
    hTitle.className = "header__title";

    // Language control. Real buttons (focusable, Enter/Space, name from their own text) in a
    // labelled group, with the active one exposed as aria-pressed. No ID-based ARIA pairing:
    // `for`/`aria-labelledby` do not cross a shadow boundary, so the group carries its own
    // aria-label and each button names itself.
    var hLang = doc.createElement("div");
    hLang.className = "header__lang";
    hLang.setAttribute("role", "group");
    var langButtons = [];
    LANGUAGES.forEach(function (code) {
      var btn = doc.createElement("button");
      btn.type = "button";
      btn.className = "header__lang-btn";
      btn.setAttribute("data-lang", code);
      // Each language in its own name, identical in both UIs - that is the discovery
      // affordance, so it must not itself be translated.
      btn.textContent = STRINGS[code].languageName;
      btn.addEventListener("click", function () { chooseLanguage(code); });
      hLang.appendChild(btn);
      langButtons.push(btn);
    });

    var hExpand = doc.createElement("button");
    hExpand.type = "button";
    hExpand.className = "header__expand";
    hExpand.setAttribute("aria-pressed", "false");
    hExpand.appendChild(iconExpand(doc)); // inline SVG, swapped on toggle
    var hClose = doc.createElement("button");
    hClose.type = "button";
    hClose.className = "header__close";
    hClose.textContent = "×"; // ×
    var hActions = doc.createElement("div");
    hActions.className = "header__actions";
    hActions.appendChild(hLang);
    hActions.appendChild(hExpand);
    hActions.appendChild(hClose);
    header.appendChild(hTitle);
    header.appendChild(hActions);

    var thread = doc.createElement("div");
    thread.className = "thread";
    thread.setAttribute("role", "log");
    thread.setAttribute("aria-live", "polite");
    thread.setAttribute("aria-relevant", "additions");

    var form = doc.createElement("form");
    form.className = "composer";
    var input = doc.createElement("textarea");
    input.className = "composer__input";
    input.setAttribute("rows", "1");
    // Advisory input cap so a user gets feedback instead of typing a wall of text. The real
    // limit is the server-side length check; this is intentionally lower and UX-only.
    input.setAttribute("maxlength", "1000");
    var send = doc.createElement("button");
    send.type = "submit";
    send.className = "composer__send";
    form.appendChild(input);
    form.appendChild(send);

    // ---- sign-in form (TEMPORARY: the whole block leaves at go-live) ----
    //
    // It sits in an OVERLAY across the panel's body, and the composer is hidden underneath it,
    // so there is never a text box on screen that silently cannot send. Both are real <form>s,
    // so Enter submits whichever is showing without any key handling of ours.
    //
    // Real <label for> pairs, not placeholder text: a placeholder disappears the moment someone
    // types, which is exactly when they most need to know which box they are in, and it is not
    // an accessible name. Both ends of each pair sit in this shadow root, the only place an id
    // reference resolves from.
    var signInForm = doc.createElement("form");
    signInForm.className = "signin";
    var siHeading = doc.createElement("div");
    siHeading.className = "signin__heading";
    var siIntro = doc.createElement("p");
    siIntro.className = "signin__intro";

    function signInField(id, type, autocomplete) {
      var wrap = doc.createElement("div");
      wrap.className = "signin__field";
      var label = doc.createElement("label");
      label.className = "signin__label";
      label.setAttribute("for", id);
      var field = doc.createElement("input");
      field.className = "signin__input";
      field.setAttribute("id", id);
      field.setAttribute("type", type);
      // Let a password manager fill this. The widget never keeps a credential itself (the
      // stored session record holds a token and nothing else); whether the BROWSER remembers
      // one is the person's own decision, not ours to block.
      field.setAttribute("autocomplete", autocomplete);
      field.setAttribute("required", "required");
      wrap.appendChild(label);
      wrap.appendChild(field);
      signInForm.appendChild(wrap);
      return { label: label, field: field };
    }

    var siError = doc.createElement("div");
    siError.className = "signin__error";
    // A failure has to be announced, not just drawn: the person's focus is in a field, not on
    // the message. role=alert so it is read when it appears, and hidden when there is nothing
    // to say so it is not an empty box in the layout or a stray node in the tree.
    siError.setAttribute("role", "alert");
    siError.hidden = true;

    var siSubmit = doc.createElement("button");
    siSubmit.type = "submit";
    siSubmit.className = "signin__submit";

    signInForm.appendChild(siHeading);
    signInForm.appendChild(siIntro);
    // type=text, not type=email: the pool signs in by plain username, and `type=email` plus the
    // `required` above is browser-enforced constraint validation - it would refuse to submit
    // "gavtesting" before any of this code ran, with a bubble nobody here wrote.
    var siUserPair = signInField(SIGNIN_USERNAME_ID, "text", "username");
    var siPasswordPair = signInField(SIGNIN_PASSWORD_ID, "password", "current-password");
    var siUser = siUserPair.field;
    var siPassword = siPasswordPair.field;
    signInForm.appendChild(siError);
    signInForm.appendChild(siSubmit);

    // The overlay is the thing that comes and goes; the form inside it never changes. It starts
    // DETACHED and applyAuthState() below attaches it if this embed is gated, so an ungated
    // widget never has an overlay node at all. (TEMPORARY, with the gate.)
    var signInOverlay = doc.createElement("div");
    signInOverlay.className = "signin-overlay";
    signInOverlay.appendChild(signInForm);

    var panelBody = doc.createElement("div");
    panelBody.className = "panel__body";
    panelBody.appendChild(thread);
    panelBody.appendChild(form);

    panel.appendChild(header);
    panel.appendChild(panelBody);
    root.appendChild(panel);

    // ---- language ----

    /**
     * Declare a language on one element, in both places the DOM exposes it: the attribute
     * (what the markup says, and what a stylesheet or an audit reads) and the property (which
     * a real browser reflects from the attribute anyway). Setting both keeps them in step.
     */
    function setLangOn(el) {
      el.lang = lang.code;
      el.setAttribute("lang", lang.code);
    }

    /**
     * Push the active language into every piece of chrome, and onto the widget's `lang`
     * attribute so assistive tech and browsers pronounce it correctly. Set on the host element
     * (the light-DOM node, where the attribute is inherited into the shadow tree) AND on the
     * shadow's own root container, so neither traversal order misses it.
     *
     * Chrome ONLY. Messages already in the thread are never touched: each was rendered with
     * its own `lang` attribute (see appendBotMessage) and keeps the wording it was said in.
     */
    function applyLanguage() {
      setLangOn(host);
      setLangOn(root);
      // The launcher gets its name from its own visible text and nothing else - no
      // aria-label here, deliberately (2.5.3; see where the launcher is built).
      lText.textContent = t("launcherLabel");
      panel.setAttribute("aria-label", t("panelAria"));
      hTitle.textContent = t("title");
      hLang.setAttribute("aria-label", t("languageGroupLabel"));
      for (var i = 0; i < langButtons.length; i++) {
        var btn = langButtons[i];
        btn.setAttribute(
          "aria-pressed",
          btn.getAttribute("data-lang") === lang.code ? "true" : "false"
        );
      }
      hExpand.setAttribute("aria-label", state.expanded ? t("shrinkAria") : t("expandAria"));
      hClose.setAttribute("aria-label", t("closeAria"));
      thread.setAttribute("aria-label", t("threadAria"));
      input.setAttribute("aria-label", t("inputAria"));
      input.setAttribute("placeholder", t("inputPlaceholder"));
      send.textContent = t("sendLabel");
      send.setAttribute("aria-label", t("sendAria"));
      // Sign-in chrome (TEMPORARY, with the rest of the gate). Painted unconditionally, even
      // for an ungated embed where the form is never shown: a switch must not be able to leave
      // a half-translated form behind, and the cost of a few textContent writes is nothing.
      siHeading.textContent = t("signInHeading");
      siIntro.textContent = t("signInIntro");
      siUserPair.label.textContent = t("signInUsernameLabel");
      siPasswordPair.label.textContent = t("signInPasswordLabel");
      siSubmit.textContent = state.signingIn ? t("signInPending") : t("signInSubmit");
    }

    /**
     * A person picked a language. Two effects, and deliberately no third:
     *   - the chrome switches, and the choice starts riding along with each request so the
     *     model answers in that language even for a question typed in the other one;
     *   - the OPENING state (canned greeting + starter questions) re-renders, but only while
     *     the conversation has not started. That text is not a turn anyone took - it is what
     *     the panel says before anyone speaks - and leaving it in the other language is the
     *     one thing that would make the switch look broken.
     * What does NOT happen: an existing conversation is never retranslated. Past turns are
     * what was actually said, and rewriting them would cost a model call per message and read
     * as the bot editing its own history.
     */
    function chooseLanguage(code) {
      if (!STRINGS[code]) return;
      var same = lang.code === code && lang.chosen;
      if (same) return;
      setLanguage(code, true);
      applyLanguage();
      // A sign-in failure message is chrome, not a turn, so it must not be left in the language
      // the person just switched away from. Clearing beats retranslating: it is one line, and
      // the next attempt writes it again. (TEMPORARY, with the gate.)
      showSignInError("");
      if (!state.started) resetOpeningState();
    }

    // ---- sign-in gate (TEMPORARY: this whole block leaves at go-live) ----

    /** Whether the overlay is currently over the panel. */
    function gateIsUp() {
      return signInOverlay.parentNode === panelBody;
    }

    /**
     * Raise or drop the overlay. For an UNGATED embed the gate is never true, so the composer is
     * the only thing that has ever existed and every path here is inert.
     *
     * Dropping it REMOVES the node rather than hiding it: once you are through the gate, nothing
     * on screen and nothing in the accessibility tree says there ever was one. Raising it puts
     * the same node back, so a 401 or an expired token returns you to the form you left.
     *
     * Three things move together, and they have to. The composer is hidden (the focus trap
     * already skips a `[hidden]` subtree, so that also takes it out of the Tab cycle), the
     * transcript behind the wash is marked aria-hidden - it cannot be read or acted on, so it
     * should not be narrated either - and focusablesInPanel() below rescopes the Tab cycle to
     * the header and the overlay.
     */
    function applyAuthState() {
      var gated = authRequired() && !signedIn();
      form.hidden = gated;
      if (gated) {
        if (!gateIsUp()) panelBody.appendChild(signInOverlay);
        thread.setAttribute("aria-hidden", "true");
      } else {
        if (signInOverlay.parentNode) signInOverlay.parentNode.removeChild(signInOverlay);
        thread.removeAttribute("aria-hidden");
      }
    }

    /** Say something went wrong, or clear it. Empty text removes the box entirely. */
    function showSignInError(message) {
      siError.textContent = message || "";
      siError.hidden = !message;
    }

    function setSigningIn(pending) {
      state.signingIn = pending;
      siSubmit.disabled = pending;
      siUser.disabled = pending;
      siPassword.disabled = pending;
      // The button says which of the two things it is doing, rather than just greying out.
      siSubmit.textContent = pending ? t("signInPending") : t("signInSubmit");
    }

    function focusSignIn() {
      var target = siUser.value ? siPassword : siUser;
      if (typeof target.focus === "function") {
        try { target.focus(); } catch (e) { /* ignore */ }
      }
    }

    /**
     * Hand the two field values to signIn() and act on the outcome. The password is read here,
     * passed once, and cleared from the field on BOTH paths - it is never held anywhere else,
     * and a failed attempt must not leave it sitting in a form for the next person at the
     * machine. The username stays, because retyping it is the wrong thing to make someone do.
     */
    function submitSignIn() {
      if (state.signingIn) return;
      var username = String(siUser.value == null ? "" : siUser.value).trim();
      var password = String(siPassword.value == null ? "" : siPassword.value);
      if (!username || !password) {
        focusSignIn();
        return;
      }
      showSignInError("");
      setSigningIn(true);
      signIn(username, password).then(
        function () {
          siPassword.value = "";
          setSigningIn(false);
          applyAuthState();
          // The starter questions were withheld while the panel could not send anything; this
          // is the moment they become real offers, so this is when they are drawn.
          if (!state.started && !suggestionsWrap) renderSuggestions();
          // Focus followed the form that just disappeared, so it has to be placed - and the
          // composer is both the next step and where the greeting's description points.
          focusInput();
        },
        function (err) {
          siPassword.value = "";
          setSigningIn(false);
          showSignInError(
            err && err.reason === "unavailable" ? t("signInUnavailable") : t("signInFailed")
          );
          focusSignIn();
        }
      );
    }

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
     *
     * The caller passes a localized string: the label is inside the turn's wrapper, which
     * carries that turn's `lang`, so it is read in the same language as the turn it names
     * and stays put when the chrome switches.
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
      // Stamp the language this turn happened in, so a later switch relabels the chrome
      // without relabelling what was already said.
      wrap.setAttribute("lang", lang.code);
      var bubble = doc.createElement("div");
      bubble.className = "bubble";
      bubble.textContent = text; // text node only — never innerHTML
      wrap.appendChild(speakerLabel(t("speakerYou")));
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
        sources.length > 1 ? tCount("sourcesMany", sources.length) : t("sourceOne");
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

    /**
     * Append one assistant turn. Returns both of its nodes, because the greeting needs
     * each for a different job: the BUBBLE is what the composer's aria-describedby points
     * at (the description has to be the text, not the wrapper with the speaker label in
     * it), and the WRAP is what a language switch removes before re-seeding.
     */
    function appendBotMessage(answer, sources) {
      state.messages.push({ role: "bot", text: answer, sources: sources });
      var wrap = doc.createElement("div");
      wrap.className = "msg msg--bot";
      // The language this answer was given in; it stays on the message even after the chrome
      // switches, because the answer itself is not retranslated.
      wrap.setAttribute("lang", lang.code);
      var bubble = doc.createElement("div");
      bubble.className = "bubble";
      var textEl = doc.createElement("div");
      textEl.className = "md";
      // Render the assistant answer as markdown (safe DOM, no innerHTML). The
      // no-response fallback is plain text and renders as a single paragraph.
      renderMarkdown(textEl, answer && answer.trim() ? answer : t("noAnswer"), doc);
      bubble.appendChild(textEl);
      if (sources && sources.length) {
        bubble.appendChild(buildSources(sources));
      }
      wrap.appendChild(speakerLabel(t("speakerBot")));
      wrap.appendChild(bubble);
      thread.appendChild(wrap);
      scrollToBottom();
      return { wrap: wrap, bubble: bubble };
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
      statusText.textContent = t("typingStatus");
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
        hint.textContent = t("workingHint");
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
      wrap.setAttribute("lang", lang.code);
      var bubble = doc.createElement("div");
      bubble.className = "bubble bubble--error";
      var msg = doc.createElement("div");
      msg.textContent = t("networkError");
      bubble.appendChild(msg);
      var retry = null;
      if (question) {
        retry = doc.createElement("button");
        retry.type = "button";
        retry.className = "retry";
        retry.textContent = t("retryLabel");
        retry.addEventListener("click", function () {
          wrap.parentNode && wrap.parentNode.removeChild(wrap);
          // Resend the existing transcript; do NOT re-append the user turn (it's already there).
          deliver(question);
        });
        bubble.appendChild(retry);
      }
      wrap.appendChild(speakerLabel(t("speakerBot")));
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
    // The greeting bubble, kept so a language switch before the first message can replace it.
    var greetingWrap = null;

    function renderSuggestions() {
      // starterQuestions(), not t(): theme.json can replace this list per language, and the
      // built-in table is what it falls back to.
      var qs = starterQuestions();
      if (!qs || !qs.length) return;
      var wrap = doc.createElement("div");
      wrap.className = "suggestions";
      wrap.setAttribute("lang", lang.code);
      var label = doc.createElement("div");
      label.className = "suggestions__label";
      label.textContent = t("suggestionsLabel");
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

    /**
     * Re-render the panel's opening state (canned greeting + starter questions) in the current
     * language. Only ever called while state.started is false, so this replaces text the widget
     * wrote itself and never a turn from a real exchange. No network call: the greeting is
     * canned in both languages, so switching costs nothing.
     */
    function resetOpeningState() {
      if (state.started) return;
      if (greetingWrap && greetingWrap.parentNode) {
        greetingWrap.parentNode.removeChild(greetingWrap);
      }
      greetingWrap = null;
      removeSuggestions();
      // Nothing but the greeting can be in the transcript before the first message.
      state.messages = [];
      seedOpeningState();
    }

    /**
     * Render the opening state: the canned greeting, the starter questions, and the
     * composer's description.
     *
     * On first launch, focus lands in the composer with the greeting and the starter
     * questions behind it in the tab order, so a screen-reader user met an empty text box.
     * Describing the composer with the greeting offers that content instead. It is dropped
     * with the suggestions on the first message (see removeSuggestions), so later turns are
     * not prefixed by a stale description every time focus returns here. Both ends of the
     * reference are inside this shadow root, which is the only way an id reference resolves.
     *
     * The wiring lives HERE rather than at mount, because a language switch before the first
     * message tears the greeting down and builds a new one: done at mount only, the
     * description would point at a removed node and the Spanish opening state would arrive
     * undescribed.
     */
    function seedOpeningState() {
      var greeting = appendBotMessage(t("greeting"), []);
      greetingWrap = greeting.wrap;
      // The starter questions wait for a session (TEMPORARY, with the gate). They are buttons
      // that send a question, and while the panel cannot send anything they would be four
      // controls that visibly do nothing. The GREETING still shows: it is what tells someone
      // what they are being asked to sign in FOR.
      if (!(authRequired() && !signedIn())) renderSuggestions();
      greeting.bubble.id = GREETING_ID;
      input.setAttribute("aria-describedby", GREETING_ID);
    }

    function submitQuestion(question) {
      var text = String(question == null ? "" : question).trim();
      if (!text || state.pending) return;
      // Belt to the hidden composer's braces (TEMPORARY, with the gate): a programmatic submit
      // - the test handle, or a starter chip that outlived a token - must not spend a request
      // that is going to come back 401.
      if (authRequired() && !signedIn()) {
        applyAuthState();
        // A started conversation means there WAS a session, so the overlay coming back over the
        // transcript needs a reason given. Before the first message there is nothing to explain:
        // the gate simply never opened.
        if (state.started) showSignInError(t("signInExpired"));
        focusSignIn();
        return;
      }
      state.started = true; // the opening state is now history: never re-rendered
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
          setPending(false);
          // The session ended mid-conversation (TEMPORARY, with the gate). Say so and put the
          // sign-in form back, rather than showing a "couldn't reach the assistant" bubble for
          // something a password fixes. Reachable in practice only after a token's full day,
          // because expiry is checked before the request rather than read off a 401.
          if (err && err.reason === "expired") {
            signOut();
            applyAuthState();
            showSignInError(t("signInExpired"));
            focusSignIn();
            return;
          }
          var retry = appendError(question);
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
      // While the sign-in overlay is up, the cycle is the header and the overlay - in that
      // order, which is also the order they are stacked. Everything the overlay covers is
      // behind it and unusable, and a sources link or a retry button left over from a
      // conversation that outlived its token is exactly the thing Tab must not reach.
      // The header stays IN, deliberately: the language control has to work before someone
      // signs in, or the prompt cannot be read in Spanish. (TEMPORARY, with the gate.)
      var scopes = gateIsUp() ? [header, signInOverlay] : [panel];
      var out = [];
      for (var s = 0; s < scopes.length; s++) {
        var found = scopes[s].querySelectorAll(FOCUSABLE_SELECTOR);
        for (var i = 0; i < found.length; i++) {
          var el = found[i];
          if (el.hidden) continue;
          if (typeof el.closest === "function" && el.closest("[hidden]")) continue;
          out.push(el);
        }
      }
      return out;
    }

    function openPanel() {
      if (state.open) return;
      state.open = true;
      panel.hidden = false;
      launcher.hidden = true;
      // Focus the control that is on screen. With the gate up that is the username field,
      // and focusing a hidden composer would drop focus to nowhere. (TEMPORARY, with the gate.)
      if (authRequired() && !signedIn()) focusSignIn();
      else focusInput();
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
      hExpand.setAttribute("aria-label", state.expanded ? t("shrinkAria") : t("expandAria"));
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

    // A real form submit, so Enter in either field works with no key handling of our own.
    // (TEMPORARY, with the gate.)
    signInForm.addEventListener("submit", function (e) {
      e.preventDefault();
      submitSignIn();
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

    // Paint every string + the lang attribute before anything is shown, so the panel opens in
    // the active language rather than in English-then-corrected.
    applyLanguage();

    // Decide which of the composer and the sign-in form is on screen BEFORE seeding, because
    // seedOpeningState asks the same question to decide whether the starter questions are real
    // offers yet. (TEMPORARY, with the gate.)
    applyAuthState();

    // seed the greeting + first-launch example questions so they're present when the panel opens
    seedOpeningState();

    return {
      host: host,
      shadow: shadow,
      open: openPanel,
      close: closePanel,
      submit: submitQuestion,
      chooseLanguage: chooseLanguage,
      // Sign-in surface, for tests only (TEMPORARY, with the gate). The handle exposes the
      // ACTION, never the token: nothing here can read the access token back out.
      signIn: submitSignIn,
      signInForm: signInForm,
      composerForm: form,
      getState: function () { return state; }
    };
  }

  // ---- auto-mount (browser only) / Node export (tests) --------------------

  var IS_COMMONJS = typeof module !== "undefined" && module.exports;

  if (!IS_COMMONJS && typeof document !== "undefined") {
    // Fire the pre-warm as early as the deferred script runs, so the OSS cold start overlaps
    // with the user reading the page rather than their first query.
    warmBackend();

    // theme.json is fetched in PARALLEL with the rest of page load and the mount waits on
    // it, so the widget is themed on its first paint rather than repainted a frame later.
    // The wait costs nothing in the normal case (a small same-CDN file, usually already in
    // flight before the DOM is ready) and is capped at CONFIG.themeTimeoutMs in the bad one:
    // a dead CDN delays the launcher by that much and then shows the default colours.
    // mount() itself stays synchronous and theme-free, which is what the tests drive.
    var themeSettled = loadTheme();
    var domSettled = new Promise(function (resolve) {
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () { resolve(); });
      } else {
        resolve();
      }
    });
    Promise.all([themeSettled, domSettled]).then(
      function () { mount(); },
      function () { mount(); }
    );
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
      // Sign-in gate (TEMPORARY, removed at go-live). signOut is here so a test that signs in
      // can hand an unauthenticated module back to the tests after it; there is deliberately no
      // way to read the access token, and no way to set one without going through signIn().
      authConfig: authConfig,
      authRequired: authRequired,
      signedIn: signedIn,
      signIn: signIn,
      signOut: signOut,
      AUTH_POOL_ATTR: AUTH_POOL_ATTR,
      AUTH_CLIENT_ATTR: AUTH_CLIENT_ATTR,
      // The storage key, so a test can seed a "previous page load" and inspect what was left
      // behind without hardcoding the string. Still no way to read the token through the API.
      AUTH_STORAGE_KEY: AUTH_STORAGE_KEY,
      CONFIG: CONFIG,
      HOST_ID: HOST_ID,
      // Runtime theme surface. resetTheme exists for the same reason resetLanguage does:
      // the module is loaded once per process, so a test that themes it has to hand the
      // shipped default back to the tests after it. loadTheme is not exported - the fetch
      // is boot-only, and applyTheme is the whole of what a test needs to drive.
      applyTheme: applyTheme,
      resetTheme: resetTheme,
      themeCss: themeCss,
      themeUrl: themeUrl,
      starterQuestions: starterQuestions,
      inkFor: inkFor,
      THEME_FILE: THEME_FILE,
      FONT_KEYWORDS: FONT_KEYWORDS,
      FONT_STACKS: FONT_STACKS,
      DEFAULT_HIGHLIGHT: DEFAULT_HIGHLIGHT,
      DEFAULT_FONT: DEFAULT_FONT,
      MAX_STARTER_QUESTIONS: MAX_STARTER_QUESTIONS,
      MAX_STARTER_CHARS: MAX_STARTER_CHARS,
      // Localization surface. setLanguage/resetLanguage exist for test isolation: the module
      // is loaded once per process, so a test that switches language has to hand the default
      // back to the tests that follow it.
      STRINGS: STRINGS,
      LANGUAGES: LANGUAGES,
      DEFAULT_LANG: DEFAULT_LANG,
      getLanguage: getLanguage,
      setLanguage: setLanguage,
      resetLanguage: resetLanguage
    };
  }
})();
