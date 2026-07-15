#!/usr/bin/env python3
"""Query the Gavilan College Primo library book catalog API.

Two endpoints are used:
  - the search endpoint (pnxs) returns ranked bib records, but NO real-time availability;
  - a per-record delivery call (pnxs/L/<recordid>?getDelivery=true) returns holdings and
    availability (on-shelf status, campus/location, call number, or online access).

By default this searches MyInstitution / LibraryCatalog - the local Gavilan holdings - which
is the right scope for "do you have this book?". CourseReserves (the old default) only covers
textbooks placed on short-term reserve; MyInst_and_CI adds the Central Index (articles, huge,
noisy). Change with --scope/--tab.

Availability is fetched per result (one extra HTTP call each); pass --no-availability to skip.
"""

import argparse
import json
import ssl
import urllib.request
import urllib.parse


BASE_URL = "https://caccl-gavilan.primo.exlibrisgroup.com/primaws/rest/pub/pnxs"

DEFAULT_PARAMS = {
    "inst": "01CACCL_GAVILAN",
    "vid": "01CACCL_GAVILAN:GAVILAN",
    "pcAvailability": "true",
    "skipDelivery": "Y",
    "lang": "en",
}


def _build_opener():
    """An HTTPS opener that verifies certs. macOS python.org builds don't use the system cert
    store, which makes urllib raise CERTIFICATE_VERIFY_FAILED against this host; prefer certifi's
    bundle when it's importable so the script just runs locally without exporting SSL_CERT_FILE.
    In AWS Lambda certifi isn't needed (the runtime trusts the system certs), so this falls back
    to the default context there."""
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi missing/broken: use the platform default
        ctx = ssl.create_default_context()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


_OPENER = _build_opener()


def _get_json(url):
    with _OPENER.open(urllib.request.Request(url)) as resp:
        return json.loads(resp.read())


def search(query, scope="MyInstitution", tab="LibraryCatalog", sort="rank", limit=10, offset=0):
    params = {
        **DEFAULT_PARAMS,
        "q": f"any,contains,{query}",
        "scope": scope,
        "tab": tab,
        "sort": sort,
        "limit": str(limit),
        "offset": str(offset),
    }
    url = f"{BASE_URL}?{urllib.parse.urlencode(params)}"
    return _get_json(url)


def fetch_availability(recordid, scope="MyInstitution"):
    """Real-time availability for one record, via the delivery endpoint. Returns a dict:
    {status, location, call_number, online, raw_codes} or {error}. The search response has no
    availability, so this is a second call per record."""
    params = {
        "inst": DEFAULT_PARAMS["inst"],
        "vid": DEFAULT_PARAMS["vid"],
        "scope": scope,
        "lang": "en",
        "getDelivery": "true",
    }
    url = f"{BASE_URL}/L/{urllib.parse.quote(recordid)}?{urllib.parse.urlencode(params)}"
    try:
        delivery = _get_json(url).get("delivery", {})
    except Exception as exc:  # noqa: BLE001 - never let an availability lookup kill the run
        return {"error": f"{type(exc).__name__}: {exc}"}

    codes = delivery.get("availability") or []
    best = delivery.get("bestlocation") or {}
    eservices = delivery.get("electronicServices") or []

    if best:
        where = ", ".join(x for x in (best.get("mainLocation"), best.get("subLocation")) if x)
        return {
            "status": best.get("availabilityStatus") or (codes[0] if codes else "unknown"),
            "location": where,
            "call_number": best.get("callNumber", ""),
            "online": False,
            "raw_codes": codes,
        }
    if eservices:
        names = [s.get("serviceType") or s.get("displayName") or "online access" for s in eservices]
        return {"status": "online", "location": ", ".join(names), "call_number": "",
                "online": True, "raw_codes": codes}
    # No physical holdings and no e-service: fall back to whatever Primo displays.
    displayed = delivery.get("displayedAvailability")
    return {"status": displayed or (", ".join(codes) if codes else "not available"),
            "location": "", "call_number": "", "online": False, "raw_codes": codes}


def _fmt_availability(avail):
    if "error" in avail:
        return f"lookup failed ({avail['error']})"
    parts = [avail["status"]]
    if avail.get("location"):
        parts.append(f"at {avail['location']}")
    if avail.get("call_number"):
        parts.append(f"call no. {avail['call_number']}")
    return " - ".join(parts)


def parse_delimited(value, code):
    """Extract values with a given code from Primo's $$C...$$V... format."""
    results = []
    for part in value.split(";"):
        part = part.strip()
        if f"$$C{code}$$V" in part:
            results.append(part.split(f"$$V")[1])
        elif part.startswith(f"$$C{code}$$V"):
            results.append(part[len(f"$$C{code}$$V"):])
    return results


def format_results(data, with_availability=True):
    total = data.get("info", {}).get("total", 0)
    docs = data.get("docs", [])
    print(f"Total results: {total}\n")

    for i, doc in enumerate(docs, 1):
        pnx = doc.get("pnx", {})
        display = pnx.get("display", {})
        addata = pnx.get("addata", {})
        control = pnx.get("control", {})

        title = (display.get("title") or [""])[0].strip()
        creator = (display.get("creator") or display.get("contributor") or [""])[0]
        author = creator.split("$$")[0] if creator else ""
        year = (display.get("creationdate") or [""])[0]
        edition = (display.get("edition") or [""])[0]
        publisher = (display.get("publisher") or [""])[0]
        description = (display.get("description") or [""])[0]
        isbns = addata.get("isbn", [])
        score = (control.get("score") or [""])[0]
        recordid = (control.get("recordid") or [""])[0]

        crsinfo = (display.get("crsinfo") or [""])[0]
        course = ""
        if crsinfo:
            for part in crsinfo.split("$$"):
                if part.startswith("V"):
                    course = part[1:]
                    break

        print(f"{i}. {title}")
        print(f"   Author: {author}")
        print(f"   Year: {year} | Edition: {edition}")
        print(f"   Publisher: {publisher}")
        if score:
            print(f"   Relevance score: {score}")
        if with_availability and recordid:
            print(f"   Availability: {_fmt_availability(fetch_availability(recordid))}")
        if course:
            print(f"   Course: {course}")
        if isbns:
            print(f"   ISBN: {', '.join(isbns[:2])}")
        if description:
            print(f"   Description: {description[:120]}{'...' if len(description) > 120 else ''}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Search Gavilan College library catalog")
    parser.add_argument("query", help="Search terms")
    parser.add_argument("--scope", default="MyInstitution",
                        help="Search scope (default: MyInstitution - local Gavilan holdings)")
    parser.add_argument("--tab", default="LibraryCatalog",
                        help="Tab (default: LibraryCatalog)")
    parser.add_argument("--sort", default="rank",
                        choices=["rank", "date", "author", "title"],
                        help="Sort order (default: rank)")
    parser.add_argument("--limit", type=int, default=10,
                        help="Results per page (default: 10)")
    parser.add_argument("--offset", type=int, default=0,
                        help="Result offset for pagination (default: 0)")
    parser.add_argument("--no-availability", action="store_true",
                        help="Skip the per-result availability lookup (one fewer HTTP call each)")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON instead of formatted text")

    args = parser.parse_args()
    data = search(args.query, args.scope, args.tab, args.sort, args.limit, args.offset)

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        format_results(data, with_availability=not args.no_availability)


if __name__ == "__main__":
    main()
