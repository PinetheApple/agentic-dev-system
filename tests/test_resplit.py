"""Ticket 005 Rule 5 + 003: the budget ceiling / handoff watcher and the
resumptive re-split it fires, wired into both `ads/dispatch.py` seams.

Uses a real temp git repo (`_run_dispatch`, worktree-isolated path) so the
step-count persistence, re-split, and `ready_batch` pickup are all exercised
against real driver/dispatch plumbing, not a mocked shortcut.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from ads import resplit
from ads.adapters.base import RunResult, StructuredPayload
from ads.config import Config, HarnessConfig, PromptDoc
from ads.driver import _run_dispatch  # pyright: ignore[reportPrivateUsage]
from ads.layout import RunLayout
from ads.resume import scratch_path
from ads.state import State
from ads.task_io import load_tasks, write_task
from ads.tasks import ExitCriterion, Task, TaskTier, ready_batch

STATUS_KEY = "status"
PARTIAL_SCRATCH = (
    "## Objective\n\nDo the thing.\n\n## Done\n\ncrit-1 — a.py:1\n\n## Remaining\n\ncrit-2\n"
)


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    (path / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


class ScriptedStatusAdapter:
    """Test-only adapter: always reports the same caller-configured status,
    never actually done — used to drive a task straight into the ceiling or
    handoff re-split path deterministically."""

    def __init__(self, status: str) -> None:
        self._status = status
        self.call_count = 0

    def resolve_model(self, tier: TaskTier) -> str:
        return "scripted"

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
        self.call_count += 1
        payload = cast(StructuredPayload, {STATUS_KEY: self._status, "summary": "never finishes"})
        return RunResult(text="scripted", structured=payload, exit_status="ok")


def _cfg() -> Config:
    return Config(
        harness=HarnessConfig(
            tier_model={"fast": "x", "standard": "x", "deep": "x"}, run_cmd=[], capabilities=[]
        ),
        base="base principles",
        experts={},
        phases={"dispatch": PromptDoc(meta={}, body="PHASE:dispatch\n\n{task}")},
    )


def _task(
    task_id: str,
    criteria: list[ExitCriterion] | None = None,
    parent: str | None = None,
    owns: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        status="pending",
        depends_on=[],
        owns=owns if owns is not None else ["a.py"],
        exit_criteria=criteria
        if criteria is not None
        else [
            ExitCriterion(check="cmd", value="crit-1"),
            ExitCriterion(check="cmd", value="crit-2"),
        ],
        expert="",
        critical=False,
        tier="standard",
        parent=parent,
        body="Do the flaky thing.",
    )


class ResplitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        _init_git_repo(self.repo)
        self.run_id = f"run-{self.id().split('.')[-1]}"
        self.layout = RunLayout(repo=self.repo, run_id=self.run_id)
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")
        self.cfg = _cfg()

    def _write(self, *tasks: Task) -> State:
        for task in tasks:
            write_task(self.layout, task)
        return State(phase="dispatch", tasks={t.id: t.status for t in tasks})


class TestCeilingBreach(ResplitTestCase):
    def test_breaching_ceiling_splits_parent_and_creates_pending_residual_child(self) -> None:
        task = _task("01-flaky")
        state = self._write(task)
        state.step_counts["01-flaky"] = resplit.STEP_CEILING - 1  # this attempt crosses it
        adapter = ScriptedStatusAdapter(status="pending")

        result = _run_dispatch(self.layout, self.cfg, adapter, state)

        self.assertIsNone(result.gate)  # not a bare halt
        self.assertEqual(result.tasks["01-flaky"], "split")
        self.assertEqual(result.tasks["01-flaky-r1"], "pending")

        on_disk = {t.id: t for t in load_tasks(self.layout)}
        self.assertEqual(on_disk["01-flaky"].status, "split")
        child = on_disk["01-flaky-r1"]
        self.assertEqual(child.status, "pending")
        self.assertEqual(child.parent, "01-flaky")
        self.assertEqual(child.owns, ["a.py"])
        self.assertEqual({c.value for c in child.exit_criteria}, {"crit-1", "crit-2"})

    def test_ready_batch_picks_up_the_residual_child(self) -> None:
        task = _task("01-flaky")
        state = self._write(task)
        state.step_counts["01-flaky"] = resplit.STEP_CEILING - 1
        adapter = ScriptedStatusAdapter(status="pending")

        _run_dispatch(self.layout, self.cfg, adapter, state)

        all_tasks = load_tasks(self.layout)
        batch = ready_batch(all_tasks)
        self.assertEqual([t.id for t in batch], ["01-flaky-r1"])


class TestHandoff(ResplitTestCase):
    def test_handoff_status_splits_before_ceiling(self) -> None:
        task = _task("01-handoff")
        state = self._write(task)  # step_counts empty: nowhere near the ceiling
        adapter = ScriptedStatusAdapter(status="handoff")

        result = _run_dispatch(self.layout, self.cfg, adapter, state)

        self.assertIsNone(result.gate)
        self.assertEqual(result.tasks["01-handoff"], "split")
        self.assertEqual(result.tasks["01-handoff-r1"], "pending")
        self.assertEqual(adapter.call_count, 1)  # fired on the very first attempt


class TestNeverRedoFinishedWork(ResplitTestCase):
    def test_residual_child_excludes_criteria_already_marked_done_in_scratch(self) -> None:
        task = _task("01-partial")
        state = self._write(task)
        state.step_counts["01-partial"] = resplit.STEP_CEILING - 1
        scratch_path(self.layout, "01-partial").write_text(PARTIAL_SCRATCH, encoding="utf-8")
        adapter = ScriptedStatusAdapter(status="pending")

        _run_dispatch(self.layout, self.cfg, adapter, state)

        child = next(t for t in load_tasks(self.layout) if t.id == "01-partial-r1")
        self.assertEqual([c.value for c in child.exit_criteria], ["crit-2"])

    def test_child_scratch_is_seeded_from_parent(self) -> None:
        task = _task("01-partial")
        state = self._write(task)
        state.step_counts["01-partial"] = resplit.STEP_CEILING - 1
        scratch_path(self.layout, "01-partial").write_text(PARTIAL_SCRATCH, encoding="utf-8")
        adapter = ScriptedStatusAdapter(status="pending")

        _run_dispatch(self.layout, self.cfg, adapter, state)

        child_scratch = scratch_path(self.layout, "01-partial-r1").read_text(encoding="utf-8")
        self.assertIn("crit-1 — a.py:1", child_scratch)


class TestDepthCap(ResplitTestCase):
    def test_repeated_splits_eventually_halt_to_a_human(self) -> None:
        # Build a lineage already `resplit.MAX_RESPLIT_DEPTH` ancestors deep
        # (root -> ... -> tip), each prior generation already `split`, so
        # the tip's *next* breach has nowhere left to go and must halt.
        ancestors: list[Task] = []
        parent_id: str | None = None
        for depth in range(resplit.MAX_RESPLIT_DEPTH):
            tid = "root" if depth == 0 else f"gen{depth}"
            ancestor = _task(tid, parent=parent_id)
            ancestor.status = "split"
            ancestors.append(ancestor)
            parent_id = tid

        tip = _task("tip", parent=parent_id)  # lineage_depth(tip) == MAX_RESPLIT_DEPTH
        for t in [*ancestors, tip]:
            write_task(self.layout, t)
        state = State(phase="dispatch", tasks={t.id: t.status for t in [*ancestors, tip]})
        state.step_counts[tip.id] = resplit.STEP_CEILING - 1
        adapter = ScriptedStatusAdapter(status="pending")

        result = _run_dispatch(self.layout, self.cfg, adapter, state)

        self.assertEqual(result.gate, "blocked")
        assert result.halt_reason is not None
        self.assertIn("re-split depth cap", result.halt_reason)


if __name__ == "__main__":
    unittest.main()
