"""Ticket 010: the operator TUI — a read-only skin over `ads.status`.

Design intent: the TUI is one renderer of `RunStatus`, not a special path.
`Frame` construction is pure (no curses, unit-testable); drawing and the
event loop are thin curses I/O wrappers around it. Nothing here ever writes
to the run dir; the only extra file reads beyond the read model are bounded
tails of one selected task's `scratch/<id>.md` and `activity/<id>.log` on
drill-down.

Observability follow-up: a dashboard, not a wall. The overview frame is
sectioned header / NOW heartbeat / task table / live activity output /
recent events / footer, so an in-flight `run()` (which can block for
30-120s) is never a dead frame — `read_status`'s `current_activity` +
`activity_tail` (see `ads/activity.py`) surface what's running and its
streamed harness output on every ~1s poll.
"""

from __future__ import annotations

import curses
import sys
from dataclasses import dataclass

from ads.layout import RunLayout
from ads.status import RunStatus, StatusUnavailable, TaskRow, read_status

ELLIPSIS = "…"
MIN_WIDTH = 20
OVERVIEW_FOOTER = "↑/↓ select · enter drill-down · r refresh · q quit"
DETAIL_FOOTER = "esc back · q quit"
WAITING_TITLE = "ads watch — waiting for run…"


class TUIUnavailable(RuntimeError):
    """Raised when the TUI can't run (non-tty terminal, curses init failure)."""


@dataclass(frozen=True)
class Frame:
    title: str
    header: str
    rows: tuple[str, ...]
    footer: str
    events: tuple[str, ...]
    detail_title: str = ""
    detail_lines: tuple[str, ...] = ()
    now_line: str = ""
    live_lines: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# pure formatting helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return ELLIPSIS[:1]
    return text[: width - 1] + ELLIPSIS


def _budget_width(width: int) -> int:
    return max(width, MIN_WIDTH)


def _format_header(status: RunStatus, width: int) -> str:
    gate = status.gate or "none"
    text = f"run {status.run_id} · phase {status.phase} · gate {gate} · {status.pending_summary}"
    return _truncate(text, _budget_width(width))


def _format_elapsed(seconds: int) -> str:
    minutes, secs = divmod(max(seconds, 0), 60)
    return f"{minutes:02d}:{secs:02d}"


def _format_now_line(status: RunStatus, width: int) -> str:
    """The dashboard's live heartbeat line: what's running, on which model,
    and for how long — or an explicit idle marker (never a blank/frozen
    line), so `watch` never looks like a dead frame while a run() blocks."""
    activity = status.current_activity
    if activity is None:
        return _truncate("— idle —", _budget_width(width))
    elapsed = _format_elapsed(status.activity_elapsed_seconds or 0)
    label = activity.get("label", "?")
    model = activity.get("model", "?")
    return _truncate(f"▶ {label} · {model} · {elapsed}", _budget_width(width))


def _format_task_row(row: TaskRow, *, width: int, is_selected: bool) -> str:
    width = _budget_width(width)
    gutter = "> " if is_selected else "  "
    # column budgets: id, expert, status get fixed budgets; checkpoint/gate
    # hint fill the remainder so the whole line fits `width`.
    id_col = _truncate(row.id, 12).ljust(12)
    expert_col = _truncate(row.expert, 10).ljust(10)
    status_col = _truncate(row.status, 18).ljust(18)
    gate_col = f"[{row.gate_hint}]" if row.gate_hint else ""
    fixed = f"{gutter}{id_col} {expert_col} {status_col} {gate_col} "
    remaining = max(width - len(fixed), 0)
    checkpoint_col = _truncate(row.checkpoint, remaining)
    line = f"{fixed}{checkpoint_col}"
    return _truncate(line, width)


def _format_event_line(event_summary: str, width: int) -> str:
    return _truncate(event_summary, _budget_width(width))


def build_waiting_frame(*, width: int, reason: str = "") -> Frame:
    """Sensible no-run-yet frame, used when `read_status` raises
    `StatusUnavailable` (or the caller has no run to show yet)."""
    width = _budget_width(width)
    header = _truncate(f"no run available{': ' + reason if reason else ''}", width)
    return Frame(
        title=WAITING_TITLE,
        header=header,
        rows=(),
        footer=_truncate(OVERVIEW_FOOTER, width),
        events=(),
    )


def build_overview_frame(status: RunStatus, *, width: int, selected: int) -> Frame:
    """Pure: the run overview dashboard — header / NOW heartbeat / task
    table / live activity output / recent events / footer, one line per
    `TaskRow`."""
    width = _budget_width(width)
    header = _format_header(status, width)
    now_line = _format_now_line(status, width)
    rows = tuple(
        _format_task_row(row, width=width, is_selected=idx == selected)
        for idx, row in enumerate(status.tasks)
    )
    live_lines = tuple(_truncate(line, width) for line in status.activity_tail)
    events = tuple(
        _format_event_line(f"{event.ts} {event.kind}: {event.summary}", width)
        for event in status.recent_events
    )
    return Frame(
        title=f"ads watch — {status.run_id}",
        header=header,
        rows=rows,
        footer=_truncate(OVERVIEW_FOOTER, width),
        events=events,
        now_line=now_line,
        live_lines=live_lines,
    )


