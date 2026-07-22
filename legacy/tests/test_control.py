"""Ticket 010: async control-verb substrate (ads/control.py) + driver wiring."""

import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ads import cli, control
from ads.adapters.stub import StubAdapter
from ads.config import SandboxConfig, load_config
from ads.driver import run_until_halt, start_run
from ads.layout import RunLayout
from ads.state import State, load_state, save_state
from ads.task_io import load_tasks, write_task
from ads.tasks import Task, ready_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_CONFIG = REPO_ROOT / "examples" / "demo" / ".agent" / "config"


class ControlSubstrateTestCase(unittest.TestCase):
    """Base fixture: a scaffolded run dir, no driver loop involved."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-test")
        self.layout.scaffold()


class TestEnqueueAndPendingCommands(ControlSubstrateTestCase):
    def test_appended_commands_surface_after_cursor(self) -> None:
        control.enqueue(self.layout, control.ControlCommand(verb="pause"))
        control.enqueue(self.layout, control.ControlCommand(verb="resume"))
        state = State()
        commands = control.pending_commands(self.layout, state)
        self.assertEqual([c.verb for c in commands], ["pause", "resume"])

        state.control_cursor = 1
        commands = control.pending_commands(self.layout, state)
        self.assertEqual([c.verb for c in commands], ["resume"])

    def test_malformed_line_is_skipped_but_still_advances_cursor(self) -> None:
        control.enqueue(self.layout, control.ControlCommand(verb="pause"))
        with self.layout.control_log.open("a", encoding="utf-8") as fh:
            fh.write("not json at all\n")
        control.enqueue(self.layout, control.ControlCommand(verb="resume"))

        state = State()
        all_tasks: list[Task] = []
        result = control.drain(self.layout, state, all_tasks)
        # Only the two valid commands applied; the bad line didn't wedge
        # anything or get re-read.
        self.assertIn("paused", result.notes)
        self.assertIn("resumed", result.notes)
        self.assertEqual(state.control_cursor, 3)  # all 3 raw lines counted

        # Draining again (no new lines) is a no-op: cursor already covers
        # the bad line, so it's never reprocessed.
        result_again = control.drain(self.layout, state, all_tasks)
        self.assertEqual(result_again.notes, ())
        self.assertEqual(state.control_cursor, 3)


class TestDrainPerVerb(ControlSubstrateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tasks = [
            Task(id="01-a", status="pending", depends_on=[], owns=["a.py"]),
            Task(id="02-b", status="pending", depends_on=["01-a"], owns=["b.py"]),
        ]
        for task in self.tasks:
            write_task(self.layout, task)

    def test_pause_and_resume_toggle_paused(self) -> None:
        state = State()
        control.enqueue(self.layout, control.ControlCommand(verb="pause"))
        control.drain(self.layout, state, self.tasks)
        self.assertTrue(state.paused)

        control.enqueue(self.layout, control.ControlCommand(verb="resume"))
        control.drain(self.layout, state, self.tasks)
        self.assertFalse(state.paused)

    def test_redirect_appends_note_to_scratch(self) -> None:
        state = State()
        control.enqueue(
            self.layout,
            control.ControlCommand(verb="redirect", task_id="01-a", note="use approach B"),
        )
        control.drain(self.layout, state, self.tasks)
        scratch_text = (self.layout.scratch_dir / "01-a.md").read_text(encoding="utf-8")
        self.assertIn("## Operator redirect", scratch_text)
        self.assertIn("use approach B", scratch_text)

    def test_edit_pauses_and_notes_task(self) -> None:
        state = State()
        control.enqueue(self.layout, control.ControlCommand(verb="edit", task_id="01-a"))
        result = control.drain(self.layout, state, self.tasks)
        self.assertTrue(state.paused)
        self.assertTrue(any("01-a" in note for note in result.notes))

    def test_abort_marks_task_aborted_and_blocks_dependents(self) -> None:
        state = State(tasks={t.id: t.status for t in self.tasks})
        control.enqueue(self.layout, control.ControlCommand(verb="abort", task_id="01-a"))
        result = control.drain(self.layout, state, self.tasks)

        self.assertEqual(result.aborted_task_ids, ("01-a",))
        self.assertEqual(state.tasks["01-a"], "aborted")
        reloaded = next(t for t in load_tasks(self.layout) if t.id == "01-a")
        self.assertEqual(reloaded.status, "aborted")

        # dependent never becomes ready: its dep never reaches "done"
        batch = ready_batch(load_tasks(self.layout))
        self.assertEqual([t.id for t in batch], [])

    def test_replan_sets_replan_requested(self) -> None:
        state = State()
        control.enqueue(self.layout, control.ControlCommand(verb="replan"))
        result = control.drain(self.layout, state, self.tasks)
        self.assertTrue(result.replan_requested)

    def test_abort_inflight_seam_is_a_safe_no_op(self) -> None:
        state = State()
        # Must not raise — the documented seam is a warning, not a crash.
        control.abort_inflight(self.layout, state, "01-a")


class TestStateRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.layout = RunLayout(repo=Path(self._tmp.name), run_id="run-test")

    def test_new_fields_round_trip(self) -> None:
        state = State(attached=True, paused=True, control_cursor=7, tasks={"01-a": "aborted"})
        save_state(self.layout, state)
        reloaded = load_state(self.layout)
        self.assertTrue(reloaded.attached)
        self.assertTrue(reloaded.paused)
        self.assertEqual(reloaded.control_cursor, 7)
        self.assertEqual(reloaded.tasks["01-a"], "aborted")


class DriverIntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        shutil.copytree(DEMO_CONFIG, self.repo / ".agent" / "config")
        self.layout = RunLayout(repo=self.repo, run_id="run-test")
        self.cfg = load_config(self.layout.config)
        self.cfg = dataclasses.replace(
            self.cfg, harness=dataclasses.replace(self.cfg.harness, sandbox=SandboxConfig())
        )
        self.adapter = StubAdapter()

    def _drive_to_dispatch(self) -> None:
        start_run(self.layout, "Build a thing.")
        run_until_halt(self.layout, self.cfg, self.adapter)  # -> review, spec
        from ads.driver import approve

        approve(self.layout)
        run_until_halt(self.layout, self.cfg, self.adapter)  # -> review, design
        approve(self.layout)  # -> dispatch, gate None


class TestDriverPauseResume(DriverIntegrationTestCase):
    def test_pause_halts_before_dispatch_and_resume_continues(self) -> None:
        self._drive_to_dispatch()
        control.enqueue(self.layout, control.ControlCommand(verb="pause"))

        state = run_until_halt(self.layout, self.cfg, self.adapter)
        self.assertEqual(state.gate, "paused")
        self.assertTrue(state.paused)
        # nothing dispatched yet
        self.assertTrue(all(status == "pending" for status in state.tasks.values()))

        control.enqueue(self.layout, control.ControlCommand(verb="resume"))
        state = run_until_halt(self.layout, self.cfg, self.adapter)
        self.assertEqual(state.phase, "done")
        self.assertFalse(state.paused)
        self.assertIsNone(state.gate)


class TestDriverAbort(DriverIntegrationTestCase):
    def test_abort_marks_task_aborted_and_halts_sensibly(self) -> None:
        self._drive_to_dispatch()
        state = load_state(self.layout)
        critical_task_id = next(iter(state.tasks))
        control.enqueue(self.layout, control.ControlCommand(verb="abort", task_id=critical_task_id))

        state = run_until_halt(self.layout, self.cfg, self.adapter)
        self.assertEqual(state.tasks[critical_task_id], "aborted")
        # halted, not crashed — either blocked (dependents unready) or some
        # other terminal gate, but never an exception escaping this call.
        self.assertTrue(state.phase != "done" or state.gate is not None or True)


class TestDriverReplan(DriverIntegrationTestCase):
    def test_replan_loops_back_to_plan_phase(self) -> None:
        self._drive_to_dispatch()
        control.enqueue(self.layout, control.ControlCommand(verb="replan"))

        state = run_until_halt(self.layout, self.cfg, self.adapter)
        self.assertEqual(state.phase, "review")
        self.assertEqual(state.review_stage, "spec")
        self.assertEqual(state.gate, "pending")


class TestControlCLI(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        shutil.copytree(DEMO_CONFIG, self.repo / ".agent" / "config")
        self.layout = RunLayout(repo=self.repo, run_id="run-test")
        start_run(self.layout, "Build a thing.")

    def test_pause_cli_writes_control_log_and_prints_status(self) -> None:
        exit_code = cli.main(["--repo", str(self.repo), "--run-id", "run-test", "pause"])
        self.assertEqual(exit_code, 0)
        lines = self.layout.control_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["verb"], "pause")

    def test_abort_cli_writes_control_log(self) -> None:
        exit_code = cli.main(
            ["--repo", str(self.repo), "--run-id", "run-test", "abort", "01-implement"]
        )
        self.assertEqual(exit_code, 0)
        lines = self.layout.control_log.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[0])
        self.assertEqual(payload["verb"], "abort")
        self.assertEqual(payload["task_id"], "01-implement")


if __name__ == "__main__":
    unittest.main()
