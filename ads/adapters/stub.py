"""Canned adapter (ticket 007) exercising the whole driver loop without
spending tokens. Emits the exact JSON text a real adapter would, so the one
driver-side parse path (`ads/phase_json.py`) is exercised token-free.

The execution-role response also performs the trivial file write its canned
task declares in `owns` — real content in the git diff, real git mechanics
exercised by `evaluate_task`/`owns_diff`, without a real harness.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from ads.adapters.base import Role, RunResult

_TASK_ID_RE = re.compile(r"TASK_ID:\s*(\S+)")
_DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/(?:.+)$", re.MULTILINE)

# The 2-task DAG the plan role hands back: disjoint `owns`, each leaf carrying
# >=1 judgment criterion plus a trivial `cmd`.
_PLAN_TASKS: list[dict[str, object]] = [
    {
        "id": "01-implement",
        "depends_on": [],
        "owns": ["src/thing.py"],
        "exit_criteria": [
            {"check": "cmd", "value": "true"},
            {"check": "judgment", "value": "src/thing.py implements the requested feature"},
        ],
        "body": "TASK_ID: 01-implement\n\nImplement thing.",
    },
    {
        "id": "02-test",
        "depends_on": ["01-implement"],
        "owns": ["tests/test_thing.py"],
        "exit_criteria": [
            {"check": "cmd", "value": "true"},
            {"check": "judgment", "value": "tests/test_thing.py tests the implemented feature"},
        ],
        "body": "TASK_ID: 02-test\n\nWrite a test for thing.",
    },
]

_PLAN_PAYLOAD: dict[str, object] = {
    "spec": "# Spec\n\nBuild the thing the user asked for.\n",
    "design": None,
    "tasks": _PLAN_TASKS,
}

# task id -> (owns-relative path, file content) the execution role writes.
_STUB_WORK: dict[str, tuple[str, str]] = {
    "01-implement": ("src/thing.py", "def thing() -> str:\n    return 'thing'\n"),
    "02-test": (
        "tests/test_thing.py",
        "from src.thing import thing\n\n\n"
        "def test_thing() -> None:\n    assert thing() == 'thing'\n",
    ),
}


def _extract_task_id(prompt: str) -> str | None:
    match = _TASK_ID_RE.search(prompt)
    return match.group(1) if match else None


def _paths_from_diff(diff_text: str) -> list[str]:
    return _DIFF_PATH_RE.findall(diff_text)


class StubAdapter:
    """Satisfies the `Adapter` Protocol with canned, token-free responses."""

    def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        role: Role = "execution",
        allowed_tools: list[str] | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> RunResult:
        if on_event is not None:
            on_event(json.dumps({"type": "system", "subtype": "init"}))

        if role == "planning":
            return RunResult(text=json.dumps(_PLAN_PAYLOAD), exit_status="ok")

        if role == "validation":
            cited = _paths_from_diff(prompt)
            verdict = {
                "pass": True,
                "evidence": "stub critic: owns-diff matches the assertion.",
                "cited_paths": cited,
            }
            return RunResult(text=json.dumps(verdict), exit_status="ok")

        task_id = _extract_task_id(prompt)
        work = _STUB_WORK.get(task_id or "")
        if work is not None:
            relpath, content = work
            path = cwd / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        handoff = {
            "task_id": task_id or "",
            "status": "complete",
            "commands": [{"cmd": "true", "exit": 0}],
            "undone": [],
            "issues": [],
        }
        if on_event is not None:
            on_event(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.0}))
        return RunResult(text=json.dumps(handoff), exit_status="ok")

    def resolve_model(self, role: Role) -> str:
        return f"stub-{role}"
