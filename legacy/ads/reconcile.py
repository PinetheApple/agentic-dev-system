"""Auto-reconcile agent (ticket 006 follow-up): the seam a worktree merge
tripwire falls into before the driver halts to a human `reconcile` gate.

Opt-in by config presence only: unless the `.agent/config/` tree declares
both a `reconcile` expert and a `reconcile` phase, `attempt` is a no-op that
hands the caller back its original, unmerged `MergeOutcome` — byte-for-byte
the same halt-to-human path as before this module existed. When configured,
a bounded loop (`RECONCILE_MAX_ATTEMPTS`) gives a fresh, cold `run()` the
violation, the task's declared `owns`, and both diffs, `cwd`'d directly into
the offending worktree so its edits land there. A successful run is
re-committed and re-merged; a failing run, an exhausted loop, or an
unavailable config all fall back to the same halt-to-human outcome the
caller already knows how to handle.

Git-lock discipline mirrors `ads/dispatch.py`: the lock is only held around
the actual git ops (commit + merge), never across the slow `adapter.run()`
call, so a reconcile attempt on one task never blocks other tasks' merges.
"""

from __future__ import annotations

import threading

from ads import worktree
from ads.activity import run_with_activity
from ads.adapters.base import Adapter, RunResult
from ads.config import Config
from ads.layout import RunLayout
from ads.prompt import compose
from ads.state import State, append_event
from ads.tasks import Task
from ads.worktree import MergeOutcome, TaskWorktree

RECONCILE_MAX_ATTEMPTS = 2
RECONCILE_EXPERT = "reconcile"
RECONCILE_PHASE = "reconcile"


def attempt(
    layout: RunLayout,
    cfg: Config,
    adapter: Adapter,
    task: Task,
    wt: TaskWorktree,
    outcome: MergeOutcome,
    git_lock: threading.Lock,
    *,
    state: State | None = None,
) -> MergeOutcome:
    """Try to resolve `outcome`'s tripwire in-place and retry the merge.

    Returns the original `outcome` unchanged when reconcile isn't
    configured. Otherwise returns either a merged `MergeOutcome` (success)
    or the latest still-unmerged one (give up after a failing run or
    `RECONCILE_MAX_ATTEMPTS` exhaustion) — the caller halts exactly as it
    would have without this module in either failure case. `state` is
    optional (observability heartbeat, see `ads/activity.py`) — when
    omitted, the reconcile agent calls `adapter.run()` directly exactly as
    before, so existing callers/tests are unaffected.
    """
    if RECONCILE_EXPERT not in cfg.experts or RECONCILE_PHASE not in cfg.phases:
        return outcome

    current = outcome
    for attempt_number in range(1, RECONCILE_MAX_ATTEMPTS + 1):
        result = _run_reconcile_agent(layout, cfg, adapter, task, wt, current, state=state)
        if result.exit_status != "ok":
            break

        with git_lock:
            worktree.commit_all(wt, f"ads: reconcile {task.id} (attempt {attempt_number})")
            retry = worktree.merge_task_branch(layout.repo, wt, task.owns)

        if retry.merged:
            append_event(layout, "reconcile_success", task_id=task.id, attempts=attempt_number)
            return retry
        current = retry

    append_event(layout, "reconcile_exhausted", task_id=task.id, violation=current.violation)
    return current


def _run_reconcile_agent(
    layout: RunLayout,
    cfg: Config,
    adapter: Adapter,
    task: Task,
    wt: TaskWorktree,
    outcome: MergeOutcome,
    *,
    state: State | None = None,
) -> RunResult:
    expert = cfg.experts[RECONCILE_EXPERT]
    design_text = layout.design.read_text(encoding="utf-8")
    spec_text = layout.spec.read_text(encoding="utf-8") if layout.spec.exists() else ""
    task_body = (
        cfg.phases[RECONCILE_PHASE]
        .body.replace("{violation}", outcome.violation or "")
        .replace("{owns}", ", ".join(task.owns))
        .replace("{uncovered}", ", ".join(outcome.uncovered_files) or "(none)")
        .replace("{diff}", outcome.diff_text or "(empty)")
        .replace("{merge_output}", outcome.merge_output or "(none)")
    )
    prompt = compose(cfg.base, expert.body, design_text, task_body, spec=spec_text)
    allowed_tools = list(expert.tools) if expert.tools else None
    if state is None:
        return adapter.run(prompt, cwd=wt.path, allowed_tools=allowed_tools, tier=task.tier)
    return run_with_activity(
        adapter,
        layout,
        state,
        label=f"{task.id}-reconcile",
        kind="reconcile",
        prompt=prompt,
        cwd=wt.path,
        allowed_tools=allowed_tools,
        tier=task.tier,
    )
