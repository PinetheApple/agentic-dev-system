"""Ticket 006: git-worktree-isolated (+ optionally parallel) dispatch.

Uses a real temp git repo (no live LLM) so the write-set audit, merge-back,
and both brakes (git floor, capability floor, critical serialization) are
exercised against real `git worktree`/`git merge` behavior.

`TestPreMergeGate` covers the 006+007 integration: a task never merges
dirty — its own `cmd`/`judgment` exit criteria now gate the merge itself,
evaluated pre-merge in its own worktree (`ads/dispatch.py`'s
`_gate_and_route`), routing a failure through the same ceiling/resplit
machinery a blocked/handoff task uses rather than a bare retry counter.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ads import resplit
from ads.adapters.base import RunResult
from ads.config import Config, HarnessConfig, PromptDoc
from ads.driver import _run_dispatch  # pyright: ignore[reportPrivateUsage]
from ads.layout import RunLayout
from ads.state import State
from ads.task_io import load_tasks, write_task
from ads.tasks import ExitCriterion, Task, TaskTier

WORKTREES_ROOT = Path(tempfile.gettempdir()) / "ads-worktrees"


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    (path / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


class WritingAdapter:
    """Test-only adapter: writes a file under `cwd` per the `WRITE_FILE:`
    marker embedded in the task body, and tracks concurrent-call
    high-water-mark deterministically (lock-guarded counter + a short sleep
    to open an overlap window — not a wall-clock speed assertion)."""

    def __init__(self, capabilities: list[str] | None = None, hold_seconds: float = 0.05) -> None:
        self._capabilities = list(capabilities) if capabilities is not None else []
        self._hold_seconds = hold_seconds
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.calls: list[Path] = []

    def resolve_model(self, tier: TaskTier) -> str:
        return "writing-stub"

    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def sync(self) -> None:
        pass

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
    ) -> RunResult:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        self.calls.append(cwd)
        time.sleep(self._hold_seconds)

        match = re.search(r"WRITE_FILE: (\S+)", prompt)
        if match:
            target = cwd / match.group(1)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("stub output\n", encoding="utf-8")

        with self._lock:
            self._active -= 1
        return RunResult(
            text='{"status": "done", "summary": "wrote file"}',
            structured={"status": "done", "summary": "wrote file"},
            exit_status="ok",
        )


def _cfg(capabilities: list[str], max_parallel: int = 4) -> Config:
    return Config(
        harness=HarnessConfig(
            tier_model={"fast": "x", "standard": "x", "deep": "x"},
            run_cmd=[],
            capabilities=capabilities,
            max_parallel=max_parallel,
        ),
        base="base principles",
        experts={},
        phases={"dispatch": PromptDoc(meta={}, body="PHASE:dispatch\n\n{task}")},
    )


def _task(
    task_id: str,
    owns: list[str],
    write_file: str,
    critical: bool = False,
    exit_cmd: str = "true",
) -> Task:
    return Task(
        id=task_id,
        status="pending",
        depends_on=[],
        owns=owns,
        exit_criteria=[ExitCriterion(check="cmd", value=exit_cmd)],
        expert="",
        critical=critical,
        tier="standard",
        body=f"Do the thing.\n\nWRITE_FILE: {write_file}\n",
    )


class TestDispatchWorktreeIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.run_id = f"run-{self.id().split('.')[-1]}"
        self.layout = RunLayout(repo=self.repo, run_id=self.run_id)
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")
        self.addCleanup(
            shutil.rmtree, WORKTREES_ROOT / self.run_id, True
        )  # ignore_errors: reconcile tests leave worktrees behind on purpose

    def _write_tasks(self, tasks: list[Task]) -> State:
        for task in tasks:
            write_task(self.layout, task)
        return State(phase="dispatch", tasks={t.id: t.status for t in tasks})

    def _worktree_dirs(self, task_id: str) -> list[Path]:
        root = WORKTREES_ROOT / self.run_id
        return list(root.glob(f"{task_id}-*")) if root.exists() else []

    # -- happy path: clean tasks merge, worktrees are cleaned up ------------

    def test_clean_tasks_merge_into_integration_branch(self) -> None:
        _init_git_repo(self.repo)
        tasks = [
            _task("01-a", owns=["a.py"], write_file="a.py"),
            _task("02-b", owns=["b.py"], write_file="b.py"),
        ]
        state = self._write_tasks(tasks)
        adapter = WritingAdapter(capabilities=[])  # no `parallel` -> still isolated, sequential

        result_state = _run_dispatch(self.layout, _cfg(capabilities=[]), adapter, state)

        self.assertIsNone(result_state.gate)
        self.assertEqual(result_state.tasks["01-a"], "done")
        self.assertEqual(result_state.tasks["02-b"], "done")
        self.assertTrue((self.repo / "a.py").exists())
        self.assertTrue((self.repo / "b.py").exists())
        self.assertEqual(self._worktree_dirs("01-a"), [])
        self.assertEqual(self._worktree_dirs("02-b"), [])

        log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--oneline"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("01-a", log.stdout)
        self.assertIn("02-b", log.stdout)

    # -- write-set audit tripwire --------------------------------------------

    def test_out_of_bounds_write_halts_to_reconcile_gate(self) -> None:
        _init_git_repo(self.repo)
        tasks = [_task("01-rogue", owns=["a.py"], write_file="b.py")]  # writes outside owns
        state = self._write_tasks(tasks)
        adapter = WritingAdapter(capabilities=[])

        result_state = _run_dispatch(self.layout, _cfg(capabilities=[]), adapter, state)

        self.assertEqual(result_state.gate, "reconcile")
        assert result_state.halt_reason is not None
        self.assertIn("01-rogue", result_state.halt_reason)
        self.assertIn("out_of_bounds", result_state.halt_reason)
        self.assertFalse((self.repo / "b.py").exists())  # never merged

        scratch = self.layout.scratch_dir / "01-rogue.reconcile.md"
        self.assertTrue(scratch.exists())
        self.assertIn("out_of_bounds", scratch.read_text(encoding="utf-8"))

        leftover = self._worktree_dirs("01-rogue")
        self.assertEqual(len(leftover), 1)  # worktree preserved for inspection
        self.assertTrue((leftover[0] / "b.py").exists())

    # -- git floor fallback ---------------------------------------------------

    def test_non_git_target_falls_back_to_sequential_inplace(self) -> None:
        # self.repo has no .git at all -> git floor trips.
        tasks = [_task("01-a", owns=["a.py"], write_file="a.py")]
        state = self._write_tasks(tasks)
        adapter = WritingAdapter(capabilities=["parallel"])

        result_state = _run_dispatch(self.layout, _cfg(capabilities=["parallel"]), adapter, state)

        self.assertIsNone(result_state.gate)
        self.assertEqual(result_state.tasks["01-a"], "done")
        self.assertTrue((self.repo / "a.py").exists())
        self.assertFalse(WORKTREES_ROOT.joinpath(self.run_id).exists())

    # -- brakes: capability floor + critical serialization ---------------------

    def test_parallel_capability_gate_allows_concurrent_noncritical_tasks(self) -> None:
        _init_git_repo(self.repo)
        tasks = [
            _task("01-a", owns=["a.py"], write_file="a.py"),
            _task("02-b", owns=["b.py"], write_file="b.py"),
        ]
        state = self._write_tasks(tasks)
        adapter = WritingAdapter(capabilities=["parallel"], hold_seconds=0.1)

        _run_dispatch(self.layout, _cfg(capabilities=["parallel"], max_parallel=4), adapter, state)

        self.assertGreaterEqual(adapter.max_active, 2)

    def test_no_parallel_capability_runs_sequentially(self) -> None:
        _init_git_repo(self.repo)
        tasks = [
            _task("01-a", owns=["a.py"], write_file="a.py"),
            _task("02-b", owns=["b.py"], write_file="b.py"),
        ]
        state = self._write_tasks(tasks)
        adapter = WritingAdapter(capabilities=[], hold_seconds=0.05)

        _run_dispatch(self.layout, _cfg(capabilities=[]), adapter, state)

        self.assertEqual(adapter.max_active, 1)

    def test_critical_tasks_never_run_concurrently_even_with_parallel_capability(self) -> None:
        _init_git_repo(self.repo)
        tasks = [
            _task("01-a", owns=["a.py"], write_file="a.py", critical=True),
            _task("02-b", owns=["b.py"], write_file="b.py", critical=True),
        ]
        state = self._write_tasks(tasks)
        adapter = WritingAdapter(capabilities=["parallel"], hold_seconds=0.1)

        result_state = _run_dispatch(
            self.layout, _cfg(capabilities=["parallel"], max_parallel=4), adapter, state
        )

        self.assertIsNone(result_state.gate)
        self.assertEqual(adapter.max_active, 1)  # critical x critical always serialized


class TestPreMergeGate(unittest.TestCase):
    """A task never merges dirty: its own exit-criteria gate runs in its own
    worktree BEFORE `merge_task_branch`, not post-merge in `validate`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.run_id = f"run-{self.id().split('.')[-1]}"
        self.layout = RunLayout(repo=self.repo, run_id=self.run_id)
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")
        self.addCleanup(shutil.rmtree, WORKTREES_ROOT / self.run_id, True)

    def _write_tasks(self, tasks: list[Task]) -> State:
        for task in tasks:
            write_task(self.layout, task)
        return State(phase="dispatch", tasks={t.id: t.status for t in tasks})

    def test_passing_gate_merges_and_marks_done(self) -> None:
        _init_git_repo(self.repo)
        task = _task("01-a", owns=["a.py"], write_file="a.py", exit_cmd="test -f a.py")
        state = self._write_tasks([task])
        adapter = WritingAdapter(capabilities=[])

        result_state = _run_dispatch(self.layout, _cfg(capabilities=[]), adapter, state)

        self.assertIsNone(result_state.gate)
        self.assertEqual(result_state.tasks["01-a"], "done")
        self.assertTrue((self.repo / "a.py").exists())  # merged onto the integration branch
        log = subprocess.run(
            ["git", "-C", str(self.repo), "log", "--oneline"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("01-a", log.stdout)

    def test_failing_gate_blocks_merge_and_resets_to_pending(self) -> None:
        _init_git_repo(self.repo)
        # The task claims done and writes its file, but its own exit
        # criterion always fails -> gate must refuse the merge.
        task = _task("01-a", owns=["a.py"], write_file="a.py", exit_cmd="false")
        state = self._write_tasks([task])
        adapter = WritingAdapter(capabilities=[])
        base_sha_before = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        result_state = _run_dispatch(self.layout, _cfg(capabilities=[]), adapter, state)

        self.assertIsNone(result_state.gate)  # not a reconcile tripwire, an ordinary re-route
        self.assertEqual(result_state.tasks["01-a"], "pending")
        self.assertFalse((self.repo / "a.py").exists())  # never merged
        head_after = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(base_sha_before, head_after)  # integration branch HEAD unchanged

        feedback = (self.layout.scratch_dir / "01-a.md").read_text(encoding="utf-8")
        self.assertIn("Validation feedback", feedback)
        self.assertIn("FAIL", feedback)

        # worktree cleaned up, not left on disk (this isn't a reconcile tripwire)
        leftover = list((WORKTREES_ROOT / self.run_id).glob("01-a-*"))
        self.assertEqual(leftover, [])

    def test_repeated_gate_failure_breaches_ceiling_and_resplits(self) -> None:
        _init_git_repo(self.repo)
        task = _task("01-a", owns=["a.py"], write_file="a.py", exit_cmd="false")
        state = self._write_tasks([task])
        adapter = WritingAdapter(capabilities=[])
        cfg = _cfg(capabilities=[])

        for _ in range(resplit.STEP_CEILING):
            state = _run_dispatch(self.layout, cfg, adapter, state)

        self.assertIsNone(state.gate)
        self.assertEqual(state.tasks["01-a"], "split")
        self.assertEqual(state.tasks["01-a-r1"], "pending")

        all_tasks = load_tasks(self.layout)
        parent = next(t for t in all_tasks if t.id == "01-a")
        child = next(t for t in all_tasks if t.id == "01-a-r1")
        self.assertEqual(parent.status, "split")
        self.assertEqual(child.parent, "01-a")
        self.assertFalse((self.repo / "a.py").exists())  # never merged across all attempts


if __name__ == "__main__":
    unittest.main()
