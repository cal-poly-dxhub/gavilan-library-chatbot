"""Load eval/eval_config.yaml.

Separate from the app's root config.yaml on purpose (test-data + eval-run lifecycle, not
deploy config). Resolves the config relative to __file__ so the cwd does not matter.
"""

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = _EVAL_DIR / "eval_config.yaml"


def load_eval_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Return the `eval` section of eval_config.yaml as a dict."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    with open(config_path, "r") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "eval" not in data:
        raise ValueError(f"{config_path} must contain a top-level 'eval' section.")
    return data["eval"]
