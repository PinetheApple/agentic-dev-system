"""Escalation state machine (ticket 011 dec 6 + dec 7 routing): the ONE gate
an agent can never bypass on its own.

Containment (011) says an agent can never self-grant a privileged/outward
op — there is no in-jail capability for that. Instead it emits a structured
request and exits a terminal `needs-escalation` status; a human approves via
the driver command language; and the driver — never the agent — performs the
op. Two triggers land here, sharing one lifecycle/gate/CLI:

- **agent-request** — a dispatch `run()` returns `status: "needs-escalation"`
  with a structured request (an outward op the agent wants: push/PR/publish/
  etc). Wired in `ads/dispatch.py`'s `_apply_run_result`.
- **cmd-flagged** — a task `cmd` exit-criterion whose command
  `ads/sandbox.py`'s `classify_cmd` flags is NOT auto-run; it's routed here
  for a human to approve before it executes (still inside the sandbox on
  approval — the jail is always the boundary, approval only clears the
  classifier flag). Wired in `ads/dispatch.py`'s `_gate_and_route`, via
  `screen_cmd` below, which is the single choke point deciding whether a
  `cmd` needs escalation.

Request bodies are human-readable markdown under `escalations_dir`
(`<id>.md`: a flat frontmatter block + a prose `## Reason` + a fenced
`## Exact` block); `state.escalations` is only the open-set cursor (request
id -> status) the loop actually reads, mirroring how task bodies live on
disk while `state.tasks` is the cursor.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

from ads import sandbox
from ads._literal import validate_literal
from ads.layout import RunLayout
from ads.state import State, append_event, save_state
from ads.task_io import load_tasks, write_task
from ads.tasks import Task

EscalationKind = Literal["agent-request", "cmd-flagged"]
ESCALATION_KINDS: tuple[EscalationKind, ...] = get_args(EscalationKind)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

_FRONTMATTER_DELIM = "---"
_REASON_HEADING = "## Reason"
_EXACT_HEADING = "## Exact"
_FENCE = "```"


class EscalationRaised(Exception):
    """Raised the moment a request is opened (either trigger); mirrors
    `ads/resplit.py`'s `ResplitDepthExceeded` — propagates up to the
    dispatch call sites, which catch it and halt to the `escalation` gate
    instead of treating the task as done/blocked."""

    def __init__(self, request_id: str) -> None:
        super().__init__(f"escalation requested: {request_id}")
        self.request_id = request_id


@dataclass(frozen=True)
class EscalationRequest:
    id: str
    task_id: str
    kind: EscalationKind
    op: str
    target: str
    reason: str
    exact: str
    status: str


# ---------------------------------------------------------------------------
# body I/O (hand-rolled, like ads/tasks.py's frontmatter parser — kept
# deliberately simple: flat scalar frontmatter + two known prose sections)
# ---------------------------------------------------------------------------


def _request_path(layout: RunLayout, request_id: str) -> Path:
    return layout.escalations_dir / f"{request_id}.md"


def _write_request(layout: RunLayout, request: EscalationRequest) -> None:
    layout.escalations_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        _FRONTMATTER_DELIM,
        f"id: {request.id}",
        f"task_id: {request.task_id}",
        f"kind: {request.kind}",
        f"op: {request.op}",
        f"target: {request.target}",
        f"status: {request.status}",
        _FRONTMATTER_DELIM,
        "",
        _REASON_HEADING,
        "",
        request.reason,
        "",
        _EXACT_HEADING,
        "",
        _FENCE,
        request.exact,
        _FENCE,
        "",
    ]
    _request_path(layout, request.id).write_text("\n".join(lines), encoding="utf-8")


def _extract_section(lines: list[str], start: str, end: str | None) -> str:
    try:
        start_idx = lines.index(start) + 1
    except ValueError:
        return ""
    end_idx = len(lines)
    if end is not None:
        try:
            end_idx = lines.index(end, start_idx)
        except ValueError:
            end_idx = len(lines)
    return "\n".join(lines[start_idx:end_idx]).strip()


def _extract_fenced(lines: list[str], heading: str) -> str:
    try:
        start_idx = lines.index(heading)
    except ValueError:
        return ""
    fences = [i for i in range(start_idx, len(lines)) if lines[i].strip() == _FENCE]
    if len(fences) < 2:
        return ""
    return "\n".join(lines[fences[0] + 1 : fences[1]])


def load_request(layout: RunLayout, request_id: str) -> EscalationRequest:
    text = _request_path(layout, request_id).read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        raise ValueError(f"escalation {request_id}: malformed body (missing frontmatter)")
    front: dict[str, str] = {}
    idx = 1
    while idx < len(lines) and lines[idx].strip() != _FRONTMATTER_DELIM:
        key, _, value = lines[idx].partition(":")
        front[key.strip()] = value.strip()
        idx += 1
    body_lines = lines[idx + 1 :]
    reason = _extract_section(body_lines, _REASON_HEADING, _EXACT_HEADING)
    exact = _extract_fenced(body_lines, _EXACT_HEADING)
    kind = cast(
        EscalationKind, validate_literal(front.get("kind", ""), ESCALATION_KINDS, field="kind")
    )
    return EscalationRequest(
        id=front.get("id", request_id),
        task_id=front.get("task_id", ""),
        kind=kind,
        op=front.get("op", ""),
        target=front.get("target", ""),
        reason=reason,
        exact=exact,
        status=front.get("status", STATUS_PENDING),
    )


def _set_status(layout: RunLayout, request_id: str, status: str) -> EscalationRequest:
    request = dataclasses.replace(load_request(layout, request_id), status=status)
    _write_request(layout, request)
    return request


# ---------------------------------------------------------------------------
# open / list
# ---------------------------------------------------------------------------


def _next_request_id(task_id: str, state: State) -> str:
    n = 1
    while f"esc-{task_id}-{n}" in state.escalations:
        n += 1
    return f"esc-{task_id}-{n}"


def open_request(
    layout: RunLayout,
    state: State,
    *,
    task_id: str,
    kind: EscalationKind,
    op: str,
    target: str,
    reason: str,
    exact: str,
) -> EscalationRequest:
    request_id = _next_request_id(task_id, state)
    request = EscalationRequest(
        id=request_id,
        task_id=task_id,
        kind=kind,
        op=op,
        target=target,
        reason=reason,
        exact=exact,
        status=STATUS_PENDING,
    )
    _write_request(layout, request)
    state.escalations[request_id] = STATUS_PENDING
    append_event(
        layout, "escalation_open", id=request_id, task_id=task_id, escalation_kind=kind, op=op
    )
    return request


def list_open(state: State) -> list[str]:
    return sorted(rid for rid, status in state.escalations.items() if status == STATUS_PENDING)


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------


def _find_task(layout: RunLayout, task_id: str) -> Task | None:
    return next((t for t in load_tasks(layout) if t.id == task_id), None)


def _resume_task(layout: RunLayout, state: State, task: Task | None, task_id: str) -> None:
    """Shared by both `approve` kinds: the outward op (or the now-approved
    cmd) is settled, so the owning task goes back to `pending` for a
    re-dispatch and the escalation gate clears."""
    if task is not None:
        task.status = "pending"
        write_task(layout, task)
    state.tasks[task_id] = "pending"
    state.gate = None


def approve(layout: RunLayout, state: State, request_id: str) -> EscalationRequest:
    if state.escalations.get(request_id) != STATUS_PENDING:
        raise ValueError(f"escalation {request_id!r} is not pending")

    request = _set_status(layout, request_id, STATUS_APPROVED)
    state.escalations[request_id] = STATUS_APPROVED
    append_event(
        layout,
        "escalation_approve",
        id=request_id,
        task_id=request.task_id,
        escalation_kind=request.kind,
    )
    save_state(layout, state)

    task = _find_task(layout, request.task_id)

    if request.kind == "cmd-flagged":
        if request.exact not in state.approved_cmds:
            state.approved_cmds.append(request.exact)
        _resume_task(layout, state, task, request.task_id)
        save_state(layout, state)
        return request

    # agent-request: the driver — never the agent — performs the outward op.
    # `perform_outward_op` is a documented seam; it raises until a later
    # slice fills it in. If it raises, the request stays "approved" on disk
    # (a human did approve it) but the owning task is NOT resumed and the
    # gate stays parked on `escalation` — the caller sees the seam directly.
    perform_outward_op(layout, request)
    _resume_task(layout, state, task, request.task_id)
    save_state(layout, state)
    return request


def reject(layout: RunLayout, state: State, request_id: str, reason: str) -> EscalationRequest:
    if state.escalations.get(request_id) != STATUS_PENDING:
        raise ValueError(f"escalation {request_id!r} is not pending")

    request = _set_status(layout, request_id, STATUS_REJECTED)
    state.escalations[request_id] = STATUS_REJECTED
    append_event(layout, "escalation_reject", id=request_id, task_id=request.task_id, reason=reason)

    task = _find_task(layout, request.task_id)
    if task is not None:
        task.status = "blocked"
        write_task(layout, task)
        scratch_path = layout.scratch_dir / f"{task.id}.md"
        with scratch_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## Escalation rejected ({request.id})\n\n{reason}\n")
    state.tasks[request.task_id] = "blocked"

    # Mirror ads/driver.py's `reject` (review gate): rejecting blocks the
    # task. Only surface the run's terminal state as `blocked` once no other
    # escalation is still open — otherwise leave the gate parked exactly as
    # it was so the loop keeps waiting on the remaining pending requests.
    if not list_open(state):
        state.gate = "blocked"
        state.halt_reason = f"escalation {request.id} rejected: {reason}"
    save_state(layout, state)
    return request


# ---------------------------------------------------------------------------
# outward-op executor — documented seam (dec-6 fog)
# ---------------------------------------------------------------------------


def perform_outward_op(layout: RunLayout, request: EscalationRequest) -> None:
    """Documented seam (ticket 011 dec-6 outward-op executor fog): this is
    where the driver — from a 007-validated branch, never the agent, and
    only after a human `escalate-approve` — would perform `request`'s
    genuinely-outward, irreversible action (git push / open a PR / publish a
    package / cut a tag), using `request.op`/`request.target`/`request.exact`
    as the plan an agent could never self-execute inside the jail.

    Left unimplemented: it needs real remotes, credentials, and side-effects
    genuinely out of scope for this slice. The full request lifecycle (open
    -> pending -> approve/reject) and the gate/CLI plumbing around it are
    real and tested up to exactly this boundary — this is a seam, not a
    silent no-op or a TODO.
    """
    raise NotImplementedError(
        f"perform_outward_op: driver-brokered outward op {request.op!r} targeting "
        f"{request.target!r} (escalation {request.id}) is not implemented in this "
        "slice — see this function's docstring"
    )


# ---------------------------------------------------------------------------
# cmd-flagged screen (dec 7): the single choke point deciding whether a
# `cmd` exit-criterion needs escalation before it runs.
# ---------------------------------------------------------------------------


def screen_cmd(command: str, approved_cmds: list[str]) -> tuple[bool, tuple[str, ...]]:
    """`(flagged, reasons)`. A previously-approved exact command is never
    re-flagged — that's the whole point of `state.approved_cmds` — otherwise
    delegates straight to `ads/sandbox.py`'s `classify_cmd`."""
    if command in approved_cmds:
        return False, ()
    verdict = sandbox.classify_cmd(command)
    return verdict.flagged, verdict.reasons
