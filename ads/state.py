"""Run state (ticket 002 + 005): the ONLY thing the driver loop reads to
decide what to do next. `state.json` is written atomically (temp file +
`os.replace`) so a crash never leaves a half-written file. `events.jsonl` is
an append-only audit trail the loop never reads back.

**Halt-state encoding (ticket 006).** There is no dedicated "halt-state"
field; halts are expressed as combinations of the 11 fields below, and
`describe_halt` derives the CLI-facing label from them:

- `awaiting_plan_approval`   — `phase="review"`, `review_stage=None`.
- `awaiting_spec_approval`   — `phase="review"`, `review_stage="spec"`.
- `awaiting_design_approval` — `phase="review"`, `review_stage="design"`.
- `awaiting_clarification`   — `phase="plan"`, `question is not None`.
- `awaiting_signoff`         — `phase="validate"`, `cursor=None`, every task
  `done` (the full graph validated but the user hasn't signed off yet — the
  loop has no code path that writes `done` itself, ticket 006).
- `blocked`                  — `gate="blocked"` (cyclic plan, ceiling hit).

`done` is terminal and carries no halt label.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast, get_args

from ads._literal import validate_literal
from ads.adapters.base import ADAPTER_STUB, AdapterName
from ads.layout import RunLayout
from ads.tasks import TaskStatus

Phase = Literal["intake", "plan", "review", "execute", "validate", "done"]
ReviewStage = Literal["spec", "design"]
Gate = Literal["blocked"]

PHASES: tuple[Phase, ...] = get_args(Phase)
REVIEW_STAGES: tuple[ReviewStage, ...] = get_args(ReviewStage)
GATES: tuple[Gate, ...] = get_args(Gate)

DEFAULT_ADAPTER: AdapterName = ADAPTER_STUB


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class State:
    phase: Phase = "intake"
    review_stage: ReviewStage | None = None
    gate: Gate | None = None
    tasks: dict[str, TaskStatus] = field(default_factory=dict[str, TaskStatus])
    attempts: dict[str, int] = field(default_factory=dict[str, int])
    cursor: str | None = None
    halt_reason: str | None = None
    adapter: AdapterName = DEFAULT_ADAPTER
    updated_at: str = ""
    event_seq: int = 0
    question: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        """`data` is raw JSON loaded from `state.json` — the one place we
        trust our own prior write rather than re-deriving strict types, so
        `Any` here is the boundary, not a leak into the rest of the module."""
        gate = data.get("gate")
        review_stage = data.get("review_stage")
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
            attempts=dict(data.get("attempts", {})),
            cursor=data.get("cursor"),
            halt_reason=data.get("halt_reason"),
            adapter=cast(
                AdapterName,
                validate_literal(
                    data.get("adapter", DEFAULT_ADAPTER), get_args(AdapterName), field="adapter"
                ),
            ),
            updated_at=data.get("updated_at", ""),
            event_seq=int(data.get("event_seq", 0)),
            question=data.get("question"),
        )


def load_state(layout: RunLayout) -> State:
    with layout.state_file.open("r", encoding="utf-8") as fh:
        return State.from_dict(json.load(fh))


def save_state(layout: RunLayout, state: State) -> None:
    """Atomic write: write to a sibling temp file, then atomically replace
    (`os.replace` is an atomic same-filesystem rename)."""
    state.updated_at = _now()
    layout.root.mkdir(parents=True, exist_ok=True)
    tmp_path = layout.state_file.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state.to_dict(), fh, indent=2, sort_keys=False)
        fh.write("\n")
    tmp_path.replace(layout.state_file)


# ---------------------------------------------------------------------------
# events.jsonl — append-only, never read back by the loop.
#
# Documented core kinds/types (ticket 002 §5 + ticket 005 §3, open to grow):
# run:start, phase:enter, intent, plan_ready/plan:done, gate_open/review:gate,
# gate_close, task:start, task:done/task_handoff, validate/validate:verdict,
# activity, halt, done, gap_decided, error.
# ---------------------------------------------------------------------------


def append_event(
    layout: RunLayout,
    state: State,
    event_type: str,
    *,
    task: str | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    """Append one feed-envelope line `{ts, seq, phase, type, task, data}` and
    bump `state.event_seq` in lockstep (persisted by the caller's next
    `save_state`) so `seq` stays monotonic and gap-free across resumes."""
    state.event_seq += 1
    event = {
        "ts": _now(),
        "seq": state.event_seq,
        "phase": state.phase,
        "type": event_type,
        "task": task,
        "data": data or {},
    }
    layout.root.mkdir(parents=True, exist_ok=True)
    with layout.events.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    return state.event_seq


def halt(layout: RunLayout, state: State, reason: str) -> State:
    """Shared halt helper: any phase runner stops the loop the same way."""
    state.gate = "blocked"
    state.halt_reason = reason
    append_event(layout, state, "halt", data={"reason": reason})
    save_state(layout, state)
    return state


def describe_halt(state: State) -> str | None:
    """The CLI-facing halt-state label `status`/`approve` reason about."""
    if state.gate == "blocked":
        return "blocked"
    if state.phase == "plan" and state.question is not None:
        return "awaiting_clarification"
    if state.phase == "review":
        if state.review_stage == "spec":
            return "awaiting_spec_approval"
        if state.review_stage == "design":
            return "awaiting_design_approval"
        return "awaiting_plan_approval"
    if (
        state.phase == "validate"
        and state.cursor is None
        and state.tasks
        and all(status == "done" for status in state.tasks.values())
    ):
        return "awaiting_signoff"
    return None
