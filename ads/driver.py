"""Phase state machine (ticket 001): intake -> plan -> review -> dispatch ->
validate -> done, with two retry-bounded backward edges (review->plan,
validate->dispatch). state.json is the only thing this module reads to decide
what to do next; events.jsonl is write-only audit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ads.adapters.base import Adapter, RunResult, TaskPayload
from ads.config import Config
from ads.layout import RunLayout
from ads.prompt import compose
from ads.state import State, append_event, load_state, save_state
from ads.tasks import (
    CycleError,
    ExitCriterion,
    Task,
    check_acyclic,
    parse_task,
    ready_batch,
    serialize_task,
)

MAX_RETRIES = 2
REVIEW_TO_PLAN = "review_to_plan"
VALIDATE_TO_DISPATCH = "validate_to_dispatch"


class DriverError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# run lifecycle
# ---------------------------------------------------------------------------


def start_run(layout: RunLayout, user_input: str) -> State:
    """Intake: thin verbatim copy of user input -> intent.md. No LLM call."""
    layout.scaffold()
    layout.intent.write_text(user_input.rstrip("\n") + "\n", encoding="utf-8")
    state = State(phase="plan")
    save_state(layout, state)
    append_event(layout, "intake", chars=len(user_input))
    layout.link_current()
    return state


def advance(layout: RunLayout, cfg: Config, adapter: Adapter) -> State:
    """Perform exactly one phase-step and persist the result."""
    state = load_state(layout)
    if state.gate is not None or state.phase == "done":
        return state
    if state.phase == "plan":
        return _run_plan(layout, cfg, adapter, state)
    if state.phase == "dispatch":
        return _run_dispatch(layout, cfg, adapter, state)
    if state.phase == "validate":
        return _run_validate(layout, cfg, adapter, state)
    if state.phase == "review":
        return state  # gate is None here only transiently; nothing to do
    raise DriverError(f"unknown phase {state.phase!r}")


def run_until_halt(layout: RunLayout, cfg: Config, adapter: Adapter) -> State:
    """Loop advance() until a gate stops us or the run is done."""
    state = load_state(layout)
    while state.phase != "done" and state.gate is None:
        state = advance(layout, cfg, adapter)
    return state


# ---------------------------------------------------------------------------
# review gate (ticket 008)
# ---------------------------------------------------------------------------


def approve(layout: RunLayout) -> State:
    state = load_state(layout)
    if state.phase != "review" or state.gate != "pending":
        raise DriverError("nothing awaiting approval")
    if state.review_stage == "spec":
        state.review_stage = "design"
        state.gate = "pending"
    elif state.review_stage == "design":
        state.phase = "dispatch"
        state.review_stage = None
        state.gate = None
    save_state(layout, state)
    append_event(layout, "approve", review_stage=state.review_stage, phase=state.phase)
    return state


def reject(layout: RunLayout, reason: str) -> State:
    state = load_state(layout)
    if state.phase != "review" or state.gate != "pending":
        raise DriverError("nothing awaiting review")
    target = layout.spec if state.review_stage == "spec" else layout.design
    with target.open("a", encoding="utf-8") as fh:
        fh.write(f"\n\n## Review Notes\n\n{reason}\n")

    count = state.retry_counts.get(REVIEW_TO_PLAN, 0) + 1
    if count > MAX_RETRIES:
        state.gate = "blocked"
        state.halt_reason = f"{REVIEW_TO_PLAN} retries exhausted"
    else:
        state.retry_counts[REVIEW_TO_PLAN] = count
        # freeze-approved-upstream: rejecting the design stage never
        # regenerates the already-approved spec.
        state.replan_scope = "design" if state.review_stage == "design" else None
        state.phase = "plan"
        state.review_stage = None
        state.gate = None
    save_state(layout, state)
    append_event(layout, "reject", reason=reason, retries=count)
    return state


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def _run_plan(layout: RunLayout, cfg: Config, adapter: Adapter, state: State) -> State:
    spec_frozen = state.replan_scope == "design"
    intent_text = layout.intent.read_text(encoding="utf-8")
    task_body = cfg.phases["plan"].body.replace("{intent}", intent_text)
    plan_expert = cfg.experts["plan"]
    prompt = compose(cfg.base, plan_expert.body, design="", task_body=task_body)

    allowed_tools = list(plan_expert.tools) if plan_expert.tools else None
    result = adapter.run(prompt, cwd=layout.repo, allowed_tools=allowed_tools, tier="standard")
    if result.exit_status != "ok" or not result.structured:
        return _halt(layout, state, f"plan run failed: {result.text[:200]}")

    payload = result.structured
    try:
        raw_tasks = payload.get("tasks")
        if raw_tasks is None:
            raise KeyError("tasks")
        task_objs = _tasks_from_payload(raw_tasks)
        check_acyclic(task_objs)
    except (KeyError, TypeError, CycleError) as exc:
        return _halt(layout, state, f"plan output invalid: {exc}")

    if not spec_frozen:
        spec_text = payload.get("spec")
        if spec_text is None:
            raise KeyError("plan payload missing 'spec'")
        layout.spec.write_text(spec_text, encoding="utf-8")
    design_text = payload.get("design")
    if design_text is None:
        raise KeyError("plan payload missing 'design'")
    layout.design.write_text(design_text, encoding="utf-8")
    _clear_dir(layout.tasks_dir)
    for task in task_objs:
        _write_task(layout, task)

    state.tasks = {t.id: t.status for t in task_objs}
    state.phase = "review"
    state.review_stage = "design" if spec_frozen else "spec"
    state.gate = "pending"
    state.replan_scope = None
    save_state(layout, state)
    append_event(layout, "plan", task_count=len(task_objs), spec_frozen=spec_frozen)
    return state


def _tasks_from_payload(raw_tasks: list[TaskPayload]) -> list[Task]:
    tasks: list[Task] = []
    for raw in raw_tasks:
        exit_criteria = [
            ExitCriterion(check=ec["check"], value=ec["value"])
            for ec in raw.get("exit_criteria", [])
        ]
        tasks.append(
            Task(
                id=raw["id"],
                status="pending",
                depends_on=list(raw.get("depends_on", [])),
                owns=list(raw.get("owns", [])),
                exit_criteria=exit_criteria,
                expert=raw.get("expert", ""),
                critical=bool(raw.get("critical", False)),
                tier=raw.get("tier", "standard"),
                parent=raw.get("parent"),
                body=raw.get("body", ""),
            )
        )
    return tasks


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def _run_dispatch(layout: RunLayout, cfg: Config, adapter: Adapter, state: State) -> State:
    all_tasks = _load_tasks(layout)
    check_acyclic(all_tasks)
    batch = ready_batch(all_tasks)

    if not batch:
        if all(t.status == "done" for t in all_tasks):
            state.phase = "validate"
            state.gate = None
            save_state(layout, state)
            append_event(layout, "dispatch_complete")
        else:
            return _halt(layout, state, "no ready tasks but some are not done (blocked deps?)")
        return state

    design_text = layout.design.read_text(encoding="utf-8")
    critical_blocked = False
    for task in batch:
        expert = cfg.experts.get(task.expert)
        expert_body = expert.body if expert else ""
        task_body = cfg.phases["dispatch"].body.replace("{task}", task.body)
        prompt = compose(cfg.base, expert_body, design_text, task_body)
        allowed_tools = list(expert.tools) if expert and expert.tools else None
        result = adapter.run(prompt, cwd=layout.repo, allowed_tools=allowed_tools, tier=task.tier)

        if (
            result.exit_status == "ok"
            and result.structured
            and result.structured.get("status") == "done"
        ):
            task.status = "done"
        else:
            task.status = "blocked"
            critical_blocked = critical_blocked or task.critical

        _write_task(layout, task)
        _write_scratch(layout, task, result)
        state.tasks[task.id] = task.status

    if critical_blocked:
        return _halt(layout, state, "a critical task blocked during dispatch")

    save_state(layout, state)
    append_event(layout, "dispatch_batch", task_ids=[t.id for t in batch])
    return state


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _run_validate(layout: RunLayout, cfg: Config, adapter: Adapter, state: State) -> State:
    all_tasks = _load_tasks(layout)
    failed_task_ids: list[str] = []

    for task in all_tasks:
        for criterion in task.exit_criteria:
            if not _check_criterion(layout, cfg, adapter, criterion):
                failed_task_ids.append(task.id)
                break

    if not failed_task_ids:
        state.phase = "done"
        state.gate = None
        save_state(layout, state)
        append_event(layout, "validate_pass")
        return state

    count = state.retry_counts.get(VALIDATE_TO_DISPATCH, 0) + 1
    if count > MAX_RETRIES:
        return _halt(layout, state, f"{VALIDATE_TO_DISPATCH} retries exhausted: {failed_task_ids}")

    state.retry_counts[VALIDATE_TO_DISPATCH] = count
    for task in all_tasks:
        if task.id in failed_task_ids:
            task.status = "pending"
            _write_task(layout, task)
            state.tasks[task.id] = "pending"
    state.phase = "dispatch"
    state.gate = None
    save_state(layout, state)
    append_event(layout, "validate_fail", task_ids=failed_task_ids, retries=count)
    return state


def _check_criterion(
    layout: RunLayout, cfg: Config, adapter: Adapter, criterion: ExitCriterion
) -> bool:
    if criterion.check == "cmd":
        proc = subprocess.run(
            criterion.value, shell=True, cwd=layout.repo, capture_output=True, text=True
        )
        return proc.returncode == 0
    if criterion.check == "judgment":
        task_body = cfg.phases["validate"].body.replace("{criterion}", criterion.value)
        critic = cfg.experts.get("critic")
        prompt = compose(cfg.base, critic.body if critic else "", "", task_body)
        allowed_tools = list(critic.tools) if critic and critic.tools else None
        result = adapter.run(prompt, cwd=layout.repo, allowed_tools=allowed_tools, tier="standard")
        return bool(result.structured and result.structured.get("pass") is True)
    return False


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _halt(layout: RunLayout, state: State, reason: str) -> State:
    state.gate = "blocked"
    state.halt_reason = reason
    save_state(layout, state)
    append_event(layout, "halt", reason=reason)
    return state


def _task_path(layout: RunLayout, task_id: str) -> Path:
    return layout.tasks_dir / f"{task_id}.md"


def _load_tasks(layout: RunLayout) -> list[Task]:
    return [
        parse_task(p.read_text(encoding="utf-8")) for p in sorted(layout.tasks_dir.glob("*.md"))
    ]


def _write_task(layout: RunLayout, task: Task) -> None:
    _task_path(layout, task.id).write_text(serialize_task(task), encoding="utf-8")


def _write_scratch(layout: RunLayout, task: Task, result: RunResult) -> None:
    scratch_path = layout.scratch_dir / f"{task.id}.md"
    scratch_path.write_text(
        f"# {task.id}\n\nstatus: {task.status}\n\n{result.text}\n", encoding="utf-8"
    )


def _clear_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.glob("*.md"):
        child.unlink()
