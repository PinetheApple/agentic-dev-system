"""Regression coverage for the `claude -p --output-format json` envelope
parse path: the model's phase-shaped JSON answer lives nested inside the
CLI's own JSON envelope's `result` field, not at the envelope's top level."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, ClassVar

from ads.adapters import claude_code
from ads.adapters.claude_code import (
    ClaudeCodeAdapter,
    _render_stream_event,  # pyright: ignore[reportPrivateUsage]
    parse_claude_stdout,
)
from ads.config import HarnessConfig, NativeConfig
from ads.sandbox import SandboxPolicy


class TestParseClaudeStdout(unittest.TestCase):
    def test_dict_envelope_result_field_parses_into_structured(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "result": '{"status": "done", "summary": "did the thing"}',
                "total_cost_usd": 0.01,
                "session_id": "abc123",
            }
        )

        text, structured = parse_claude_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertEqual(structured.get("status"), "done")
        self.assertEqual(structured.get("summary"), "did the thing")
        self.assertEqual(text, '{"status": "done", "summary": "did the thing"}')

    def test_result_wrapped_in_markdown_fence_still_parses(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "result": '```json\n{"pass": true, "notes": "looks fine"}\n```',
            }
        )

        _, structured = parse_claude_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertIs(structured.get("pass"), True)

    def test_array_of_stream_events_extracts_trailing_result_entry(self) -> None:
        """Observed live: hook/plugin-heavy sessions emit a JSON array of the
        full event stream instead of a single result object; the terminal
        `type: "result"` entry still carries the model's answer."""
        stdout = json.dumps(
            [
                {"type": "system", "subtype": "init"},
                {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 5},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}},
                {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
                {
                    "type": "result",
                    "subtype": "success",
                    "result": '{"status": "blocked", "summary": "missing dependency"}',
                    "total_cost_usd": 0.02,
                },
            ]
        )

        _, structured = parse_claude_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertEqual(structured.get("status"), "blocked")

    def test_non_json_result_text_yields_no_structured_payload(self) -> None:
        stdout = json.dumps({"type": "result", "result": "I could not comply with that request."})

        text, structured = parse_claude_stdout(stdout)

        self.assertIsNone(structured)
        self.assertEqual(text, "I could not comply with that request.")

    def test_unparseable_stdout_falls_back_to_raw_text(self) -> None:
        stdout = "not json at all"

        text, structured = parse_claude_stdout(stdout)

        self.assertIsNone(structured)
        self.assertEqual(text, stdout)

    def test_array_with_no_result_entry_yields_no_structured_payload(self) -> None:
        stdout = json.dumps([{"type": "system", "subtype": "init"}])

        text, structured = parse_claude_stdout(stdout)

        self.assertIsNone(structured)
        self.assertEqual(text, stdout)


class TestRunAllowedToolsArgv(unittest.TestCase):
    """`--allowedTools` is space-variadic in the real `claude` CLI: each tool
    must be its own argv token, not a comma-joined string, or claude parses
    it as one unknown tool name and grants nothing."""

    def test_allowed_tools_are_separate_trailing_argv_tokens(self) -> None:
        captured: dict[str, list[str]] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"type": "result", "result": "{}"}), stderr=""
            )

        harness = HarnessConfig(
            tier_model={"standard": "claude-sonnet-5"},
            run_cmd=["claude", "-p"],
            capabilities=["tools", "allowedtools-cli"],
        )
        adapter = ClaudeCodeAdapter(harness, policy=SandboxPolicy(enabled=False))

        real_run = subprocess.run
        subprocess.run = fake_run  # type: ignore[assignment]
        try:
            adapter.run(
                "do the thing",
                cwd=Path(),
                allowed_tools=["Read", "Edit", "Write"],
                tier="standard",
            )
        finally:
            subprocess.run = real_run  # type: ignore[assignment]

        cmd = captured["cmd"]
        self.assertEqual(cmd[-4:], ["--allowedTools", "Read", "Edit", "Write"])


def _run_and_capture(adapter: ClaudeCodeAdapter, **run_kwargs: Any) -> list[str]:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=json.dumps({"type": "result", "result": "{}"}), stderr=""
        )

    real_run = subprocess.run
    subprocess.run = fake_run  # type: ignore[assignment]
    try:
        adapter.run("do the thing", cwd=Path(), tier="standard", **run_kwargs)
    finally:
        subprocess.run = real_run  # type: ignore[assignment]
    return captured["cmd"]


