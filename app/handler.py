"""Query-path Lambda for the Gavilan Library Chatbot.

Fronted by an API Gateway HTTP API (payload format 2.0) with two routes on this one Lambda:
  - POST /query -> the real query path (_handle_query):
      0. _apply_input_guardrail() -> Bedrock `ApplyGuardrail` (source=INPUT) on the BARE
         user query.
      1. run_agent() -> an AGENTIC Bedrock Converse tool-use loop under the real system
         prompt (app/system_prompt.md), with the OUTPUT guardrail (content filters, answer
         side only) attached to every Converse call as a backstop. The model is given THREE
         tools: `search_library_info` (KB `Retrieve`), `database_catalog` (authoritative
         research-database lookup from static JSON), and `search_book_catalog` (a LIVE search
         of the Primo book/media catalog - evidence the model judges, not an authoritative
         verdict). The model decides which to call and how often. The loop feeds each tool
         result back and re-calls Converse until stopReason == "end_turn" (or a safety
         iteration cap). Sources accumulate from every tool the model triggered during the loop.
  - GET /warm -> _handle_warm(): a retrieval-only pre-warm before the first real query. No
      generation, no guardrail.

Wiring comes from env vars set by the CDK stack.

/query response JSON shape:
  {
    "answer": "<generated answer text>",
    "sources": [
      {"uri": "<source page url>", "excerpt": "<short snippet of that passage>"},
      ...
    ]
  }
  - `sources` is deduplicated by uri, in retrieval order, accumulated across every tool
    retrieval the model ran during the loop. Passages with no resolvable source uri are
    omitted from `sources` (they still inform the answer). If the model answers without
    calling the tool (e.g. a greeting), `sources` is [].
  - When the tool retrieves nothing relevant, the system prompt instructs the model to say
    it does not have the information.
  - When the request sets `include_full_context: true`, the response also carries
    `full_context`: the full, un-deduped, un-truncated retrieved passages (`[{text, source}]`)
    the model actually saw, for answer-quality eval. The widget never sets the flag, so its
    responses are exactly the `{answer, sources}` shape above.
"""

import base64
import json
import os
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path

import boto3

KNOWLEDGE_BASE_ID = os.environ["KNOWLEDGE_BASE_ID"]
GENERATION_MODEL_ID = os.environ["GENERATION_MODEL_ID"]
# Lambda auto-sets AWS_REGION; BEDROCK_REGION lets the stack pin it explicitly.
REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION")
NUMBER_OF_RESULTS = int(os.environ.get("NUMBER_OF_RESULTS", "5"))

# Generation inference knobs, wired from config.yaml by the stack. Defaults are a safety net
# for local runs without the env set; config.yaml is the source of truth.
GENERATION_MAX_TOKENS = int(os.environ.get("GENERATION_MAX_TOKENS", "600"))
GENERATION_TEMPERATURE = float(os.environ.get("GENERATION_TEMPERATURE", "0.2"))

# Max characters accepted for a user query. Over this -> HTTP 400, BEFORE any retrieval or
# guardrail call. The real server-side size control; the widget maxlength is only advisory UX,
# and the platform limits (API GW 10MB / Lambda 6MB) are far too high to protect.
MAX_QUERY_CHARS = int(os.environ.get("MAX_QUERY_CHARS", "2000"))

# Max number of conversation turns (prior + current) used from a request. Single-session history
# is trimmed to the LAST this-many messages SERVER-SIDE before seeding the Converse loop - the
# client is never trusted to cap it. Older turns are dropped. See _seed_messages for why the trim
# can never yield a request Converse rejects (it drops a leading assistant turn and merges any
# consecutive same-role turns, so the seed always starts with user and stays alternating).
MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", "10"))

# The only conversation roles accepted from the client. A widget "bot" turn maps to "assistant".
_VALID_ROLES = ("user", "assistant")

# Two Bedrock guardrails, set by the CDK stack from config.yaml. Either pair may be unset
# locally, in which case that screen is skipped rather than failing:
#   INPUT  - screened on the bare user query BEFORE retrieval via the ApplyGuardrail API
#            (source=INPUT). PII is masked-and-proceeds; content/prompt-attack is blocked.
#   OUTPUT - attached to the Converse call as a backstop on the generated answer only.
INPUT_GUARDRAIL_ID = os.environ.get("INPUT_GUARDRAIL_ID")
INPUT_GUARDRAIL_VERSION = os.environ.get("INPUT_GUARDRAIL_VERSION")
OUTPUT_GUARDRAIL_ID = os.environ.get("OUTPUT_GUARDRAIL_ID")
OUTPUT_GUARDRAIL_VERSION = os.environ.get("OUTPUT_GUARDRAIL_VERSION")
GUARDRAIL_TRACE = os.environ.get("GUARDRAIL_TRACE", "enabled")

# Converse stopReason when the output guardrail blocks the generated response.
_GUARDRAIL_STOP_REASON = "guardrail_intervened"

# ApplyGuardrail response fields (verified against the installed bedrock-runtime model).
# Top-level action is "NONE" or "GUARDRAIL_INTERVENED"; both mask and block report
# GUARDRAIL_INTERVENED, so the mask-vs-block decision comes from the per-item action in the
# assessment: content/topic/word policies only ever "BLOCKED"; PII entities/regexes report
# "ANONYMIZED" when masked or "BLOCKED" when blocked.
_ACTION_INTERVENED = "GUARDRAIL_INTERVENED"
_ITEM_BLOCKED = "BLOCKED"
_ITEM_ANONYMIZED = "ANONYMIZED"

# Last-resort student-facing message if the input guardrail blocks but returns no message
# text (documented behavior is that `outputs` carries the configured block message, so this
# is only a defensive fallback and should not normally be reached).
_FALLBACK_BLOCK_MESSAGE = (
    "I can't help with that request. Try asking about the Gavilan College Library, like "
    "hours, checkouts, and finding materials."
)

# The tools the agent is given. Both names are referenced by the system prompt's <tools>
# section (app/system_prompt.md), so keep the two in sync.
#   search_library_info - semantic retrieval over the Bedrock KB (hours, services, policies, ...).
#   database_catalog     - authoritative lookup of the research-database catalog (is X held? what
#                          databases for subject Y?), from a bundled static JSON.
SEARCH_TOOL_NAME = "search_library_info"
CATALOG_TOOL_NAME = "database_catalog"
# search_book_catalog - a LIVE search of the Primo book/media catalog. Unlike database_catalog
# (authoritative), this returns EVIDENCE (candidate records + availability) and the MODEL judges
# whether any is a real match; total == 0 is the only clean not-held signal. See the Primo section.
PRIMO_TOOL_NAME = "search_book_catalog"

# Safety cap on the Converse tool-use loop: the model can call the tool and be re-invoked at
# most this many times before we stop and return the best answer so far. Prevents a runaway
# (or adversarial) loop from spending unboundedly. A factual FAQ needs 1-2 iterations.
MAX_AGENT_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "5"))

# Shown if the loop hits the iteration cap without the model producing any answer text.
_MAX_ITERS_FALLBACK_MESSAGE = (
    "I'm having trouble answering that right now. Please try rephrasing, or reach out to a "
    "librarian for help."
)

