"""Entrypoint: `driver start|approve|reject|status|resume`."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import cast, get_args

from ads import control, escalation, sandbox, status, tui
from ads._literal import validate_literal
from ads.adapters.base import (
    ADAPTER_CLAUDE_CODE,
    ADAPTER_NAMES,
    ADAPTER_OPENCODE,
    ADAPTER_STUB,
    Adapter,
    AdapterName,
)
from ads.adapters.claude_code import ClaudeCodeAdapter
from ads.adapters.opencode import OpenCodeAdapter
from ads.adapters.stub import StubAdapter
from ads.config import Config, load_config
from ads.driver import approve as driver_approve
from ads.driver import reject as driver_reject
from ads.driver import run_until_halt, start_run
from ads.layout import RunLayout
from ads.state import load_state, save_state


def _adapter_name_arg(raw: str | None) -> AdapterName | None:
    """`argparse`'s `choices=` already restricts `--adapter` to valid names at
    the shell boundary; this makes that runtime guarantee visible to the type
    checker instead of trusting `argparse.Namespace`'s untyped attributes."""
    if raw is None:
        return None
    return cast(AdapterName, validate_literal(raw, get_args(AdapterName), field="--adapter"))


def _build_adapter(name: AdapterName, cfg: Config) -> Adapter:
    if name == ADAPTER_STUB:
        return StubAdapter()
    policy = sandbox.policy_from_harness(cfg.harness)
    if name == ADAPTER_OPENCODE:
        return OpenCodeAdapter(cfg.harness, policy=policy)
    return ClaudeCodeAdapter(cfg.harness, policy=policy)


def _adapter_for_run(layout: RunLayout, cfg: Config, override: AdapterName | None) -> Adapter:
    """Use the run's persisted adapter; an explicit --adapter override is
    honored but persisted so it sticks and no mid-run mismatch can recur."""
    state = load_state(layout)
    name = override or state.adapter
    if override and override != state.adapter:
        print(f"warning: switching run adapter {state.adapter!r} -> {override!r}", file=sys.stderr)
        state.adapter = override
        save_state(layout, state)
    return _build_adapter(name, cfg)


def _require_adapter_name(raw: str | None) -> AdapterName:
    name = _adapter_name_arg(raw)
    if name is None:
        raise SystemExit(f"unknown adapter: {raw!r}")
    return name


def _resolve_run_id(layout_root: RunLayout, explicit: str | None) -> str:
    if explicit:
        return explicit
    link = layout_root.current_link
    if not link.exists():
        raise SystemExit(
            "no run-id given and no .agent/runs/current — use --run-id or `driver start`"
        )
    return Path(link).resolve().name


def _print_status(layout: RunLayout) -> None:
    print(status.render_plain(status.read_status(layout)), end="")


