"""Live status TUI (ticket 010): read-only, full-screen dashboard for a
running ADS loop. Renders `state.json` + a tail of `events.jsonl` on an
interval; never writes anything, so a crash or bug here can never corrupt
a run.

`rich` is the one blessed runtime-dependency exception (SPEC §7), import-
guarded here exactly as `ads/feed.py` does: token-free test suite imports
and exercises the pure helpers below without `rich` installed, and
`--help` works before `rich` is ever touched.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ads.layout import RunLayout
from ads.state import State, describe_halt

DEFAULT_EVENT_LIMIT = 20
DEFAULT_INTERVAL = 1.0


# Static `from rich import ...` would make pyright require rich resolvable;
# `rich` is the one blessed runtime-dependency exception (SPEC §7), so the
# token-free test suite must import and run without it installed.
def _try_import_rich() -> tuple[Any, Any, Any, Any, bool]:
    try:
        console = importlib.import_module("rich.console")
        live = importlib.import_module("rich.live")
        panel = importlib.import_module("rich.panel")
        table = importlib.import_module("rich.table")
        return console, live, panel, table, True
    except ImportError:
        return None, None, None, None, False


_rich_console, _rich_live, _rich_panel, _rich_table, _RICH_AVAILABLE = _try_import_rich()


# ---------------------------------------------------------------------------
# Pure helpers (no `rich`) — importable and testable without the dependency.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """One frame's worth of state, or the reason it couldn't be read."""

    state: State | None
    error: str | None

    @property
    def halt_label(self) -> str | None:
        return describe_halt(self.state) if self.state is not None else None


def read_snapshot(layout: RunLayout) -> Snapshot:
    """Read + parse `state.json`. Any absence/corruption degrades to a
    `Snapshot` instead of raising — the render loop must never crash on its
    own inputs racing a concurrent writer."""
    try:
        with layout.state_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return Snapshot(state=None, error=None)
    except json.JSONDecodeError as exc:
        return Snapshot(state=None, error=f"unreadable state.json: {exc}")
    if not isinstance(data, dict):
        return Snapshot(state=None, error="invalid state.json: not an object")
    try:
        state = State.from_dict(cast(dict[str, Any], data))
    except (KeyError, TypeError, ValueError) as exc:
        return Snapshot(state=None, error=f"invalid state.json: {exc}")
    return Snapshot(state=state, error=None)


def tail_events(layout: RunLayout, limit: int) -> list[dict[str, Any]]:
    """Last `limit` parseable lines of `events.jsonl`. A missing file yields
    `[]`; a malformed or half-written trailing line (the writer may be mid
    -append) is silently dropped rather than raised."""
    try:
        text = layout.events.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    events: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(cast(dict[str, Any], event))
    return events


def progress(state: State) -> tuple[int, int]:
    """`(done, total)` task counts."""
    total = len(state.tasks)
    done = sum(1 for status in state.tasks.values() if status == "done")
    return done, total


def event_summary(event: dict[str, Any]) -> str:
    """One legible `seq · type · task · data-summary` line for an event
    envelope `{ts, seq, phase, type, task, data}`."""
    seq = event.get("seq", "-")
    etype = event.get("type", "-")
    task = event.get("task") or "-"
    parts = [str(seq), str(etype), str(task)]
    data = event.get("data")
    if data:
        summary = json.dumps(data, sort_keys=True)
        if len(summary) > 80:
            summary = summary[:80] + "…"
        parts.append(summary)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# `rich`-dependent render / run loop.
# ---------------------------------------------------------------------------


def render(snapshot: Snapshot, events: list[dict[str, Any]]) -> Any:
    if snapshot.state is None:
        message = snapshot.error or "waiting for run to start…"
        return _rich_panel.Panel(message, title="ads tui")

    state = snapshot.state
    done, total = progress(state)

    header = _rich_table.Table.grid(padding=(0, 1))
    header.add_column(style="dim")
    header.add_column()
    header.add_row("phase", state.phase)
    header.add_row("review_stage", state.review_stage or "-")
    header.add_row("gate", state.gate or "-")
    header.add_row("halt_reason", state.halt_reason or "-")
    header.add_row("halt", snapshot.halt_label or "-")
    header.add_row("cursor", state.cursor or "-")
    header.add_row("tasks", f"{done}/{total}")

    tasks_table = _rich_table.Table(title="tasks")
    tasks_table.add_column("id")
    tasks_table.add_column("status")
    tasks_table.add_column("attempts")
    for task_id in sorted(state.tasks):
        tasks_table.add_row(task_id, state.tasks[task_id], str(state.attempts.get(task_id, 0)))

    events_body = "\n".join(event_summary(event) for event in events) or "(no events yet)"

    return _rich_console.Group(
        _rich_panel.Panel(header, title="ads run"),
        tasks_table,
        _rich_panel.Panel(events_body, title="recent events"),
    )


def run_tui(
    layout: RunLayout,
    *,
    interval: float = DEFAULT_INTERVAL,
    event_limit: int = DEFAULT_EVENT_LIMIT,
) -> None:
    initial = render(read_snapshot(layout), tail_events(layout, event_limit))
    with _rich_live.Live(initial, refresh_per_second=2) as live:
        try:
            while True:
                time.sleep(interval)
                snapshot = read_snapshot(layout)
                events = tail_events(layout, event_limit)
                live.update(render(snapshot, events))
        except KeyboardInterrupt:
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ads.tui", description=__doc__)
    parser.add_argument("--repo", default=".", help="repo root (default: .)")
    parser.add_argument("--run-id", default="current", help="run id (default: current)")
    args = parser.parse_args(argv)

    if not _RICH_AVAILABLE:
        print("ads.tui requires the `rich` package", file=sys.stderr)
        return 1

    layout = RunLayout(repo=Path(args.repo), run_id=args.run_id)
    run_tui(layout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