# Max characters of a passage surfaced as a source excerpt in the response.
_EXCERPT_CHARS = 300

# Warm path. The widget fires GET /warm on page load to warm the query Lambda container (and
# exercise the KB Retrieve path) before the first real query. WARM_PATH is matched against the
# request path; _WARM_QUERY is a throwaway retrieval query (the goal is to warm the path, not to
# get useful results).
WARM_PATH = "/warm"
_WARM_QUERY = "library hours"

# The real system prompt is packaged with the Lambda: app/system_prompt.md lives inside
# the from_asset(app/) bundle, next to this file. Read once at cold start.
_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()

# Database catalog (Phase 2b: self-updating).
#   - The HELD list is regenerated weekly by the scraper and written to S3; this Lambda reads the
#     fresh copy from CATALOG_BUCKET/CATALOG_KEY at query time (cached per container, see below).
#   - The bundled app/data/database_catalog.json is the SEED: it provides the hand-authored NOT_HELD
#     list + catalog_url + default_alternatives (merged in at read time, since absence can't be
#     scraped), AND a fallback HELD list used before the first scrape or if the S3 read fails.
# Shape (both S3 and seed): {catalog_url, default_alternatives, held:[{name,subjects,description,
# aliases}], not_held:[{name,aliases,suggested_alternatives}]}. The S3 object carries only `held`.
_SEED_CATALOG_PATH = Path(__file__).resolve().parent / "data" / "database_catalog.json"
_SEED_CATALOG = json.loads(_SEED_CATALOG_PATH.read_text(encoding="utf-8"))

CATALOG_BUCKET = os.environ.get("CATALOG_BUCKET")
CATALOG_KEY = os.environ.get("CATALOG_KEY", "database_catalog.json")
# Per-container cache TTL. The catalog changes at most weekly, so serving a copy up to this many
# seconds stale (default 15 min) is fine and avoids an S3 GET on every query; the next lookup after
# the TTL re-fetches, so weekly updates are picked up quickly without a redeploy. A cold container
# always fetches fresh.
CATALOG_CACHE_TTL_SECONDS = int(os.environ.get("CATALOG_CACHE_TTL_SECONDS", "900"))

# A catalog answer isn't a scraped-page passage, so it never adds a KB `sources` excerpt. Instead a
# substantive catalog lookup contributes ONE synthetic source: the library's A-Z database page,
# where the data comes from and where the user can browse/verify. Deduped like any other source.
_CATALOG_SOURCE = {
    "uri": _SEED_CATALOG.get("catalog_url", ""),
    "excerpt": "Gavilan Library A-Z database list",
}

# --- Primo book/media catalog tool (search_book_catalog) -----------------------------------
#
# A LIVE search of Gavilan's Primo discovery catalog (an undocumented public JSON endpoint the
# library sanctioned). It is fundamentally different from database_catalog:
#   - database_catalog is AUTHORITATIVE (a curated list), so it can say "not held".
#   - Primo is NOT. It always returns fuzzy matches, and its relevance score is query-relative
#     (a held book can score BELOW not-held noise), so the handler makes NO held/not-held
#     judgment and applies NO score threshold. It returns EVIDENCE - the top few candidate
#     records with fields + availability + the total match count - and the MODEL decides whether
#     any candidate is a real match. `total == 0` is the ONLY clean not-held signal, surfaced raw.
#
# This is a live third-party HTTP call INSIDE the Converse loop, a new class of dependency, so:
#   - every call is timed out (it eats the request/Lambda budget);
#   - ANY failure (network, timeout, HTTP error, changed/absent fields, parse error) degrades to
#     a "catalog unavailable" result - it never throws and never kills the query;
#   - parsing is defensive (the $$C..$$V.. encoding + undocumented shape); missing fields degrade.
#
# Endpoint identity (the reverse-engineered discovery API; tied to the institution, not a
# per-deploy knob, so it stays in code rather than config.yaml):
PRIMO_SEARCH_URL = "https://caccl-gavilan.primo.exlibrisgroup.com/primaws/rest/pub/pnxs"
PRIMO_DISCOVERY_URL = "https://caccl-gavilan.primo.exlibrisgroup.com/discovery/search"
PRIMO_INST = "01CACCL_GAVILAN"
PRIMO_VID = "01CACCL_GAVILAN:GAVILAN"
# General local-holdings scope ONLY. Deliberately NOT CourseReserves: Gavilan's general catalog
# does not stock course textbooks (they live in course reserves + the bookstore), so textbook
# questions must stay on the system prompt's <textbook_flow> and never route through this tool.
PRIMO_SCOPE = "MyInstitution"
PRIMO_TAB = "LibraryCatalog"

# Behavioral knobs, wired from config.yaml by the stack (env). Defaults are a local-run safety net.
PRIMO_TIMEOUT_SECONDS = float(os.environ.get("PRIMO_TIMEOUT_SECONDS", "5"))
PRIMO_NUMBER_OF_RESULTS = int(os.environ.get("PRIMO_NUMBER_OF_RESULTS", "4"))
# Total wall-clock cap across all per-result availability lookups (each result needs its own
# delivery call). Once exceeded, the remaining results report availability "unknown" rather than
# blocking - bounds worst-case latency of the tool inside the agent loop.
PRIMO_AVAILABILITY_BUDGET_SECONDS = float(os.environ.get("PRIMO_AVAILABILITY_BUDGET_SECONDS", "8"))

# Fed back to the model when the live lookup fails: it must NOT claim the item is absent, only
# that the search is down. (Absence may ONLY be stated on a real total == 0 result.)
_PRIMO_UNAVAILABLE_NOTE = (
    "The library book catalog search is temporarily unavailable. Do not say whether the library "
    "holds this item; suggest checking the library catalog directly or asking a librarian."
)

_agent_runtime = None
_bedrock_runtime = None
_catalog_s3 = None
# Cached merged catalog: {"catalog": <dict>, "at": <monotonic seconds>}. None until first load.
_catalog_cache = {"catalog": None, "at": 0.0}


def _agent_client():
    global _agent_runtime
    if _agent_runtime is None:
        _agent_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)
    return _agent_runtime


def _bedrock_client():
    global _bedrock_runtime
    if _bedrock_runtime is None:
        _bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock_runtime


def _catalog_s3_client():
    global _catalog_s3
    if _catalog_s3 is None:
        _catalog_s3 = boto3.client("s3", region_name=REGION)
    return _catalog_s3


def _read_s3_held():
    """The fresh held list the scraper wrote to S3, or None if unavailable (no bucket configured,
    object missing before the first scrape, or a read error). None -> caller falls back to the
    bundled seed held list, so the tool always works."""
    if not CATALOG_BUCKET:
        return None
    try:
        obj = _catalog_s3_client().get_object(Bucket=CATALOG_BUCKET, Key=CATALOG_KEY)
        held = json.loads(obj["Body"].read()).get("held")
        return held if isinstance(held, list) and held else None
    except Exception as exc:  # noqa: BLE001 - degrade to the bundled seed, never hard-fail a query
        print(json.dumps({"event": "catalog_s3_read_failed", "error": f"{type(exc).__name__}: {exc}"}))
        return None


