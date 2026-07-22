"""Canned adapter for exercising the driver loop without spending tokens.

Keys its canned response off marker strings the phase templates embed in the
composed prompt (e.g. `PHASE:plan`). This is a test/dry-run tool only — real
runs use ClaudeCodeAdapter.
"""

from __future__ import annotations

import json
from pathlib import Path

from ads.adapters.base import RunResult, StructuredPayload
from ads.tasks import TaskTier

STUB_TIER_MODEL: dict[TaskTier, str] = {
    "fast": "stub-fast",
    "standard": "stub-standard",
    "deep": "stub-deep",
}

_PLAN_RESPONSE: StructuredPayload = {
    "spec": "# Spec\n\nBuild the thing the user asked for.\n",
    "design": "# Design\n\nUse a simple layered design.\n",
    "tasks": [
        {
            "filename": "01-implement.md",
            "id": "01-implement",
            "depends_on": [],
            "owns": ["ads/thing.py"],
            "exit_criteria": [{"check": "cmd", "value": "true"}],
            "expert": "python-expert",
            "critical": True,
            "tier": "standard",
            "body": "Implement the thing.",
        },
        {
            "filename": "02-test.md",
            "id": "02-test",
            "depends_on": ["01-implement"],
            "owns": ["tests/test_thing.py"],
            "exit_criteria": [{"check": "cmd", "value": "true"}],
            "expert": "python-expert",
            "critical": True,
            "tier": "standard",
            "body": "Write a test for the thing.",
        },
    ],
}


DEFAULT_STUB_CAPABILITIES: list[str] = ["stub"]


class StubAdapter:
    def __init__(self, capabilities: list[str] | None = None) -> None:
        self._capabilities = (
            list(capabilities) if capabilities is not None else list(DEFAULT_STUB_CAPABILITIES)
        )

    def resolve_model(self, tier: TaskTier) -> str:
        return STUB_TIER_MODEL[tier]

    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
        *,
        activity_log: Path | None = None,
    ) -> RunResult:
        if activity_log is not None:
            activity_log.parent.mkdir(parents=True, exist_ok=True)
            with activity_log.open("a", encoding="utf-8") as fh:
                fh.write("stub: run started\n")
        if "PHASE:plan" in prompt:
            return RunResult(text="stub plan", structured=_PLAN_RESPONSE, exit_status="ok")
        if "PHASE:validate-judgment" in prompt or "PHASE:validate-integration" in prompt:
            judgment_payload: StructuredPayload = {
                "pass": True,
                "evidence": "stub: looks fine",
                "cited_paths": ["stub-evidence.txt"],
            }
            return RunResult(
                text=json.dumps(judgment_payload), structured=judgment_payload, exit_status="ok"
            )
        # dispatch (task execution) call
        dispatch_payload: StructuredPayload = {"status": "done", "summary": "stub: task completed"}
        return RunResult(
            text=json.dumps(dispatch_payload), structured=dispatch_payload, exit_status="ok"
        )

    def sync(self) -> None:
        pass
