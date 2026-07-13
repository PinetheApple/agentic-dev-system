"""Ticket 005 Rule 1: `run_firewalled` returns summary + cited paths only,
never the raw transcript."""

from __future__ import annotations

import unittest
from pathlib import Path

from ads.adapters.base import RunResult, StructuredPayload
from ads.firewall import run_firewalled
from ads.tasks import TaskTier

RAW_TRANSCRIPT_MARKER = "RAW TRANSCRIPT: read 40 files, ran 12 commands, do not surface this"


class StubFirewalledAdapter:
    def __init__(self, structured: StructuredPayload | None) -> None:
        self._structured = structured
        self.calls: list[tuple[str, Path, list[str] | None, TaskTier]] = []

    def resolve_model(self, tier: TaskTier) -> str:
        return "stub"

    def capabilities(self) -> list[str]:
        return []

    def sync(self) -> None:
        pass

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
    ) -> RunResult:
        self.calls.append((prompt, cwd, allowed_tools, tier))
        return RunResult(text=RAW_TRANSCRIPT_MARKER, structured=self._structured, exit_status="ok")


class TestRunFirewalled(unittest.TestCase):
    def test_returns_summary_and_cited_paths_from_structured_payload(self) -> None:
        payload: StructuredPayload = {
            "summary": "distilled answer",
            "cited_paths": ["ads/foo.py", "ads/bar.py"],
        }
        adapter = StubFirewalledAdapter(payload)

        result = run_firewalled(adapter, "research this", cwd=Path("/tmp"))

        self.assertEqual(result.summary, "distilled answer")
        self.assertEqual(result.cited_paths, ["ads/foo.py", "ads/bar.py"])

    def test_does_not_leak_raw_transcript(self) -> None:
        payload: StructuredPayload = {"summary": "distilled answer", "cited_paths": []}
        adapter = StubFirewalledAdapter(payload)

        result = run_firewalled(adapter, "research this", cwd=Path("/tmp"))

        self.assertNotIn(RAW_TRANSCRIPT_MARKER, result.summary)
        self.assertNotIn(RAW_TRANSCRIPT_MARKER, result.cited_paths)
        self.assertFalse(hasattr(result, "text"))  # structurally: no transcript field at all

    def test_falls_back_to_raw_text_only_when_no_structured_summary(self) -> None:
        adapter = StubFirewalledAdapter(None)

        result = run_firewalled(adapter, "research this", cwd=Path("/tmp"))

        self.assertEqual(result.summary, RAW_TRANSCRIPT_MARKER)
        self.assertEqual(result.cited_paths, [])

    def test_forwards_prompt_cwd_tools_and_tier_to_the_adapter(self) -> None:
        adapter = StubFirewalledAdapter({"summary": "ok", "cited_paths": []})

        run_firewalled(
            adapter, "prompt text", cwd=Path("/tmp/x"), allowed_tools=["Read"], tier="fast"
        )

        [(prompt, cwd, tools, tier)] = adapter.calls
        self.assertEqual(prompt, "prompt text")
        self.assertEqual(cwd, Path("/tmp/x"))
        self.assertEqual(tools, ["Read"])
        self.assertEqual(tier, "fast")


if __name__ == "__main__":
    unittest.main()
