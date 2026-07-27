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
