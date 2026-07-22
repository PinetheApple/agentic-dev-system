"""Shared "extract the phase JSON payload from a model's answer text" helpers.

Both `claude_code.py` and `opencode.py` receive an answer that may be a bare
JSON object or one wrapped in a markdown ```json fence — this is the one
place that ambiguity is resolved, so each adapter's own stdout-envelope
parsing stays focused on its harness's event shape.
"""

from __future__ import annotations

import json
import re
from typing import cast

from ads.adapters.base import StructuredPayload

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def strip_json_fence(text: str) -> str:
    match = _JSON_FENCE_RE.match(text.strip())
    return match.group(1) if match else text


def parse_phase_payload(text: str) -> StructuredPayload | None:
    """The model is instructed to answer with a bare JSON object, but may
    still wrap it in markdown code fences — try both."""
    for candidate in (text, strip_json_fence(text)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return cast(StructuredPayload, parsed)
    return None
