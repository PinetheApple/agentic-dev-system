"""Full intake -> plan -> review -> dispatch -> validate -> done loop against
the stub adapter — no tokens spent, proves the spine end to end."""

import shutil
import tempfile
import unittest
from pathlib import Path

from ads.adapters.stub import StubAdapter
from ads.config import load_config
from ads.driver import approve, reject, run_until_halt, start_run
from ads.layout import RunLayout
from ads.state import load_state

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_CONFIG = REPO_ROOT / "examples" / "demo" / ".agent" / "config"


class TestDriverStubLoop(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        shutil.copytree(DEMO_CONFIG, self.repo / ".agent" / "config")
        self.layout = RunLayout(repo=self.repo, run_id="run-test")
        self.cfg = load_config(self.layout.config)
        self.adapter = StubAdapter()

    def test_full_loop_with_gates(self) -> None:
        start_run(self.layout, "Build a thing.")
        self.assertEqual(load_state(self.layout).phase, "plan")

        state = run_until_halt(self.layout, self.cfg, self.adapter)
        self.assertEqual(state.phase, "review")
        self.assertEqual(state.review_stage, "spec")
        self.assertEqual(state.gate, "pending")
        self.assertTrue(self.layout.spec.exists())
        self.assertTrue(self.layout.design.exists())
        self.assertEqual(len(list(self.layout.tasks_dir.glob("*.md"))), 2)

        state = approve(self.layout)
        self.assertEqual(state.review_stage, "design")
        self.assertEqual(state.gate, "pending")

        state = run_until_halt(self.layout, self.cfg, self.adapter)
        self.assertEqual(state.phase, "review")  # still waiting on design-stage gate

        state = approve(self.layout)
        self.assertEqual(state.phase, "dispatch")
        self.assertIsNone(state.gate)

        state = run_until_halt(self.layout, self.cfg, self.adapter)
        self.assertEqual(state.phase, "done")
        self.assertTrue(all(status == "done" for status in state.tasks.values()))

    def test_reject_loops_back_to_plan_and_freezes_spec(self) -> None:
        start_run(self.layout, "Build a thing.")
        run_until_halt(self.layout, self.cfg, self.adapter)
        spec_before = self.layout.spec.read_text(encoding="utf-8")

        state = approve(self.layout)  # spec -> design stage
        self.assertEqual(state.review_stage, "design")

        state = reject(self.layout, "design needs more detail")
        self.assertEqual(state.phase, "plan")
        self.assertEqual(state.retry_counts["review_to_plan"], 1)
        # rejection note lands on the artifact under review, before replan overwrites it
        design_with_notes = self.layout.design.read_text(encoding="utf-8")
        self.assertIn("Review Notes", design_with_notes)

        state = run_until_halt(self.layout, self.cfg, self.adapter)
        self.assertEqual(state.phase, "review")
        self.assertEqual(state.review_stage, "design")  # spec was frozen, straight to design stage
        spec_after = self.layout.spec.read_text(encoding="utf-8")
        self.assertEqual(
            spec_after, spec_before
        )  # spec frozen: untouched by the design-stage rejection

    def test_review_retry_exhaustion_halts(self) -> None:
        start_run(self.layout, "Build a thing.")
        run_until_halt(self.layout, self.cfg, self.adapter)

        for _ in range(2):
            reject(self.layout, "not good enough")
            run_until_halt(self.layout, self.cfg, self.adapter)

        state = reject(self.layout, "still not good enough")
        self.assertEqual(state.gate, "blocked")
        self.assertIsNotNone(state.halt_reason)
        assert state.halt_reason is not None  # narrow for type-checkers
        self.assertIn("exhausted", state.halt_reason)

    def test_resume_is_idempotent_after_done(self) -> None:
        start_run(self.layout, "Build a thing.")
        run_until_halt(self.layout, self.cfg, self.adapter)
        approve(self.layout)
        run_until_halt(self.layout, self.cfg, self.adapter)
        approve(self.layout)
        run_until_halt(self.layout, self.cfg, self.adapter)

        state_before = load_state(self.layout)
        self.assertEqual(state_before.phase, "done")
        state_after = run_until_halt(self.layout, self.cfg, self.adapter)
        self.assertEqual(state_after.phase, "done")


if __name__ == "__main__":
    unittest.main()
