"""Ticket 010: the async control-verb substrate.

Hybrid, async-substrate-as-ground-truth: all intervention is command-files
drained at unit boundaries (008's cold `run()` mechanism, mirrored here by
`drain` being called once per driver-loop iteration, before `advance`).
Sync = block inline only when a human is already attached — that's
`State.attached`, not this module's concern.

A second terminal (the CLI's `pause`/`resume`/`redirect`/`edit`/`replan`/
`abort` subcommands) appends one JSON line per command to `control.jsonl` via
`enqueue`; the driver — whichever process currently owns the run loop —
drains everything past `state.control_cursor` at the next boundary via
`drain`. This is the same append-only-log + machine-owned-cursor shape as
`state.py`'s `events.jsonl`/`step_counts`, just readable instead of
write-only.

Four verbs (`pause`, `resume`, `redirect`, `edit`) are pure boundary-safe
file/state ops — they never touch a running process. `replan` is also
boundary-safe; it only sets a signal the driver acts on between phase steps.
`abort`'s graph bookkeeping (mark aborted, block dependents by construction)
is boundary-safe too; only a hard-kill of an *already in-flight* `run()`
needs a running-process seam — see `abort_inflight` below.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args

from ads.layout import RunLayout
from ads.state import State, append_event
from ads.task_io import write_task
from ads.tasks import Task

ControlVerb = Literal["pause", "resume", "redirect", "edit", "replan", "abort"]
CONTROL_VERBS: tuple[ControlVerb, ...] = get_args(ControlVerb)

_REDIRECT_HEADING = "## Operator redirect"


@dataclass(frozen=True)
class ControlCommand:
    verb: ControlVerb
    task_id: str = ""  # "" when not task-scoped (pause/resume/replan)
    note: str = ""  # only meaningful for redirect
    ts: str = ""


@dataclass(frozen=True)
class DrainResult:
    pause_requested: bool = False
    replan_requested: bool = False
    aborted_task_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# operator side: enqueue
# ---------------------------------------------------------------------------


def enqueue(layout: RunLayout, command: ControlCommand) -> None:
    """Append one command to `control.jsonl`. Append-only, like events —
    the operator side never mutates or truncates this file."""
    layout.root.mkdir(parents=True, exist_ok=True)
    line = {
        "verb": command.verb,
        "task_id": command.task_id,
        "note": command.note,
        "ts": command.ts or _now(),
    }
    with layout.control_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# driver side: read past the cursor, apply, advance the cursor
# ---------------------------------------------------------------------------


def _parse_line(raw: str) -> ControlCommand | None:
    """Returns `None` on a malformed line. Callers must still count a
    malformed line toward the cursor advance so a single bad line can never
    wedge the queue (permanently re-read the same bad line every boundary)."""
    try:
        raw_payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw_payload, dict):
        return None
    payload = cast(dict[str, Any], raw_payload)
    verb = payload.get("verb")
    if verb not in CONTROL_VERBS:
        return None
    return ControlCommand(
        verb=verb,
        task_id=str(payload.get("task_id", "")),
        note=str(payload.get("note", "")),
        ts=str(payload.get("ts", "")),
    )


def pending_commands(layout: RunLayout, state: State) -> list[ControlCommand]:
    """Commands after `state.control_cursor` (index into raw lines, not
    parsed commands — a malformed line still occupies a cursor slot)."""
    if not layout.control_log.exists():
        return []
    raw_lines = layout.control_log.read_text(encoding="utf-8").splitlines()
    unread = raw_lines[state.control_cursor :]
    commands: list[ControlCommand] = []
    for raw in unread:
        if not raw.strip():
            continue
        parsed = _parse_line(raw)
        if parsed is not None:
            commands.append(parsed)
    return commands


def _apply_pause(state: State, notes: list[str]) -> None:
    state.paused = True
    notes.append("paused")


def _apply_resume(state: State) -> list[str]:
    state.paused = False
    notes = ["resumed"]
    if state.gate == "paused":
        state.gate = None
        notes.append("cleared paused gate")
    return notes


def _apply_redirect(layout: RunLayout, command: ControlCommand, notes: list[str]) -> None:
    """005-firewall-safe injection: append the operator's note into the
    task's scratch file under a dedicated heading, the same
    append-not-overwrite shape `task_io.write_scratch`/escalation's rejection
    note use, so it's read-set-friendly for the next dispatch/resume."""
    if not command.task_id:
        notes.append("redirect skipped: no task_id")
        return
    scratch_path = layout.scratch_dir / f"{command.task_id}.md"
    scratch_path.parent.mkdir(parents=True, exist_ok=True)
    with scratch_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n{_REDIRECT_HEADING}\n\n{command.note}\n")
    notes.append(f"redirected {command.task_id}: {command.note}")


