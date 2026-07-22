"""The spine (ticket 002): a hard-coded phase-transition table + thin
bookkeeping. All reasoning lives in the adapter/prompt (bitter-lesson
posture) — this module only sequences phases, parses phase JSON, and
persists state atomically.

Stateless-per-iteration: `drive()` reloads `state.json` from disk at the top
of every loop turn, advances exactly one phase, persists, and only returns
once the loop reaches a halt (`gate=blocked`, a review/clarification/signoff
wait) or `done`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

from ads._literal import validate_literal
from ads.adapters.base import Adapter
from ads.config import load_base, load_optional, render_phase
from ads.layout import RunLayout
from ads.phase_json import PlanPayload, TaskPayload, parse_execute_handoff, parse_plan_payload
from ads.prompt import compose
from ads.state import State, append_event, describe_halt, halt, load_state, save_state
from ads.task_io import load_tasks, write_task
from ads.tasks import (
    EXIT_CRITERION_CHECKS,
    CycleError,
    ExitCriterion,
    ExitCriterionCheck,
    Task,
    TaskParseError,
    check_acyclic,
    ready_batch,
)
from ads.validate import check_leaf_judgment, evaluate_task, owns_diff

ATTEMPTS_CEILING = 3
PLAN_ATTEMPTS_KEY = "__plan__"


def _git_head(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _tasks_from_payload(payloads: list[TaskPayload]) -> list[Task]:
    tasks: list[Task] = []
    for tp in payloads:
        criteria = [
            ExitCriterion(
                check=cast(
                    ExitCriterionCheck,
                    validate_literal(
                        c["check"],
                        EXIT_CRITERION_CHECKS,
                        field="exit_criteria.check",
                        error=TaskParseError,
                    ),
                ),
                value=c["value"],
            )
            for c in tp.get("exit_criteria", [])
        ]
        tasks.append(
            Task(
                id=tp["id"],
                status="pending",
                depends_on=list(tp.get("depends_on", [])),
                owns=list(tp.get("owns", [])),
                exit_criteria=criteria,
                body=tp.get("body", ""),
            )
        )
    return tasks


def _advance_intake(layout: RunLayout, state: State) -> State:
    if not layout.intent.exists():
        msg = "intake requires layout.intent to already be written (cli 'start' writes it)"
        raise RuntimeError(msg)
    append_event(layout, state, "run:start")
    state.phase = "plan"
    append_event(layout, state, "phase:enter", data={"phase": "plan"})
    save_state(layout, state)
    return state


def _advance_plan(
    layout: RunLayout, state: State, adapter: Adapter, on_event: Callable[[str], None] | None
) -> State:
    # ticket 006 freeze-approved-upstream: a design-stage reject bounces here
    # with review_stage still "design" — that is this module's signal the
    # spec is already user-approved and must not be regenerated.
    design_frozen = state.review_stage == "design"
    intent_text = layout.intent.read_text(encoding="utf-8")
    task_body = render_phase(layout.config, "plan", {"intent": intent_text})
    prompt = compose(base=load_base(layout.config), expert_body="", design="", task_body=task_body)
    result = adapter.run(prompt, layout.repo, role="planning", on_event=on_event)
    payload: PlanPayload = parse_plan_payload(result.text)

    gap = payload.get("gap")
    if gap is not None and gap.get("ambiguous"):
        state.question = gap.get("question", "")
        append_event(
            layout, state, "gate_open", data={"gate": "clarification", "question": state.question}
        )
        save_state(layout, state)
        return state
    if gap is not None and not gap.get("ambiguous"):
        append_event(layout, state, "gap_decided", data={"decision": gap.get("decision", "")})
    state.question = None

    tasks = _tasks_from_payload(payload["tasks"])
    try:
        check_acyclic(tasks)
        check_leaf_judgment(tasks)
    except (CycleError, TaskParseError) as exc:
        return halt(layout, state, f"invalid plan graph: {exc}")

    layout.scaffold()
    if not design_frozen:
        layout.spec.write_text(payload["spec"], encoding="utf-8")
    design_text = payload.get("design")
    if design_text:
        layout.design.write_text(design_text, encoding="utf-8")
    for task in tasks:
        write_task(layout, task)

    state.tasks = {task.id: task.status for task in tasks}
    state.phase = "review"
    state.review_stage = "design" if design_frozen else ("spec" if design_text else None)
    append_event(
        layout, state, "plan:done", data={"task_count": len(tasks), "has_design": bool(design_text)}
    )
    save_state(layout, state)
    return state


def _reset_active(layout: RunLayout, state: State) -> None:
    """A task file left `active` on disk means a prior iteration crashed
    mid-run: reset it to `pending` so `ready_batch` picks it up again — the
    resume-after-crash guarantee, generalized to the on-disk task status."""
    changed = False
    for task in load_tasks(layout):
        if task.status == "active":
            task.status = "pending"
            write_task(layout, task)
            state.tasks[task.id] = "pending"
            changed = True
    if changed:
        save_state(layout, state)


def _run_task(
    layout: RunLayout,
    state: State,
    adapter: Adapter,
    task: Task,
    on_event: Callable[[str], None] | None,
) -> State:
    baseline = _git_head(layout.repo)
    task.status = "active"
    write_task(layout, task)
    state.tasks[task.id] = "active"
    state.cursor = task.id
    append_event(layout, state, "task:start", task=task.id)
    save_state(layout, state)

    task_body = render_phase(
        layout.config,
        "execute",
        {"task_id": task.id, "owns": ", ".join(task.owns), "task": task.body},
    )
    prompt = compose(
        base=load_base(layout.config),
        expert_body="",
        design=load_optional(layout.design),
        task_body=task_body,
        spec=load_optional(layout.spec),
    )
    result = adapter.run(prompt, layout.repo, role="execution", on_event=on_event)
    handoff = parse_execute_handoff(result.text)
    blocking = [issue["desc"] for issue in handoff["issues"] if issue.get("blocking")]
    append_event(
        layout,
        state,
        "task:done",
        task=task.id,
        data={
            "status": handoff["status"],
            "undone_n": len(handoff["undone"]),
            "issues_n": len(handoff["issues"]),
        },
    )
    if blocking:
        task.status = "pending"
        write_task(layout, task)
        state.tasks[task.id] = "pending"
        state.cursor = None
        return halt(layout, state, "; ".join(blocking))

    diff_text = owns_diff(layout.repo, baseline, task.owns)
    tv = evaluate_task(layout, adapter, task, diff_text=diff_text)
    append_event(layout, state, "validate:verdict", task=task.id, data={"pass": tv.passed})

    if not tv.passed:
        task.status = "pending"
        write_task(layout, task)
        state.tasks[task.id] = "pending"
        state.cursor = None
        attempts = state.attempts.get(task.id, 0) + 1
        state.attempts[task.id] = attempts
        if attempts >= ATTEMPTS_CEILING:
            msg = f"{task.id} exceeded validation attempts ceiling ({ATTEMPTS_CEILING})"
            return halt(layout, state, msg)
        state.phase = "execute"
        save_state(layout, state)
        return state

    task.status = "done"
    write_task(layout, task)
    state.tasks[task.id] = "done"
    state.cursor = None
    all_tasks = load_tasks(layout)
    if all(t.status == "done" for t in all_tasks):
        state.phase = "validate"  # awaiting_signoff: describe_halt catches this next iteration
        append_event(layout, state, "gate_open", data={"gate": "signoff"})
    else:
        state.phase = "execute"
    save_state(layout, state)
    return state


def _advance_execute(
    layout: RunLayout, state: State, adapter: Adapter, on_event: Callable[[str], None] | None
) -> State:
    _reset_active(layout, state)
    tasks = load_tasks(layout)
    batch = ready_batch(tasks)
    if not batch:
        if tasks and all(t.status == "done" for t in tasks):
            state.phase = "validate"
            state.cursor = None
            save_state(layout, state)
            return state
        return halt(layout, state, "no ready task but tasks remain undone (dependency stall)")
    return _run_task(layout, state, adapter, batch[0], on_event)


def drive(
    layout: RunLayout, adapter: Adapter, *, on_event: Callable[[str], None] | None = None
) -> State:
    """Run iterations until a halt/gate/awaiting state or `done`, then return.
    Public entry point; every verb that "re-drains" the loop calls this."""
    while True:
        state = load_state(layout)
        if state.phase == "done":
            return state
        if describe_halt(state) is not None:
            return state
        if state.phase == "intake":
            state = _advance_intake(layout, state)
        elif state.phase == "plan":
            state = _advance_plan(layout, state, adapter, on_event)
        elif state.phase == "execute":
            state = _advance_execute(layout, state, adapter, on_event)
        else:
            raise AssertionError(f"unreachable phase in drive(): {state.phase!r}")
        if describe_halt(state) is not None or state.phase == "done":
            return state
