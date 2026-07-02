"""Capture-outputs stage for retrieve-and-generate evaluation.

Its job, once the bot is deployed: run each evaluation question through OUR bot's real user
path (the HTTP API /query endpoint, the same path a widget hits, NOT the KB directly) and
capture what the bot produced into the shared CapturedOutput type that format_generate.py
consumes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from dataset_loader import QAPair
from format_generate import CapturedOutput


def capture_outputs(
    pairs: Sequence[QAPair],
    config: Dict[str, Any],
    *,
    http_client: Any = None,
) -> List[CapturedOutput]:
    """Run each question through the deployed bot and capture its output.

    Intended real implementation (once the bot is deployed):
      1. Read the bot endpoint from config["generate"]["bot_api_url"].
      2. For each QAPair, POST {"query": pair.question} to that /query endpoint
         (http_client is injectable for testing; default to a real HTTP client).
      3. Parse the bot's JSON response into a CapturedOutput:
           - answer    <- the generated answer text
           - passages  <- the retrieved chunks (text + optional name/metadata)
           - citations <- the bot's citations if present, else None
         The exact response shape MUST be confirmed against the deployed Lambda's
         response body before mapping; today's handler returns only a placeholder answer
         and does not yet expose retrieved passages or citations.
      4. Return the list aligned 1:1 with `pairs`.

    Raises:
      NotImplementedError: always, until the bot is deployed and its response shape is
      confirmed.
    """
    raise NotImplementedError(
        "capture_outputs is a stub. TODO: implement once the bot is deployed. It needs "
        "config['generate']['bot_api_url'] (currently TBD) and the deployed Lambda's "
        "confirmed /query response shape (answer + retrieved passages + optional "
        "citations) before it can build CapturedOutput objects. Do not fake this."
    )
