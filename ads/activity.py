"""Observability choke point: every long-running `adapter.run()` call goes
through `run_with_activity` instead of calling `adapter.run()` directly, so
an in-flight run is never a black box (the problem this module exists to
fix) — the instant a run starts, `state.current_activity` records what's
running and since when, and the adapter streams (or tees) its own output to
a per-label file under `activity/` for the duration of the call.

`current_activity` is single-valued on `State`; PARALLEL dispatch (ticket
006) can have several tasks running `adapter.run()` concurrently, so for
that case this field is deliberately best-effort ("last writer wins") — the
real per-task live stream is each task's own `activity/<task_id>.log` file,
written by the adapter regardless of who currently owns `current_activity`.
Callers that already hold a lock around concurrent `state` mutations (e.g.
`ads/dispatch.py`'s `git_lock`) should pass it through so the heartbeat
set/clear doesn't race with other threads' state edits; this module does not
introduce a new lock of its own.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from ads.adapters.base import Adapter, RunResult
from ads.layout import RunLayout
from ads.state import State, append_event, save_state
from ads.tasks import TaskTier

_UNSAFE_LABEL_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_label(label: str) -> str:
    """Labels become filenames (`activity/<label>.log`) — replace anything
    that isn't filesystem-safe with `_`."""
    return _UNSAFE_LABEL_CHARS.sub("_", label)


def activity_log_path(layout: RunLayout, label: str) -> Path:
    layout.activity_dir.mkdir(parents=True, exist_ok=True)
    return layout.activity_dir / f"{_sanitize_label(label)}.log"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_with_activity(
    adapter: Adapter,
    layout: RunLayout,
    state: State,
    *,
    label: str,
    kind: str,
    prompt: str,
    cwd: Path,
    allowed_tools: list[str] | None = None,
    tier: TaskTier = "standard",
    lock: threading.Lock | None = None,
) -> RunResult:
    """Run `adapter.run(...)` with the heartbeat + live activity log wired
    up. `lock` guards the `state.current_activity` set/clear against
    concurrent dispatch threads sharing the same `state`/`layout` — pass the
    caller's existing lock (e.g. dispatch's `git_lock`) rather than
    introducing a new one; omit it for single-threaded call sites."""
    guard = lock if lock is not None else nullcontext()
    model = adapter.resolve_model(tier)
    log_path = activity_log_path(layout, label)

    with guard:
        state.current_activity = {
            "label": label,
            "kind": kind,
            "model": model,
            "started_at": _now_iso(),
        }
        save_state(layout, state)
        append_event(layout, "run_start", label=label, activity_kind=kind)

    exit_status = "error"
    try:
        result = adapter.run(
            prompt, cwd=cwd, allowed_tools=allowed_tools, tier=tier, activity_log=log_path
        )
        exit_status = result.exit_status
        return result
    finally:
        with guard:
            state.current_activity = None
            save_state(layout, state)
            append_event(layout, "run_end", label=label, activity_kind=kind, exit=exit_status)
