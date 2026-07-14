"""Regression coverage for the `claude -p --output-format json` envelope
parse path: the model's phase-shaped JSON answer lives nested inside the
CLI's own JSON envelope's `result` field, not at the envelope's top level."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from typing import Any

from ads.adapters.claude_code import ClaudeCodeAdapter, parse_claude_stdout
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


if __name__ == "__main__":
    unittest.main()
