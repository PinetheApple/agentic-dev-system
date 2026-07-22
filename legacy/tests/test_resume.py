"""Ticket 005, increment A: Rule 2 scratch skeleton + Rule 4 resume read-set.

Uses temp dirs (`RunLayout.scaffold`) and, for the git-owns-diff path, a real
temp git repo — same pattern as `test_dispatch_worktree.py`'s
`_init_git_repo` — since owns-diff needs real `git diff` behavior.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from ads.adapters.base import RunResult, StructuredPayload
from ads.config import Config, HarnessConfig, PromptDoc
from ads.dispatch import run as dispatch_run
from ads.layout import RunLayout
from ads.resume import (
    assemble_resume_context,
    owns_diff,
    render_scratch_skeleton,
    scaffold_scratch,
)
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


def _task(
    task_id: str = "01-a",
    owns: list[str] | None = None,
    exit_criteria: list[ExitCriterion] | None = None,
) -> Task:
    return Task(
        id=task_id,
        status="pending",
        depends_on=[],
        owns=owns if owns is not None else ["a.py"],
        exit_criteria=(
            exit_criteria
            if exit_criteria is not None
            else [
                ExitCriterion(check="cmd", value="pytest tests/test_a.py"),
                ExitCriterion(check="judgment", value="a.py is idiomatic"),
            ]
        ),
        expert="",
        critical=False,
        tier="standard",
        body="Implement a.py.",
    )


class TestScratchSkeleton(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-test")
        self.layout.scaffold()

    def test_skeleton_has_objective_and_all_exit_criteria_under_remaining(self) -> None:
        task = _task()
        skeleton = render_scratch_skeleton(task)

        self.assertIn("## Objective", skeleton)
        self.assertIn("Implement a.py.", skeleton)
        self.assertIn("## Done", skeleton)
        self.assertIn("## Remaining", skeleton)
        self.assertIn("- pytest tests/test_a.py", skeleton)
        self.assertIn("- a.py is idiomatic", skeleton)
        self.assertIn("## Decisions / gotchas", skeleton)

    def test_scaffold_creates_file_when_absent(self) -> None:
        task = _task()

        scaffold_scratch(self.layout, task)

        scratch_path = self.layout.scratch_dir / f"{task.id}.md"
        self.assertTrue(scratch_path.exists())
        self.assertIn("## Remaining", scratch_path.read_text(encoding="utf-8"))

    def test_scaffold_does_not_overwrite_existing_progress(self) -> None:
        task = _task()
        scratch_path = self.layout.scratch_dir / f"{task.id}.md"
        scratch_path.write_text("## Done\n\n- did the thing — a.py:1\n", encoding="utf-8")

        scaffold_scratch(self.layout, task)

        self.assertEqual(
            scratch_path.read_text(encoding="utf-8"), "## Done\n\n- did the thing — a.py:1\n"
        )


class TestResumeReadSet(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-test")
        self.layout.scaffold()

    def test_fresh_task_has_no_resume_context(self) -> None:
        task = _task()
        scaffold_scratch(self.layout, task)  # first dispatch always scaffolds first

        self.assertIsNone(assemble_resume_context(self.layout, task))

    def test_prior_scratch_done_plus_owns_diff_yields_resume_context(self) -> None:
        _init_git_repo(self.repo)
        task = _task(owns=["a.py"])
        scratch_path = self.layout.scratch_dir / f"{task.id}.md"
        scratch_path.write_text(
            "## Objective\n\nImplement a.py.\n\n"
            "## Done\n\n- wrote skeleton — a.py:1\n\n"
            "## Remaining\n- pytest tests/test_a.py\n\n## Decisions / gotchas\n",
            encoding="utf-8",
        )
        (self.repo / "a.py").write_text("def a(): ...\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "wip a.py"], check=True)

        diff = owns_diff(self.repo, task.owns)
        self.assertIn("def a()", diff)

        context = assemble_resume_context(self.layout, task)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertIn("scratch/01-a.md", context)
        self.assertIn("wrote skeleton", context)
        self.assertIn("def a()", context)
        self.assertIn("overrides", context.lower())
        self.assertIn("Remaining", context)


class TestResumeReadSetInPrompt(unittest.TestCase):
    """Function-level test on the composed dispatch prompt itself, per the
    ticket's test list: fresh task -> no resume block; prior work -> the
    full 4-item read-set (spec+design+task+scratch+owns-diff) with the
    diff-overrides-scratch / continue-on-Remaining instruction."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        _init_git_repo(self.repo)
        self.layout = RunLayout(repo=self.repo, run_id="run-test")
        self.layout.scaffold()
        self.layout.spec.write_text("# Spec\n\nBuild the thing.\n", encoding="utf-8")
        self.layout.design.write_text("# Design\n\nLayered design.\n", encoding="utf-8")
        self.cfg = Config(
            harness=HarnessConfig(
                tier_model={"fast": "x", "standard": "x", "deep": "x"}, run_cmd=[], capabilities=[]
            ),
            base="base principles",
            experts={},
            phases={"dispatch": PromptDoc(meta={}, body="PHASE:dispatch\n\n{task}")},
        )

    def _run(self, tasks: list[Task]) -> RecordingAdapter:
        for task in tasks:
            write_task(self.layout, task)
        state = State(phase="dispatch", tasks={t.id: t.status for t in tasks})
        adapter = RecordingAdapter()
        dispatch_run(self.layout, self.cfg, adapter, state, tasks)
        return adapter

    def test_fresh_task_prompt_has_no_resume_block(self) -> None:
        # exit_criteria=[]: this test is about resume-context assembly, not
        # gate evaluation (the 006+007 pre-merge gate calls the adapter
        # again for a judgment criterion, which would add a second recorded
        # prompt unrelated to what's under test here).
        adapter = self._run([_task("01-fresh", owns=["fresh.py"], exit_criteria=[])])

        self.assertEqual(len(adapter.prompts), 1)
        prompt = adapter.prompts[0]
        self.assertIn("## Spec", prompt)
        self.assertIn("## Design", prompt)
        self.assertIn("## Task", prompt)
        self.assertNotIn("## Resume", prompt)
        self.assertNotIn("on-disk diff", prompt)

    def test_prior_work_task_prompt_carries_full_read_set(self) -> None:
        task = _task("01-resume", owns=["resume.py"], exit_criteria=[])
        scratch_path = self.layout.scratch_dir / f"{task.id}.md"
        scratch_path.write_text(
            "## Objective\n\nImplement a.py.\n\n"
            "## Done\n\n- wrote first draft — resume.py:1\n\n"
            "## Remaining\n- pytest tests/test_a.py\n\n## Decisions / gotchas\n",
            encoding="utf-8",
        )
        (self.repo / "resume.py").write_text("def f(): ...\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "wip"], check=True)

        adapter = self._run([task])

        self.assertEqual(len(adapter.prompts), 1)
        prompt = adapter.prompts[0]
        self.assertIn("## Spec", prompt)
        self.assertIn("## Design", prompt)
        self.assertIn("## Task", prompt)
        self.assertIn("## Resume", prompt)
        self.assertIn("scratch/01-resume.md", prompt)
        self.assertIn("wrote first draft", prompt)
        self.assertIn("def f()", prompt)
        self.assertIn("overrides", prompt.lower())
        self.assertIn("Remaining", prompt)


class RecordingAdapter:
    """Test-only adapter: records every composed prompt, always reports the
    task done, writes nothing to disk (owns-diff detection is exercised via
    pre-seeded git state in the fixtures above, not via what this adapter
    writes)."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def resolve_model(self, tier: TaskTier) -> str:
        return "recording-stub"

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
        *,
        activity_log: Path | None = None,
    ) -> RunResult:
        self.prompts.append(prompt)
        payload: StructuredPayload = {"status": "done", "summary": "stub done"}
        return RunResult(text="stub done", structured=payload, exit_status="ok")


if __name__ == "__main__":
    unittest.main()
