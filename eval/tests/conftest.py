"""Shared test setup for the eval harness.

Puts eval/ on sys.path so the flat modules import as `runner`, `dataset_loader`, `config`,
and stubs boto3 so nothing needs it installed and no live AWS is ever reached. Tests
inject fake clients, so the stub's client() is deliberately made to fail loudly if hit.
"""

import sys
import types
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

if "boto3" not in sys.modules:
    _fake_boto3 = types.ModuleType("boto3")

    def _no_client(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError(
            "boto3.client was called in a unit test; inject a fake client instead."
        )

    _fake_boto3.client = _no_client
    sys.modules["boto3"] = _fake_boto3
