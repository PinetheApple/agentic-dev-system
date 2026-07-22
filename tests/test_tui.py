"""Ticket 010: `ads/tui.py` pure helpers must be importable and exercisable
without `rich` installed (SPEC §7's token-free test suite), and `--help`
must work before `rich` is ever touched."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from ads.layout import RunLayout
from ads.tui import event_summary, main, progress, read_snapshot, tail_events

VALID_STATE = {
    "phase": "execute",
    "review_stage": None,
    "gate": None,
    "tasks": {"01-a": "done", "02-b": "pending"},
    "attempts": {"02-b": 1},
    "cursor": "02-b",
    "halt_reason": None,
    "adapter": "stub",
    "updated_at": "2026-01-01T00:00:00Z",
    "event_seq": 2,
    "question": None,
}


@contextmanager
def _layout() -> Generator[RunLayout]:
    with tempfile.TemporaryDirectory() as tmp:
        layout = RunLayout(repo=Path(tmp), run_id="current")
        layout.root.mkdir(parents=True, exist_ok=True)
        yield layout


class ReadSnapshotTest(unittest.TestCase):
    def test_missing_state_file_is_waiting_not_error(self) -> None:
        with _layout() as layout:
            snapshot = read_snapshot(layout)
        self.assertIsNone(snapshot.state)
        self.assertIsNone(snapshot.error)

    def test_malformed_json_is_error_snapshot(self) -> None:
        with _layout() as layout:
            layout.state_file.write_text("{not json", encoding="utf-8")
            snapshot = read_snapshot(layout)
        self.assertIsNone(snapshot.state)
        self.assertIsNotNone(snapshot.error)

    def test_bad_schema_is_error_snapshot(self) -> None:
        with _layout() as layout:
            bad = {**VALID_STATE, "phase": "not-a-real-phase"}
            layout.state_file.write_text(json.dumps(bad), encoding="utf-8")
            snapshot = read_snapshot(layout)
        self.assertIsNone(snapshot.state)
        self.assertIsNotNone(snapshot.error)

    def test_non_object_json_is_error_snapshot_not_a_crash(self) -> None:
        with _layout() as layout:
            layout.state_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            snapshot = read_snapshot(layout)
        self.assertIsNone(snapshot.state)
        self.assertIsNotNone(snapshot.error)

    def test_valid_state_file_is_populated_snapshot(self) -> None:
        with _layout() as layout:
            layout.state_file.write_text(json.dumps(VALID_STATE), encoding="utf-8")
            snapshot = read_snapshot(layout)
        self.assertIsNotNone(snapshot.state)
        self.assertIsNone(snapshot.error)
        assert snapshot.state is not None
        self.assertEqual(snapshot.state.phase, "execute")
        self.assertEqual(snapshot.halt_label, None)


class TailEventsTest(unittest.TestCase):
    def test_missing_events_file_returns_empty(self) -> None:
        with _layout() as layout:
            events = tail_events(layout, 10)
        self.assertEqual(events, [])

    def test_malformed_trailing_line_is_dropped(self) -> None:
        with _layout() as layout:
            lines = [
                json.dumps({"ts": "t1", "seq": 1, "phase": "plan", "type": "run:start"}),
                '{"ts": "t2", "seq": 2, "phase": "plan", "type": "activity"',  # truncated
            ]
            layout.events.write_text("\n".join(lines) + "\n", encoding="utf-8")
            events = tail_events(layout, 10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["seq"], 1)

    def test_respects_limit(self) -> None:
        with _layout() as layout:
            lines = [
                json.dumps({"ts": f"t{i}", "seq": i, "phase": "plan", "type": "activity"})
                for i in range(5)
            ]
            layout.events.write_text("\n".join(lines) + "\n", encoding="utf-8")
            events = tail_events(layout, 2)
        self.assertEqual([e["seq"] for e in events], [3, 4])


class ProgressTest(unittest.TestCase):
    def test_counts_done_against_total(self) -> None:
        with _layout() as layout:
            layout.state_file.write_text(json.dumps(VALID_STATE), encoding="utf-8")
            snapshot = read_snapshot(layout)
        assert snapshot.state is not None
        self.assertEqual(progress(snapshot.state), (1, 2))


class EventSummaryTest(unittest.TestCase):
    def test_summary_includes_seq_type_and_task(self) -> None:
        summary = event_summary(
            {"ts": "t1", "seq": 1, "phase": "execute", "type": "task:done", "task": "01-a"}
        )
        self.assertEqual(summary, "1 · task:done · 01-a")

    def test_summary_includes_clipped_data(self) -> None:
        event = {
            "ts": "t1",
            "seq": 3,
            "phase": "execute",
            "type": "activity",
            "task": None,
            "data": {"note": "x" * 100},
        }
        summary = event_summary(event)
        self.assertTrue(summary.startswith("3 · activity · -"))
        self.assertIn("…", summary)


class MainHelpTest(unittest.TestCase):
    def test_help_exits_zero_without_rich(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            main(["--help"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
