"""Validation phase mechanics (ticket 007): the three gates over a task's
typed `exit_criteria` (ticket 003), plus the per-run integration critic.

Three gates, all author-agnostic and forgery-proof:

1. **`cmd` gate** — a driver-executed subprocess; the real exit code is the
   verdict. Runs in the task's still-on-disk worktree if one exists, else
   the target repo (`worktree.find_worktree_dir`).
2. **`judgment` gate** — a fresh, cold critic `run()` given spec.md + the
   task's on-disk owns-diff ONLY (`resume.owns_diff`) — never the author's
   scratch/self-summary. Output is a structured `{pass, evidence,
   cited_paths}` verdict; a `pass: true` with empty `cited_paths` is
   auto-failed here, not left to the critic's honor (`_parse_verdict`).
3. **integration critic** — one extra critic `run()` over the whole
   `spec.md` + the full merged run diff (`resume.owns_diff(repo, ["."])`,
   ticket 005's root-commit-to-worktree ground truth), run once after every
   leaf passes and before the run reaches `done`. Catches cross-task seam
   gaps no single leaf's own gate would see.

`ads/driver.py` owns the retry-bounded state machine (write feedback, reset
tasks to pending, loop back to `dispatch`, halt after 2 rounds); this module
only evaluates criteria/verdicts and writes the audit trail
(`validation-report.md`).

Deferred (ticket 007, explicitly out of scope for this increment): gates
currently run post-merge, in the separate `validate` phase — before-merge
resequencing (a task never merges dirty) is a 006+007 integration
follow-up, not built here.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ads import resume as resume_module
from ads import worktree
from ads.adapters.base import Adapter, RunResult
from ads.config import Config
from ads.layout import RunLayout
from ads.prompt import compose
from ads.tasks import ExitCriterion, ExitCriterionCheck, Task

CMD_TIMEOUT_SECONDS = 300
CODE_REVIEW_CAPABILITY = "code-review"
REPORT_FILENAME = "validation-report.md"

CODE_REVIEW_INSTRUCTION = (
    "This harness advertises a `code-review` capability — drive that skill "
    "to review the diff below, then translate its findings into the exact "
    "JSON verdict contract required here. Where no such skill is available, "
    "the structured-verdict instructions below are the mandatory floor."
)


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


@dataclass(frozen=True)
class IntegrationVerdict:
    passed: bool
    evidence: str
    cited_paths: list[str]


# ---------------------------------------------------------------------------
# per-leaf evaluation
# ---------------------------------------------------------------------------


def evaluate_task(layout: RunLayout, cfg: Config, adapter: Adapter, task: Task) -> TaskValidation:
    results = [
        _evaluate_criterion(layout, cfg, adapter, task, criterion)
        for criterion in task.exit_criteria
    ]
    return TaskValidation(task=task, results=results)


def _evaluate_criterion(
    layout: RunLayout, cfg: Config, adapter: Adapter, task: Task, criterion: ExitCriterion
) -> CriterionResult:
    if criterion.check == "cmd":
        return _run_cmd_criterion(layout, task, criterion)
    return _run_judgment_criterion(layout, cfg, adapter, task, criterion)


def _run_cmd_criterion(layout: RunLayout, task: Task, criterion: ExitCriterion) -> CriterionResult:
    cwd = worktree.find_worktree_dir(layout.run_id, task.id) or layout.repo
    try:
        proc = subprocess.run(
            criterion.value,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        detail = f"timed out after {CMD_TIMEOUT_SECONDS}s: {criterion.value!r}"
        return CriterionResult(check="cmd", value=criterion.value, passed=False, detail=detail)
    detail = f"exit={proc.returncode}\n--- stdout ---\n{proc.stdout}--- stderr ---\n{proc.stderr}"
    return CriterionResult(
        check="cmd", value=criterion.value, passed=proc.returncode == 0, detail=detail
    )


def _run_judgment_criterion(
    layout: RunLayout, cfg: Config, adapter: Adapter, task: Task, criterion: ExitCriterion
) -> CriterionResult:
    diff_text = resume_module.owns_diff(layout.repo, task.owns)
    task_body = (
        cfg.phases["validate"]
        .body.replace("{criterion}", criterion.value)
        .replace(
            "{diff}",
            diff_text or "(no diff: nothing changed under this task's declared `owns` paths)",
        )
    )
    result = _run_critic(layout, cfg, adapter, task_body)
    passed, evidence, cited = _parse_verdict(result)
    return CriterionResult(
        check="judgment", value=criterion.value, passed=passed, detail=evidence, cited_paths=cited
    )


# ---------------------------------------------------------------------------
# integration critic (runs once per run, over the full spec + full diff)
# ---------------------------------------------------------------------------


def run_integration_critic(layout: RunLayout, cfg: Config, adapter: Adapter) -> IntegrationVerdict:
    diff_text = resume_module.owns_diff(layout.repo, ["."])
    task_body = cfg.phases["validate-integration"].body.replace(
        "{diff}", diff_text or "(no diff: nothing changed in this run)"
    )
    result = _run_critic(layout, cfg, adapter, task_body)
    passed, evidence, cited = _parse_verdict(result)
    return IntegrationVerdict(passed=passed, evidence=evidence, cited_paths=cited)


def attribute_paths(all_tasks: list[Task], cited_paths: list[str]) -> list[str]:
    """Which tasks' declared `owns` cover the integration critic's cited
    paths. A cited path nobody's `owns` covers means the gap has no owning
    task — that's a "missing work" case the driver must not silently retry
    (see the module docstring's deferred-work note)."""
    attributed: set[str] = set()
    for path in cited_paths:
        for task in all_tasks:
            if any(worktree.covers(path, entry) for entry in task.owns):
                attributed.add(task.id)
    return sorted(attributed)


# ---------------------------------------------------------------------------
# shared critic plumbing
# ---------------------------------------------------------------------------


def _run_critic(layout: RunLayout, cfg: Config, adapter: Adapter, task_body: str) -> RunResult:
    spec_text = layout.spec.read_text(encoding="utf-8") if layout.spec.exists() else ""
    critic = cfg.experts.get("critic")
    critic_body = _critic_body(critic.body if critic else "", adapter)
    prompt = compose(cfg.base, critic_body, "", task_body, spec=spec_text)
    allowed_tools = list(critic.tools) if critic and critic.tools else None
    return adapter.run(prompt, cwd=layout.repo, allowed_tools=allowed_tools, tier="standard")


def _critic_body(body: str, adapter: Adapter) -> str:
    if CODE_REVIEW_CAPABILITY in adapter.capabilities():
        return f"{body}\n\n{CODE_REVIEW_INSTRUCTION}"
    return body


def _parse_verdict(result: RunResult) -> tuple[bool, str, list[str]]:
    if result.exit_status != "ok" or result.structured is None:
        return False, f"critic run failed: {result.text[:200]}", []
    payload = result.structured
    passed = payload.get("pass") is True
    evidence = str(payload.get("evidence", "") or "")
    cited = list(payload.get("cited_paths", []) or [])
    if passed and not cited:
        return False, f"AUTO-FAIL (pass=true with no cited_paths): {evidence}", []
    return passed, evidence, cited


# ---------------------------------------------------------------------------
# feedback (ticket 005 Rule 4 resume read-set: appended to scratch, never a
# transcript) + the always-written audit report
# ---------------------------------------------------------------------------


def _append_scratch(layout: RunLayout, task_id: str, text: str) -> None:
    path = layout.scratch_dir / f"{task_id}.md"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def write_task_feedback(layout: RunLayout, tv: TaskValidation) -> None:
    lines = [f"\n## Validation feedback ({tv.task.id})\n"]
    for r in tv.results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(f"- [{r.check}] {status}: {r.value}")
        lines.append(f"  {r.detail.strip()}")
        if r.cited_paths:
            lines.append(f"  cited_paths: {r.cited_paths}")
    _append_scratch(layout, tv.task.id, "\n".join(lines) + "\n")


def write_integration_feedback(
    layout: RunLayout, task_id: str, integration: IntegrationVerdict
) -> None:
    lines = [
        f"\n## Integration validation feedback ({task_id})\n",
        f"- integration critic FAIL: {integration.evidence}",
        f"  cited_paths: {integration.cited_paths}",
    ]
    _append_scratch(layout, task_id, "\n".join(lines) + "\n")


def write_report(
    layout: RunLayout,
    task_validations: list[TaskValidation],
    integration: IntegrationVerdict | None,
) -> None:
    lines = [f"# Validation report — {layout.run_id}", ""]
    for tv in task_validations:
        lines.append(f"## {tv.task.id} — {'PASS' if tv.passed else 'FAIL'}")
        lines.append("")
        for r in tv.results:
            lines.append(f"- [{r.check}] {'PASS' if r.passed else 'FAIL'}: {r.value}")
            lines.extend(f"  {line}" for line in r.detail.strip().splitlines())
            if r.cited_paths:
                lines.append(f"  cited_paths: {r.cited_paths}")
        lines.append("")
    lines.append("## Integration")
    lines.append("")
    if integration is None:
        lines.append("not run — task-level exit criteria failed first")
    else:
        lines.append(f"status: {'PASS' if integration.passed else 'FAIL'}")
        lines.append(f"evidence: {integration.evidence}")
        lines.append(f"cited_paths: {integration.cited_paths}")
    _report_path(layout).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _report_path(layout: RunLayout) -> Path:
    return layout.root / REPORT_FILENAME
