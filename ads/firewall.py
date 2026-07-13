"""Firewall seam (ticket 005, Rule 1) — not wired into the phase loop yet.

`run_firewalled` runs a nested `adapter.run()` whose output contract is
"answer + cited paths only, never the raw transcript". Callers that need to
route noisy sub-work through an adapter — many-file reads, verbose command
output, open-ended research — should call this instead of `adapter.run()`
directly, so that noise never enters the parent conversation/context window;
only the distilled `FirewalledResult` (summary + cited paths) comes back.

This module is a thin, tested seam only. Nothing in `ads/dispatch.py` calls
it yet — ticket 005 Rule 5 (budget/re-split) and later multi-phase work are
expected to route sub-work through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ads.adapters.base import Adapter
from ads.tasks import TaskTier

SUMMARY_KEY = "summary"
CITED_PATHS_KEY = "cited_paths"


@dataclass(frozen=True)
class FirewalledResult:
    """The only thing that crosses the firewall back to the caller. There is
    deliberately no `text`/transcript field here — that's the contract."""

    summary: str
    cited_paths: list[str]


def run_firewalled(
    adapter: Adapter,
    prompt: str,
    cwd: Path,
    allowed_tools: list[str] | None = None,
    tier: TaskTier = "standard",
) -> FirewalledResult:
    result = adapter.run(prompt, cwd=cwd, allowed_tools=allowed_tools, tier=tier)
    structured = result.structured or {}
    summary = structured.get(SUMMARY_KEY) or result.text
    cited_paths = list(structured.get(CITED_PATHS_KEY, []))
    return FirewalledResult(summary=summary, cited_paths=cited_paths)