def build_detail_frame(
    status: RunStatus,
    task_id: str,
    scratch_tail: tuple[str, ...],
    *,
    width: int,
    activity_tail: tuple[str, ...] = (),
) -> Frame:
    """Pure: one task's summary + its live activity stream + a bounded
    scratch tail + its recent events.

    The impure reads happen in the caller (`_read_scratch_tail`,
    `_read_activity_tail`); this function only formats the lines it's given.
    """
    width = _budget_width(width)
    matched = next((row for row in status.tasks if row.id == task_id), None)
    if matched is not None:
        summary_line = _format_task_row(matched, width=width, is_selected=False)
    else:
        summary_line = _truncate(f"  {task_id} (not found in current status)", width)

    detail_lines = tuple(_truncate(line, width) for line in scratch_tail)
    live_lines = tuple(_truncate(line, width) for line in activity_tail)
    task_events = tuple(
        _format_event_line(f"{event.ts} {event.kind}: {event.summary}", width)
        for event in status.recent_events
        if task_id in event.summary
    )
    return Frame(
        title=f"ads watch — {status.run_id} — {task_id}",
        header=summary_line,
        rows=(),
        footer=_truncate(DETAIL_FOOTER, width),
        events=task_events,
        detail_title=f"scratch: {task_id}",
        detail_lines=detail_lines,
        live_lines=live_lines,
    )


# ---------------------------------------------------------------------------
# impure: bounded scratch read
# ---------------------------------------------------------------------------


def _read_scratch_tail(layout: RunLayout, task_id: str, *, max_lines: int = 40) -> tuple[str, ...]:
    """Last `max_lines` of `scratch/<task_id>.md`. Read-only, bounded — never
    the whole transcript. `()` if the file doesn't exist."""
    path = layout.scratch_dir / f"{task_id}.md"
    if not path.exists():
        return ()
    lines = path.read_text(encoding="utf-8").splitlines()
    return tuple(lines[-max_lines:])


def _read_activity_tail(layout: RunLayout, label: str, *, max_lines: int = 40) -> tuple[str, ...]:
    """Last `max_lines` of `activity/<label>.log` — the drilled-into task's
    own live/streamed harness output. Read-only, bounded. `()` if the file
    doesn't exist (task hasn't run yet, or isn't the one currently active)."""
    path = layout.activity_dir / f"{label}.log"
    if not path.exists():
        return ()
    lines = path.read_text(encoding="utf-8").splitlines()
    return tuple(lines[-max_lines:])


# ---------------------------------------------------------------------------
# impure: curses drawing + event loop
# ---------------------------------------------------------------------------


def _draw_frame(stdscr: curses.window, frame: Frame) -> None:
    try:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        line_no = 0

        def put(text: str) -> None:
            nonlocal line_no
            if line_no >= height - 1:
                return
            stdscr.addnstr(line_no, 0, text, max(width - 1, 0))
            line_no += 1

        put(frame.title)
        put(frame.header)
        if frame.now_line:
            put(f"NOW: {frame.now_line}")
        put("")
        for row in frame.rows:
            put(row)
        if frame.detail_lines:
            put("")
            put(frame.detail_title)
            for line in frame.detail_lines:
                put(line)
        if frame.live_lines:
            put("")
            put("live:")
            for line in frame.live_lines:
                put(line)
        if frame.events:
            put("")
            put("recent events:")
            for event in frame.events:
                put(event)
        if height >= 2:
            stdscr.addnstr(height - 1, 0, frame.footer, max(width - 1, 0))
        stdscr.refresh()
    except curses.error:
        pass  # terminal too small mid-draw: degrade gracefully, try again next tick


def _clamp_selection(selected: int, task_count: int) -> int:
    if task_count == 0:
        return 0
    return min(max(selected, 0), task_count - 1)


def _tui_loop(stdscr: curses.window, layout: RunLayout, poll_seconds: float) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(int(poll_seconds * 1000))

    selected = 0
    detail_task_id: str | None = None

    while True:
        _, width = stdscr.getmaxyx()
        try:
            status = read_status(layout)
        except StatusUnavailable as exc:
            frame = build_waiting_frame(width=width, reason=str(exc))
            _draw_frame(stdscr, frame)
            status = None
        else:
            selected = _clamp_selection(selected, len(status.tasks))
            if detail_task_id is not None:
                # Re-read every poll, not just on drill-down: a live run's
                # scratch/activity files keep changing while the loop sits
                # here (see the module docstring — this is the whole point).
                scratch_tail = _read_scratch_tail(layout, detail_task_id)
                activity_tail = _read_activity_tail(layout, detail_task_id)
                frame = build_detail_frame(
                    status,
                    detail_task_id,
                    scratch_tail,
                    width=width,
                    activity_tail=activity_tail,
                )
            else:
                frame = build_overview_frame(status, width=width, selected=selected)
            _draw_frame(stdscr, frame)

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return
        if key == 27:  # esc
            detail_task_id = None
            continue
        if status is None:
            continue
        if key in (ord("r"), ord("R")):
            continue
        if detail_task_id is None:
            if key in (curses.KEY_UP, ord("k")):
                selected = _clamp_selection(selected - 1, len(status.tasks))
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = _clamp_selection(selected + 1, len(status.tasks))
            elif key in (curses.KEY_ENTER, ord("\n"), ord("\r")) and status.tasks:
                detail_task_id = status.tasks[selected].id


def run_tui(layout: RunLayout, *, poll_seconds: float = 1.0) -> None:
    """The curses event loop. Never writes to the run dir."""
    if not sys.stdout.isatty():
        raise TUIUnavailable("not a tty; use `driver status` / `driver status --json` instead")
    try:
        curses.wrapper(_tui_loop, layout, poll_seconds)
    except curses.error as exc:
        raise TUIUnavailable(f"curses init failed: {exc}") from exc
