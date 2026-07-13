"""Dispatch phase (ticket 001/004/006): run the ready batch of tasks.

Two dispatch strategies, chosen by the git floor (`worktree.is_git_repo`):

- `_dispatch_inplace` — pre-006 behavior. Every task in the batch runs
  sequentially with `cwd` set directly to the target repo. Used whenever the
  target isn't (the root of) a git repository, so worktree isolation has no
  sane branch/worktree boundary to isolate against.
- `_dispatch_isolated` — ticket 006. Every task runs in its own git
  worktree/branch, with two tripwires (write-set audit, merge conflict)
  gating the merge back into the integration branch. Critical tasks always
  run sequentially relative to each other; non-critical tasks run
  concurrently (bounded by `harness.toml`'s `max_parallel`) only if the
  adapter advertises the `parallel` capability.

Ticket 005 Rule 3: there is deliberately no mid-task summarize/compaction
step here. The Rule-2 scratch skeleton (`ads/resume.py`) plus a fresh, cold
`adapter.run()` on redispatch already IS the compaction — a task never gets
its own transcript "summarized in place" mid-flight. Whatever compaction a
harness performs natively inside one `run()` call is a non-load-bearing
accelerator ADS never depends on or drives; nothing here builds or calls
into such a mechanism.
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from ads import resume as resume_module
from ads import worktree
from ads.adapters.base import Adapter, RunResult
from ads.config import Config
from ads.layout import RunLayout
from ads.prompt import compose
from ads.state import State, append_event, halt, save_state
from ads.task_io import write_scratch, write_task
from ads.tasks import Task, ready_batch
from ads.worktree import MergeOutcome, TaskWorktree

PARALLEL_CAPABILITY = "parallel"


def run(
    layout: RunLayout, cfg: Config, adapter: Adapter, state: State, all_tasks: list[Task]
) -> State:
    batch = ready_batch(all_tasks)

    if not batch:
        if all(t.status == "done" for t in all_tasks):
            state.phase = "validate"
            state.gate = None
            save_state(layout, state)
            append_event(layout, "dispatch_complete")
        else:
            return halt(layout, state, "no ready tasks but some are not done (blocked deps?)")
        return state

    design_text = layout.design.read_text(encoding="utf-8")
    spec_text = layout.spec.read_text(encoding="utf-8") if layout.spec.exists() else ""

    if not worktree.is_git_repo(layout.repo):
        # Git floor: no worktree isolation possible (not a git repo, or not
        # its own git root) -> pre-006 sequential, in-place behavior.
        print(
            f"warning: {layout.repo} is not a git repository root; "
            "dispatch running sequential, in-place, without worktree isolation",
            file=sys.stderr,
        )
        return _dispatch_inplace(layout, cfg, adapter, state, batch, spec_text, design_text)

    return _dispatch_isolated(layout, cfg, adapter, state, batch, spec_text, design_text)


def _dispatch_inplace(
    layout: RunLayout,
    cfg: Config,
    adapter: Adapter,
    state: State,
    batch: list[Task],
    spec_text: str,
    design_text: str,
) -> State:
    critical_blocked = False
    for task in batch:
        resume_module.scaffold_scratch(layout, task)
        prompt, allowed_tools = _compose_task_prompt(cfg, layout, task, spec_text, design_text)
        result = adapter.run(prompt, cwd=layout.repo, allowed_tools=allowed_tools, tier=task.tier)
        # TODO(ticket-005-rule-5): a step/tool-call budget ceiling would gate
        # here — an over-budget run should route to a "handoff" status and a
        # resumptive re-split instead of falling straight through to "blocked".
        task.status = "done" if _task_succeeded(result) else "blocked"
        critical_blocked = critical_blocked or (task.critical and task.status == "blocked")

        write_task(layout, task)
        write_scratch(layout, task, result)
        state.tasks[task.id] = task.status

    if critical_blocked:
        return halt(layout, state, "a critical task blocked during dispatch")

    save_state(layout, state)
    append_event(layout, "dispatch_batch", task_ids=[t.id for t in batch])
    return state


# ---------------------------------------------------------------------------
# worktree-isolated (+ optionally parallel)
# ---------------------------------------------------------------------------


def _compose_task_prompt(
    cfg: Config, layout: RunLayout, task: Task, spec_text: str, design_text: str
) -> tuple[str, list[str] | None]:
    expert = cfg.experts.get(task.expert)
    expert_body = expert.body if expert else ""
    task_body = cfg.phases["dispatch"].body.replace("{task}", task.body)
    resume_text = resume_module.assemble_resume_context(layout, task) or ""
    prompt = compose(
        cfg.base, expert_body, design_text, task_body, spec=spec_text, resume=resume_text
    )
    allowed_tools = list(expert.tools) if expert and expert.tools else None
    return prompt, allowed_tools


def _task_succeeded(result: RunResult) -> bool:
    return (
        result.exit_status == "ok"
        and result.structured is not None
        and result.structured.get("status") == "done"
    )


def _dispatch_one_isolated(
    layout: RunLayout,
    cfg: Config,
    adapter: Adapter,
    task: Task,
    spec_text: str,
    design_text: str,
    base_sha: str,
    git_lock: threading.Lock,
) -> tuple[Task, MergeOutcome | None]:
    """Run one task in its own worktree; commit + audit + merge back.

    Returns `(task-with-updated-status, tripwire-outcome)`. A non-`None`
    outcome means a tripwire fired: the worktree is left on disk (not
    cleaned up) and the caller must halt to the `reconcile` gate rather
    than treat this as an ordinary blocked task.
    """
    resume_module.scaffold_scratch(layout, task)
    prompt, allowed_tools = _compose_task_prompt(cfg, layout, task, spec_text, design_text)

    with git_lock:
        wt = worktree.create_worktree(layout.repo, base_sha, layout.run_id, task.id)

    result = adapter.run(prompt, cwd=wt.path, allowed_tools=allowed_tools, tier=task.tier)
    task_ok = _task_succeeded(result)

    with git_lock:
        worktree.commit_all(wt, f"ads: {task.id}")
        outcome = worktree.merge_task_branch(layout.repo, wt, task.owns) if task_ok else None
        if outcome is not None and not outcome.merged:
            _write_reconcile_scratch(layout, task, outcome, wt)
            return task, outcome  # worktree intentionally left intact
        if not worktree.remove_worktree(layout.repo, wt):
            print(f"warning: failed to clean up worktree for {task.id}: {wt.path}", file=sys.stderr)

    # TODO(ticket-005-rule-5): a step/tool-call budget ceiling would gate
    # here — an over-budget run should route to a "handoff" status and a
    # resumptive re-split instead of falling straight through to "blocked".
    task.status = "done" if task_ok else "blocked"
    write_task(layout, task)
    write_scratch(layout, task, result)
    return task, None


def _dispatch_isolated(
    layout: RunLayout,
    cfg: Config,
    adapter: Adapter,
    state: State,
    batch: list[Task],
    spec_text: str,
    design_text: str,
) -> State:
    base_sha = worktree.head_sha(layout.repo)
    git_lock = threading.Lock()
    critical_tasks = [t for t in batch if t.critical]
    noncritical_tasks = [t for t in batch if not t.critical]
    parallel_ok = PARALLEL_CAPABILITY in adapter.capabilities()
    max_workers = max(1, cfg.harness.max_parallel)

    def run_one(task: Task) -> tuple[Task, MergeOutcome | None]:
        return _dispatch_one_isolated(
            layout, cfg, adapter, task, spec_text, design_text, base_sha, git_lock
        )

    # Never parallelize critical x critical: critical tasks always run
    # sequentially relative to each other, whatever the adapter can do.
    critical_blocked = False
    for task in critical_tasks:
        updated, outcome = run_one(task)
        state.tasks[updated.id] = updated.status
        if outcome is not None:
            return _halt_reconcile(layout, state, updated, outcome)
        critical_blocked = critical_blocked or updated.status == "blocked"

    reconcile: tuple[Task, MergeOutcome] | None = None
    if parallel_ok and len(noncritical_tasks) > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(noncritical_tasks))) as pool:
            futures = [pool.submit(run_one, task) for task in noncritical_tasks]
            for future in as_completed(futures):
                updated, outcome = future.result()
                state.tasks[updated.id] = updated.status
                if outcome is not None and reconcile is None:
                    reconcile = (updated, outcome)
    else:
        for task in noncritical_tasks:
            updated, outcome = run_one(task)
            state.tasks[updated.id] = updated.status
            if outcome is not None:
                reconcile = (updated, outcome)
                break

    if reconcile is not None:
        return _halt_reconcile(layout, state, reconcile[0], reconcile[1])

    if critical_blocked:
        return halt(layout, state, "a critical task blocked during dispatch")

    save_state(layout, state)
    append_event(layout, "dispatch_batch", task_ids=[t.id for t in batch])
    return state


def _write_reconcile_scratch(
    layout: RunLayout, task: Task, outcome: MergeOutcome, wt: TaskWorktree
) -> None:
    """Record the tripwire violation + both diffs for human inspection. The
    worktree itself stays on disk (not cleaned up) — this file points at it
    and captures what the audit/merge saw at halt time."""
    scratch_path = layout.scratch_dir / f"{task.id}.reconcile.md"
    lines = [
        f"# {task.id} — reconcile ({outcome.violation})",
        "",
        f"worktree: {wt.path}",
        f"branch: {wt.branch}",
        f"base_sha: {wt.base_sha}",
        f"declared owns: {task.owns}",
        f"changed files: {outcome.changed_files}",
    ]
    if outcome.uncovered_files:
        lines.append(f"uncovered files (out-of-bounds): {outcome.uncovered_files}")
    lines += ["", "## task branch diff (vs base)", "", outcome.diff_text or "(empty)"]
    if outcome.merge_output:
        lines += ["", "## merge attempt output", "", outcome.merge_output]
    scratch_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _halt_reconcile(layout: RunLayout, state: State, task: Task, outcome: MergeOutcome) -> State:
    reason = (
        f"{task.id}: {outcome.violation} — see scratch/{task.id}.reconcile.md "
        f"and worktree left on disk for inspection"
    )
    return halt(layout, state, reason, gate="reconcile")
