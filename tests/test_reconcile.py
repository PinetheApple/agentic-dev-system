"""Ticket 006 follow-up: the auto-reconcile loop (`ads/reconcile.py`).

Uses a real temp git repo + real `ads.worktree` helpers (no live LLM) so the
opt-in gate, the re-commit/re-merge retry, and exhaustion are all exercised
against real `git worktree`/`git merge` behavior — mirroring
tests/test_dispatch_worktree.py's fixture shape.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from ads import reconcile, worktree
from ads.adapters.base import RunResult
from ads.config import Config, HarnessConfig, PromptDoc
from ads.driver import _run_dispatch  # pyright: ignore[reportPrivateUsage]
from ads.layout import RunLayout
from ads.state import State
from ads.task_io import write_task
from ads.tasks import ExitCriterion, Task, TaskTier


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    (path / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


class _FixingAdapter:
    """Deletes the offending out-of-bounds file in `cwd`, resolving the
    write-set-audit violation for real on disk."""

    def __init__(self, offending_relpath: str) -> None:
        self.offending_relpath = offending_relpath
        self.calls: list[Path] = []

    def resolve_model(self, tier: TaskTier) -> str:
        return "fixing-stub"

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
        self.calls.append(cwd)
        (cwd / self.offending_relpath).unlink(missing_ok=True)
        return RunResult(
            text='{"status": "done"}', structured={"status": "done"}, exit_status="ok"
        )


class _NoOpAdapter:
    """Runs 'successfully' but never touches the worktree — the violation
    never clears, so every retry keeps failing."""

    def __init__(self) -> None:
        self.calls = 0

    def resolve_model(self, tier: TaskTier) -> str:
        return "noop-stub"

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
        self.calls += 1
        return RunResult(
            text='{"status": "done"}', structured={"status": "done"}, exit_status="ok"
        )


class _NeverCalledAdapter:
    def resolve_model(self, tier: TaskTier) -> str:
        raise AssertionError("adapter.run must not be called when reconcile is not configured")

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
        raise AssertionError("adapter.run must not be called when reconcile is not configured")


def _cfg(with_reconcile: bool) -> Config:
    phases: dict[str, PromptDoc] = {"dispatch": PromptDoc(meta={}, body="PHASE:dispatch\n\n{task}")}
    experts: dict[str, PromptDoc] = {}
    if with_reconcile:
        phases["reconcile"] = PromptDoc(
            meta={},
            body=(
                "PHASE:reconcile\n\nviolation={violation} owns={owns} "
                "uncovered={uncovered} diff={diff} merge_output={merge_output}"
            ),
        )
        experts["reconcile"] = PromptDoc(
            meta={}, body="fix the violation", tools=("Read", "Edit", "Write")
        )
    return Config(
        harness=HarnessConfig(
            tier_model={"fast": "x", "standard": "x", "deep": "x"}, run_cmd=[], capabilities=[]
        ),
        base="base principles",
        experts=experts,
        phases=phases,
    )


def _task(owns: list[str]) -> Task:
    return Task(
        id="01-rogue",
        status="pending",
        depends_on=[],
        owns=owns,
        exit_criteria=[ExitCriterion(check="cmd", value="true")],
        expert="",
        critical=False,
        tier="standard",
        body="Do the thing.",
    )


class TestReconcile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        _init_git_repo(self.repo)
        self.run_id = f"run-{self.id().split('.')[-1]}"
        self.layout = RunLayout(repo=self.repo, run_id=self.run_id)
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")
        self.git_lock = threading.Lock()

    def _make_out_of_bounds_worktree(
        self, task: Task
    ) -> tuple[worktree.TaskWorktree, worktree.MergeOutcome]:
        base_sha = worktree.head_sha(self.repo)
        wt = worktree.create_worktree(self.repo, base_sha, self.run_id, task.id)
        (wt.path / "b.py").write_text("outside owns\n", encoding="utf-8")
        worktree.commit_all(wt, "initial (out of bounds)")
        outcome = worktree.merge_task_branch(self.repo, wt, task.owns)
        self.assertFalse(outcome.merged)
        self.assertEqual(outcome.violation, worktree.MERGE_VIOLATION_OUT_OF_BOUNDS)
        return wt, outcome

    def test_opt_in_off_returns_same_outcome_and_never_runs_agent(self) -> None:
        task = _task(owns=["a.py"])
        wt, outcome = self._make_out_of_bounds_worktree(task)
        cfg = _cfg(with_reconcile=False)

        result = reconcile.attempt(
            self.layout, cfg, _NeverCalledAdapter(), task, wt, outcome, self.git_lock
        )

        self.assertIs(result, outcome)

    def test_success_recommits_and_remerges(self) -> None:
        task = _task(owns=["a.py"])
        wt, outcome = self._make_out_of_bounds_worktree(task)
        cfg = _cfg(with_reconcile=True)
        adapter = _FixingAdapter("b.py")

        result = reconcile.attempt(self.layout, cfg, adapter, task, wt, outcome, self.git_lock)

        self.assertTrue(result.merged)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0], wt.path)
        self.assertFalse((self.repo / "b.py").exists())

        events = self.layout.events.read_text(encoding="utf-8").splitlines()
        kinds = [json.loads(line)["kind"] for line in events]
        self.assertIn("reconcile_success", kinds)

    def test_exhaustion_after_max_attempts_leaves_outcome_unmerged(self) -> None:
        task = _task(owns=["a.py"])
        wt, outcome = self._make_out_of_bounds_worktree(task)
        cfg = _cfg(with_reconcile=True)
        adapter = _NoOpAdapter()

        result = reconcile.attempt(self.layout, cfg, adapter, task, wt, outcome, self.git_lock)

        self.assertFalse(result.merged)
        self.assertEqual(adapter.calls, reconcile.RECONCILE_MAX_ATTEMPTS)

        events = self.layout.events.read_text(encoding="utf-8").splitlines()
        kinds = [json.loads(line)["kind"] for line in events]
        self.assertIn("reconcile_exhausted", kinds)


class _DualPhaseAdapter:
    """A dispatch-level fake: writes an out-of-bounds file on the `dispatch`
    phase, then deletes it on the `reconcile` phase — proving the full
    `_dispatch_one_isolated` -> `reconcile.attempt` wiring re-merges to a
    clean, `done` task with no gate."""

    def __init__(self, offending_relpath: str, fix: bool) -> None:
        self.offending_relpath = offending_relpath
        self.fix = fix
        self.calls: list[str] = []

    def resolve_model(self, tier: TaskTier) -> str:
        return "dual-stub"

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
        if "PHASE:reconcile" in prompt:
            self.calls.append("reconcile")
            if self.fix:
                (cwd / self.offending_relpath).unlink(missing_ok=True)
        else:
            self.calls.append("dispatch")
            (cwd / self.offending_relpath).write_text("outside owns\n", encoding="utf-8")
        return RunResult(
            text='{"status": "done", "summary": "ok"}',
            structured={"status": "done", "summary": "ok"},
            exit_status="ok",
        )


class TestReconcileDispatchWiring(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        _init_git_repo(self.repo)
        self.run_id = f"run-{self.id().split('.')[-1]}"
        self.layout = RunLayout(repo=self.repo, run_id=self.run_id)
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")

    def _write_task(self, task: Task) -> State:
        write_task(self.layout, task)
        return State(phase="dispatch", tasks={task.id: task.status})

    def test_dispatch_reconciles_and_completes_when_configured(self) -> None:
        task = _task(owns=["a.py"])
        state = self._write_task(task)
        adapter = _DualPhaseAdapter("b.py", fix=True)

        result_state = _run_dispatch(self.layout, _cfg(with_reconcile=True), adapter, state)

        self.assertIsNone(result_state.gate)
        self.assertEqual(result_state.tasks["01-rogue"], "done")
        self.assertFalse((self.repo / "b.py").exists())
        self.assertEqual(adapter.calls, ["dispatch", "reconcile"])

    def test_dispatch_halts_to_reconcile_gate_when_not_configured(self) -> None:
        task = _task(owns=["a.py"])
        state = self._write_task(task)
        adapter = _DualPhaseAdapter("b.py", fix=True)

        result_state = _run_dispatch(self.layout, _cfg(with_reconcile=False), adapter, state)

        self.assertEqual(result_state.gate, "reconcile")
        self.assertEqual(adapter.calls, ["dispatch"])  # reconcile never invoked


if __name__ == "__main__":
    unittest.main()
