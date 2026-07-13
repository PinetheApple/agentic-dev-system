"""Adapter for the `opencode` CLI, headless `run` mode.

`run()` shells out to `opencode run <prompt> -m <provider/model> --format json
--dir <cwd>` and parses the NDJSON event stream `--format json` emits (one
JSON object per line; no single enclosing envelope like `claude`'s
`--output-format json`).

Grounded in a real (free-tier, zero-cost, no-credentials) `opencode run
--format json` invocation against v1.17.16: each line is `{"type": ...,
"part": {"type": ..., ...}}`. The events actually observed were
`step_start`, `text` (`part.text` carries a chunk of the answer), and
`step_finish`. Tool-call event shapes were not observed and are not required
here — this parser only needs the text stream, and is tolerant of unknown
event/part types so it won't break if the real schema has more variants.

Tool scoping: unlike `claude`, there is no `--allowedTools` CLI flag.
OpenCode scopes tools via `opencode.json`/agent permission config, or
`--auto` (auto-approve everything not explicitly denied). This adapter does
NOT fake a per-call allowlist flag — when `allowed_tools` is requested it
falls back to `--auto` so the run can still act, and `capabilities()` omits
`allowedtools-cli` so callers can see the gap instead of assuming parity.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import cast

from ads import sandbox
from ads.adapters._json_envelope import parse_phase_payload
from ads.adapters.base import RunResult, StructuredPayload
from ads.config import HarnessConfig
from ads.sandbox import SANDBOX_NATIVE_CAPABILITY, SandboxPolicy
from ads.tasks import TaskTier

DEFAULT_TIMEOUT_SECONDS = 600

_TEXT_EVENT_TYPE = "text"


def _event_text(event: dict[str, object]) -> str | None:
    if event.get("type") != _TEXT_EVENT_TYPE:
        return None
    part = event.get("part")
    if not isinstance(part, dict):
        return None
    part_dict = cast(dict[str, object], part)
    if part_dict.get("type") != _TEXT_EVENT_TYPE:
        return None
    text = part_dict.get("text")
    return text if isinstance(text, str) else None


def parse_opencode_stdout(stdout: str) -> tuple[str, StructuredPayload | None]:
    """Pure parse of `opencode run --format json` NDJSON stdout into the
    model's answer text (concatenation of all `text` event parts, in
    stream order) and the phase-shaped structured payload nested inside it.

    Falls back to `(stdout, None)` if no `text` events are found, and to
    `(text, None)` if the accumulated text isn't phase-shaped JSON.
    """
    chunks: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = cast(object, json.loads(line))
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        text = _event_text(cast(dict[str, object], event))
        if text is not None:
            chunks.append(text)

    if not chunks:
        return stdout, None

    text = "".join(chunks)
    return text, parse_phase_payload(text)


class OpenCodeAdapter:
    def __init__(
        self,
        harness: HarnessConfig,
        opencode_bin: str = "opencode",
        policy: SandboxPolicy | None = None,
    ) -> None:
        self._harness = harness
        self._opencode_bin = opencode_bin
        self._policy = policy or SandboxPolicy(enabled=False)

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
            self._opencode_bin,
            "run",
            prompt,
            "-m",
            self.resolve_model(tier),
            "--format",
            "json",
            "--dir",
            str(cwd),
        ]
        if allowed_tools:
            # No --allowedTools equivalent exists; --auto is the closest
            # honest mapping (auto-approve, rather than scope, tool use).
            cmd.append("--auto")

        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        if SANDBOX_NATIVE_CAPABILITY in self.capabilities():
            # dec 9: the harness advertises its own native sandbox, so the
            # driver does not double-wrap — specified-but-unvalidated, no
            # ref harness does this today (see ads/sandbox.py docstring).
            pass
        else:
            sandbox.require(self._policy)  # fail-closed
            env = sandbox.resolve_env(self._policy, os.environ)
            cmd = sandbox.wrap(cmd, cwd, self._policy, env)
            if self._policy.enabled and self._policy.wall_clock_seconds:
                # OS-backstop timeout (dec 8); a true scope-kill is
                # systemd-run's job and the hard-kill -> `killed` outcome
                # mapping stays fog for this slice (dec 6 escalation area).
                timeout_seconds = self._policy.wall_clock_seconds

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(text=str(exc), structured=None, exit_status="error")

        if proc.returncode != 0:
            return RunResult(text=proc.stderr or proc.stdout, structured=None, exit_status="error")

        text, structured = parse_opencode_stdout(proc.stdout)
        return RunResult(text=text, structured=structured, exit_status="ok")

    def sync(self) -> None:
        # TODO(ticket 002 follow-up): project allowed_tools into an
        # opencode.json / agent permission file for fine-grained per-task
        # tool scoping. Not needed for this slice's --auto fallback.
        pass
