"""Ticket 007: the validate phase's three gates — grounded `cmd` gate,
judgment critic (with structural anti-rubber-stamp enforcement), and the
per-run integration critic — plus the retry-bounded state machine around
them in `ads/driver.py`.

Uses a `ScriptedAdapter` (test-only) so each critic call's verdict is fully
controlled, and a stub adapter's task-status shape for `cmd`-only dispatch
where a real run() isn't needed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ads import validate
from ads.adapters.base import RunResult, StructuredPayload
from ads.config import Config, HarnessConfig, PromptDoc
from ads.driver import MAX_RETRIES, _run_validate  # pyright: ignore[reportPrivateUsage]
from ads.layout import RunLayout
from ads.state import State
from ads.task_io import write_task
from ads.tasks import ExitCriterion, Task, TaskTier

VALIDATE_JUDGMENT_BODY = "PHASE:validate-judgment\n\n{criterion}\n\n{diff}\n"
VALIDATE_INTEGRATION_BODY = "PHASE:validate-integration\n\n{diff}\n"

PASS_PAYLOAD: StructuredPayload = {"pass": True, "evidence": "looks fine", "cited_paths": ["x.py"]}


class ScriptedAdapter:
    """Test-only adapter: returns a caller-configured structured payload
    keyed off which phase marker is embedded in the composed prompt, so a
    single adapter instance can drive dispatch, judgment, and integration
    calls with independently controlled verdicts."""

    def __init__(
        self,
        judgment_payload: StructuredPayload | None = None,
        integration_payload: StructuredPayload | None = None,
        capabilities: list[str] | None = None,
    ) -> None:
        self._judgment_payload = judgment_payload or PASS_PAYLOAD
        self._integration_payload = integration_payload or PASS_PAYLOAD
        self._capabilities = list(capabilities) if capabilities is not None else []

    def resolve_model(self, tier: TaskTier) -> str:
        return "scripted"

    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def sync(self) -> None:
        pass

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
    ) -> RunResult:
        payload: StructuredPayload
        if "PHASE:validate-integration" in prompt:
            payload = self._integration_payload
        elif "PHASE:validate-judgment" in prompt:
            payload = self._judgment_payload
        else:
            payload = {"status": "done", "summary": "scripted dispatch"}
        return RunResult(text=json.dumps(payload), structured=payload, exit_status="ok")


def _cfg() -> Config:
    return Config(
        harness=HarnessConfig(
            tier_model={"fast": "x", "standard": "x", "deep": "x"}, run_cmd=[], capabilities=[]
        ),
        base="base principles",
        experts={"critic": PromptDoc(meta={}, body="You are a strict critic.", tools=None)},
        phases={
            "validate": PromptDoc(meta={}, body=VALIDATE_JUDGMENT_BODY),
            "validate-integration": PromptDoc(meta={}, body=VALIDATE_INTEGRATION_BODY),
        },
    )


def _task(task_id: str, exit_criteria: list[ExitCriterion], owns: list[str] | None = None) -> Task:
    return Task(
        id=task_id,
        status="done",
        depends_on=[],
        owns=owns if owns is not None else ["a.py"],
        exit_criteria=exit_criteria,
        expert="",
        critical=False,
        tier="standard",
        body="Do the thing.",
    )


class ValidateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-test")
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")
        self.layout.spec.write_text("# Spec\n\nBuild the thing.\n", encoding="utf-8")
        self.cfg = _cfg()

    def _write(self, *tasks: Task) -> State:
        for task in tasks:
            write_task(self.layout, task)
        return State(phase="validate", tasks={t.id: t.status for t in tasks})


class TestCmdGate(ValidateTestCase):
    def test_passing_and_failing_cmd_captured_in_report(self) -> None:
        ok_task = _task("01-ok", [ExitCriterion(check="cmd", value="true")], owns=["a.py"])
        bad_task = _task("02-bad", [ExitCriterion(check="cmd", value="false")], owns=["b.py"])
        state = self._write(ok_task, bad_task)
        adapter = ScriptedAdapter()

        result = _run_validate(self.layout, self.cfg, adapter, state)

        self.assertEqual(result.tasks["01-ok"], "done")
        self.assertEqual(result.tasks["02-bad"], "pending")  # reset, loop back to dispatch
        self.assertEqual(result.phase, "dispatch")

        report = (self.layout.root / validate.REPORT_FILENAME).read_text(encoding="utf-8")
        self.assertIn("01-ok — PASS", report)
        self.assertIn("02-bad — FAIL", report)
        self.assertIn("exit=0", report)
        self.assertIn("exit=1", report)
        self.assertIn("not run — task-level exit criteria failed first", report)

        feedback = (self.layout.scratch_dir / "02-bad.md").read_text(encoding="utf-8")
        self.assertIn("Validation feedback", feedback)
        self.assertIn("FAIL", feedback)


class TestJudgmentCritic(ValidateTestCase):
    def _judgment_task(self) -> Task:
        return _task("01-judged", [ExitCriterion(check="judgment", value="code is clean")])

    def test_pass_with_citation_passes(self) -> None:
        task = self._judgment_task()
        adapter = ScriptedAdapter(
            judgment_payload={"pass": True, "evidence": "clean", "cited_paths": ["x.py"]}
        )

        tv = validate.evaluate_task(self.layout, self.cfg, adapter, task)

        self.assertTrue(tv.passed)
        self.assertEqual(tv.results[0].cited_paths, ["x.py"])

    def test_pass_with_empty_citations_is_auto_failed(self) -> None:
        task = self._judgment_task()
        adapter = ScriptedAdapter(
            judgment_payload={"pass": True, "evidence": "trust me", "cited_paths": []}
        )

        tv = validate.evaluate_task(self.layout, self.cfg, adapter, task)

        self.assertFalse(tv.passed)  # the anti-rubber-stamp case
        self.assertIn("AUTO-FAIL", tv.results[0].detail)

    def test_explicit_fail_fails(self) -> None:
        task = self._judgment_task()
        adapter = ScriptedAdapter(
            judgment_payload={"pass": False, "evidence": "missing tests", "cited_paths": []}
        )

        tv = validate.evaluate_task(self.layout, self.cfg, adapter, task)

        self.assertFalse(tv.passed)
        self.assertEqual(tv.results[0].detail, "missing tests")


class TestDefinitionOfDone(ValidateTestCase):
    def test_passing_cmd_and_cited_judgment_reaches_done(self) -> None:
        task = _task(
            "01-both",
            [
                ExitCriterion(check="cmd", value="true"),
                ExitCriterion(check="judgment", value="clean code"),
            ],
        )
        state = self._write(task)
        adapter = ScriptedAdapter(
            judgment_payload={"pass": True, "evidence": "clean", "cited_paths": ["x.py"]},
            integration_payload=PASS_PAYLOAD,
        )

        result = _run_validate(self.layout, self.cfg, adapter, state)

        self.assertEqual(result.phase, "done")
        self.assertIsNone(result.gate)

    def test_either_failing_resets_to_pending_and_exhausts_after_two_rounds(self) -> None:
        task = _task(
            "01-both",
            [
                ExitCriterion(check="cmd", value="true"),
                ExitCriterion(check="judgment", value="clean code"),
            ],
        )
        state = self._write(task)
        adapter = ScriptedAdapter(
            judgment_payload={"pass": False, "evidence": "sloppy", "cited_paths": []}
        )

        for _ in range(MAX_RETRIES):
            state.phase = "validate"
            task.status = "done"
            write_task(self.layout, task)
            state.tasks[task.id] = "done"
            state = _run_validate(self.layout, self.cfg, adapter, state)
            self.assertEqual(state.tasks[task.id], "pending")
            self.assertEqual(state.phase, "dispatch")
            self.assertIsNone(state.gate)

        state.phase = "validate"
        task.status = "done"
        write_task(self.layout, task)
        state.tasks[task.id] = "done"
        state = _run_validate(self.layout, self.cfg, adapter, state)

        self.assertEqual(state.gate, "blocked")
        assert state.halt_reason is not None
        self.assertIn("exhausted", state.halt_reason)


class TestIntegrationCritic(ValidateTestCase):
    def test_runs_once_after_all_leaves_pass_and_reaches_done(self) -> None:
        task = _task("01-ok", [ExitCriterion(check="cmd", value="true")])
        state = self._write(task)
        adapter = ScriptedAdapter(integration_payload=PASS_PAYLOAD)

        result = _run_validate(self.layout, self.cfg, adapter, state)

        self.assertEqual(result.phase, "done")
        report = (self.layout.root / validate.REPORT_FILENAME).read_text(encoding="utf-8")
        self.assertIn("## Integration", report)
        self.assertIn("status: PASS", report)

    def test_failing_verdict_on_identifiable_leaf_retries_it(self) -> None:
        task = _task("01-ok", [ExitCriterion(check="cmd", value="true")], owns=["a.py"])
        state = self._write(task)
        adapter = ScriptedAdapter(
            integration_payload={
                "pass": False,
                "evidence": "seam gap in a.py",
                "cited_paths": ["a.py"],
            }
        )

        result = _run_validate(self.layout, self.cfg, adapter, state)

        self.assertEqual(result.phase, "dispatch")
        self.assertEqual(result.tasks["01-ok"], "pending")
        feedback = (self.layout.scratch_dir / "01-ok.md").read_text(encoding="utf-8")
        self.assertIn("Integration validation feedback", feedback)

    def test_failing_verdict_with_no_attributable_task_halts(self) -> None:
        task = _task("01-ok", [ExitCriterion(check="cmd", value="true")], owns=["a.py"])
        state = self._write(task)
        adapter = ScriptedAdapter(
            integration_payload={
                "pass": False,
                "evidence": "gap nobody owns",
                "cited_paths": ["nowhere.py"],
            }
        )

        result = _run_validate(self.layout, self.cfg, adapter, state)

        self.assertEqual(result.gate, "blocked")
        assert result.halt_reason is not None
        self.assertIn("resumptive re-split", result.halt_reason)
        self.assertIn("ticket-005-rule-5", result.halt_reason)
        self.assertEqual(result.tasks["01-ok"], "done")  # not touched: nothing to retry


if __name__ == "__main__":
    unittest.main()
