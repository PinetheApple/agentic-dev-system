import tempfile
import unittest
from pathlib import Path

from ads.layout import RunLayout
from ads.state import State, append_event, load_state, save_state


class TestStateAtomicWrite(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.layout = RunLayout(repo=Path(self._tmp.name), run_id="run-1")

    def test_write_reload(self) -> None:
        state = State(phase="dispatch", review_stage=None, gate=None, tasks={"a": "done"})
        save_state(self.layout, state)
        reloaded = load_state(self.layout)
        self.assertEqual(reloaded.phase, "dispatch")
        self.assertEqual(reloaded.tasks, {"a": "done"})
        self.assertNotEqual(reloaded.updated_at, "")

    def test_no_temp_file_left_behind(self) -> None:
        state = State(phase="plan")
        save_state(self.layout, state)
        leftovers = list(self.layout.root.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertTrue(self.layout.state_file.exists())

    def test_append_event_is_jsonl(self) -> None:
        append_event(self.layout, "intake", chars=42)
        append_event(self.layout, "plan", task_count=2)
        lines = self.layout.events.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('"kind": "intake"', lines[0])


if __name__ == "__main__":
    unittest.main()
