"""Ticket 010 foundation: a single pure read model over the run directory.

`read_status` is the ONE place that assembles a `RunStatus` snapshot from
`state.json` + a bounded tail of `events.jsonl` + the tail/skeleton of each
task's `scratch/<id>.md`. It never mutates anything — no writes, no mkdir —
so observability stays fully decoupled from the driver loop (mirrors
`state.py`'s note that `events.jsonl` is "never read by the loop": this is a
separate reader, fine).

Later slices sit on top of this module without changing it: the curses TUI
is a renderer over `RunStatus`, `watch` polls `read_status` on an interval,
and the async control verbs (008) reuse it for their trailing status print.

Unification note (ticket 007 DoD): `RunStatus`'s run-id/phase/gate/per-task
shape is deliberately a superset that `ads/validate.py`'s
`validation-report.md` could be reframed as a terminal serialization of —
not built here, just shaped so a later slice can freeze that without a
rewrite.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ads import resume as resume_module
from ads import task_io
from ads.layout import RunLayout
from ads.state import State, load_state
from ads.tasks import Task

DEFAULT_EVENT_TAIL = 20
OBJECTIVE_HEADING = "## Objective"
REMAINING_HEADING = "## Remaining"


class StatusUnavailable(RuntimeError):
    """Raised when a run's `state.json` can't be read (missing/empty run dir)."""


@dataclass(frozen=True)
class TaskRow:
    id: str
    status: str
    expert: str
    tier: str
    critical: bool
    gate_hint: str
    checkpoint: str
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class EventLine:
    ts: str
    kind: str
    summary: str


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    phase: str
    review_stage: str | None
    gate: str | None
    halt_reason: str | None
    adapter: str
    updated_at: str
    attached: bool
    tasks: tuple[TaskRow, ...]
    recent_events: tuple[EventLine, ...]
    counts: dict[str, int] = field(default_factory=dict[str, int])
    escalations: tuple[str, ...] = field(default_factory=tuple)
    pending_summary: str = ""


# ---------------------------------------------------------------------------
# scratch checkpoint (bounded — never the whole file)
# ---------------------------------------------------------------------------


def _first_content_line_after(lines: list[str], heading: str) -> str | None:
    try:
        start = lines.index(heading) + 1
    except ValueError:
        return None
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("##"):
            break
        if not stripped or stripped.startswith("<!--"):
            continue
        return stripped
    return None


def _scratch_checkpoint(path: Path) -> str:
    """First non-empty content line under `## Remaining`, falling back to
    `## Objective`, then `""`. Reads the file but only scans for those two
    sections — never surfaces the whole scratch file."""
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8").splitlines()
    for heading in (REMAINING_HEADING, OBJECTIVE_HEADING):
        line = _first_content_line_after(lines, heading)
        if line:
            return line
    return ""


# ---------------------------------------------------------------------------
# event tail
# ---------------------------------------------------------------------------


def _event_summary(kind: str, payload: dict[str, Any]) -> str:
    renderer = _EVENT_RENDERERS.get(kind)
    if renderer is not None:
        return renderer(payload)
    extras = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:2])
    return f"{kind}: {extras}" if extras else kind


def _render_plan(payload: dict[str, Any]) -> str:
    return f"planned {payload.get('task_count', '?')} tasks"


def _render_halt(payload: dict[str, Any]) -> str:
    return f"halt: {payload.get('reason', '?')}"


def _render_escalation_open(payload: dict[str, Any]) -> str:
    return f"escalation {payload.get('id', '?')} ({payload.get('op', '?')})"


def _render_dispatch_batch(payload: dict[str, Any]) -> str:
    task_ids = payload.get("task_ids", [])
    return f"dispatched: {', '.join(task_ids)}"


_EVENT_RENDERERS = {
    "plan": _render_plan,
    "halt": _render_halt,
    "escalation_open": _render_escalation_open,
    "dispatch_batch": _render_dispatch_batch,
}


def _read_recent_events(layout: RunLayout, event_tail: int) -> tuple[EventLine, ...]:
    if not layout.events.exists():
        return ()
    lines = layout.events.read_text(encoding="utf-8").splitlines()
    events: list[EventLine] = []
    for raw in lines[-event_tail:]:
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue  # malformed line: skip, this is a best-effort audit read
        kind = str(payload.get("kind", ""))
        ts = str(payload.get("ts", ""))
        events.append(EventLine(ts=ts, kind=kind, summary=_event_summary(kind, payload)))
    return tuple(events)


# ---------------------------------------------------------------------------
# task rows
# ---------------------------------------------------------------------------


def _gated_task_ids(state: State) -> set[str]:
    """Best-effort: which task ids the run is currently gated/blocked on."""
    if state.gate == "escalation":
        return {tid for tid, status in state.tasks.items() if status == "needs-escalation"}
    if state.gate in ("blocked", "reconcile"):
        return {tid for tid, status in state.tasks.items() if status == "blocked"}
    return set()


def _build_task_rows(layout: RunLayout, state: State) -> tuple[TaskRow, ...]:
    tasks_by_id: dict[str, Task] = {t.id: t for t in task_io.load_tasks(layout)}
    gated_ids = _gated_task_ids(state)
    rows: list[TaskRow] = []
    for task_id in sorted(state.tasks):
        task = tasks_by_id.get(task_id)
        status = state.tasks[task_id]
        checkpoint = _scratch_checkpoint(resume_module.scratch_path(layout, task_id))
        rows.append(
            TaskRow(
                id=task_id,
                status=status,
                expert=task.expert if task else "",
                tier=task.tier if task else "",
                critical=task.critical if task else False,
                gate_hint="gated" if task_id in gated_ids else "",
                checkpoint=checkpoint,
                depends_on=tuple(task.depends_on) if task else (),
            )
        )
    return tuple(rows)


def _counts_by_status(rows: tuple[TaskRow, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# pending summary
# ---------------------------------------------------------------------------


def _pending_summary(state: State, rows: tuple[TaskRow, ...]) -> str:
    if state.phase == "done":
        return "complete"
    if state.gate == "pending" and state.review_stage:
        return f"awaiting {state.review_stage} approval"
    if state.gate == "escalation":
        open_ids = sorted(rid for rid, s in state.escalations.items() if s == "pending")
        return f"awaiting escalation approval: {', '.join(open_ids)}"
    if state.gate == "reconcile":
        return f"awaiting reconcile: {state.halt_reason or ''}"
    if state.gate == "blocked":
        return f"blocked: {state.halt_reason or ''}"
    active = sum(1 for r in rows if r.status not in ("done", "split"))
    return f"phase {state.phase}, {active} active"


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def read_status(layout: RunLayout, *, event_tail: int = DEFAULT_EVENT_TAIL) -> RunStatus:
    """Pure read over the run dir. Never writes anything."""
    try:
        state = load_state(layout)
    except FileNotFoundError as exc:
        raise StatusUnavailable(f"no run state at {layout.state_file}") from exc

    rows = _build_task_rows(layout, state)
    # `attached` is added by a later control slice; read defensively so this
    # slice has no hard dependency on that field existing yet.
    attached = bool(getattr(state, "attached", False))
    open_escalations = tuple(
        sorted(rid for rid, status in state.escalations.items() if status == "pending")
    )
    return RunStatus(
        run_id=layout.run_id,
        phase=state.phase,
        review_stage=state.review_stage,
        gate=state.gate,
        halt_reason=state.halt_reason,
        adapter=state.adapter,
        updated_at=state.updated_at,
        attached=attached,
        tasks=rows,
        recent_events=_read_recent_events(layout, event_tail),
        counts=_counts_by_status(rows),
        escalations=open_escalations,
        pending_summary=_pending_summary(state, rows),
    )


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------


def render_plain(status: RunStatus) -> str:
    lines = [
        f"run:          {status.run_id}",
        f"phase:        {status.phase}",
        f"review_stage: {status.review_stage}",
        f"gate:         {status.gate}",
        f"halt_reason:  {status.halt_reason}",
        f"adapter:      {status.adapter}",
        f"updated_at:   {status.updated_at}",
        f"attached:     {status.attached}",
        f"pending:      {status.pending_summary}",
        f"counts:       {status.counts}",
    ]
    if status.escalations:
        lines.append(f"escalations:  {', '.join(status.escalations)}")
    lines.append("")
    lines.append("tasks:")
    if not status.tasks:
        lines.append("  (none)")
    for row in status.tasks:
        gate_suffix = f" [{row.gate_hint}]" if row.gate_hint else ""
        lines.append(
            f"  {row.id:<12} {row.expert:<10} {row.status:<18}{gate_suffix}  {row.checkpoint}"
        )
    if status.recent_events:
        lines.append("")
        lines.append("recent events:")
        for event in status.recent_events:
            lines.append(f"  {event.ts}  {event.kind:<18} {event.summary}")
    return "\n".join(lines) + "\n"


def to_json(status: RunStatus) -> str:
    return json.dumps(dataclasses.asdict(status), indent=2, sort_keys=True)