def _get_catalog():
    """The merged catalog the tool reads: the hand-authored not_held + catalog_url +
    default_alternatives from the bundled seed, with the HELD list from S3 (falling back to the
    seed's held if S3 is unavailable). Cached per container for CATALOG_CACHE_TTL_SECONDS so a
    warm container doesn't GET S3 on every query but still picks up the weekly refresh."""
    now = time.monotonic()
    if _catalog_cache["catalog"] is not None and (now - _catalog_cache["at"]) < CATALOG_CACHE_TTL_SECONDS:
        return _catalog_cache["catalog"]
    held = _read_s3_held()
    catalog = {
        "catalog_url": _SEED_CATALOG.get("catalog_url", ""),
        "default_alternatives": _SEED_CATALOG.get("default_alternatives", []),
        "held": held if held is not None else _SEED_CATALOG.get("held", []),
        "not_held": _SEED_CATALOG.get("not_held", []),
    }
    _catalog_cache["catalog"] = catalog
    _catalog_cache["at"] = now
    return catalog


def _extract_source(result):
    """Pull the public source URL for a KB Retrieve result.

    Order of preference:
      1. metadata["source_url"] - the public original page URL, ingested per document by the
         scraper into each document's Bedrock metadata sidecar. This is what we want to show.
      2. a location URL (e.g. a web crawler's page url).
      3. the internal S3 URI (s3Location / the bedrock source-uri metadata) as a LAST resort.

    The S3 URI is an internal path, not something to show a student: _build_sources drops any
    source that resolves only to an s3:// URI so it never reaches the client."""
    metadata = result.get("metadata") or {}
    source_url = metadata.get("source_url")
    if source_url:
        return source_url
    location = result.get("location") or {}
    for key in (
        "webLocation",
        "s3Location",
        "confluenceLocation",
        "salesforceLocation",
        "sharePointLocation",
    ):
        loc = location.get(key)
        if isinstance(loc, dict):
            uri = loc.get("url") or loc.get("uri")
            if uri:
                return uri
    return metadata.get("x-amz-bedrock-kb-source-uri")


def retrieve(query):
    """Knowledge Base Retrieve API. Returns a list of {"text", "source"} dicts."""
    response = _agent_client().retrieve(
        knowledgeBaseId=KNOWLEDGE_BASE_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": NUMBER_OF_RESULTS}
        },
    )
    chunks = []
    for result in response.get("retrievalResults", []):
        text = (result.get("content") or {}).get("text")
        if text:
            chunks.append({"text": text, "source": _extract_source(result)})
    return chunks


def _tool_config():
    """The Converse `toolConfig`: three tools the model routes between (toolChoice left at the
    model's default `auto`, so it may also answer a greeting without any tool):
      - search_library_info: semantic search over the library website (hours, services, policies,
        how-to, borrowing, contact, general questions).
      - database_catalog: authoritative lookup of the research-database catalog - whether a named
        database is available (including confirming it is NOT), and databases by subject.
      - search_book_catalog: LIVE search of the Primo book/media catalog - returns candidate
        records + availability for a title/author/work; EVIDENCE the model judges, not a verdict.
    The descriptions are deliberately differentiated so the model picks the right one."""
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": SEARCH_TOOL_NAME,
                    "description": (
                        "Search the Gavilan College Library's website content for general library "
                        "information: hours, locations, checkout and borrowing policies, laptops "
                        "and equipment, textbooks and course reserves, services, contact info, and "
                        "how-to/FAQ questions. Answer from the results it returns rather than from "
                        "memory. You may call it more than once with different queries. Do NOT use "
                        "this to check whether a specific named research database is available or "
                        "to list databases by subject - use database_catalog for that."
                    ),
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": (
                                        "The search query describing the library information to "
                                        "look up."
                                    ),
                                }
                            },
                            "required": ["query"],
                        }
                    },
                }
            },
            {
                "toolSpec": {
                    "name": CATALOG_TOOL_NAME,
                    "description": (
                        "Look up the library's research-database catalog. Use this for two things: "
                        "(1) to check whether a SPECIFIC named database or resource is available - "
                        "e.g. 'do you have JSTOR / EBSCO / Opposing Viewpoints?' - it authoritatively "
                        "returns whether the database is held, and if not, suggests held alternatives; "
                        "and (2) to list the databases the library has for a SUBJECT - e.g. 'databases "
                        "for business / nursing / psychology'. This catalog is authoritative for "
                        "database availability: trust its held / not-held answer. It does NOT cover "
                        "hours, services, or policies - use search_library_info for those."
                    ),
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "query_type": {
                                    "type": "string",
                                    "enum": ["name", "subject"],
                                    "description": (
                                        "'name' to check availability of a specific named database; "
                                        "'subject' to list databases for a subject area."
                                    ),
                                },
                                "value": {
                                    "type": "string",
                                    "description": (
                                        "The database name (for query_type 'name') or the subject "
                                        "(for query_type 'subject')."
                                    ),
                                },
                            },
                            "required": ["query_type", "value"],
                        }
                    },
                }
            },
            {
                "toolSpec": {
                    "name": PRIMO_TOOL_NAME,
                    "description": (
                        "Search the Gavilan College Library's BOOK and MEDIA catalog (Primo) for a "
                        "specific title, author, or work the student wants to find or borrow - e.g. "
                        "'do you have The Great Gatsby?', 'is the Citizen Kane film available?', "
                        "'books by Toni Morrison'. It returns the top few candidate records (title, "
                        "author, year, type) with the catalog's current availability (status, "
                        "campus/location, call number) and the total match count. This is EVIDENCE, "
                        "NOT a verdict: Primo always returns fuzzy matches and its ranking is "
                        "unreliable, so YOU must decide whether any candidate is really the item "
                        "asked for, checking ALL returned candidates for a matching title with an "
                        "available copy (the top-ranked result is often not the available one). Do "
                        "NOT conclude the library lacks an item unless total is 0. Availability is "
                        "what the catalog SHOWS, not a guarantee the copy is on the shelf. Use this "
                        "for books and media the library owns; NOT for research databases (use "
                        "database_catalog), NOT for hours/services/policies (use search_library_info), "
                        "and NOT for course textbooks (handle those with the textbook flow)."
                    ),
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": (
                                        "The title, author, or subject to look up in the book/media "
                                        "catalog."
                                    ),
                                }
                            },
                            "required": ["query"],
                        }
                    },
                }
            },
        ]
    }


def _run_search_tool(tool_input):
    """Execute the search_library_info tool: run KB `Retrieve` on the model's query. Returns
    (chunks, result_json) where result_json is the toolResult payload handed back to the model."""
    query = ""
    if isinstance(tool_input, dict):
        query = (tool_input.get("query") or "").strip()
    if not query:
        return [], {"passages": [], "note": "No search query was provided."}
    chunks = retrieve(query)
    passages = [{"text": c["text"], "source": c.get("source")} for c in chunks]
    if not passages:
        return chunks, {"passages": [], "note": "No relevant passages were found."}
    return chunks, {"passages": passages}


def _norm(text):
    """Normalize a name/subject for matching: lowercase, drop punctuation, collapse whitespace."""
    return " ".join("".join(c if c.isalnum() else " " for c in (text or "").lower()).split())


