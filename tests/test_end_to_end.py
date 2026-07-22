"""Drives the whole loop on the stub adapter, token-free, through the full
happy path: init -> start -> approve(plan) -> [execute+validate loop drains
automatically] -> approve(signoff) -> done.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from ads import cli
from ads.layout import AGENT_DIR, RUNS_DIRNAME, RunLayout
from ads.state import load_state
from tests.helpers import temp_git_repo


class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self._repo_cm = temp_git_repo()
        self.repo = self._repo_cm.__enter__()
        self._prev_cwd = Path.cwd()
        os.chdir(self.repo)

    def tearDown(self) -> None:
        os.chdir(self._prev_cwd)
        self._repo_cm.__exit__(None, None, None)

    def _current_layout(self) -> RunLayout:
        link = self.repo / AGENT_DIR / RUNS_DIRNAME / "current"
        return RunLayout(repo=self.repo, run_id=link.resolve().name)

    def _events(self, layout: RunLayout) -> list[dict[str, object]]:
        text = layout.events.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def test_full_happy_path(self) -> None:
        self.assertEqual(cli.main(["init", "--adapter", "stub"]), 0)
        layout = self._current_layout()
        state = load_state(layout)
        self.assertEqual(state.phase, "intake")
        self.assertEqual(state.adapter, "stub")

        # start -> intake -> plan -> halts at awaiting_plan_approval (review).
        self.assertEqual(cli.main(["start", "build the thing"]), 0)
        self.assertEqual(layout.intent.read_text(encoding="utf-8"), "build the thing")
        state = load_state(layout)
        self.assertEqual(state.phase, "review")
        self.assertIsNone(state.review_stage)
        self.assertTrue(layout.spec.exists())
        self.assertEqual(set(state.tasks), {"01-implement", "02-test"})
        self.assertTrue(all(status == "pending" for status in state.tasks.values()))

        # approve plan -> drains execute+validate for both tasks -> halts at
        # awaiting_signoff (phase=validate, cursor=None, all tasks done).
        self.assertEqual(cli.main(["approve"]), 0)
        state = load_state(layout)
        self.assertEqual(state.phase, "validate")
        self.assertIsNone(state.cursor)
        self.assertEqual(state.tasks, {"01-implement": "done", "02-test": "done"})
        self.assertTrue((self.repo / "src" / "thing.py").exists())
        self.assertTrue((self.repo / "tests" / "test_thing.py").exists())

        # approve signoff -> done.
        self.assertEqual(cli.main(["approve"]), 0)
        state = load_state(layout)
        self.assertEqual(state.phase, "done")

    def test_events_are_append_only_and_gap_free(self) -> None:
        cli.main(["init", "--adapter", "stub"])
        layout = self._current_layout()
        cli.main(["start", "build the thing"])
        cli.main(["approve"])
        cli.main(["approve"])

        events = self._events(layout)
        self.assertGreater(len(events), 0)
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, list(range(1, len(events) + 1)))
        for event in events:
            self.assertEqual(set(event.keys()), {"ts", "seq", "phase", "type", "task", "data"})
        state = load_state(layout)
        self.assertEqual(state.event_seq, len(events))

    def test_stale_approve_at_rejected(self) -> None:
        cli.main(["init", "--adapter", "stub"])
        cli.main(["start", "build the thing"])
        with self.assertRaises(SystemExit):
            cli.main(["approve", "--at", "blocked"])

    def test_approve_at_matches_current_halt(self) -> None:
        cli.main(["init", "--adapter", "stub"])
        cli.main(["start", "build the thing"])
        self.assertEqual(cli.main(["approve", "--at", "awaiting_plan_approval"]), 0)


if __name__ == "__main__":
    unittest.main()
