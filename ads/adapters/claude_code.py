"""Adapter for the `claude` CLI (Claude Code), headless print mode.

Two invocation shapes, chosen by whether the caller wants live observability:

- **Batch** (`activity_log=None`, the default): `run()` shells out to
  `claude -p <prompt> --model <id> --output-format json` via
  `subprocess.run` exactly as before, and parses the result envelope, then
  the phase-shaped JSON payload nested inside its `result` field (see
  `parse_claude_stdout`).
- **Streaming** (`activity_log` set): `run()` instead uses
  `--output-format stream-json --verbose` via `subprocess.Popen`, reading
  NDJSON events off stdout as they arrive and appending a compact
  human-readable line per event to `activity_log` (see
  `_render_stream_event`). The final `type: "result"` event carries the same
  envelope the batch path parses, so both paths yield an identical
  `RunResult` shape.

Model ids and capability flags come from harness.toml — this file never
hardcodes a model name.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import cast

from ads import sandbox
from ads.adapters._json_envelope import parse_phase_payload
from ads.adapters.base import RunResult, StructuredPayload
from ads.config import HarnessConfig
from ads.sandbox import SANDBOX_NATIVE_CAPABILITY, SandboxPolicy
from ads.tasks import TaskTier

DEFAULT_TIMEOUT_SECONDS = 600

# How long to wait for the process to actually exit / for stderr to finish
# draining once stdout has hit EOF (or once we've killed it on timeout).
# Short: at that point the process is either already dead or dying.
_REAP_TIMEOUT_SECONDS = 10


def _extract_result_envelope(raw: object) -> dict[str, object] | None:
    """`claude -p --output-format json` is documented to return a single
    `{"type": "result", ...}` object, but hook/plugin-heavy sessions have been
    observed to instead emit a JSON array of the full event stream (system,
    assistant, thinking, rate-limit, ... entries) that terminates in the
    `type: "result"` entry. Handle both shapes.

    The streaming path (`--output-format stream-json`) collects its parsed
    NDJSON events into a `list[dict]` and passes it here too — same terminal
    `type: "result"` entry, just already parsed rather than re-decoded from a
    single JSON blob."""
    if isinstance(raw, dict):
        return cast(dict[str, object], raw)
    if isinstance(raw, list):
        for item in reversed(cast(list[object], raw)):
            if isinstance(item, dict) and cast(dict[str, object], item).get("type") == "result":
                return cast(dict[str, object], item)
        return None
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
    return text, parse_phase_payload(text)


# ---------------------------------------------------------------------------
# stream-json event -> compact human trace line
# ---------------------------------------------------------------------------


def _tool_use_line(block: dict[str, object]) -> str:
    name = str(block.get("name", "tool"))
    tool_input = block.get("input")
    arg = ""
    if isinstance(tool_input, dict) and tool_input:
        first_value = next(iter(cast(dict[str, object], tool_input).values()))
        arg = str(first_value)
    return f"→ {name} {arg}".rstrip()


def _tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in cast(list[object], content):
            if isinstance(item, dict) and cast(dict[str, object], item).get("type") == "text":
                parts.append(str(cast(dict[str, object], item).get("text", "")))
        return " ".join(parts)
    return ""


def _tool_result_line(block: dict[str, object]) -> str:
    marker = "✗" if block.get("is_error") else "✓"
    text = _tool_result_text(block.get("content")).strip()
    short = text.splitlines()[0][:120] if text else ""
    return f"  {marker} {short}".rstrip()


def _render_message_content(content: object) -> str | None:
    if not isinstance(content, list):
        return None
    lines: list[str] = []
    for block in cast(list[object], content):
        if not isinstance(block, dict):
            continue
        block_dict = cast(dict[str, object], block)
        block_type = block_dict.get("type")
        if block_type == "text":
            text = str(block_dict.get("text", "")).strip()
            if text:
                lines.append(text)
        elif block_type == "tool_use":
            lines.append(_tool_use_line(block_dict))
        elif block_type == "tool_result":
            lines.append(_tool_result_line(block_dict))
    return "\n".join(lines) if lines else None


def _render_stream_event(event: dict[str, object]) -> str | None:
    """One `claude --output-format stream-json` NDJSON event -> a compact,
    human-readable trace line (or several, newline-joined), or `None` for
    events that carry no user-facing signal (system init, thinking-token
    accounting, rate-limit pings, the terminal `result` envelope itself —
    that's parsed separately, not re-rendered into the trace).

    Handles the two event types the CLI actually emits content in:
    `assistant` (model text + tool_use) and `user` (tool_result, echoed back
    by the CLI as the next turn)."""
    event_type = event.get("type")
    if event_type not in ("assistant", "user"):
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    return _render_message_content(cast(dict[str, object], message).get("content"))


class ClaudeCodeAdapter:
    def __init__(
        self,
        harness: HarnessConfig,
        claude_bin: str = "claude",
        policy: SandboxPolicy | None = None,
    ) -> None:
        self._harness = harness
        self._claude_bin = claude_bin
        self._policy = policy or SandboxPolicy(enabled=False)

    def resolve_model(self, tier: TaskTier) -> str:
        try:
            return self._harness.tier_model[tier]
        except KeyError as exc:
            raise ValueError(f"no model configured for tier {tier!r}") from exc

    def capabilities(self) -> list[str]:
        return list(self._harness.capabilities)

    # -- shared argv/sandbox construction (batch + stream both go through
    # this) -------------------------------------------------------------

    def _build_cmd(
        self,
        prompt: str,
        allowed_tools: list[str] | None,
        tier: TaskTier,
        is_native: bool,
        output_format_args: list[str],
    ) -> list[str]:
        cmd = [
            self._claude_bin,
            "-p",
            prompt,
            "--model",
            self.resolve_model(tier),
            *output_format_args,
        ]

        if is_native:
            # dec 9: the harness advertises its own native sandbox, so the
            # driver does not double-wrap the whole process in bwrap — that
            # host-level boundary is expected to come from an OUTER
            # container/VM around the whole driver instead. What we CAN do
            # here is tighten claude's own tool gating as defense-in-depth.
            # Honesty check (do not oversell): `--permission-mode` /
            # `--disallowedTools` gate tool *invocation* inside claude, not
            # raw filesystem reads of a path — they do not stop a rogue Bash
            # from reading anything the outer container exposes. Closing
            # that residual needs real process separation (outer
            # deny-egress container, or a future API-based adapter that
            # keeps the model call out of a tool-capable jail).
            #
            # Hard invariant, deliberate and tested: this adapter NEVER
            # emits `--dangerously-skip-permissions` /
            # `--allow-dangerously-skip-permissions`. Agents never
            # self-grant a bypass of claude's own permission system (dec
            # 6/dec 9) — that flag must never appear in built argv, in any
            # mode.
            if self._harness.native.permission_mode is not None:
                cmd += ["--permission-mode", self._harness.native.permission_mode]
            if self._harness.native.disallowed_tools:
                # Space-variadic, same shape as --allowedTools below — must
                # not be last, since a trailing variadic would swallow the
                # following --allowedTools flag/tokens.
                cmd += ["--disallowedTools", *self._harness.native.disallowed_tools]

        if allowed_tools:
            # `--allowedTools` is space-variadic (claude 2.1.207): it takes
            # each tool name as its own argv token, not a single
            # comma-joined string — `--allowedTools Read,Edit,Write` parses
            # as ONE (unknown) tool name and silently grants nothing. Must
            # stay last in argv so the variadic doesn't swallow later flags.
            cmd += ["--allowedTools", *allowed_tools]

        return cmd

    def _wrap_for_execution(
        self, cmd: list[str], cwd: Path, is_native: bool
    ) -> tuple[list[str], int]:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        if not is_native:
            sandbox.require(self._policy)  # fail-closed
            env = sandbox.resolve_env(self._policy, os.environ)
            cmd = sandbox.wrap_command(cmd, cwd, self._policy, env)
            if self._policy.enabled and self._policy.wall_clock_seconds:
                # OS-backstop timeout (dec 8); a true scope-kill is
                # systemd-run's job and the hard-kill -> `killed` outcome
                # mapping stays fog for this slice (dec 6 escalation area).
                timeout_seconds = self._policy.wall_clock_seconds
        return cmd, timeout_seconds

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
        *,
        activity_log: Path | None = None,
    ) -> RunResult:
        is_native = SANDBOX_NATIVE_CAPABILITY in self.capabilities()

        if activity_log is None:
            cmd = self._build_cmd(
                prompt, allowed_tools, tier, is_native, ["--output-format", "json"]
            )
            cmd, timeout_seconds = self._wrap_for_execution(cmd, cwd, is_native)
            return self._run_batch(cmd, cwd, timeout_seconds)

        cmd = self._build_cmd(
            prompt, allowed_tools, tier, is_native, ["--output-format", "stream-json", "--verbose"]
        )
        cmd, timeout_seconds = self._wrap_for_execution(cmd, cwd, is_native)
        return self._run_stream(cmd, cwd, timeout_seconds, activity_log)

    def _run_batch(self, cmd: list[str], cwd: Path, timeout_seconds: int) -> RunResult:
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                # Headless: never inherit a terminal stdin. If the CLI ever
                # tries to prompt (trust/onboarding), it must fail fast rather
                # than hang the whole driver waiting on input that never comes.
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(text=str(exc), structured=None, exit_status="error")

        if proc.returncode != 0:
            return RunResult(text=proc.stderr or proc.stdout, structured=None, exit_status="error")

        text, structured = parse_claude_stdout(proc.stdout)
        return RunResult(text=text, structured=structured, exit_status="ok")

    def _run_stream(
        self, cmd: list[str], cwd: Path, timeout_seconds: int, activity_log: Path
    ) -> RunResult:
        """`subprocess.Popen`-based streaming run.

        Wall-clock timeout is enforced by hand (Popen iteration has no
        `timeout=`): a background thread reads stdout continuously (so a
        full pipe can never deadlock the process) while the main thread
        waits on that thread with a bounded `join(timeout_seconds)`. If the
        reader thread is still alive after that, the process is killed and
        an `error` result is returned — no hang, no zombie (the kill+join
        below always reaps). stderr is drained on its own thread for the
        same reason: an unread, full stderr pipe can deadlock a process that
        writes enough to it.
        """
        activity_log.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            return RunResult(text=str(exc), structured=None, exit_status="error")

        events: list[dict[str, object]] = []
        stderr_chunks: list[str] = []

        def _read_stdout() -> None:
            assert proc.stdout is not None
            with activity_log.open("a", encoding="utf-8") as fh:
                for raw_line in proc.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        parsed = cast(object, json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    event = cast(dict[str, object], parsed)
                    events.append(event)
                    rendered = _render_stream_event(event)
                    if rendered is not None:
                        fh.write(rendered + "\n")
                        fh.flush()

        def _read_stderr() -> None:
            assert proc.stderr is not None
            for raw_line in proc.stderr:
                stderr_chunks.append(raw_line)

        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        stdout_thread.join(timeout_seconds)
        if stdout_thread.is_alive():
            proc.kill()
            stdout_thread.join(_REAP_TIMEOUT_SECONDS)
            stderr_thread.join(_REAP_TIMEOUT_SECONDS)
            # kill() was already sent; nothing further to do if it's slow to reap.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_REAP_TIMEOUT_SECONDS)
            return RunResult(
                text=f"timed out after {timeout_seconds}s", structured=None, exit_status="error"
            )

        stderr_thread.join(_REAP_TIMEOUT_SECONDS)
        try:
            returncode = proc.wait(timeout=_REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=_REAP_TIMEOUT_SECONDS)
            return RunResult(
                text="claude process did not exit", structured=None, exit_status="error"
            )

        if returncode != 0:
            return RunResult(
                text="".join(stderr_chunks) or f"claude exited {returncode}",
                structured=None,
                exit_status="error",
            )

        envelope = _extract_result_envelope(cast(object, events))
        if envelope is None:
            return RunResult(text="no result event in stream", structured=None, exit_status="error")
        text = cast(str, envelope.get("result", ""))
        return RunResult(text=text, structured=parse_phase_payload(text), exit_status="ok")

    def sync(self) -> None:
        pass  # no cross-process session state to reconcile for headless -p calls
