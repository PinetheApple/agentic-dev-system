"""Transport-only Claude Code adapter (ticket 007/009): owns the subprocess
envelope (cmd flags, stdin, stream-json parsing); the driver still parses
phase-shaped JSON out of `RunResult.text` (`ads/phase_json.py`).
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ads.adapters.base import Role, RunResult

DEFAULT_CMD: list[str] = ["claude", "-p"]
DEFAULT_TIER_MODEL: dict[str, str] = {
    "fast": "claude-haiku-4-5",
    "standard": "claude-sonnet-5",
    "deep": "claude-opus-4-8",
}
ROLE_TIER: dict[Role, str] = {
    "planning": "deep",
    "validation": "deep",
    "execution": "standard",
}


def _load_harness_config(cwd: Path) -> dict[str, Any]:
    path = cwd / ".agent" / "config" / "harness.toml"
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _model_for(config: dict[str, Any], role: Role) -> str:
    tier_model: dict[str, str] = config.get("tier_model", DEFAULT_TIER_MODEL)
    tier = ROLE_TIER[role]
    return tier_model.get(tier, DEFAULT_TIER_MODEL[tier])


class ClaudeCodeAdapter:
    """Satisfies the `Adapter` Protocol by shelling out to the real `claude`
    CLI in `stream-json` mode. Pure transport — no phase-shape awareness."""

    def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        role: Role = "execution",
        allowed_tools: list[str] | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> RunResult:
        config = _load_harness_config(cwd)
        run_cfg = config.get("run", {})
        cmd: list[str] = list(run_cfg.get("cmd", DEFAULT_CMD))
        cmd += [
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            _model_for(config, role),
            "--permission-mode",
            "acceptEdits",
            # Hermetic: the ADS prompt is the sole instruction source. Skip the
            # operator's user/project/local CLAUDE.md, hooks, and skills, which
            # otherwise drown the task prompt and derail the run.
            "--setting-sources",
            "",
        ]
        if allowed_tools:
            cmd += ["--allowed-tools", " ".join(allowed_tools)]

        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(prompt)
        proc.stdin.close()

        result_text: str | None = None
        for line in proc.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            if on_event is not None:
                on_event(stripped)
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                result_text = event.get("result", "")

        exit_code = proc.wait()
        if exit_code != 0 or result_text is None:
            return RunResult(text=result_text or "", exit_status="error")
        return RunResult(text=result_text, exit_status="ok")

    def resolve_model(self, role: Role) -> str:
        return _model_for(_load_harness_config(Path.cwd()), role)
