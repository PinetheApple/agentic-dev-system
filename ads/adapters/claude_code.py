"""Adapter for the `claude` CLI (Claude Code), headless print mode.

`run()` shells out to `claude -p <prompt> --model <id> --output-format json`
and parses the result envelope, then the phase-shaped JSON payload nested
inside its `result` field (see `parse_claude_stdout`). Model ids and
capability flags come from harness.toml — this file never hardcodes a model
name.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import cast

from ads.adapters.base import RunResult, StructuredPayload
from ads.config import HarnessConfig
from ads.tasks import TaskTier

DEFAULT_TIMEOUT_SECONDS = 600

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def _extract_result_envelope(raw: object) -> dict[str, object] | None:
    """`claude -p --output-format json` is documented to return a single
    `{"type": "result", ...}` object, but hook/plugin-heavy sessions have been
    observed to instead emit a JSON array of the full event stream (system,
    assistant, thinking, rate-limit, ... entries) that terminates in the
    `type: "result"` entry. Handle both shapes."""
    if isinstance(raw, dict):
        return cast(dict[str, object], raw)
    if isinstance(raw, list):
        for item in reversed(cast(list[object], raw)):
            if isinstance(item, dict) and cast(dict[str, object], item).get("type") == "result":
                return cast(dict[str, object], item)
        return None
    return None


def _strip_json_fence(text: str) -> str:
    match = _JSON_FENCE_RE.match(text.strip())
    return match.group(1) if match else text


def _parse_phase_payload(text: str) -> StructuredPayload | None:
    """The model is instructed to answer with a bare JSON object, but may
    still wrap it in markdown code fences — try both."""
    for candidate in (text, _strip_json_fence(text)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return cast(StructuredPayload, parsed)
    return None


def parse_claude_stdout(stdout: str) -> tuple[str, StructuredPayload | None]:
    """Pure parse of `claude -p --output-format json` stdout into the model's
    answer text and the phase-shaped structured payload nested inside it
    (or `None` if either the envelope or the nested payload can't be found).

    `claude`'s own JSON shape is not a contract we own — parsing it is the one
    place `Any` is unavoidable; it collapses into `StructuredPayload` here.
    """
    try:
        raw_payload = cast(object, json.loads(stdout))
    except json.JSONDecodeError:
        return stdout, None

    envelope = _extract_result_envelope(raw_payload)
    if envelope is None:
        return stdout, None

    text = cast(str, envelope.get("result", stdout))
    return text, _parse_phase_payload(text)


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

        text, structured = parse_claude_stdout(proc.stdout)
        return RunResult(text=text, structured=structured, exit_status="ok")

    def sync(self) -> None:
        pass  # no cross-process session state to reconcile for headless -p calls