def _name_matches(query_norm, name, aliases):
    """Whether a normalized query names this database, by its name or any alias. Matches on exact
    normalized equality or a whole-string containment either way (so 'ebsco' hits the 'EBSCO'
    alias and 'jstor database' still hits 'JSTOR'), which is safe for specific named-database
    lookups."""
    for candidate in [name] + list(aliases or []):
        cand = _norm(candidate)
        if not cand:
            continue
        if query_norm == cand or query_norm in cand or cand in query_norm:
            return True
    return False


def _catalog_name_lookup(value):
    """Authoritatively answer whether a named database is held. Returns structured JSON (the model
    writes the prose). Held -> {held:true,...}; known-absent -> {held:false, suggested_alternatives};
    unknown -> {held:false, not in catalog, generic alternatives}."""
    catalog = _get_catalog()
    query_norm = _norm(value)
    for db in catalog.get("held", []):
        if _name_matches(query_norm, db["name"], db.get("aliases")):
            return {
                "held": True,
                "name": db["name"],
                "subjects": db.get("subjects", []),
                "description": db.get("description", ""),
            }
    for db in catalog.get("not_held", []):
        if _name_matches(query_norm, db["name"], db.get("aliases")):
            return {
                "held": False,
                "name": db["name"],
                "suggested_alternatives": db.get("suggested_alternatives", []),
            }
    # Not in either list: the catalog is authoritative for what is held, so an unknown name is not
    # held. Offer a generic starting point rather than a curated alternative.
    return {
        "held": False,
        "name": value,
        "suggested_alternatives": catalog.get("default_alternatives", []),
        "note": "This database was not found in the library's catalog.",
    }


def _catalog_subject_lookup(value):
    """List the held databases for a subject. Matches the subject query against each database's
    subject tags (normalized, containment either way). Returns {subject, databases:[{name,
    description}], note?}."""
    query_norm = _norm(value)
    matches = []
    for db in _get_catalog().get("held", []):
        for subject in db.get("subjects", []):
            sub = _norm(subject)
            if query_norm == sub or query_norm in sub or sub in query_norm:
                matches.append({"name": db["name"], "description": db.get("description", "")})
                break
    result = {"subject": value, "databases": matches}
    if not matches:
        result["note"] = "No databases were found for that subject in the catalog."
    return result


def _run_catalog_tool(tool_input):
    """Execute the database_catalog tool. Returns (result_json, contributed_source) where
    contributed_source is _CATALOG_SOURCE for a real lookup (name or subject) or None for a bad
    input - so only substantive catalog answers add the synthetic A-Z-page source."""
    if not isinstance(tool_input, dict):
        return {"error": "Invalid catalog input."}, None
    value = (tool_input.get("value") or "").strip()
    if not value:
        return {"error": "No database name or subject was provided."}, None
    query_type = (tool_input.get("query_type") or "").strip().lower()
    if query_type == "subject":
        return _catalog_subject_lookup(value), _CATALOG_SOURCE
    # Default to a name lookup (the common "do you have X?" case) for 'name' or anything else.
    return _catalog_name_lookup(value), _CATALOG_SOURCE


# --- Primo book/media catalog tool implementation ------------------------------------------

_primo_ssl_ctx = None


def _primo_ssl_context():
    """A certificate-verifying SSL context for the Primo HTTPS calls, resolved once per container.

    Priority: (1) certifi if importable - this is the LOCAL-DEV path (the macOS python.org build
    ships no OS trust store, so a bare default context fails cert verification); (2) the platform
    default trust store, which is what the Lambda runtime (Amazon Linux 2023) uses and which is
    the expected production path; (3) botocore's bundled CA (botocore is always present in the
    Lambda runtime) as a final belt-and-suspenders. We only accept the default context if it
    actually loaded CA certs, so a certificate-less environment falls through to (3).

    NOTE: the production (Lambda) leg can only be fully confirmed at deploy - it cannot be
    exercised offline. See the change summary."""
    global _primo_ssl_ctx
    if _primo_ssl_ctx is not None:
        return _primo_ssl_ctx
    ctx = None
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi absent (e.g. Lambda): try the OS trust store next
        ctx = None
    if ctx is None:
        try:
            candidate = ssl.create_default_context()
            if candidate.cert_store_stats().get("x509_ca", 0) > 0:
                ctx = candidate
        except Exception:  # noqa: BLE001
            ctx = None
    if ctx is None:
        try:
            import botocore  # guaranteed in the Lambda runtime; bundles a CA cert file

            cafile = os.path.join(os.path.dirname(botocore.__file__), "cacert.pem")
            if os.path.exists(cafile):
                ctx = ssl.create_default_context(cafile=cafile)
        except Exception:  # noqa: BLE001
            ctx = None
    _primo_ssl_ctx = ctx or ssl.create_default_context()
    return _primo_ssl_ctx


