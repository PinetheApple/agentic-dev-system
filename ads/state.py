"""Run state: the ONLY thing the driver loop reads to decide what to do next.

`state.json` is written atomically (temp file + os.replace) so a crash never
leaves a half-written file. `events.jsonl` is an append-only audit trail the
loop never reads back — it exists for humans/debugging only.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast, get_args

from ads._literal import validate_literal
from ads.adapters.base import ADAPTER_CLAUDE_CODE, AdapterName
from ads.layout import RunLayout
from ads.tasks import TaskStatus

Phase = Literal["intake", "plan", "review", "dispatch", "validate", "done"]
ReviewStage = Literal["spec", "design"]
Gate = Literal["pending", "blocked", "reconcile", "escalation", "paused"]
ReplanScope = Literal["design"]

PHASES: tuple[Phase, ...] = get_args(Phase)
REVIEW_STAGES: tuple[ReviewStage, ...] = get_args(ReviewStage)
GATES: tuple[Gate, ...] = get_args(Gate)

DEFAULT_ADAPTER: AdapterName = ADAPTER_CLAUDE_CODE


@dataclass
class State:
    phase: Phase = "intake"
    review_stage: ReviewStage | None = None
    gate: Gate | None = None
    tasks: dict[str, TaskStatus] = field(default_factory=dict[str, TaskStatus])
    retry_counts: dict[str, int] = field(default_factory=dict[str, int])
    # Ticket 005 Rule 5: machine-owned dispatch-attempt counter per task id,
    # the portable budget-ceiling floor (ads/resplit.py's STEP_CEILING).
    # Survives crash since it's part of the atomically-written state.json.
    step_counts: dict[str, int] = field(default_factory=dict[str, int])
    # Ticket 011 dec 6 + dec 7: the open-set cursor for the escalation gate —
    # request id -> "pending"|"approved"|"rejected". Bodies (reason/exact
    # payload) live on disk under `escalations_dir`; this is only the status
    # the loop reads to decide whether the gate is still open.
    escalations: dict[str, str] = field(default_factory=dict[str, str])
    # Exact `cmd` strings a human has approved via a cmd-flagged escalation,
    # so `escalation.screen_cmd` skips them on re-dispatch instead of
    # re-flagging and re-halting the same approved command forever.
    approved_cmds: list[str] = field(default_factory=list[str])
    cursor: str | None = None
    halt_reason: str | None = None
    # None = full (re)plan; "design" = spec.md is frozen-approved, only
    # regenerate design.md + tasks (freeze-approved-upstream, ticket 008).
    replan_scope: ReplanScope | None = None
    # Harness adapter this run was started with. Persisted so later commands
    # (approve/resume) can't silently switch harness mid-run.
    adapter: AdapterName = DEFAULT_ADAPTER
    updated_at: str = ""
    # Ticket 010: whether a foreground driver process currently holds this
    # run (a human is attached, in the sync-block sense). Set by a
    # foreground drive; false on detached/resume/crash — a later concern,
    # just the field + round-trip here.
    attached: bool = False
    # Ticket 010: operator-requested pause, drained from control.jsonl at a
    # unit boundary. Distinct from `gate` so pause/resume can toggle without
    # touching the phase-gate machinery; the driver halts to gate="paused"
    # when this is True and clears that gate when a drained `resume` flips
    # it back to False.
    paused: bool = False
    # Ticket 010: machine-owned cursor over control.jsonl — count of control
    # commands already drained, same spirit as `step_counts`.
    control_cursor: int = 0
    # Observability heartbeat: set the instant a long-running `adapter.run()`
    # begins, cleared when it ends (`ads/activity.py`'s `run_with_activity`
    # is the single choke point that sets/clears this). A plain dict, not a
    # dataclass, so state.json serialization stays trivial — keys: `label`,
    # `kind`, `model`, `started_at` (iso8601 Z).
    current_activity: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        """`data` is raw JSON loaded from `state.json` — the one place we
        trust our own prior write rather than re-deriving strict types, so
        `Any` here is the boundary, not a leak into the rest of the module."""
        gate = data.get("gate")
        review_stage = data.get("review_stage")
        replan_scope = data.get("replan_scope")
        return cls(
            phase=cast(Phase, validate_literal(data.get("phase", "intake"), PHASES, field="phase")),
            review_stage=cast(
                ReviewStage, validate_literal(review_stage, REVIEW_STAGES, field="review_stage")
            )
            if review_stage is not None
            else None,
            gate=cast(Gate, validate_literal(gate, GATES, field="gate"))
            if gate is not None
            else None,
            tasks=dict(data.get("tasks", {})),
            retry_counts=dict(data.get("retry_counts", {})),
            step_counts=dict(data.get("step_counts", {})),
            escalations=dict(data.get("escalations", {})),
            approved_cmds=list(data.get("approved_cmds", [])),
            cursor=data.get("cursor"),
            halt_reason=data.get("halt_reason"),
            replan_scope=cast(
                ReplanScope,
                validate_literal(replan_scope, get_args(ReplanScope), field="replan_scope"),
            )
            if replan_scope is not None
            else None,
            adapter=cast(
                AdapterName,
                validate_literal(
                    data.get("adapter", DEFAULT_ADAPTER), get_args(AdapterName), field="adapter"
                ),
            ),
            updated_at=data.get("updated_at", ""),
            attached=bool(data.get("attached", False)),
            paused=bool(data.get("paused", False)),
            control_cursor=int(data.get("control_cursor", 0)),
            current_activity=dict(data["current_activity"])
            if data.get("current_activity") is not None
            else None,
        )


def load_state(layout: RunLayout) -> State:
    with layout.state_file.open("r", encoding="utf-8") as fh:
        return State.from_dict(json.load(fh))


def save_state(layout: RunLayout, state: State) -> None:
    """Atomic write: write to a sibling temp file, then atomically replace
    (Path.replace maps to os.replace — atomic same-filesystem rename)."""
    state.updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    layout.root.mkdir(parents=True, exist_ok=True)
    tmp_path = layout.state_file.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state.to_dict(), fh, indent=2, sort_keys=False)
        fh.write("\n")
    tmp_path.replace(layout.state_file)


def append_event(layout: RunLayout, kind: str, **payload: Any) -> None:
    """Append one audit line. Never read by the loop — best-effort only."""
    layout.root.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": kind,
        **payload,
    }
    with layout.events.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def halt(layout: RunLayout, state: State, reason: str, gate: Gate = "blocked") -> State:
    """Shared halt helper: every phase runner (plan/dispatch/validate) stops
    the loop the same way, whether the gate is a plain `blocked` or the
    `reconcile` gate a worktree merge tripwire raises (ticket 006)."""
    state.gate = gate
    state.halt_reason = reason
    save_state(layout, state)
    append_event(layout, "halt", reason=reason, gate=gate)
    return state