class TestRunNativePosture(unittest.TestCase):
    """dec 9: `sandbox-native` capability + `[native]` knobs inject
    least-authority claude flags, but never a permissions bypass."""

    def test_native_mode_emits_permission_mode_and_disallowed_tools(self) -> None:
        harness = HarnessConfig(
            tier_model={"standard": "claude-sonnet-5"},
            run_cmd=["claude", "-p"],
            capabilities=["tools", "allowedtools-cli", "sandbox-native"],
            native=NativeConfig(
                permission_mode="acceptEdits",
                disallowed_tools=("WebFetch", "WebSearch"),
            ),
        )
        adapter = ClaudeCodeAdapter(harness, policy=SandboxPolicy(enabled=False))

        cmd = _run_and_capture(adapter, allowed_tools=["Read", "Edit"])

        self.assertIn("--permission-mode", cmd)
        self.assertEqual(
            cmd[cmd.index("--permission-mode") : cmd.index("--permission-mode") + 2],
            ["--permission-mode", "acceptEdits"],
        )
        disallowed_idx = cmd.index("--disallowedTools")
        self.assertEqual(
            cmd[disallowed_idx : disallowed_idx + 3],
            ["--disallowedTools", "WebFetch", "WebSearch"],
        )
        # --allowedTools stays last, unaffected by the native flags.
        self.assertEqual(cmd[-3:], ["--allowedTools", "Read", "Edit"])
        bypass_flags = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")
        for bypass_flag in bypass_flags:
            self.assertNotIn(bypass_flag, cmd)

    def test_non_native_mode_omits_native_flags(self) -> None:
        harness = HarnessConfig(
            tier_model={"standard": "claude-sonnet-5"},
            run_cmd=["claude", "-p"],
            capabilities=["tools", "allowedtools-cli"],
            native=NativeConfig(permission_mode="acceptEdits", disallowed_tools=("WebFetch",)),
        )
        adapter = ClaudeCodeAdapter(harness, policy=SandboxPolicy(enabled=False))

        cmd = _run_and_capture(adapter, allowed_tools=["Read"])

        self.assertNotIn("--permission-mode", cmd)
        self.assertNotIn("--disallowedTools", cmd)
        self.assertEqual(cmd[-2:], ["--allowedTools", "Read"])
        bypass_flags = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")
        for bypass_flag in bypass_flags:
            self.assertNotIn(bypass_flag, cmd)


class TestRenderStreamEvent(unittest.TestCase):
    def test_assistant_text_delta_renders_the_text(self) -> None:
        event: dict[str, object] = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Reading the file..."}]},
        }
        self.assertEqual(_render_stream_event(event), "Reading the file...")

    def test_tool_use_renders_arrow_line_with_tool_name_and_first_arg(self) -> None:
        event: dict[str, object] = {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]
            },
        }
        self.assertEqual(_render_stream_event(event), "→ Read a.py")

    def test_tool_result_renders_check_mark_line(self) -> None:
        event: dict[str, object] = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": "file contents ok", "is_error": False}
                ],
            },
        }
        self.assertEqual(_render_stream_event(event), "  ✓ file contents ok")

    def test_tool_result_error_renders_cross_mark_line(self) -> None:
        event: dict[str, object] = {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "boom", "is_error": True}]},
        }
        self.assertEqual(_render_stream_event(event), "  ✗ boom")

    def test_system_init_event_is_noise_and_yields_none(self) -> None:
        event: dict[str, object] = {"type": "system", "subtype": "init"}
        self.assertIsNone(_render_stream_event(event))

    def test_result_event_is_not_re_rendered(self) -> None:
        event: dict[str, object] = {"type": "result", "result": "{}"}
        self.assertIsNone(_render_stream_event(event))

    def test_unknown_event_type_yields_none(self) -> None:
        event: dict[str, object] = {"type": "rate_limit_event"}
        self.assertIsNone(_render_stream_event(event))