def _primo_get_json(url, timeout):
    """GET a Primo URL and parse JSON. Timed out. Raises on any network / timeout / HTTP / parse
    error; every caller wraps this and soft-fails (a dead Primo must never kill the request)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_primo_ssl_context()) as resp:
        return json.loads(resp.read())


def _primo_search_request(query, limit, timeout):
    """The Primo search call (ranked bib records, NO availability). MyInstitution/LibraryCatalog
    scope only - the general local catalog, never CourseReserves."""
    params = {
        "q": f"any,contains,{query}",
        "inst": PRIMO_INST,
        "vid": PRIMO_VID,
        "scope": PRIMO_SCOPE,
        "tab": PRIMO_TAB,
        "pcAvailability": "true",
        "skipDelivery": "Y",
        "sort": "rank",
        "lang": "en",
        "limit": str(limit),
        "offset": "0",
    }
    return _primo_get_json(f"{PRIMO_SEARCH_URL}?{urllib.parse.urlencode(params)}", timeout)


def _primo_delivery_request(record_id, timeout):
    """The per-record delivery call (real-time holdings + availability). Separate from search."""
    params = {
        "inst": PRIMO_INST,
        "vid": PRIMO_VID,
        "scope": PRIMO_SCOPE,
        "lang": "en",
        "getDelivery": "true",
    }
    url = f"{PRIMO_SEARCH_URL}/L/{urllib.parse.quote(record_id)}?{urllib.parse.urlencode(params)}"
    return _primo_get_json(url, timeout)


def _primo_first(field):
    """First non-empty string in a Primo list field (or the bare string). '' if absent or oddly
    shaped. Primo display/control fields are almost always single-element lists, but the
    undocumented shape is not guaranteed, so never index blindly."""
    if isinstance(field, list):
        for v in field:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    if isinstance(field, str):
        return field.strip()
    return ""


def _primo_availability(record_id, timeout):
    """Real-time availability for one record from the delivery endpoint, as a dict
    {status, location, call_number}. Any failure (or a shape we don't recognize) degrades to
    status 'unknown' - it never raises. NOTE: 'available' here is what the catalog SHOWS, not a
    guarantee the physical copy is on the shelf; the system prompt phrases it accordingly."""
    unknown = {"status": "unknown", "location": "", "call_number": ""}
    if not record_id:
        return unknown
    try:
        payload = _primo_delivery_request(record_id, timeout)
    except Exception as exc:  # noqa: BLE001 - degrade to unknown, never fail the whole lookup
        print(json.dumps({"event": "primo_availability_failed", "error": f"{type(exc).__name__}: {exc}"}))
        return unknown
    delivery = payload.get("delivery") if isinstance(payload, dict) else None
    if not isinstance(delivery, dict):
        return unknown

    codes = delivery.get("availability")
    codes = codes if isinstance(codes, list) else []
    best = delivery.get("bestlocation")
    best = best if isinstance(best, dict) else {}
    eservices = delivery.get("electronicServices")
    eservices = eservices if isinstance(eservices, list) else []

    if best:
        where = ", ".join(
            x for x in (best.get("mainLocation"), best.get("subLocation")) if isinstance(x, str) and x
        )
        status = best.get("availabilityStatus") or (codes[0] if codes else "") or "unknown"
        call = best.get("callNumber")
        return {"status": status, "location": where, "call_number": call if isinstance(call, str) else ""}
    if eservices:
        names = ", ".join(
            (s.get("serviceType") or s.get("displayName") or "online access")
            for s in eservices
            if isinstance(s, dict)
        )
        return {"status": "online", "location": names, "call_number": ""}
    displayed = delivery.get("displayedAvailability")
    if isinstance(displayed, str) and displayed:
        status = displayed
    elif codes:
        status = ", ".join(c for c in codes if isinstance(c, str))
    else:
        status = "not available"
    return {"status": status or "unknown", "location": "", "call_number": ""}


def _parse_primo_doc(doc):
    """One Primo `docs` entry -> {title, author, year, type, record_id}, or None if there is no
    usable title. Defensive against missing pnx sections and the $$C..$$V.. delimited creator."""
    if not isinstance(doc, dict):
        return None
    pnx = doc.get("pnx")
    pnx = pnx if isinstance(pnx, dict) else {}
    display = pnx.get("display")
    display = display if isinstance(display, dict) else {}
    control = pnx.get("control")
    control = control if isinstance(control, dict) else {}

    title = _primo_first(display.get("title"))
    if not title:
        return None
    creator = _primo_first(display.get("creator")) or _primo_first(display.get("contributor"))
    author = creator.split("$$")[0].strip() if creator else ""
    return {
        "title": title,
        "author": author,
        "year": _primo_first(display.get("creationdate")),
        "type": _primo_first(display.get("type")),
        "record_id": _primo_first(control.get("recordid")),
    }


def _primo_total(data):
    """The reported total match count. 0 (the only clean not-held signal) if absent/odd-shaped."""
    info = data.get("info") if isinstance(data, dict) else None
    total = info.get("total") if isinstance(info, dict) else None
    return total if isinstance(total, int) else 0


def _primo_search_page(query):
    """A student-facing Primo results URL for this query - the synthetic source and the place to
    verify catalog holdings (matching how database_catalog cites the A-Z page)."""
    params = {
        "query": f"any,contains,{query}",
        "tab": PRIMO_TAB,
        "search_scope": PRIMO_SCOPE,
        "vid": PRIMO_VID,
        "offset": "0",
    }
    return f"{PRIMO_DISCOVERY_URL}?{urllib.parse.urlencode(params)}"


def _run_primo_tool(tool_input):
    """Execute search_book_catalog: a live Primo search plus a per-result availability lookup.

    Returns (result_json, source). result_json carries `total` (the ONLY clean not-held signal,
    == 0) and up to PRIMO_NUMBER_OF_RESULTS candidate `results` (title/author/year/type +
    availability) for the MODEL to judge - the handler makes NO held/not-held decision and applies
    NO score threshold. On a blank query it returns an error result; on ANY live-call failure it
    soft-fails to a 'catalog_unavailable' result so the model can still answer. `source` is the
    Primo results page for a lookup that produced candidates (deduped like the catalog source),
    otherwise None (a no-match or an unavailable lookup contributes no source)."""
    query = ""
    if isinstance(tool_input, dict):
        query = (tool_input.get("query") or "").strip()
    if not query:
        return {"error": "No search terms were provided."}, None

    try:
        data = _primo_search_request(query, PRIMO_NUMBER_OF_RESULTS, PRIMO_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - live third-party call: soft-fail, never throw
        print(json.dumps({"event": "primo_search_failed", "error": f"{type(exc).__name__}: {exc}"}))
        return {"error": "catalog_unavailable", "note": _PRIMO_UNAVAILABLE_NOTE}, None
    if not isinstance(data, dict):
        print(json.dumps({"event": "primo_search_failed", "error": "unexpected response shape"}))
        return {"error": "catalog_unavailable", "note": _PRIMO_UNAVAILABLE_NOTE}, None

    total = _primo_total(data)
    docs = data.get("docs")
    docs = docs if isinstance(docs, list) else []

    results = []
    # Bound the total time spent on per-result availability lookups; past the budget the rest
    # report 'unknown' rather than pushing the request toward the Lambda timeout.
    deadline = time.monotonic() + PRIMO_AVAILABILITY_BUDGET_SECONDS
    for doc in docs[:PRIMO_NUMBER_OF_RESULTS]:
        parsed = _parse_primo_doc(doc)
        if not parsed:
            continue
        if time.monotonic() < deadline:
            availability = _primo_availability(parsed["record_id"], PRIMO_TIMEOUT_SECONDS)
        else:
            availability = {"status": "unknown", "location": "", "call_number": ""}
        results.append(
            {
                "title": parsed["title"],
                "author": parsed["author"],
                "year": parsed["year"],
                "type": parsed["type"],
                "availability": availability,
            }
        )

    result_json = {"query": query, "total": total, "results": results}
    if not results:
        result_json["note"] = (
            "No matching records were found in the library catalog."
            if total == 0
            else "No usable catalog records were returned for this search."
        )
    source = None
    if results:
        source = {"uri": _primo_search_page(query), "excerpt": f"Gavilan Library catalog search: {query}"}
    return result_json, source


def _output_guardrail_config():
    """Converse guardrailConfig for the OUTPUT backstop, or None if not wired (id + version
    required).

    This guardrail is content-filters-only with input strengths NONE and no PII policy, so
    attaching it to Converse screens the generated answer WITHOUT touching the retrieved
    <context> in the user message. Input screening happens separately in
    _apply_input_guardrail(), so no guardContent tagging is needed here."""
    if not OUTPUT_GUARDRAIL_ID or not OUTPUT_GUARDRAIL_VERSION:
        return None
    return {
        "guardrailIdentifier": OUTPUT_GUARDRAIL_ID,
        "guardrailVersion": OUTPUT_GUARDRAIL_VERSION,
        "trace": GUARDRAIL_TRACE,
    }


def _guardrail_output_text(response):
    """The text the guardrail returned in `outputs` - the masked query (on anonymize) or the
    configured block message (on block). Empty string if absent."""
    for out in response.get("outputs") or []:
        if isinstance(out, dict) and out.get("text"):
            return out["text"]
    return ""


def _classify_input_assessment(response):
    """Reduce an ApplyGuardrail(source=INPUT) response to one decision.

    Returns "block" if any policy hard-blocked the query (content filter, prompt attack,
    denied topic, blocked word, or a PII entity configured to BLOCK); "mask" if the only
    intervention was PII anonymization; "clean" if nothing intervened.

    Both mask and block report action=GUARDRAIL_INTERVENED at the top level, so we inspect
    the per-item actions in the assessment rather than trusting the top-level action alone."""
    if response.get("action") != _ACTION_INTERVENED:
        return "clean"

    blocked = False
    anonymized = False
    for assessment in response.get("assessments") or []:
        for f in (assessment.get("contentPolicy") or {}).get("filters", []) or []:
            if f.get("action") == _ITEM_BLOCKED:
                blocked = True
        for t in (assessment.get("topicPolicy") or {}).get("topics", []) or []:
            if t.get("action") == _ITEM_BLOCKED:
                blocked = True
        word_policy = assessment.get("wordPolicy") or {}
        for w in (word_policy.get("customWords") or []) + (
            word_policy.get("managedWordLists") or []
        ):
            if w.get("action") == _ITEM_BLOCKED:
                blocked = True
        sip = assessment.get("sensitiveInformationPolicy") or {}
        for item in (sip.get("piiEntities") or []) + (sip.get("regexes") or []):
            if item.get("action") == _ITEM_BLOCKED:
                blocked = True
            elif item.get("action") == _ITEM_ANONYMIZED:
                anonymized = True

    if blocked:
        return "block"
    if anonymized:
        return "mask"
    # Intervened but the assessment showed neither a hard block nor an anonymization. This
    # should not happen given the model's action enums; block conservatively rather than
    # silently forwarding a query the guardrail flagged.
    return "block"


def _reduce_assessments(assessments):
    """Privacy-safe summary of guardrail assessments for logging: policy/entity TYPES +
    actions + counts only, NEVER the raw matched text (item["match"] is the very PII/content
    the guardrail exists to keep out of plaintext logs). Shared by the input screen and the
    output-backstop logging."""
    content_filters = []
    topics_blocked = 0
    words_blocked = 0
    pii = {}
    for assessment in assessments or []:
        for f in (assessment.get("contentPolicy") or {}).get("filters", []) or []:
            content_filters.append({"type": f.get("type"), "action": f.get("action")})
        for t in (assessment.get("topicPolicy") or {}).get("topics", []) or []:
            if t.get("action") == _ITEM_BLOCKED:
                topics_blocked += 1
        word_policy = assessment.get("wordPolicy") or {}
        for w in (word_policy.get("customWords") or []) + (
            word_policy.get("managedWordLists") or []
        ):
            if w.get("action") == _ITEM_BLOCKED:
                words_blocked += 1
        sip = assessment.get("sensitiveInformationPolicy") or {}
        for item in (sip.get("piiEntities") or []) + (sip.get("regexes") or []):
            # Bucket by entity type + action; never include item["match"].
            key = f"{item.get('type') or item.get('name')}:{item.get('action')}"
            pii[key] = pii.get(key, 0) + 1
    return {
        "content_filters": content_filters,
        "topics_blocked": topics_blocked,
        "words_blocked": words_blocked,
        "pii": pii,
    }


def _converse_trace_assessments(trace):
    """Collect the guardrail assessment objects out of a Converse response trace.guardrail,
    ignoring modelOutput and any other raw-text fields. inputAssessment is a map of
    guardrail-id -> assessment; outputAssessments is a map of guardrail-id -> list of
    assessments (verified against the installed bedrock-runtime Converse model)."""
    guardrail = (trace or {}).get("guardrail") or {}
    collected = []
    input_assessment = guardrail.get("inputAssessment")
    if isinstance(input_assessment, dict):
        collected.extend(a for a in input_assessment.values() if isinstance(a, dict))
    output_assessments = guardrail.get("outputAssessments")
    if isinstance(output_assessments, dict):
        for entries in output_assessments.values():
            if isinstance(entries, list):
                collected.extend(a for a in entries if isinstance(a, dict))
    return collected


def _log_input_guardrail(response, decision):
    """Structured, PII-safe log of the input-screen outcome on every request."""
    print(
        json.dumps(
            {
                "event": "input_guardrail",
                "action": response.get("action"),
                "decision": decision,
                "assessment": _reduce_assessments(response.get("assessments") or []),
            },
            default=str,
        )
    )


def _apply_input_guardrail(query):
    """Screen the bare user query BEFORE retrieval via ApplyGuardrail (source=INPUT).

    Returns a (decision, text) pair:
      ("proceed", <query>)          - clean, or PII masked; run retrieval/generation on <query>
                                      (the masked text when PII was present, silently).
      ("block", <blocked message>)  - content-filter / prompt-attack / PII-block hit; the
                                      caller returns the message with no retrieval or generation.

    If the input guardrail is not wired (local/dev), the query passes through untouched."""
    if not INPUT_GUARDRAIL_ID or not INPUT_GUARDRAIL_VERSION:
        return "proceed", query

    response = _bedrock_client().apply_guardrail(
        guardrailIdentifier=INPUT_GUARDRAIL_ID,
        guardrailVersion=INPUT_GUARDRAIL_VERSION,
        source="INPUT",
        # Bare user text, no qualifiers: qualifiers are the contextual-grounding tagging
        # path, which this project deliberately does not use.
        content=[{"text": {"text": query}}],
    )
    decision = _classify_input_assessment(response)
    _log_input_guardrail(response, decision)

    if decision == "block":
        return "block", _guardrail_output_text(response) or _FALLBACK_BLOCK_MESSAGE
    if decision == "mask":
        # Proceed silently on the masked query; the student is not told masking happened.
        return "proceed", _guardrail_output_text(response) or query
    return "proceed", query


def _message_text(message):
    """The first text block of a Converse output message (a message may mix text with
    toolUse blocks; we surface the text). Empty string if there is none."""
    for block in (message or {}).get("content", []) or []:
        if isinstance(block, dict) and "text" in block:
            return block["text"]
    return ""


def _first_text(response):
    """The first text block of a Converse response. On a guardrail block this is the
    configured blocked message; otherwise the generated answer."""
    return _message_text(response.get("output", {}).get("message", {}))


def _log_guardrail_assessment(response):
    """Structured, PII-safe log of the OUTPUT guardrail outcome on every generation, so
    interventions are measurable for later tuning. Logs stopReason + a REDUCED assessment
    (types/actions/counts), never the raw trace - trace.guardrail carries modelOutput and
    matched text, which must not land in plaintext logs. -> CloudWatch Logs."""
    stop_reason = response.get("stopReason")
    print(
        json.dumps(
            {
                "event": "guardrail_assessment",
                "stop_reason": stop_reason,
                "intervened": stop_reason == _GUARDRAIL_STOP_REASON,
                "assessment": _reduce_assessments(
                    _converse_trace_assessments(response.get("trace"))
                ),
            },
            default=str,
        )
    )


def run_agent(messages):
    """Agentic Bedrock Converse tool-use loop over three tools (KB search, database catalog,
    live Primo book/media catalog).

    `messages` is the seed conversation in Converse shape (a list of {role, content} turns,
    starting with user and ending with the newest user turn - see _seed_messages), already
    input-screened. The system prompt goes in Converse `system`, never into these messages. The
    loop mutates its own copy of `messages`, so callers should pass a fresh list. Each turn:
      - call Converse with the one-tool toolConfig + the OUTPUT guardrail (backstop on every turn);
      - if the OUTPUT guardrail intervenes -> return the blocked message, no sources;
      - if stopReason == "tool_use" -> run the tool for EACH toolUse block the model requested
        (it may ask for several at once), append the assistant turn and ONE user message carrying
        all the toolResults, and loop;
      - any other stopReason (end_turn, max_tokens, ...) -> done, return the answer.
    Retrieval only happens when the model calls the tool, so `chunks` accumulates across the
    whole loop (empty if the model answered directly, e.g. a greeting). A safety cap of
    MAX_AGENT_ITERATIONS bounds the loop.

    Returns {"answer", "blocked", "chunks", "catalog_sources"}: `chunks` are the KB passages from
    search_library_info (drive `sources` + eval full_context); `catalog_sources` are synthetic
    source entries a substantive database_catalog lookup contributed."""
    collected_chunks = []
    catalog_sources = []
    answer = ""
    tool_config = _tool_config()
    guardrail = _output_guardrail_config()

    for _ in range(MAX_AGENT_ITERATIONS):
        kwargs = {
            "modelId": GENERATION_MODEL_ID,
            "system": [{"text": SYSTEM_PROMPT}],
            "messages": messages,
            "toolConfig": tool_config,
            # Short, direct, low-variance answers for a factual FAQ bot. The maxTokens/temperature
            # key names and nesting match the bedrock-runtime Converse inferenceConfig shape.
            "inferenceConfig": {
                "maxTokens": GENERATION_MAX_TOKENS,
                "temperature": GENERATION_TEMPERATURE,
            },
        }
        # OUTPUT guardrail on EVERY turn so the final answer is always screened.
        if guardrail:
            kwargs["guardrailConfig"] = guardrail

        response = _bedrock_client().converse(**kwargs)
        _log_guardrail_assessment(response)

        if response.get("stopReason") == _GUARDRAIL_STOP_REASON:
            # The OUTPUT guardrail blocked this turn: return its message, drop all sources.
            return {
                "answer": _first_text(response),
                "blocked": True,
                "chunks": [],
                "catalog_sources": [],
            }

        out_message = response.get("output", {}).get("message", {}) or {}
        text = _message_text(out_message)
        if text:
            answer = text  # keep the latest model text as the running best answer

        if response.get("stopReason") != "tool_use":
            # Terminal turn (end_turn / max_tokens / stop_sequence / ...): we have the answer.
            return {
                "answer": answer,
                "blocked": False,
                "chunks": collected_chunks,
                "catalog_sources": catalog_sources,
            }

        # tool_use: echo the assistant turn back verbatim (it carries the toolUse blocks), then
        # run every requested tool and return all results in a single following user message.
        messages.append(out_message)
        tool_results = []
        for block in out_message.get("content", []) or []:
            tool_use = block.get("toolUse") if isinstance(block, dict) else None
            if not tool_use:
                continue
            name = tool_use.get("name")
            if name == SEARCH_TOOL_NAME:
                chunks, result_json = _run_search_tool(tool_use.get("input"))
                collected_chunks.extend(chunks)
                status = "success"
            elif name == CATALOG_TOOL_NAME:
                result_json, source = _run_catalog_tool(tool_use.get("input"))
                # A substantive catalog lookup contributes the synthetic A-Z-page source (deduped).
                if source and source not in catalog_sources:
                    catalog_sources.append(source)
                status = "error" if "error" in result_json else "success"
            elif name == PRIMO_TOOL_NAME:
                result_json, source = _run_primo_tool(tool_use.get("input"))
                # A lookup that produced candidates contributes the Primo results-page source
                # (deduped; a per-query verify link, unlike the single A-Z page). A soft-fail or a
                # no-match contributes none. status=error on a soft-fail lets the model recover.
                if source and source not in catalog_sources:
                    catalog_sources.append(source)
                status = "error" if "error" in result_json else "success"
            else:
                # The model requested a tool we did not define; report an error result so it can
                # recover rather than silently hanging.
                result_json = {"error": f"Unknown tool: {name}"}
                status = "error"
            tool_results.append(
                {
                    "toolResult": {
                        "toolUseId": tool_use.get("toolUseId"),
                        "content": [{"json": result_json}],
                        "status": status,
                    }
                }
            )
        if not tool_results:
            # stopReason was tool_use but no toolUse block was present: bail rather than loop.
            break
        messages.append({"role": "user", "content": tool_results})

    # Iteration cap hit without a terminal turn: return the best answer we have (or a fallback).
    return {
        "answer": answer or _MAX_ITERS_FALLBACK_MESSAGE,
        "blocked": False,
        "chunks": collected_chunks,
        "catalog_sources": catalog_sources,
    }


def _build_sources(chunks):
    """Deduplicate retrieved chunks by source uri for the response `sources` list.

    Only public URLs are surfaced. A chunk whose source resolves only to an internal s3:// URI
    (e.g. an older document ingested before the public-url sidecar existed) is OMITTED entirely -
    we never leak internal S3 bucket paths to the client. Fewer, clean sources beats a raw path."""
    sources = []
    seen = set()
    for chunk in chunks:
        uri = chunk.get("source")
        if not uri or uri in seen or uri.startswith("s3://"):
            continue
        seen.add(uri)
        sources.append({"uri": uri, "excerpt": chunk["text"][:_EXCERPT_CHARS]})
    return sources


def _parse_body(event):
    """The JSON object body of an HTTP API (payload format 2.0) event, or None if the body is
    absent, not valid JSON, or not a JSON object."""
    body = event.get("body")
    if body is None:
        return None
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _extract_query(data):
    """The validated user query from a parsed request body, or None for a missing / non-string
    / blank query. None yields the caller's clean 400 - never a downstream 500."""
    if not isinstance(data, dict):
        return None
    query = data.get("query") or data.get("question")
    # A non-string (e.g. {"query": 123} or {"query": {...}}) is truthy but would blow up inside
    # boto3 as an opaque 500. Reject it here as a clean 400 instead.
    if not isinstance(query, str):
        return None
    query = query.strip()
    return query or None


def _normalize_message(item):
    """Coerce one client-supplied history entry into {"role", "text"}, or None if unusable.

    Accepts a role of "user" or "assistant" (a widget "bot" turn maps to "assistant"); the text
    may arrive as `content` or `text` and must be a non-empty string once stripped. Anything
    malformed returns None so the caller can drop it rather than forwarding junk to Converse."""
    if not isinstance(item, dict):
        return None
    role = item.get("role")
    if not isinstance(role, str):
        return None
    role = role.strip().lower()
    if role == "bot":
        role = "assistant"
    if role not in _VALID_ROLES:
        return None
    text = item.get("content")
    if text is None:
        text = item.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    return {"role": role, "text": text}


def _extract_conversation(data):
    """The user/assistant conversation from a parsed request body (newest turn last), or None if
    there is no usable user question to answer (-> the caller returns a clean 400).

    Two accepted request shapes:
      - {"messages": [{"role", "content"}, ...]} - single-session history. Malformed entries are
        dropped; the conversation MUST end with a user turn (the new question).
      - {"query": "..."} / {"question": "..."} - the legacy single-turn shape (eval + curl), which
        becomes a one-message user conversation. Kept working for backward compatibility."""
    if not isinstance(data, dict):
        return None
    raw = data.get("messages")
    if isinstance(raw, list):
        msgs = [m for m in (_normalize_message(x) for x in raw) if m]
        # The newest turn must be the user's question; otherwise there is nothing to answer.
        if not msgs or msgs[-1]["role"] != "user":
            return None
        return msgs
    # Legacy single-query shape.
    query = _extract_query(data)
    if not query:
        return None
    return [{"role": "user", "text": query}]


def _seed_messages(conversation):
    """Build the Converse `messages` seed from a normalized conversation, guaranteeing a request
    Converse will accept (start with user, roles strictly alternate):
      1. keep only the last MAX_HISTORY_MESSAGES turns (the server-side cap; the client is never
         trusted to limit history);
      2. drop any leading assistant turn(s) so the seed starts with a user message (a trailing
         window of an alternating chat, or a widget greeting, can begin with assistant);
      3. merge any consecutive same-role turns into one message with multiple text blocks, since
         Converse/Anthropic reject repeated roles - this covers both a buggy client and any
         adjacency the trim/drop introduced.
    The result always starts with user and ends with the newest user turn."""
    trimmed = conversation[-MAX_HISTORY_MESSAGES:]
    start = 0
    while start < len(trimmed) and trimmed[start]["role"] != "user":
        start += 1
    trimmed = trimmed[start:]
    messages = []
    for msg in trimmed:
        if messages and messages[-1]["role"] == msg["role"]:
            messages[-1]["content"].append({"text": msg["text"]})
        else:
            messages.append({"role": msg["role"], "content": [{"text": msg["text"]}]})
    return messages


# Optional request flag: when true, /query additionally returns the full retrieved passages
# (what the model actually saw). The answer-quality eval sets it; the widget never does.
_FULL_CONTEXT_FLAG = "include_full_context"


def _full_context(chunks):
    """The retrieved passages exactly as they were fed into the model's <context>: full text
    and source, in retrieval order, with no truncation and no per-source dedup (unlike the
    public `sources`). Lets the answer-quality eval score faithfulness against what the model
    actually saw, not the truncated/deduped excerpts."""
    return [{"text": chunk["text"], "source": chunk.get("source")} for chunk in chunks]


def _response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


# Shown to the caller on any upstream failure; the widget renders its retry bubble on any
# non-2xx, so this stays generic and never leaks internals.
_UPSTREAM_ERROR_MESSAGE = (
    "The library assistant is temporarily unavailable. Please try again in a moment."
)


def _error_response(stage, exc):
    """Log a structured {event, stage, error} record and return a clean JSON error. 502: an
    upstream Bedrock/OSS dependency failed. Logs the exception type + message (not a raw
    traceback, and not the user query) so the failing stage is diagnosable without leaking a
    stack trace to the caller."""
    print(
        json.dumps(
            {
                "event": "query_failed",
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
            },
            default=str,
        )
    )
    return _response(502, {"error": _UPSTREAM_ERROR_MESSAGE})


def _request_path(event):
    """Request path for an HTTP API (payload format 2.0) event, e.g. '/query' or '/warm'."""
    http = (event.get("requestContext") or {}).get("http") or {}
    return http.get("path") or event.get("rawPath") or ""


def _handle_warm():
    """Warm path (GET /warm): a single KB Retrieve to warm the query Lambda container and the
    retrieval path before the student's first real query. No generation and no guardrail input
    screen - there is no user query to screen. The Bedrock Converse path is deliberately left
    cold."""
    try:
        retrieve(_WARM_QUERY)
    except Exception as exc:  # noqa: BLE001 - warm is fire-and-forget; return a clean error
        return _error_response("warm", exc)
    return _response(200, {"warmed": True})


def _handle_query(event):
    """Query path (POST /query): validate -> input screen -> retrieve -> generate.

    Accepts either the single-session history shape ({"messages": [...]}) or the legacy single
    {"query": ...} shape; both collapse to a normalized conversation whose newest turn is the
    user's current question."""
    data = _parse_body(event)
    conversation = _extract_conversation(data)
    if not conversation:
        return _response(400, {"error": "Missing 'query' in request body."})
    # The newest user turn is the question being asked now; it drives the size cap and the input
    # screen. Prior turns were already screened when they were first sent, one request each.
    query = conversation[-1]["text"]
    # Server-side size cap: reject an oversized query BEFORE any retrieval or guardrail call.
    # This is a clean 400, distinct from a guardrail block (a 200 carrying the block message).
    if len(query) > MAX_QUERY_CHARS:
        return _response(
            400,
            {"error": f"Query exceeds the maximum length of {MAX_QUERY_CHARS} characters."},
        )

    # Opt-in eval payload: the full retrieved passages, added to the response only when the
    # request explicitly asks (the widget never does, so its responses are unchanged).
    include_full_context = bool(data and data.get(_FULL_CONTEXT_FLAG))

    # Everything past validation touches AWS; wrap it so any fault surfaces as a clean, staged
    # JSON error instead of an opaque 500. `stage` names the step that failed. No retry logic.
    stage = "input_guardrail"
    try:
        # Screen the bare query BEFORE the agent runs. A content-filter / prompt-attack hit is
        # blocked here and returns immediately - no retrieval, no generation, no Bedrock spend.
        # PII is masked and we proceed silently on the masked text; the retrieved passages the
        # tool returns are never input-screened, so contact facts survive.
        decision, screened_query = _apply_input_guardrail(query)
        if decision == "block":
            return _response(200, {"answer": screened_query, "sources": []})
        # Replace the newest user turn with the screened text (masked, if the guardrail
        # anonymized PII) before seeding, so the agent runs on the screened question.
        conversation[-1]["text"] = screened_query
        # The agentic loop: Converse tool-use with KB retrieval as the sole tool. Retrieval,
        # generation, and the output-guardrail backstop all happen inside; on any fault this
        # whole step reports as the "agent" stage. Seed it with the trimmed conversation history.
        stage = "agent"
        result = run_agent(_seed_messages(conversation))
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any AWS/runtime fault
        return _error_response(stage, exc)

    # Sources: KB passages from search_library_info (deduped by uri) PLUS any synthetic
    # database-catalog source (the A-Z page). On an OUTPUT guardrail block the answer is the
    # blocked message, so attach no sources at all. full_context stays KB-only (what the model
    # semantically retrieved), so the catalog's synthetic source never pollutes eval data.
    chunks = result["chunks"]
    if result["blocked"]:
        sources = []
    else:
        sources = _build_sources(chunks)
        seen = {s["uri"] for s in sources}
        for cs in result.get("catalog_sources", []):
            if cs["uri"] and cs["uri"] not in seen:
                seen.add(cs["uri"])
                sources.append(cs)
    payload = {"answer": result["answer"], "sources": sources}
    if include_full_context:
        payload["full_context"] = _full_context(chunks)
    return _response(200, payload)


def lambda_handler(event, context):
    # Two clean routes on one Lambda: /warm (lightweight retrieval-only pre-warm) and
    # everything else -> the real /query path.
    if _request_path(event) == WARM_PATH:
        return _handle_warm()
    return _handle_query(event)
