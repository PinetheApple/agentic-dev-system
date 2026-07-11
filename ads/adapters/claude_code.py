"""Adapter for the `claude` CLI (Claude Code), headless print mode.

`run()` shells out to `claude -p <prompt> --model <id> --output-format json`
and parses the single JSON result. Model ids and capability flags come from
harness.toml — this file never hardcodes a model name.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

from ads.adapters.base import RunResult, StructuredPayload
from ads.config import HarnessConfig
from ads.tasks import TaskTier

DEFAULT_TIMEOUT_SECONDS = 600


class ClaudeCodeAdapter:
    def __init__(self, harness: HarnessConfig, claude_bin: str = "claude") -> None:
        self._harness = harness
        self._claude_bin = claude_bin

    def resolve_model(self, tier: TaskTier) -> str:
        try:
            return self._harness.tier_model[tier]
        except KeyError as exc:
            raise ValueError(f"no model configured for tier {tier!r}") from exc

    def capabilities(self) -> list[str]:
        return list(self._harness.capabilities)

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
    ) -> RunResult:
        cmd = [
            self._claude_bin,
            "-p",
            prompt,
            "--model",
            self.resolve_model(tier),
            "--output-format",
            "json",
        ]
        if allowed_tools:
            cmd += ["--allowedTools", ",".join(allowed_tools)]

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(text=str(exc), structured=None, exit_status="error")

        if proc.returncode != 0:
            return RunResult(text=proc.stderr or proc.stdout, structured=None, exit_status="error")

        structured: StructuredPayload | None = None
        text = proc.stdout
        try:
            # `claude`'s `--output-format json` payload shape is defined by the
            # CLI, not by us — this is the one Any we can't type away, so it's
            # cast to our own contract right at the boundary.
            raw_payload = cast(dict[str, object], json.loads(proc.stdout))
            # `claude`'s own JSON envelope wraps the model's answer in
            # `result`; that answer is what the phase-shaped payload lives in.
            text = cast(str, raw_payload.get("result", proc.stdout))
            structured = cast(StructuredPayload, raw_payload)
        except json.JSONDecodeError:
            pass  # fall back to raw stdout as text

        return RunResult(text=text, structured=structured, exit_status="ok")

    def sync(self) -> None:
        pass  # no cross-process session state to reconcile for headless -p calls
