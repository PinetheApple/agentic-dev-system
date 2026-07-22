"""Ticket 004: the two author-agnostic gates and their structural
anti-forgery rules — never honor-based, never self-report."""

from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from pathlib import Path

from ads.adapters.base import Role, RunResult
from ads.layout import RunLayout
from ads.tasks import ExitCriterion, Task, TaskParseError
from ads.validate import check_leaf_judgment, evaluate_task, owns_diff
from tests.helpers import temp_git_repo


class _CannedAdapter:
    """Hands back a fixed judgment verdict regardless of prompt."""

    def __init__(self, verdict: dict[str, object]) -> None:
        self._verdict = verdict

    def run(
        self,
        prompt: str,
        cwd: Path,
        *,
        role: Role = "execution",
        allowed_tools: list[str] | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> RunResult:
        return RunResult(text=json.dumps(self._verdict), exit_status="ok")

    def resolve_model(self, role: Role) -> str:
        return "canned"


def _layout(repo: Path) -> RunLayout:
    layout = RunLayout(repo=repo, run_id="run")
    layout.scaffold()
    layout.spec.write_text("# spec\n\nBuild thing.\n", encoding="utf-8")
    return layout


class CheckLeafJudgmentTest(unittest.TestCase):
    def test_missing_judgment_criterion_raises(self) -> None:
        task = Task(id="a", exit_criteria=[ExitCriterion(check="cmd", value="true")])
        with self.assertRaises(TaskParseError):
            check_leaf_judgment([task])

    def test_at_least_one_judgment_criterion_passes(self) -> None:
        task = Task(
            id="a",
            exit_criteria=[
                ExitCriterion(check="cmd", value="true"),
                ExitCriterion(check="judgment", value="does the thing"),
            ],
        )
        check_leaf_judgment([task])  # no raise


class AntiForgeryTest(unittest.TestCase):
    def test_empty_cited_paths_auto_fails_even_if_pass_true(self) -> None:
        with temp_git_repo() as repo:
            layout = _layout(repo)
            task = Task(
                id="a",
                owns=["src/thing.py"],
                exit_criteria=[ExitCriterion(check="judgment", value="assertion")],
            )
            adapter = _CannedAdapter({"pass": True, "evidence": "trust me", "cited_paths": []})
            tv = evaluate_task(layout, adapter, task, diff_text="diff --git a/src/thing.py ...")
            self.assertFalse(tv.passed)
            self.assertIn("AUTO-FAIL", tv.results[0].detail)

    def test_hallucinated_cited_path_auto_fails(self) -> None:
        with temp_git_repo() as repo:
            layout = _layout(repo)
            task = Task(
                id="a",
                owns=["src/thing.py"],
                exit_criteria=[ExitCriterion(check="judgment", value="assertion")],
            )
            adapter = _CannedAdapter(
                {"pass": True, "evidence": "looks good", "cited_paths": ["src/not_in_diff.py"]}
            )
            tv = evaluate_task(layout, adapter, task, diff_text="diff --git a/src/thing.py ...")
            self.assertFalse(tv.passed)
            self.assertIn("AUTO-FAIL", tv.results[0].detail)

    def test_genuine_cited_path_in_diff_passes(self) -> None:
        with temp_git_repo() as repo:
            layout = _layout(repo)
            task = Task(
                id="a",
                owns=["src/thing.py"],
                exit_criteria=[ExitCriterion(check="judgment", value="assertion")],
            )
            diff_text = "diff --git a/src/thing.py b/src/thing.py\n+def thing(): ...\n"
            adapter = _CannedAdapter(
                {"pass": True, "evidence": "matches", "cited_paths": ["src/thing.py"]}
            )
            tv = evaluate_task(layout, adapter, task, diff_text=diff_text)
            self.assertTrue(tv.passed)

    def test_cmd_criterion_uses_real_subprocess(self) -> None:
        with temp_git_repo() as repo:
            layout = _layout(repo)
            task = Task(
                id="a",
                exit_criteria=[
                    ExitCriterion(check="cmd", value="true"),
                    ExitCriterion(check="judgment", value="assertion"),
                ],
            )
            adapter = _CannedAdapter({"pass": True, "evidence": "ok", "cited_paths": ["x"]})
            tv = evaluate_task(layout, adapter, task, diff_text="x")
            self.assertTrue(tv.results[0].passed)

    def test_failing_cmd_criterion_fails_the_task(self) -> None:
        with temp_git_repo() as repo:
            layout = _layout(repo)
            task = Task(
                id="a",
                exit_criteria=[
                    ExitCriterion(check="cmd", value="false"),
                    ExitCriterion(check="judgment", value="assertion"),
                ],
            )
            adapter = _CannedAdapter({"pass": True, "evidence": "ok", "cited_paths": ["x"]})
            tv = evaluate_task(layout, adapter, task, diff_text="x")
            self.assertFalse(tv.passed)
            self.assertFalse(tv.results[0].passed)

    def test_owns_diff_reflects_real_git_diff(self) -> None:
        with temp_git_repo() as repo:
            baseline = _run_head(repo)
            (repo / "src").mkdir()
            (repo / "src" / "thing.py").write_text("x = 1\n", encoding="utf-8")
            diff = owns_diff(repo, baseline, ["src/thing.py"])
            self.assertIn("src/thing.py", diff)

    def test_owns_diff_empty_owns_returns_empty(self) -> None:
        with temp_git_repo() as repo:
            baseline = _run_head(repo)
            self.assertEqual(owns_diff(repo, baseline, []), "")


def _run_head(repo: Path) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


if __name__ == "__main__":
    unittest.main()
