"""Ticket 010 foundation: the pure run-status read model + --json floor."""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ads import cli
from ads.layout import RunLayout
from ads.state import State, append_event, save_state
from ads.status import (
    StatusUnavailable,
    _event_summary,  # pyright: ignore[reportPrivateUsage]
    _scratch_checkpoint,  # pyright: ignore[reportPrivateUsage]
    read_status,
    render_plain,
    to_json,
)
from ads.task_io import write_task
from ads.tasks import ExitCriterion, Task


def _task(
    task_id: str,
    status: str = "pending",
    expert: str = "coder",
    tier: str = "standard",
    critical: bool = False,
    depends_on: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        status=status,  # type: ignore[arg-type]
        depends_on=depends_on or [],
        owns=["a.py"],
        exit_criteria=[ExitCriterion(check="cmd", value="pytest")],
        expert=expert,
        critical=critical,
        tier=tier,  # type: ignore[arg-type]
        body="Do the thing.",
    )


class TestStatusReadModel(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-1")
        self.layout.scaffold()

    def _write_scratch(self, task_id: str, text: str) -> None:
        (self.layout.scratch_dir / f"{task_id}.md").write_text(text, encoding="utf-8")

    def test_join_state_and_task_files(self) -> None:
        write_task(self.layout, _task("01-a", status="done", expert="coder", critical=True))
        write_task(self.layout, _task("01-b", status="pending", depends_on=["01-a"]))
        state = State(phase="dispatch", tasks={"01-a": "done", "01-b": "pending"})
        save_state(self.layout, state)

        result = read_status(self.layout)

        by_id = {row.id: row for row in result.tasks}
        self.assertEqual(by_id["01-a"].status, "done")
        self.assertEqual(by_id["01-a"].expert, "coder")
        self.assertTrue(by_id["01-a"].critical)
        self.assertEqual(by_id["01-b"].depends_on, ("01-a",))
        self.assertEqual(result.counts, {"done": 1, "pending": 1})

    def test_task_in_state_without_file_still_emits_row(self) -> None:
        state = State(phase="dispatch", tasks={"ghost": "blocked"})
        save_state(self.layout, state)

        result = read_status(self.layout)

        self.assertEqual(len(result.tasks), 1)
        self.assertEqual(result.tasks[0].id, "ghost")
        self.assertEqual(result.tasks[0].status, "blocked")
        self.assertEqual(result.tasks[0].expert, "")

    def test_scratch_checkpoint_prefers_remaining_then_objective_then_empty(self) -> None:
        path = self.layout.scratch_dir / "x.md"
        path.write_text(
            "## Objective\nBuild x.\n\n## Done\n\n## Remaining\n- finish y\n- finish z\n",
            encoding="utf-8",
        )
        self.assertEqual(_scratch_checkpoint(path), "- finish y")

        path.write_text("## Objective\nBuild x.\n\n## Remaining\n\n", encoding="utf-8")
        self.assertEqual(_scratch_checkpoint(path), "Build x.")

        path.write_text("## Decisions\nnothing relevant\n", encoding="utf-8")
        self.assertEqual(_scratch_checkpoint(path), "")

        self.assertEqual(_scratch_checkpoint(self.layout.scratch_dir / "missing.md"), "")

    def test_checkpoint_flows_into_task_row(self) -> None:
        write_task(self.layout, _task("01-a"))
        self._write_scratch("01-a", "## Objective\nBuild a.\n\n## Remaining\n- wire up b\n")
        save_state(self.layout, State(phase="dispatch", tasks={"01-a": "active"}))

        result = read_status(self.layout)

        self.assertEqual(result.tasks[0].checkpoint, "- wire up b")

    def test_recent_events_tail_skips_malformed_and_renders_known_kinds(self) -> None:
        append_event(self.layout, "plan", task_count=3)
        with self.layout.events.open("a", encoding="utf-8") as fh:
            fh.write("not json\n")
        append_event(self.layout, "halt", reason="budget exceeded")
        append_event(self.layout, "escalation_open", id="esc-1", op="run-cmd")
        append_event(self.layout, "dispatch_batch", task_ids=["01-a", "01-b"])
        save_state(self.layout, State(phase="dispatch"))

        result = read_status(self.layout, event_tail=10)

        kinds = [e.kind for e in result.recent_events]
        self.assertEqual(kinds, ["plan", "halt", "escalation_open", "dispatch_batch"])
        summaries = {e.kind: e.summary for e in result.recent_events}
        self.assertEqual(summaries["plan"], "planned 3 tasks")
        self.assertEqual(summaries["halt"], "halt: budget exceeded")
        self.assertEqual(summaries["escalation_open"], "escalation esc-1 (run-cmd)")
        self.assertEqual(summaries["dispatch_batch"], "dispatched: 01-a, 01-b")

    def test_event_summary_default_formatter(self) -> None:
        summary = _event_summary("intake", {"chars": 42})
        self.assertEqual(summary, "intake: chars=42")

    def test_pending_summary_and_gate_hint_review_pending(self) -> None:
        write_task(self.layout, _task("01-a"))
        save_state(
            self.layout,
            State(phase="review", review_stage="spec", gate="pending", tasks={"01-a": "pending"}),
        )
        result = read_status(self.layout)
        self.assertEqual(result.pending_summary, "awaiting spec approval")
        self.assertEqual(result.tasks[0].gate_hint, "")

    def test_pending_summary_and_gate_hint_escalation(self) -> None:
        write_task(self.layout, _task("01-a", status="needs-escalation"))
        save_state(
            self.layout,
            State(
                phase="dispatch",
                gate="escalation",
                tasks={"01-a": "needs-escalation"},
                escalations={"esc-01-a-1": "pending"},
            ),
        )
        result = read_status(self.layout)
        self.assertEqual(result.pending_summary, "awaiting escalation approval: esc-01-a-1")
        self.assertEqual(result.tasks[0].gate_hint, "gated")
        self.assertEqual(result.escalations, ("esc-01-a-1",))

    def test_pending_summary_reconcile(self) -> None:
        save_state(
            self.layout,
            State(phase="dispatch", gate="reconcile", halt_reason="merge conflict on a.py"),
        )
        result = read_status(self.layout)
        self.assertEqual(result.pending_summary, "awaiting reconcile: merge conflict on a.py")

    def test_pending_summary_blocked(self) -> None:
        write_task(self.layout, _task("01-a", status="blocked"))
        save_state(
            self.layout,
            State(
                phase="dispatch",
                gate="blocked",
                halt_reason="review retries exhausted",
                tasks={"01-a": "blocked"},
            ),
        )
        result = read_status(self.layout)
        self.assertEqual(result.pending_summary, "blocked: review retries exhausted")
        self.assertEqual(result.tasks[0].gate_hint, "gated")

    def test_pending_summary_done(self) -> None:
        save_state(self.layout, State(phase="done", tasks={"01-a": "done"}))
        result = read_status(self.layout)
        self.assertEqual(result.pending_summary, "complete")

    def test_to_json_round_trips(self) -> None:
        write_task(self.layout, _task("01-a"))
        save_state(self.layout, State(phase="dispatch", tasks={"01-a": "pending"}))
        result = read_status(self.layout)

        payload = json.loads(to_json(result))

        self.assertEqual(payload["run_id"], "run-1")
        self.assertIn("tasks", payload)
        self.assertIn("counts", payload)
        self.assertEqual(payload["tasks"][0]["id"], "01-a")

    def test_render_plain_contains_run_id_tasks_and_pending_summary(self) -> None:
        write_task(self.layout, _task("01-a"))
        write_task(self.layout, _task("01-b"))
        save_state(self.layout, State(phase="dispatch", tasks={"01-a": "pending", "01-b": "done"}))
        result = read_status(self.layout)

        text = render_plain(result)

        self.assertIn("run-1", text)
        self.assertIn("01-a", text)
        self.assertIn("01-b", text)
        self.assertIn(result.pending_summary, text)

    def test_missing_run_dir_raises_status_unavailable(self) -> None:
        empty_layout = RunLayout(repo=self.repo, run_id="does-not-exist")
        with self.assertRaises(StatusUnavailable):
            read_status(empty_layout)

    def test_current_activity_surfaces_elapsed_and_tail_when_active(self) -> None:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
        save_state(
            self.layout,
            State(
                phase="dispatch",
                current_activity={
                    "label": "01-a",
                    "kind": "dispatch",
                    "model": "claude-sonnet-5",
                    "started_at": started_at,
                },
            ),
        )
        activity_log = self.layout.activity_dir / "01-a.log"
        activity_log.parent.mkdir(parents=True, exist_ok=True)
        activity_log.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")

        result = read_status(self.layout)

        self.assertIsNotNone(result.current_activity)
        assert result.current_activity is not None
        self.assertEqual(result.current_activity["label"], "01-a")
        assert result.activity_elapsed_seconds is not None
        self.assertGreaterEqual(result.activity_elapsed_seconds, 0)
        self.assertEqual(result.activity_tail, ("line 1", "line 2", "line 3"))

    def test_current_activity_idle_when_state_has_none(self) -> None:
        save_state(self.layout, State(phase="dispatch"))

        result = read_status(self.layout)

        self.assertIsNone(result.current_activity)
        self.assertIsNone(result.activity_elapsed_seconds)
        self.assertEqual(result.activity_tail, ())

    def test_render_plain_shows_now_line_when_active(self) -> None:
        save_state(
            self.layout,
            State(
                phase="dispatch",
                current_activity={
                    "label": "01-a",
                    "kind": "dispatch",
                    "model": "claude-sonnet-5",
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            ),
        )

        text = render_plain(read_status(self.layout))

        self.assertIn("NOW:", text)
        self.assertIn("01-a", text)
        self.assertIn("claude-sonnet-5", text)

    def test_render_plain_shows_idle_marker_when_no_run_active(self) -> None:
        save_state(self.layout, State(phase="dispatch"))

        text = render_plain(read_status(self.layout))

        self.assertIn("idle", text)

    def test_defensive_read_without_attached_or_escalations_fields(self) -> None:
        """A state.json written before the later control slice adds
        `attached` still reads fine here — defaults false/empty."""
        raw_state_path = self.layout.state_file
        self.layout.root.mkdir(parents=True, exist_ok=True)
        raw_state_path.write_text(
            json.dumps({"phase": "dispatch", "tasks": {"01-a": "pending"}}), encoding="utf-8"
        )

        result = read_status(self.layout)

        self.assertFalse(result.attached)
        self.assertEqual(result.escalations, ())


class TestStatusCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-1")
        self.layout.scaffold()
        write_task(self.layout, _task("01-a"))
        save_state(self.layout, State(phase="dispatch", tasks={"01-a": "pending"}))

    def test_status_json_flag_prints_valid_json(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--repo", str(self.repo), "--run-id", "run-1", "status", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["run_id"], "run-1")

    def test_status_without_json_flag_prints_plain_render(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--repo", str(self.repo), "--run-id", "run-1", "status"])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("run:", out)
        self.assertIn("01-a", out)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


if __name__ == "__main__":
    unittest.main()
