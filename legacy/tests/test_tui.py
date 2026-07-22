"""Ticket 010: the TUI is a pure renderer over `RunStatus` — test frame
construction and the bounded scratch tail. curses itself can't be driven
headlessly, so `run_tui`/curses drawing are out of scope here."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from ads.layout import RunLayout
from ads.status import EventLine, RunStatus, TaskRow
from ads.tui import (
    TUIUnavailable,
    _read_activity_tail,  # pyright: ignore[reportPrivateUsage]
    _read_scratch_tail,  # pyright: ignore[reportPrivateUsage]
    build_detail_frame,
    build_overview_frame,
    build_waiting_frame,
    run_tui,
)


def _row(
    task_id: str,
    status: str = "pending",
    expert: str = "coder",
    checkpoint: str = "",
    gate_hint: str = "",
) -> TaskRow:
    return TaskRow(
        id=task_id,
        status=status,
        expert=expert,
        tier="standard",
        critical=False,
        gate_hint=gate_hint,
        checkpoint=checkpoint,
        depends_on=(),
    )


def _status(
    tasks: tuple[TaskRow, ...],
    events: tuple[EventLine, ...] = (),
    *,
    current_activity: dict[str, str] | None = None,
    activity_elapsed_seconds: int | None = None,
    activity_tail: tuple[str, ...] = (),
) -> RunStatus:
    return RunStatus(
        run_id="run-1",
        phase="dispatch",
        review_stage=None,
        gate=None,
        halt_reason=None,
        adapter="stub",
        updated_at="2026-07-13T00:00:00Z",
        attached=False,
        tasks=tasks,
        recent_events=events,
        counts={},
        escalations=(),
        pending_summary="phase dispatch, 1 active",
        current_activity=current_activity,
        activity_elapsed_seconds=activity_elapsed_seconds,
        activity_tail=activity_tail,
    )


class TestBuildOverviewFrame(unittest.TestCase):
    def test_header_contains_run_id_phase_gate_pending_summary(self) -> None:
        status = _status((_row("01-a"),))
        status = dataclasses.replace(status, gate="pending")

        frame = build_overview_frame(status, width=100, selected=0)

        self.assertIn(status.run_id, frame.header)
        self.assertIn(status.phase, frame.header)
        self.assertIn("pending", frame.header)
        self.assertIn(status.pending_summary, frame.header)

    def test_one_row_per_task_and_selected_row_marked(self) -> None:
        status = _status((_row("01-a"), _row("01-b")))

        frame = build_overview_frame(status, width=100, selected=1)

        self.assertEqual(len(frame.rows), 2)
        self.assertFalse(frame.rows[0].startswith(">"))
        self.assertTrue(frame.rows[1].startswith(">"))

    def test_lines_truncated_to_fit_width(self) -> None:
        long_id = "a" * 50
        long_checkpoint = "remaining work " * 20
        status = _status((_row(long_id, checkpoint=long_checkpoint),))

        width = 40
        frame = build_overview_frame(status, width=width, selected=0)

        self.assertLessEqual(len(frame.header), width)
        for line in frame.rows:
            self.assertLessEqual(len(line), width)
        self.assertLessEqual(len(frame.footer), width)
        self.assertIn("…", frame.rows[0])

    def test_footer_has_key_hints(self) -> None:
        status = _status((_row("01-a"),))

        frame = build_overview_frame(status, width=100, selected=0)

        self.assertIn("q quit", frame.footer)
        self.assertIn("enter", frame.footer)
        self.assertIn("refresh", frame.footer)

    def test_gate_hint_surfaces_in_row(self) -> None:
        status = _status((_row("01-a", status="blocked", gate_hint="gated"),))

        frame = build_overview_frame(status, width=100, selected=0)

        self.assertIn("[gated]", frame.rows[0])

    def test_empty_tasks_does_not_crash(self) -> None:
        status = _status(())

        frame = build_overview_frame(status, width=100, selected=0)

        self.assertEqual(frame.rows, ())

    def test_now_line_shows_label_model_and_elapsed_when_active(self) -> None:
        status = _status(
            (_row("01-a"),),
            current_activity={"label": "01-a", "kind": "dispatch", "model": "claude-sonnet-5"},
            activity_elapsed_seconds=65,
        )

        frame = build_overview_frame(status, width=100, selected=0)

        self.assertIn("01-a", frame.now_line)
        self.assertIn("claude-sonnet-5", frame.now_line)
        self.assertIn("01:05", frame.now_line)
        self.assertLessEqual(len(frame.now_line), 100)

    def test_now_line_shows_idle_marker_when_no_activity(self) -> None:
        status = _status((_row("01-a"),))

        frame = build_overview_frame(status, width=100, selected=0)

        self.assertIn("idle", frame.now_line)

    def test_live_panel_contains_activity_tail_lines(self) -> None:
        status = _status(
            (_row("01-a"),),
            current_activity={"label": "01-a", "kind": "dispatch", "model": "claude-sonnet-5"},
            activity_elapsed_seconds=5,
            activity_tail=("→ Read a.py", "  ✓ file contents ok"),
        )

        frame = build_overview_frame(status, width=100, selected=0)

        self.assertEqual(frame.live_lines, ("→ Read a.py", "  ✓ file contents ok"))

    def test_now_line_and_live_lines_truncated_to_width(self) -> None:
        status = _status(
            (_row("01-a"),),
            current_activity={"label": "a" * 60, "kind": "dispatch", "model": "b" * 60},
            activity_elapsed_seconds=5,
            activity_tail=("x" * 200,),
        )

        frame = build_overview_frame(status, width=40, selected=0)

        self.assertLessEqual(len(frame.now_line), 40)
        for line in frame.live_lines:
            self.assertLessEqual(len(line), 40)


class TestBuildDetailFrame(unittest.TestCase):
    def test_shows_task_id_and_scratch_tail(self) -> None:
        status = _status((_row("01-a", status="active"),))
        tail = ("## Objective", "Build a.", "## Remaining", "- wire up b")

        frame = build_detail_frame(status, "01-a", tail, width=100)

        self.assertIn("01-a", frame.header)
        self.assertEqual(frame.detail_lines, tail)

    def test_detail_lines_truncated_to_width(self) -> None:
        status = _status((_row("01-a"),))
        long_line = "x" * 200

        frame = build_detail_frame(status, "01-a", (long_line,), width=40)

        self.assertLessEqual(len(frame.detail_lines[0]), 40)
        self.assertIn("…", frame.detail_lines[0])

    def test_footer_has_back_and_quit_hints(self) -> None:
        status = _status((_row("01-a"),))

        frame = build_detail_frame(status, "01-a", (), width=100)

        self.assertIn("esc back", frame.footer)
        self.assertIn("q quit", frame.footer)

    def test_unknown_task_id_does_not_crash(self) -> None:
        status = _status((_row("01-a"),))

        frame = build_detail_frame(status, "not-a-task", (), width=100)

        self.assertIn("not-a-task", frame.header)

    def test_activity_tail_populates_live_lines_separately_from_scratch(self) -> None:
        status = _status((_row("01-a", status="active"),))
        scratch = ("## Objective", "Build a.")
        activity = ("→ Read a.py", "  ✓ ok")

        frame = build_detail_frame(status, "01-a", scratch, width=100, activity_tail=activity)

        self.assertEqual(frame.detail_lines, scratch)
        self.assertEqual(frame.live_lines, activity)


class TestBuildWaitingFrame(unittest.TestCase):
    def test_renders_without_crashing_on_no_run(self) -> None:
        frame = build_waiting_frame(width=100)

        self.assertIn("no run", frame.header)
        self.assertEqual(frame.rows, ())

    def test_degenerate_width_does_not_crash(self) -> None:
        frame = build_waiting_frame(width=0, reason="no run state at state.json")

        self.assertTrue(frame.header)


class TestReadScratchTail(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-1")
        self.layout.scaffold()

    def test_returns_last_n_lines(self) -> None:
        lines = [f"line {i}" for i in range(100)]
        (self.layout.scratch_dir / "01-a.md").write_text("\n".join(lines), encoding="utf-8")

        tail = _read_scratch_tail(self.layout, "01-a", max_lines=10)

        self.assertEqual(len(tail), 10)
        self.assertEqual(tail[-1], "line 99")
        self.assertEqual(tail[0], "line 90")

    def test_missing_file_returns_empty_tuple(self) -> None:
        tail = _read_scratch_tail(self.layout, "does-not-exist")

        self.assertEqual(tail, ())

    def test_never_exceeds_max_lines(self) -> None:
        (self.layout.scratch_dir / "01-b.md").write_text("only one line", encoding="utf-8")

        tail = _read_scratch_tail(self.layout, "01-b", max_lines=40)

        self.assertLessEqual(len(tail), 40)
        self.assertEqual(tail, ("only one line",))


class TestReadActivityTail(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-1")
        self.layout.scaffold()

    def test_returns_last_n_lines(self) -> None:
        lines = [f"line {i}" for i in range(100)]
        (self.layout.activity_dir / "01-a.log").write_text("\n".join(lines), encoding="utf-8")

        tail = _read_activity_tail(self.layout, "01-a", max_lines=10)

        self.assertEqual(len(tail), 10)
        self.assertEqual(tail[-1], "line 99")
        self.assertEqual(tail[0], "line 90")

    def test_missing_file_returns_empty_tuple(self) -> None:
        tail = _read_activity_tail(self.layout, "does-not-exist")

        self.assertEqual(tail, ())


class TestRunTuiGuard(unittest.TestCase):
    def test_raises_tui_unavailable_when_not_a_tty(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        layout = RunLayout(repo=Path(self._tmp.name), run_id="run-1")

        with self.assertRaises(TUIUnavailable):
            run_tui(layout)


if __name__ == "__main__":
    unittest.main()