class _ScriptedStdout:
    """Iterable of NDJSON lines, mimicking `Popen.stdout` iterated line by
    line as a real process would produce them."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._lines = [json.dumps(e) + "\n" for e in events]

    def __iter__(self) -> Any:
        return iter(self._lines)


class _EmptyStdout:
    def __iter__(self) -> Any:
        return iter(())


class _HangingStdout:
    """Never yields — simulates a process that produces no output and never
    exits, to exercise the wall-clock timeout path deterministically."""

    def __iter__(self) -> Any:
        while True:
            time.sleep(0.01)


class _FakeStreamPopen:
    """Stand-in for `subprocess.Popen` used by the streaming path. Records
    the argv it was built with and hands back scripted stdout/stderr."""

    last_cmd: ClassVar[list[str] | None] = None
    events: ClassVar[list[dict[str, Any]]] = []

    def __init__(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        text: bool | None = None,
        stdin: int | None = None,
    ) -> None:
        _FakeStreamPopen.last_cmd = cmd
        self.stdout: Any = _ScriptedStdout(_FakeStreamPopen.events)
        self.stderr: Any = _EmptyStdout()
        self.killed = False
        self._returncode = 0

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode


class _FakeHangingPopen:
    """Stand-in for `subprocess.Popen` whose stdout never produces a line
    and never exits — exercises the timeout-kills-and-returns-error path."""

    def __init__(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        text: bool | None = None,
        stdin: int | None = None,
    ) -> None:
        self.stdout: Any = _HangingStdout()
        self.stderr: Any = _EmptyStdout()
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return -9


def _harness() -> HarnessConfig:
    return HarnessConfig(
        tier_model={"standard": "claude-sonnet-5"},
        run_cmd=["claude", "-p"],
        capabilities=["tools"],
    )


class TestClaudeStreamingRun(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.activity_log = Path(self._tmp.name) / "activity" / "01-a.log"

    def test_streaming_run_writes_rendered_lines_and_parses_final_result(self) -> None:
        _FakeStreamPopen.events = [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Reading the file..."}]},
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "file contents ok", "is_error": False}
                    ]
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "result": json.dumps({"status": "done", "summary": "did it"}),
            },
        ]
        adapter = ClaudeCodeAdapter(_harness(), policy=SandboxPolicy(enabled=False))

        real_popen = subprocess.Popen
        claude_code.subprocess.Popen = _FakeStreamPopen  # type: ignore[assignment]
        try:
            result = adapter.run(
                "do the thing",
                cwd=Path(self._tmp.name),
                tier="standard",
                activity_log=self.activity_log,
            )
        finally:
            claude_code.subprocess.Popen = real_popen  # type: ignore[assignment]

        assert _FakeStreamPopen.last_cmd is not None
        self.assertIn("stream-json", _FakeStreamPopen.last_cmd)
        self.assertIn("--verbose", _FakeStreamPopen.last_cmd)

        self.assertEqual(result.exit_status, "ok")
        self.assertEqual(result.structured, {"status": "done", "summary": "did it"})

        log_contents = self.activity_log.read_text(encoding="utf-8")
        self.assertEqual(
            log_contents,
            "Reading the file...\n→ Read a.py\n  ✓ file contents ok\n",
        )

    def test_batch_path_unaffected_when_activity_log_is_none(self) -> None:
        """`activity_log=None` must still take the old `--output-format
        json` + `subprocess.run` path, never `Popen`."""
        adapter = ClaudeCodeAdapter(_harness(), policy=SandboxPolicy(enabled=False))
        captured: dict[str, list[str]] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"type": "result", "result": "{}"}), stderr=""
            )

        real_run = subprocess.run
        subprocess.run = fake_run  # type: ignore[assignment]
        try:
            adapter.run("do the thing", cwd=Path(self._tmp.name), tier="standard")
        finally:
            subprocess.run = real_run  # type: ignore[assignment]

        self.assertIn("--output-format", captured["cmd"])
        self.assertIn("json", captured["cmd"])
        self.assertNotIn("stream-json", captured["cmd"])

    def test_stream_timeout_kills_process_and_returns_error_without_hanging(self) -> None:
        adapter = ClaudeCodeAdapter(_harness(), policy=SandboxPolicy(enabled=False))

        real_popen = subprocess.Popen
        real_timeout = claude_code.DEFAULT_TIMEOUT_SECONDS
        real_reap_timeout = claude_code._REAP_TIMEOUT_SECONDS  # pyright: ignore[reportPrivateUsage]
        claude_code.subprocess.Popen = _FakeHangingPopen  # type: ignore[assignment]
        claude_code.DEFAULT_TIMEOUT_SECONDS = 0.05  # type: ignore[assignment]
        claude_code._REAP_TIMEOUT_SECONDS = 0.05  # type: ignore[assignment] # pyright: ignore[reportPrivateUsage]
        start = time.monotonic()
        try:
            result = adapter.run(
                "do the thing",
                cwd=Path(self._tmp.name),
                tier="standard",
                activity_log=self.activity_log,
            )
        finally:
            claude_code.subprocess.Popen = real_popen  # type: ignore[assignment]
            claude_code.DEFAULT_TIMEOUT_SECONDS = real_timeout  # type: ignore[assignment]
            claude_code._REAP_TIMEOUT_SECONDS = real_reap_timeout  # type: ignore[assignment] # pyright: ignore[reportPrivateUsage]
        elapsed = time.monotonic() - start

        self.assertEqual(result.exit_status, "error")
        self.assertLess(elapsed, 5)  # deterministic, fast — no hang


if __name__ == "__main__":
    unittest.main()