def _apply_edit(state: State, command: ControlCommand, notes: list[str]) -> None:
    """Only meaningful for a not-yet-dispatched task: pause so the human can
    edit `tasks/<task>.md` in place; a later `resume` reloads it for free
    (the driver already re-reads task files each dispatch boundary)."""
    state.paused = True
    notes.append(f"edit requested for {command.task_id}: paused for in-place edit")


def _apply_abort(
    layout: RunLayout,
    state: State,
    all_tasks: list[Task],
    command: ControlCommand,
    notes: list[str],
) -> str | None:
    """Graph bookkeeping only (boundary-safe, built): mark the task
    `aborted` on disk and in `state.tasks`. Dependents never become ready
    because `ready_batch` requires deps to reach `done` — abort blocks them
    by construction, no extra bookkeeping needed here.

    Also calls the documented `abort_inflight` seam for the in-flight
    hard-kill case; see that function's docstring."""
    task = next((t for t in all_tasks if t.id == command.task_id), None)
    if task is None:
        notes.append(f"abort skipped: unknown task {command.task_id!r}")
        return None
    task.status = "aborted"
    write_task(layout, task)
    state.tasks[task.id] = "aborted"
    abort_inflight(layout, state, task.id)
    notes.append(f"aborted {task.id}")
    return task.id


def abort_inflight(layout: RunLayout, state: State, task_id: str) -> None:
    """Documented seam (ticket 011 fog): hard-killing a currently-running
    `run()` needs the adapter to launch each `run()` inside a named 011
    `systemd-run --user --scope` unit (`ads-<run_id>-<task_id>.scope`) this
    function would `systemctl --user stop`, sharing 011's resource-cap
    `killed` teardown path. Not wired: adapters don't name their scopes yet.

    Since dispatch is synchronous within a boundary today, a boundary-drain
    abort of an already-finished-or-not-yet-started task is fully handled by
    `_apply_abort`'s bookkeeping alone; only a *concurrently executing*
    parallel task (ticket 006's isolated-dispatch thread pool) would ever
    need this hard-kill. Deliberately a safe no-op-plus-warning, never a
    raise, so an abort command can't crash a real run just because the seam
    isn't wired yet.
    """
    print(
        f"warning: abort_inflight seam not wired (ticket 011 fog) — "
        f"{task_id!r} in run {state.adapter!r}@{layout.run_id} was only "
        "bookkept as aborted, not hard-killed if it was mid-flight",
        file=sys.stderr,
    )


def drain(layout: RunLayout, state: State, all_tasks: list[Task]) -> DrainResult:
    """Apply every pending command in order, advance `state.control_cursor`
    to the total raw line count, and return the signals the driver acts on.
    Pure-ish w.r.t. control flow: mutates file/state effects but never halts
    or changes `state.phase`/`state.gate` itself beyond what individual verb
    effects need (resume clearing the `paused` gate) — the driver owns
    halting/phase transitions."""
    if not layout.control_log.exists():
        return DrainResult()
    raw_lines = layout.control_log.read_text(encoding="utf-8").splitlines()
    unread = raw_lines[state.control_cursor :]

    pause_requested = False
    replan_requested = False
    aborted_task_ids: list[str] = []
    notes: list[str] = []

    for raw in unread:
        if raw.strip():
            command = _parse_line(raw)
            if command is not None:
                if command.verb == "pause":
                    _apply_pause(state, notes)
                    pause_requested = True
                elif command.verb == "resume":
                    notes.extend(_apply_resume(state))
                elif command.verb == "redirect":
                    _apply_redirect(layout, command, notes)
                elif command.verb == "edit":
                    _apply_edit(state, command, notes)
                elif command.verb == "abort":
                    aborted_id = _apply_abort(layout, state, all_tasks, command, notes)
                    if aborted_id is not None:
                        aborted_task_ids.append(aborted_id)
                elif command.verb == "replan":
                    replan_requested = True
                    notes.append("replan requested")
                append_event(
                    layout,
                    "control_apply",
                    verb=command.verb,
                    task=command.task_id,
                )
        # malformed or blank lines still count toward the cursor advance —
        # a bad line can never wedge the queue.

    state.control_cursor = len(raw_lines)
    return DrainResult(
        pause_requested=pause_requested,
        replan_requested=replan_requested,
        aborted_task_ids=tuple(aborted_task_ids),
        notes=tuple(notes),
    )
