"""Resumptive re-split (ticket 003, wired by ticket 005 Rule 5).

Two triggers hand a task here instead of letting it fall to `blocked`:

1. **Budget ceiling** — the driver-persisted `state.step_counts[task.id]`
   (dispatch-attempt count, `ads/state.py`) crosses `STEP_CEILING`. The
   agent never self-declares over-budget on this floor; the driver decides
   from the persisted count alone.
2. **Handoff** — a `run()` returns `status: "handoff"` (or the explicit
   `"over-budget"` synonym) in its structured payload, a proactive
   accelerator some adapters can offer via a context-fraction signal. Routes
   to the same re-split path, before the coarse ceiling would even fire.

Either trigger produces the same consequence: the parent task is marked
`split` (never `blocked`, never rolled back on disk) and a **residual
child** task is created carrying only the exit criteria the parent's
scratch checkpoint doesn't yet show as `Done`. The child's scratch is
seeded from the parent's, so ticket 005's resume read-set
(`ads/resume.py`) plus the on-disk owns-diff let it continue exactly where
the parent left off — never restart.

Partition-of-owns note: the ticket frames this as splitting over a
"partitioned" owns, but exit criteria don't declare per-criterion
ownership, so a real partition into multiple disjoint-owns parallel
children isn't derivable from what's on disk. This module implements a
single residual child carrying the parent's full, unpartitioned `owns` —
resumptive and correct, just not parallel-fanned-out. Splitting into
several disjoint-owns children would need per-criterion ownership metadata
that doesn't exist yet; noted here as fog, not faked.

Re-split is subdivision, never a re-gate (003): a residual child is pending
work over the same already-approved scope, so it goes straight back through
`ready_batch` — it never loops back to review/plan. `MAX_RESPLIT_DEPTH`
bounds how many times one lineage can re-split before halting to a human,
so a pathological task can't split forever.
"""

from __future__ import annotations

from ads.adapters.base import RunResult
from ads.layout import RunLayout
from ads.resume import scratch_path
from ads.task_io import load_tasks, write_task
from ads.tasks import ExitCriterion, Task, check_acyclic

STEP_CEILING = 3
MAX_RESPLIT_DEPTH = 3
RESPLIT_ID_SUFFIX = "-r"

STATUS_HANDOFF = "handoff"
STATUS_OVER_BUDGET = "over-budget"
HANDOFF_STATUSES = frozenset({STATUS_HANDOFF, STATUS_OVER_BUDGET})

MISSING_WORK_ID_PREFIX = "gap"
DONE_ENTRY_SEPARATOR = " — "


class ResplitDepthExceeded(RuntimeError):
    """A task lineage has already been re-split `MAX_RESPLIT_DEPTH` times.
    The driver halts to a human instead of subdividing forever."""


# ---------------------------------------------------------------------------
# trigger detection
# ---------------------------------------------------------------------------


def is_handoff(result: RunResult) -> bool:
    if result.structured is None:
        return False
    # `structured["status"]` is typed as the narrower `TaskStatus` (the set a
    # persisted task file may hold), but a run's raw JSON is a trust boundary
    # (like ads/task_io.py's frontmatter parsing) — "handoff"/"over-budget"
    # are valid values here even though they're not persisted task statuses.
    status = str(result.structured.get("status", ""))
    return status in HANDOFF_STATUSES


def breached_ceiling(step_count: int) -> bool:
    return step_count >= STEP_CEILING


# ---------------------------------------------------------------------------
# remaining-criteria parsing (never redo finished work)
# ---------------------------------------------------------------------------


def _done_criteria_values(scratch_text: str) -> set[str]:
    """Criterion text recorded under scratch's `## Done` heading. Each line
    is rendered `<criterion> — <where>` (see resume.render_scratch_skeleton);
    the criterion text is everything before the first ` — `."""
    lines = scratch_text.splitlines()
    try:
        start = lines.index("## Done") + 1
    except ValueError:
        return set()
    done: set[str] = set()
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("##"):
            break
        if not stripped or stripped.startswith("<!--"):
            continue
        text = stripped.removeprefix("- ").strip()
        criterion_text = text.split(DONE_ENTRY_SEPARATOR, 1)[0].strip()
        if criterion_text:
            done.add(criterion_text)
    return done


def remaining_criteria(
    scratch_text: str, exit_criteria: list[ExitCriterion]
) -> list[ExitCriterion]:
    """Criteria not yet proven `Done` in the scratch checkpoint. Falls back
    to "all criteria remain" when the scratch has no `Done` entries at all
    — finished work is never silently dropped, but nothing is assumed done
    without evidence either."""
    done = _done_criteria_values(scratch_text)
    if not done:
        return list(exit_criteria)
    return [ec for ec in exit_criteria if ec.value not in done]


# ---------------------------------------------------------------------------
# lineage + child construction
# ---------------------------------------------------------------------------


