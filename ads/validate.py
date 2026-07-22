"""Validation mechanics (ticket 004): two author-agnostic gates per leaf task,
a driver-run `cmd` and a cold-critic `judgment`, with structural anti-forgery.
No agent self-report ever counts as done.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ads.adapters.base import Adapter
from ads.config import load_base, render_phase
from ads.layout import RunLayout
from ads.phase_json import parse_judgment_verdict
from ads.prompt import compose
from ads.task_io import append_scratch
from ads.tasks import ExitCriterion, ExitCriterionCheck, Task, TaskParseError

CMD_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class CriterionResult:
    check: ExitCriterionCheck
    value: str
    passed: bool
    detail: str
    cited_paths: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class TaskValidation:
    task: Task
    results: list[CriterionResult]

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)


def check_leaf_judgment(tasks: list[Task]) -> None:
    """Plan-commit gate: every task must carry >=1 `judgment` criterion — makes
    the "AND a cold critic" rule of the validation gate non-optional."""
    for task in tasks:
        if not any(c.check == "judgment" for c in task.exit_criteria):
            raise TaskParseError(f"{task.id!r} has no judgment exit_criteria (>=1 required)")


def owns_diff(repo: Path, baseline: str, owns: list[str]) -> str:
    """`git diff <baseline> -- <owns paths>` text, handed to the cold critic
    as the only evidence of what a task actually changed.

    A task's `owns` paths are commonly brand-new files, and `git diff` never
    shows an untracked file as an addition — so mark them intent-to-add first
    (`git add -N`: registers the path without staging its content) so a new
    file shows up as a real diff hunk instead of silently disappearing.
    """
    if not owns:
        return ""
    subprocess.run(
        ["git", "add", "-N", "--", *owns], cwd=repo, capture_output=True, text=True, check=False
    )
    proc = subprocess.run(
        ["git", "diff", baseline, "--", *owns],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


def _run_cmd_criterion(cwd: Path, criterion: ExitCriterion) -> CriterionResult:
    try:
        proc = subprocess.run(
            criterion.value,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        detail = f"timed out after {CMD_TIMEOUT_SECONDS}s: {criterion.value!r}"
        return CriterionResult(check="cmd", value=criterion.value, passed=False, detail=detail)
    detail = f"exit={proc.returncode}\n--- stdout ---\n{proc.stdout}--- stderr ---\n{proc.stderr}"
    return CriterionResult(
        check="cmd", value=criterion.value, passed=proc.returncode == 0, detail=detail
    )


def _judgment_prompt(layout: RunLayout, spec_text: str, diff_text: str, assertion: str) -> str:
    task_body = render_phase(layout.config, "validate", {"criterion": assertion, "diff": diff_text})
    return compose(
        base=load_base(layout.config),
        expert_body="",
        design="",
        task_body=task_body,
        spec=spec_text,
    )


def _run_judgment_criterion(
    layout: RunLayout,
    adapter: Adapter,
    cwd: Path,
    criterion: ExitCriterion,
    *,
    spec_text: str,
    diff_text: str,
) -> CriterionResult:
    prompt = _judgment_prompt(layout, spec_text, diff_text, criterion.value)
    result = adapter.run(prompt, cwd, role="validation")
    verdict = parse_judgment_verdict(result.text)
    passed = verdict["pass"]
    evidence = verdict["evidence"]
    cited_paths = verdict["cited_paths"]

    # Anti-rubber-stamp, structural not honor-based (ticket 004).
    if passed and not cited_paths:
        passed = False
        evidence = f"AUTO-FAIL: pass:true with empty cited_paths ({evidence})"
    elif passed:
        hallucinated = [p for p in cited_paths if p not in diff_text]
        if hallucinated:
            passed = False
            evidence = f"AUTO-FAIL: cited_paths not in owns-diff {hallucinated} ({evidence})"

    return CriterionResult(
        check="judgment",
        value=criterion.value,
        passed=passed,
        detail=evidence,
        cited_paths=cited_paths,
    )


def _write_feedback(layout: RunLayout, tv: TaskValidation) -> None:
    lines = [f"\n## Validation feedback ({tv.task.id})\n"]
    for r in tv.results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"- [{r.check}] {status}: {r.value}")
        lines.append(f"  {r.detail.strip()}")
        if r.cited_paths:
            lines.append(f"  cited_paths: {r.cited_paths}")
    append_scratch(layout, tv.task.id, "\n".join(lines) + "\n")


def evaluate_task(
    layout: RunLayout, adapter: Adapter, task: Task, *, diff_text: str
) -> TaskValidation:
    """Leaf done iff every `cmd` criterion exits 0 AND the `judgment` verdict
    passes structurally. On failure, append findings to the task's scratch
    file — a resume read-set, not a transcript."""
    spec_text = layout.spec.read_text(encoding="utf-8") if layout.spec.exists() else ""
    results: list[CriterionResult] = []
    for criterion in task.exit_criteria:
        if criterion.check == "cmd":
            results.append(_run_cmd_criterion(layout.repo, criterion))
        else:
            results.append(
                _run_judgment_criterion(
                    layout,
                    adapter,
                    layout.repo,
                    criterion,
                    spec_text=spec_text,
                    diff_text=diff_text,
                )
            )
    tv = TaskValidation(task=task, results=results)
    if not tv.passed:
        _write_feedback(layout, tv)
    return tv
