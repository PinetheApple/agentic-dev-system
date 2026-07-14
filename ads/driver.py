"""Phase state machine (ticket 001): intake -> plan -> review -> dispatch ->
validate -> done, with two retry-bounded backward edges (review->plan,
validate->dispatch). state.json is the only thing this module reads to decide
what to do next; events.jsonl is write-only audit.
"""

from __future__ import annotations

from ads import control, dispatch, resplit, validate
from ads.activity import run_with_activity
from ads.adapters.base import Adapter, TaskPayload
from ads.config import Config
from ads.layout import RunLayout
from ads.prompt import compose
from ads.state import State, append_event, load_state, save_state
from ads.state import halt as _halt
from ads.task_io import clear_dir, load_tasks, write_task
from ads.tasks import CycleError, ExitCriterion, Task, check_acyclic

MAX_RETRIES = 2
REVIEW_TO_PLAN = "review_to_plan"
VALIDATE_INTEGRATION = "validate_integration"
MISSING_WORK = "missing_work"


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
    """Loop advance() until a gate stops us or the run is done.

    Ticket 010: at the TOP of each iteration (the unit boundary), drains
    ads/control.py's command queue and acts on the result before taking
    another phase-step — the async-substrate-as-ground-truth control model.
    """
    state = load_state(layout)
    # The `paused` gate is special: unlike every other gate it must still be
    # re-checked on entry (via drain, below) because only a drained `resume`
    # command can clear it — every other gate needs an explicit human
    # approve/reject/reconcile action outside this loop.
    while state.phase != "done" and (state.gate is None or state.gate == "paused"):
        state = _drain_control(layout, state)
        if state.gate is not None or state.phase == "done":
            break
        if state.paused:
            state = _halt(layout, state, "paused by operator", gate="paused")
            break
        state = advance(layout, cfg, adapter)
    return state


def _drain_control(layout: RunLayout, state: State) -> State:
    """Apply every pending control command, then act on the drain signals:
    `replan_requested` loops the phase back to `plan` (mirrors `reject`'s
    loopback shape); `pause_requested` is left for the caller (`paused` is
    already set on `state` by the drain itself, checked by the caller right
    after this returns so a pause command lands before the next phase-step
    even within one boundary)."""
    all_tasks = load_tasks(layout)
    result = control.drain(layout, state, all_tasks)
    save_state(layout, state)
    if result.replan_requested:
        state.phase = "plan"
        state.review_stage = None
        state.gate = None
        state.replan_scope = None
        save_state(layout, state)
        append_event(layout, "control_replan")
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
    result = run_with_activity(
        adapter,
        layout,
        state,
        label="plan",
        kind="plan",
        prompt=prompt,
        cwd=layout.repo,
        allowed_tools=allowed_tools,
        tier="standard",
    )
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
    clear_dir(layout.tasks_dir)
    for task in task_objs:
        write_task(layout, task)

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
# dispatch (ticket 006 dispatch strategies live in ads/dispatch.py)
# ---------------------------------------------------------------------------


def _run_dispatch(layout: RunLayout, cfg: Config, adapter: Adapter, state: State) -> State:
    all_tasks = load_tasks(layout)
    check_acyclic(all_tasks)
    return dispatch.run(layout, cfg, adapter, state, all_tasks)


# ---------------------------------------------------------------------------
# validate (ticket 007 + 006+007 integration: per-task `cmd`/`judgment` gates
# now run pre-merge in ads/dispatch.py — a task never merges dirty — so this
# phase is purely the post-merge integration critic: the one deliberately
# cross-task check that needs every task already merged. See ads/validate.py
# for the gate mechanics; this is only the retry-bounded state machine
# around the integration critic, mirroring the review gate's
# evaluator-optimizer shape below)
# ---------------------------------------------------------------------------


def _run_validate(layout: RunLayout, cfg: Config, adapter: Adapter, state: State) -> State:
    all_tasks = load_tasks(layout)
    integration = validate.run_integration_critic(layout, cfg, adapter, state=state)
    validate.write_report(layout, [], integration=integration)

    if not integration.passed:
        attributed = validate.attribute_paths(all_tasks, integration.cited_paths)
        if not attributed:
            return _handle_missing_work(layout, state, all_tasks, integration)
        for task_id in attributed:
            validate.write_integration_feedback(layout, task_id, integration)
        return _retry_validate(
            layout,
            state,
            all_tasks,
            attributed,
            VALIDATE_INTEGRATION,
            f"{VALIDATE_INTEGRATION} retries exhausted: {attributed}",
        )

    state.phase = "done"
    state.gate = None
    save_state(layout, state)
    append_event(layout, "validate_pass")
    return state


def _handle_missing_work(
    layout: RunLayout, state: State, all_tasks: list[Task], integration: validate.IntegrationVerdict
) -> State:
    """Ticket 005 Rule 5 / 003: the integration critic cited a gap no task's
    `owns` covers. Rather than halting outright, spawn a new parentless task
    over the cited paths and loop back to dispatch — matching the resplit
    module's "residual work, never a re-gate" shape. Bounded by the same
    `MAX_RETRIES` ceiling as every other validate retry edge; halts to a
    human on exhaustion or when the gap is truly unattributable (no cited
    paths at all)."""
    new_task = resplit.missing_work_task(all_tasks, integration.evidence, integration.cited_paths)
    if new_task is None:
        return _halt(
            layout,
            state,
            "integration critic failed with no attributable task and no cited "
            f"paths to build a new one from: {integration.evidence}",
        )

    count = state.retry_counts.get(MISSING_WORK, 0) + 1
    if count > MAX_RETRIES:
        return _halt(layout, state, f"{MISSING_WORK} retries exhausted: {integration.evidence}")
    state.retry_counts[MISSING_WORK] = count

    write_task(layout, new_task)
    validate.write_integration_feedback(layout, new_task.id, integration)
    all_tasks.append(new_task)
    check_acyclic(all_tasks)

    state.tasks[new_task.id] = new_task.status
    state.phase = "dispatch"
    state.gate = None
    save_state(layout, state)
    append_event(
        layout,
        "validate_missing_work",
        new_task_id=new_task.id,
        cited_paths=integration.cited_paths,
    )
    return state


def _retry_validate(
    layout: RunLayout,
    state: State,
    all_tasks: list[Task],
    task_ids: list[str],
    retry_key: str,
    exhausted_reason: str,
) -> State:
    count = state.retry_counts.get(retry_key, 0) + 1
    if count > MAX_RETRIES:
        return _halt(layout, state, exhausted_reason)

    state.retry_counts[retry_key] = count
    for task in all_tasks:
        if task.id in task_ids:
            task.status = "pending"
            write_task(layout, task)
            state.tasks[task.id] = "pending"
    state.phase = "dispatch"
    state.gate = None
    save_state(layout, state)
    append_event(layout, "validate_fail", task_ids=task_ids, retries=count, retry_key=retry_key)
    return state
