"""Entrypoint: `driver start|approve|reject|status|resume`."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import cast, get_args

from ads import sandbox
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
    state = load_state(layout)
    print(f"run:          {layout.run_id}")
    print(f"phase:        {state.phase}")
    print(f"review_stage: {state.review_stage}")
    print(f"gate:         {state.gate}")
    print(f"halt_reason:  {state.halt_reason}")
    print(f"tasks:        {state.tasks}")
    print(f"retry_counts: {state.retry_counts}")
    print(f"updated_at:   {state.updated_at}")


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
    _print_status(layout)


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
    p_status.set_defaults(func=cmd_status)

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
