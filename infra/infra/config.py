"""Load the repo-root config.yaml for the CDK app.

config.yaml is the single source of truth for changeable knobs (embedding model,
chunking, index/field names, crawler seed URLs and filters). This module resolves it
relative to __file__ so it works no matter what the current working directory is.

Layout: this file is <repo>/infra/infra/config.py, so the repo root is parents[2] and
config.yaml sits directly under it.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "config.yaml"

# The tier that performs the COMPLETE sweep (every URL in every tier), as opposed to a tier that
# fetches only its own slice. Mirrors scraper.TIER_FULL: infra/ is the CDK app and scraper/ is
# Lambda source, so the two packages cannot import each other. A drift between the two spellings
# is harmless in both directions by design - the scraper treats any tier name it does not
# recognise as the complete sweep, and its prune always keys off the union of all tiers rather
# than off whatever the current run fetched.
TIER_FULL = "full"


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Parse config.yaml into a dict. Defaults to the repo-root config.yaml."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def resolve_cors_allow_origins(config: Dict[str, Any]) -> List[str]:
    """The browser origin allowlist for the HTTP API, from config's `cors.allow_origins`.

    There is no safe default here, so a missing/empty list is a synth-time error rather than
    a silent fallback. A wildcard is rejected outright: the endpoint fans out to paid Bedrock
    and Primo calls, and "*" would let any site drive it from its visitors' browsers.
    """
    origins = (config.get("cors") or {}).get("allow_origins")
    if not origins:
        raise ValueError(
            "config.yaml is missing cors.allow_origins - set the browser origin allowlist "
            "for the HTTP API (e.g. https://www.gavilan.edu)."
        )
    if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
        raise ValueError("cors.allow_origins must be a list of origin strings.")
    if any(o.strip() == "*" for o in origins):
        raise ValueError(
            "cors.allow_origins must not contain '*' - list the exact origins allowed to "
            "call this endpoint from a browser."
        )
    return list(origins)


def resolve_scraper_tiers(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The scraper's freshness tiers from config's `scraper.tiers`, validated at synth.

    Returns `{tier_name: {"schedule_cron": str, "urls": [str]}}` preserving the declaration
    order in config.yaml. Every seed URL belongs to exactly one tier; the stack builds one
    EventBridge rule per tier and hands the whole map to the Lambda, so this is the single
    place cadence and tier membership are defined.

    Validated here rather than trusted, because every failure mode is silent at deploy and
    only shows up as stale content weeks later: a missing cron produces a Lambda nothing ever
    invokes, and a URL listed under two tiers gets scraped twice per full run and makes
    "which tier owns this page" unanswerable.
    """
    tiers = (config.get("scraper") or {}).get("tiers")
    if not tiers or not isinstance(tiers, dict):
        raise ValueError(
            "config.yaml is missing scraper.tiers - declare each freshness tier with its own "
            "schedule_cron and urls (see the scraper block in config.yaml)."
        )

    resolved: Dict[str, Dict[str, Any]] = {}
    seen: Dict[str, str] = {}
    for name, tier in tiers.items():
        if not isinstance(tier, dict):
            raise ValueError(f"scraper.tiers.{name} must be a mapping with schedule_cron + urls.")
        cron = tier.get("schedule_cron")
        if not isinstance(cron, str) or not cron.strip():
            raise ValueError(
                f"scraper.tiers.{name} is missing schedule_cron - every tier carries its own "
                "EventBridge schedule expression."
            )
        urls = tier.get("urls")
        if not urls or not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise ValueError(
                f"scraper.tiers.{name}.urls must be a non-empty list of URL strings."
            )
        for url in urls:
            if url in seen:
                raise ValueError(
                    f"{url} is listed in both scraper.tiers.{seen[url]} and "
                    f"scraper.tiers.{name} - each seed URL belongs to exactly one tier."
                )
            seen[url] = name
        resolved[name] = {"schedule_cron": cron, "urls": list(urls)}

    if TIER_FULL not in resolved:
        raise ValueError(
            f"scraper.tiers must define a '{TIER_FULL}' tier - it is the complete sweep that "
            "fetches every URL and the only tier the stale-object prune is safe to run from."
        )
    return resolved


def resolve_seed_urls(config: Dict[str, Any]) -> List[str]:
    """Every configured seed URL, across all tiers, in declaration order.

    This is the corpus as configuration defines it - what a full run fetches and what the
    prune keeps in the KB source bucket - as opposed to any single tier's slice of it.
    """
    return [url for tier in resolve_scraper_tiers(config).values() for url in tier["urls"]]