def cmd_start(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    run_id = args.run_id or time.strftime("run-%Y%m%d-%H%M%S")
    layout = RunLayout(repo=repo, run_id=run_id)
    cfg = load_config(layout.config)
    adapter_name = _adapter_name_arg(args.adapter) or ADAPTER_CLAUDE_CODE
    adapter = _build_adapter(adapter_name, cfg)

    start_run(layout, args.task)
    state = load_state(layout)
    state.adapter = adapter_name
    # Ticket 010: best-effort marker that a foreground process currently
    # holds this run; mainly feeds the read model today, a future sync-block
    # would key off it too. Kept minimal — not cleared mid-loop, only here.
    state.attached = True
    save_state(layout, state)
    run_until_halt(layout, cfg, adapter)
    _print_status(layout)


def cmd_resume(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    stub_layout = RunLayout(repo=repo, run_id="current")
    run_id = _resolve_run_id(stub_layout, args.run_id)
    layout = RunLayout(repo=repo, run_id=run_id)
    cfg = load_config(layout.config)
    adapter = _build_adapter(_require_adapter_name(args.adapter), cfg)

    # Ticket 010: `driver resume` is both the "resume" control verb (clears
    # any operator pause) AND the foreground drive that drains it — the
    # async substrate's own doc says "a foreground `driver resume` drains
    # them", so this is the one command that enqueues its own resume signal
    # and immediately drains it, rather than forcing two separate
    # invocations under the same subcommand name.
    control.enqueue(layout, control.ControlCommand(verb="resume"))
    run_until_halt(layout, cfg, adapter)
    _print_status(layout)


def cmd_approve(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    stub_layout = RunLayout(repo=repo, run_id="current")
    run_id = _resolve_run_id(stub_layout, args.run_id)
    layout = RunLayout(repo=repo, run_id=run_id)
    driver_approve(layout)
    if args.no_continue:
        _print_status(layout)
        return
    cfg = load_config(layout.config)
    adapter = _adapter_for_run(layout, cfg, _adapter_name_arg(args.adapter))
    run_until_halt(layout, cfg, adapter)
    _print_status(layout)


def cmd_reject(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    stub_layout = RunLayout(repo=repo, run_id="current")
    run_id = _resolve_run_id(stub_layout, args.run_id)
    layout = RunLayout(repo=repo, run_id=run_id)
    driver_reject(layout, args.reason)
    if args.no_continue:
        _print_status(layout)
        return
    cfg = load_config(layout.config)
    adapter = _adapter_for_run(layout, cfg, _adapter_name_arg(args.adapter))
    run_until_halt(layout, cfg, adapter)
    _print_status(layout)


def cmd_status(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    stub_layout = RunLayout(repo=repo, run_id="current")
    run_id = _resolve_run_id(stub_layout, args.run_id)
    layout = RunLayout(repo=repo, run_id=run_id)
    run_status = status.read_status(layout)
    if args.json:
        print(status.to_json(run_status))
    else:
        print(status.render_plain(run_status), end="")


def cmd_watch(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    stub_layout = RunLayout(repo=repo, run_id="current")
    run_id = _resolve_run_id(stub_layout, args.run_id)
    layout = RunLayout(repo=repo, run_id=run_id)
    try:
        tui.run_tui(layout, poll_seconds=args.poll)
    except tui.TUIUnavailable as exc:
        print(f"watch needs an interactive terminal ({exc}); showing a snapshot instead:")
        _print_status(layout)


def cmd_escalations(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    stub_layout = RunLayout(repo=repo, run_id="current")
    run_id = _resolve_run_id(stub_layout, args.run_id)
    layout = RunLayout(repo=repo, run_id=run_id)
    state = load_state(layout)
    open_ids = escalation.list_open(state)
    if not open_ids:
        print("no open escalations")
        return
    for request_id in open_ids:
        request = escalation.load_request(layout, request_id)
        reason = request.reason.splitlines()[0] if request.reason else ""
        print(
            f"{request.id}\ttask={request.task_id}\tkind={request.kind}\t"
            f"op={request.op}\ttarget={request.target}\treason={reason}"
        )


def cmd_escalate_approve(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    stub_layout = RunLayout(repo=repo, run_id="current")
    run_id = _resolve_run_id(stub_layout, args.run_id)
    layout = RunLayout(repo=repo, run_id=run_id)
    state = load_state(layout)
    escalation.approve(layout, state, args.request_id)
    if args.no_continue:
        _print_status(layout)
        return
    cfg = load_config(layout.config)
    adapter = _adapter_for_run(layout, cfg, _adapter_name_arg(args.adapter))
    run_until_halt(layout, cfg, adapter)
    _print_status(layout)


def cmd_escalate_reject(args: argparse.Namespace) -> None:
    repo = Path(args.repo).resolve()
    stub_layout = RunLayout(repo=repo, run_id="current")
    run_id = _resolve_run_id(stub_layout, args.run_id)
    layout = RunLayout(repo=repo, run_id=run_id)
    state = load_state(layout)
    escalation.reject(layout, state, args.request_id, args.reason)
    if args.no_continue:
        _print_status(layout)
        return
    cfg = load_config(layout.config)
    adapter = _adapter_for_run(layout, cfg, _adapter_name_arg(args.adapter))
    run_until_halt(layout, cfg, adapter)
    _print_status(layout)


def _resolve_layout(args: argparse.Namespace) -> RunLayout:
    repo = Path(args.repo).resolve()
    stub_layout = RunLayout(repo=repo, run_id="current")
    run_id = _resolve_run_id(stub_layout, args.run_id)
    return RunLayout(repo=repo, run_id=run_id)


def _enqueue_and_report(layout: RunLayout, command: control.ControlCommand) -> None:
    """Ticket 010: async substrate — these commands only append to
    control.jsonl and print a confirmation + status snapshot. They never
    drive the loop themselves; a foreground `driver resume` drains them at
    the next unit boundary, which is the intended async model."""
    control.enqueue(layout, command)
    label = f" {command.task_id}" if command.task_id else ""
    print(f"queued: {command.verb}{label}")
    _print_status(layout)


def cmd_pause(args: argparse.Namespace) -> None:
    layout = _resolve_layout(args)
    _enqueue_and_report(layout, control.ControlCommand(verb="pause"))


def cmd_replan(args: argparse.Namespace) -> None:
    layout = _resolve_layout(args)
    _enqueue_and_report(layout, control.ControlCommand(verb="replan"))


def cmd_redirect(args: argparse.Namespace) -> None:
    layout = _resolve_layout(args)
    _enqueue_and_report(
        layout, control.ControlCommand(verb="redirect", task_id=args.task, note=args.note)
    )


def cmd_edit(args: argparse.Namespace) -> None:
    layout = _resolve_layout(args)
    _enqueue_and_report(layout, control.ControlCommand(verb="edit", task_id=args.task))


def cmd_abort(args: argparse.Namespace) -> None:
    layout = _resolve_layout(args)
    _enqueue_and_report(layout, control.ControlCommand(verb="abort", task_id=args.task))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="driver")
    parser.add_argument("--repo", default=".", help="repo root (default: cwd)")
    parser.add_argument("--run-id", default=None, help="run id (default: .agent/runs/current)")
    parser.add_argument(
        "--adapter",
        default=None,
        choices=ADAPTER_NAMES,
        help="harness adapter; set at start, persisted per-run",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="start a new run")
    p_start.add_argument("task", help="the user's task/intent text")
    p_start.set_defaults(func=cmd_start)

    p_resume = sub.add_parser("resume", help="resume the current (or given) run")
    p_resume.set_defaults(func=cmd_resume)

    p_approve = sub.add_parser("approve", help="approve the current review gate")
    p_approve.add_argument(
        "--no-continue", action="store_true", help="don't auto-advance after approving"
    )
    p_approve.set_defaults(func=cmd_approve)

    p_reject = sub.add_parser("reject", help="reject the current review gate")
    p_reject.add_argument("reason", help="why it was rejected")
    p_reject.add_argument(
        "--no-continue", action="store_true", help="don't auto-advance after rejecting"
    )
    p_reject.set_defaults(func=cmd_reject)

    p_status = sub.add_parser("status", help="show run state")
    p_status.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p_status.set_defaults(func=cmd_status)

    p_watch = sub.add_parser("watch", help="live TUI dashboard over run status")
    p_watch.add_argument(
        "--poll", type=float, default=1.0, help="poll interval in seconds (default: 1.0)"
    )
    p_watch.set_defaults(func=cmd_watch)

    p_escalations = sub.add_parser("escalations", help="list open escalation requests")
    p_escalations.set_defaults(func=cmd_escalations)

    p_esc_approve = sub.add_parser("escalate-approve", help="approve an escalation request")
    p_esc_approve.add_argument("request_id", help="escalation request id (e.g. esc-01-a-1)")
    p_esc_approve.add_argument(
        "--no-continue", action="store_true", help="don't auto-advance after approving"
    )
    p_esc_approve.set_defaults(func=cmd_escalate_approve)

    p_esc_reject = sub.add_parser("escalate-reject", help="reject an escalation request")
    p_esc_reject.add_argument("request_id", help="escalation request id (e.g. esc-01-a-1)")
    p_esc_reject.add_argument("reason", help="why it was rejected")
    p_esc_reject.add_argument(
        "--no-continue", action="store_true", help="don't auto-advance after rejecting"
    )
    p_esc_reject.set_defaults(func=cmd_escalate_reject)

    p_pause = sub.add_parser("pause", help="queue an operator pause (drained at next boundary)")
    p_pause.set_defaults(func=cmd_pause)

    p_replan = sub.add_parser("replan", help="queue a full replan loopback to the plan phase")
    p_replan.set_defaults(func=cmd_replan)

    p_redirect = sub.add_parser("redirect", help="queue a note injected into a task's scratch file")
    p_redirect.add_argument("task", help="task id to redirect")
    p_redirect.add_argument("note", help="operator note to inject")
    p_redirect.set_defaults(func=cmd_redirect)

    p_edit = sub.add_parser(
        "edit", help="queue a pause so a pending task's file can be hand-edited"
    )
    p_edit.add_argument("task", help="task id to edit")
    p_edit.set_defaults(func=cmd_edit)

    p_abort = sub.add_parser("abort", help="queue an abort for a task (graph bookkeeping only)")
    p_abort.add_argument("task", help="task id to abort")
    p_abort.set_defaults(func=cmd_abort)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # top-level CLI boundary: fail loud, no traceback spam
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
