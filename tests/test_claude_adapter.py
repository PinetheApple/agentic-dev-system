"""Ticket 009: `ClaudeCodeAdapter` owns only the transport envelope — feed a
canned `stream-json` transcript through a monkeypatched subprocess and assert
the result-event extraction, event forwarding, and error path."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ads.adapters.claude import ClaudeCodeAdapter

_TRANSCRIPT: list[dict[str, Any]] = [
    {"type": "system", "subtype": "init"},
    {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "working on it"}], "usage": {}},
    },
    {"type": "result", "subtype": "success", "result": "final answer text", "total_cost_usd": 0.01},
]


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self) -> Any:
        return iter(self._lines)


class _FakeStdin:
    def __init__(self) -> None:
        self.written = ""
        self.closed = False

    def write(self, text: str) -> None:
        self.written += text

    def close(self) -> None:
        self.closed = True


class _FakePopen:
    def __init__(self, lines: list[str], returncode: int) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStdout([])
        self._returncode = returncode

    def wait(self) -> int:
        return self._returncode


def _transcript_lines() -> list[str]:
    return [json.dumps(event) + "\n" for event in _TRANSCRIPT]


class ClaudeCodeAdapterTest(unittest.TestCase):
    def test_ok_run_extracts_result_and_forwards_events(self) -> None:
        seen: list[str] = []
        with patch(
            "ads.adapters.claude.subprocess.Popen",
            return_value=_FakePopen(_transcript_lines(), returncode=0),
        ):
            adapter = ClaudeCodeAdapter()
            result = adapter.run(
                "prompt text", Path("/nonexistent"), role="execution", on_event=seen.append
            )
        self.assertEqual(result.exit_status, "ok")
        self.assertEqual(result.text, "final answer text")
        self.assertEqual(len(seen), len(_TRANSCRIPT))
        for line, event in zip(seen, _TRANSCRIPT, strict=True):
            self.assertEqual(json.loads(line), event)

    def test_nonzero_exit_is_error(self) -> None:
        with patch(
            "ads.adapters.claude.subprocess.Popen",
            return_value=_FakePopen(_transcript_lines(), returncode=1),
        ):
            adapter = ClaudeCodeAdapter()
            result = adapter.run("prompt text", Path("/nonexistent"), role="execution")
        self.assertEqual(result.exit_status, "error")

    def test_no_result_event_is_error(self) -> None:
        lines = [json.dumps({"type": "system", "subtype": "init"}) + "\n"]
        with patch(
            "ads.adapters.claude.subprocess.Popen",
            return_value=_FakePopen(lines, returncode=0),
        ):
            adapter = ClaudeCodeAdapter()
            result = adapter.run("prompt text", Path("/nonexistent"), role="execution")
        self.assertEqual(result.exit_status, "error")

    def test_malformed_line_is_forwarded_but_skipped_for_result(self) -> None:
        lines = ["not json\n", *_transcript_lines()]
        seen: list[str] = []
        with patch(
            "ads.adapters.claude.subprocess.Popen",
            return_value=_FakePopen(lines, returncode=0),
        ):
            adapter = ClaudeCodeAdapter()
            result = adapter.run(
                "prompt text", Path("/nonexistent"), role="execution", on_event=seen.append
            )
        self.assertEqual(result.text, "final answer text")
        self.assertIn("not json", seen)

    def test_resolve_model_maps_role_to_tier(self) -> None:
        adapter = ClaudeCodeAdapter()
        self.assertEqual(adapter.resolve_model("planning"), "claude-opus-4-8")
        self.assertEqual(adapter.resolve_model("execution"), "claude-sonnet-5")
        self.assertEqual(adapter.resolve_model("validation"), "claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
