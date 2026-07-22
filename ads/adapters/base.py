"""Adapter Protocol (ticket 007): the entire portable seam.

Everything above this layer (driver, phases, prompts) is harness-agnostic.
Everything below (subprocess calls, CLI flags, model ids) is adapter-owned.
The driver parses phase-shaped JSON out of `RunResult.text` itself
(`ads/phase_json.py`) — the adapter owns only its transport envelope.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, get_args

AdapterName = Literal["claude-code", "stub"]
Role = Literal["planning", "execution", "validation"]
ExitStatus = Literal["ok", "error"]

ADAPTER_CLAUDE_CODE: AdapterName = "claude-code"
ADAPTER_STUB: AdapterName = "stub"
ADAPTER_NAMES: tuple[AdapterName, ...] = get_args(AdapterName)


@dataclass(frozen=True)
class RunResult:
    text: str
    exit_status: ExitStatus


class Adapter(Protocol):
    def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        role: Role = "execution",
        allowed_tools: list[str] | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> RunResult: ...

    def resolve_model(self, role: Role) -> str: ...