def lineage_depth(task_id: str, tasks_by_id: dict[str, Task]) -> int:
    """How many ancestors `task_id` has via `parent` links — 0 for an
    original (never-split) task."""
    depth = 0
    current = tasks_by_id.get(task_id)
    while current is not None and current.parent is not None:
        depth += 1
        current = tasks_by_id.get(current.parent)
    return depth


def next_child_id(parent_id: str, tasks_by_id: dict[str, Task]) -> str:
    n = 1
    while f"{parent_id}{RESPLIT_ID_SUFFIX}{n}" in tasks_by_id:
        n += 1
    return f"{parent_id}{RESPLIT_ID_SUFFIX}{n}"


def build_residual_child(parent: Task, child_id: str, criteria: list[ExitCriterion]) -> Task:
    """The single residual child: same `owns` (see module docstring's
    partition-of-owns note), same `depends_on`, fresh `pending` status,
    `parent` pointing back at the split task."""
    return Task(
        id=child_id,
        status="pending",
        depends_on=list(parent.depends_on),
        owns=list(parent.owns),
        exit_criteria=criteria,
        expert=parent.expert,
        critical=parent.critical,
        tier=parent.tier,
        parent=parent.id,
        body=parent.body,
    )


def seed_child_scratch(layout: RunLayout, parent: Task, child: Task) -> None:
    """Carry the parent's scratch checkpoint into the child's resume
    read-set: `ads.resume.assemble_resume_context` reads `scratch/<id>.md`
    keyed by the child's own id, so the child needs its own copy of
    whatever Done/Remaining state the parent had checkpointed."""
    parent_path = scratch_path(layout, parent.id)
    if not parent_path.exists():
        return
    scratch_path(layout, child.id).write_text(
        parent_path.read_text(encoding="utf-8"), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# the re-split (A/B): budget ceiling / handoff -> residual child
# ---------------------------------------------------------------------------


def perform(layout: RunLayout, parent: Task) -> Task:
    """Mark `parent` `split` and produce its residual child, in place.

    Loads the full on-disk task set (not just the caller's in-memory batch)
    so lineage depth, child-id uniqueness, and the acyclic re-check all see
    every task ever written this run — including prior re-splits. Writes
    both task files and seeds the child's scratch before returning it; the
    caller only needs to fold the child into `state.tasks`.

    Raises `ResplitDepthExceeded` if `parent`'s lineage has already hit
    `MAX_RESPLIT_DEPTH` — the caller halts to a human instead of looping.
    """
    all_tasks = load_tasks(layout)
    tasks_by_id = {t.id: t for t in all_tasks}
    tasks_by_id[parent.id] = parent  # in-memory parent may be ahead of disk

    if lineage_depth(parent.id, tasks_by_id) >= MAX_RESPLIT_DEPTH:
        raise ResplitDepthExceeded(
            f"{parent.id}: re-split depth cap ({MAX_RESPLIT_DEPTH}) reached after "
            "repeated budget/handoff breaches — halting to a human"
        )

    scratch_text = (
        scratch_path(layout, parent.id).read_text(encoding="utf-8")
        if scratch_path(layout, parent.id).exists()
        else ""
    )
    criteria = remaining_criteria(scratch_text, parent.exit_criteria)
    child = build_residual_child(parent, next_child_id(parent.id, tasks_by_id), criteria)

    check_acyclic([*all_tasks, child])  # defensive: DAG must stay acyclic post-split
    parent.status = "split"

    write_task(layout, parent)
    write_task(layout, child)
    seed_child_scratch(layout, parent, child)
    return child


# ---------------------------------------------------------------------------
# missing-work re-split (C): integration critic cites a gap no task owns
# ---------------------------------------------------------------------------


def _next_missing_work_id(all_tasks: list[Task]) -> str:
    existing = {t.id for t in all_tasks}
    n = 1
    while f"{MISSING_WORK_ID_PREFIX}-{n}" in existing:
        n += 1
    return f"{MISSING_WORK_ID_PREFIX}-{n}"


def missing_work_task(all_tasks: list[Task], evidence: str, cited_paths: list[str]) -> Task | None:
    """A new, parentless task covering integration-critic-cited paths no
    existing task's `owns` covers ("missing work with no owning task").
    `owns` is derived directly from the cited paths — the only ground truth
    available for a gap nobody declared ownership of.

    Returns `None` when there are no cited paths to build `owns` or an exit
    criterion from at all — genuinely unattributable, the caller halts.
    """
    if not cited_paths:
        return None
    return Task(
        id=_next_missing_work_id(all_tasks),
        status="pending",
        depends_on=[],
        owns=list(cited_paths),
        exit_criteria=[ExitCriterion(check="judgment", value=evidence)],
        expert="",
        critical=False,
        tier="standard",
        parent=None,
        body=f"Integration critic found missing work:\n\n{evidence}",
    )
