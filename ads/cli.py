"""The 7 user-in-the-loop CLI verbs (ticket 006): `init`, `start`, `approve`,
`reject`, `answer`, `status`, `resume`. Blocking-drain model — each verb that
mutates the halt-state calls `drive()` in the foreground, streaming the live
feed, until the loop halts again or reaches `done`.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, cast

from ads.adapters.base import Adapter, AdapterName
from ads.adapters.stub import StubAdapter
from ads.driver import ATTEMPTS_CEILING, PLAN_ATTEMPTS_KEY, drive
from ads.feed import Feed
from ads.layout import AGENT_DIR, RUNS_DIRNAME, RunLayout
from ads.state import State, append_event, describe_halt, halt, load_state, save_state


def _make_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _link_current(layout: RunLayout) -> None:
    link = layout.current_link
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(Path(layout.run_id))


def _resolve_current(repo: Path) -> RunLayout:
    link = repo / AGENT_DIR / RUNS_DIRNAME / "current"
    if not link.exists():
        raise SystemExit("no current run — run `driver init` first")
    run_id = link.resolve().name
    return RunLayout(repo=repo, run_id=run_id)


def _adapter_for(state: State) -> Adapter:
    if state.adapter == "stub":
        return StubAdapter()
    raise NotImplementedError("the claude-code adapter is out of scope for ticket 008 (stub-only)")


def _feed_sink(feed: Feed) -> Any:
    def sink(line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            feed.emit_line(line)
            return
        feed.emit_event(event)

    return sink


def _drain(layout: RunLayout) -> int:
    state = load_state(layout)
    adapter = _adapter_for(state)
    feed = Feed()
    try:
        final_state = drive(layout, adapter, on_event=_feed_sink(feed))
    finally:
        feed.close()
    _print_status(layout, final_state)
    return 0


def _print_status(layout: RunLayout, state: State, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
        return
    halt_label = describe_halt(state) or "-"
    print(f"run {layout.run_id}: phase={state.phase} halt={halt_label} tasks={state.tasks}")


# ---------------------------------------------------------------------------
# verbs
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    repo = Path.cwd()
    run_id = _make_run_id()
    layout = RunLayout(repo=repo, run_id=run_id)
    layout.scaffold()
    state = State(adapter=cast(AdapterName, args.adapter))
    save_state(layout, state)
    _link_current(layout)
    print(f"initialized run {run_id}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    layout = _resolve_current(Path.cwd())
    layout.intent.write_text(args.task, encoding="utf-8")
    return _drain(layout)


def cmd_approve(args: argparse.Namespace) -> int:
    layout = _resolve_current(Path.cwd())
    state = load_state(layout)
    current = describe_halt(state)
    if args.at is not None and args.at != current:
        raise SystemExit(f"stale approve: current halt is {current!r}, not {args.at!r}")
    if current is None:
        raise SystemExit("nothing to approve")

    if current == "awaiting_plan_approval":
        state.phase = "execute"
        state.cursor = None
    elif current == "awaiting_spec_approval":
        state.review_stage = "design"
    elif current == "awaiting_design_approval":
        state.phase = "execute"
        state.cursor = None
    elif current == "awaiting_clarification":
        state.question = None
    elif current == "awaiting_signoff":
        state.phase = "done"
    elif current == "blocked":
        state.gate = None
        state.halt_reason = None

    if current == "awaiting_signoff":
        append_event(layout, state, "done")
    append_event(layout, state, "gate_close", data={"gate": current, "decision": "approve"})
    save_state(layout, state)
    return _drain(layout)


def cmd_reject(args: argparse.Namespace) -> int:
    layout = _resolve_current(Path.cwd())
    state = load_state(layout)
    current = describe_halt(state)
    if current is None:
        raise SystemExit("nothing to reject")

    if current in ("awaiting_plan_approval", "awaiting_spec_approval", "awaiting_design_approval"):
        attempts = state.attempts.get(PLAN_ATTEMPTS_KEY, 0) + 1
        state.attempts[PLAN_ATTEMPTS_KEY] = attempts
        if attempts >= ATTEMPTS_CEILING:
            final_state = halt(layout, state, f"plan rejected {attempts} times: {args.reason}")
            _print_status(layout, final_state)
            return 0
        state.phase = "plan"
        if current != "awaiting_design_approval":
            state.review_stage = None
        # else: review_stage stays "design" — freeze-approved-upstream, spec
        # is not regenerated by a design-stage reject.
    elif current == "awaiting_signoff":
        # Ticket 006 doesn't pin an exact task-selection mechanic for a
        # signoff-stage reject; bounce to a full replan as the safe default.
        state.phase = "plan"
        state.review_stage = None
    else:
        raise SystemExit(f"reject is not valid from halt-state {current!r}")

    append_event(
        layout,
        state,
        "gate_close",
        data={"gate": current, "decision": "reject", "reason": args.reason},
    )
    save_state(layout, state)
    return _drain(layout)


def cmd_answer(args: argparse.Namespace) -> int:
    layout = _resolve_current(Path.cwd())
    state = load_state(layout)
    current = describe_halt(state)
    if current != "awaiting_clarification":
        msg = f"answer is only valid at awaiting_clarification, current halt is {current!r}"
        raise SystemExit(msg)

    with layout.intent.open("a", encoding="utf-8") as fh:
        fh.write(f"\n\n## Clarification\n\n{args.text}\n")
    state.question = None
    append_event(layout, state, "gate_close", data={"gate": current, "decision": "answer"})
    save_state(layout, state)
    return _drain(layout)


def cmd_status(args: argparse.Namespace) -> int:
    layout = _resolve_current(Path.cwd())
    state = load_state(layout)
    _print_status(layout, state, as_json=args.json)
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    layout = _resolve_current(Path.cwd())
    return _drain(layout)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="driver")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("--adapter", default="stub", choices=["stub", "claude-code"])
    p_init.set_defaults(func=cmd_init)

    p_start = sub.add_parser("start")
    p_start.add_argument("task")
    p_start.set_defaults(func=cmd_start)

    p_approve = sub.add_parser("approve")
    p_approve.add_argument("--at", default=None)
    p_approve.set_defaults(func=cmd_approve)

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("reason")
    p_reject.set_defaults(func=cmd_reject)

    p_answer = sub.add_parser("answer")
    p_answer.add_argument("text")
    p_answer.set_defaults(func=cmd_answer)

    p_status = sub.add_parser("status")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_resume = sub.add_parser("resume")
    p_resume.set_defaults(func=cmd_resume)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return cast(int, args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
