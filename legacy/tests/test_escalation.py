"""Ticket 011 dec 6 + dec 7: the escalation state machine.

No live LLM, no bwrap/systemd-run — a fake adapter drives the agent-request
trigger, and a real (but sandbox-disabled) `cmd` exit-criterion drives the
cmd-flagged trigger, mirroring tests/test_dispatch_worktree.py's fixture
shape but over `_dispatch_inplace` (no git floor needed for this gate).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ads import cli, dispatch, escalation
from ads.adapters.base import RunResult, StructuredPayload
from ads.config import Config, HarnessConfig, PromptDoc
from ads.layout import RunLayout
from ads.state import State, load_state, save_state
from ads.task_io import load_tasks, write_task
from ads.tasks import ExitCriterion, Task, TaskTier


def _cfg() -> Config:
    return Config(
        harness=HarnessConfig(
            tier_model={"fast": "x", "standard": "x", "deep": "x"},
            run_cmd=[],
            capabilities=[],
        ),
        base="base principles",
        experts={},
        phases={"dispatch": PromptDoc(meta={}, body="PHASE:dispatch\n\n{task}")},
    )


def _task(task_id: str, exit_criteria: list[ExitCriterion] | None = None) -> Task:
    return Task(
        id=task_id,
        status="pending",
        depends_on=[],
        owns=["a.py"],
        exit_criteria=exit_criteria or [],
        expert="",
        critical=False,
        tier="standard",
        body="Do the thing.",
    )


class _AgentRequestAdapter:
    """Always asks for an outward op it can never self-grant."""

    def __init__(self) -> None:
        self.calls = 0

    def resolve_model(self, tier: TaskTier) -> str:
        return "escalation-stub"

    def capabilities(self) -> list[str]:
        return []

    def sync(self) -> None:
        pass

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
        *,
        activity_log: Path | None = None,
    ) -> RunResult:
        self.calls += 1
        payload: StructuredPayload = {
            "status": "needs-escalation",
            "summary": "need to push the release branch",
            "op": "git-push",
            "target": "origin/main",
            "exact": "git push origin main",
        }
        return RunResult(text=json.dumps(payload), structured=payload, exit_status="ok")


class _AlwaysDoneAdapter:
    """Always claims done — used to drive the cmd-flagged screen, which
    fires from `_gate_and_route`, reached only once a task claims done."""

    def resolve_model(self, tier: TaskTier) -> str:
        return "done-stub"

    def capabilities(self) -> list[str]:
        return []

    def sync(self) -> None:
        pass

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
        *,
        activity_log: Path | None = None,
    ) -> RunResult:
        payload: StructuredPayload = {"status": "done", "summary": "did the thing"}
        return RunResult(text=json.dumps(payload), structured=payload, exit_status="ok")


# A command flagged by the obfuscation screen (echo piped into a decoder)
# that is nonetheless harmless and exits 0 once actually run.
FLAGGED_HARMLESS_CMD = "echo ZWNobyBvaw== | base64 -d"


class TestAgentRequestEscalation(unittest.TestCase):
    """Trigger 1: a dispatch run() asks for an outward op."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)  # deliberately not a git repo
        self.run_id = "run-agent-request"
        self.layout = RunLayout(repo=self.repo, run_id=self.run_id)
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")
        self.cfg = _cfg()

    def _dispatch(self, task: Task, adapter: _AgentRequestAdapter) -> State:
        write_task(self.layout, task)
        state = State(phase="dispatch", tasks={task.id: task.status})
        return dispatch.run(self.layout, self.cfg, adapter, state, [task])

    def test_open_halts_to_escalation_gate_with_body_and_event(self) -> None:
        task = _task("01-a")
        adapter = _AgentRequestAdapter()

        state = self._dispatch(task, adapter)

        self.assertEqual(state.gate, "escalation")
        self.assertEqual(state.tasks["01-a"], "needs-escalation")
        self.assertEqual(list(state.escalations.values()), ["pending"])
        request_id = next(iter(state.escalations))
        self.assertEqual(request_id, "esc-01-a-1")

        on_disk = next(iter(load_tasks(self.layout)))
        self.assertEqual(on_disk.status, "needs-escalation")

        body_path = self.layout.escalations_dir / f"{request_id}.md"
        self.assertTrue(body_path.exists())
        body = body_path.read_text(encoding="utf-8")
        self.assertIn("kind: agent-request", body)
        self.assertIn("op: git-push", body)
        self.assertIn("git push origin main", body)

        events = [
            json.loads(line) for line in self.layout.events.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(any(e["kind"] == "escalation_open" for e in events))

        request = escalation.load_request(self.layout, request_id)
        self.assertEqual(request.kind, "agent-request")
        self.assertEqual(request.op, "git-push")
        self.assertEqual(request.target, "origin/main")
        self.assertEqual(request.exact, "git push origin main")
        self.assertEqual(request.reason, "need to push the release branch")

    def test_approve_surfaces_the_outward_op_seam(self) -> None:
        task = _task("01-a")
        adapter = _AgentRequestAdapter()
        state = self._dispatch(task, adapter)
        request_id = next(iter(state.escalations))

        with self.assertRaises(NotImplementedError) as ctx:
            escalation.approve(self.layout, state, request_id)
        self.assertIn("git-push", str(ctx.exception))

        # The seam raised before resuming the task: approval is recorded,
        # but the task stays parked and the gate is untouched by the seam.
        self.assertEqual(state.escalations[request_id], "approved")
        on_disk = next(iter(load_tasks(self.layout)))
        self.assertEqual(on_disk.status, "needs-escalation")


class TestCmdFlaggedEscalation(unittest.TestCase):
    """Trigger 2: a `cmd` exit-criterion the classifier flags."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)  # deliberately not a git repo
        self.run_id = "run-cmd-flagged"
        self.layout = RunLayout(repo=self.repo, run_id=self.run_id)
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")
        self.cfg = _cfg()

    def _dispatch(self, task: Task, state: State) -> State:
        return dispatch.run(self.layout, self.cfg, _AlwaysDoneAdapter(), state, [task])

    def test_flagged_cmd_halts_without_running(self) -> None:
        task = _task("01-a", [ExitCriterion(check="cmd", value=FLAGGED_HARMLESS_CMD)])
        write_task(self.layout, task)
        state = State(phase="dispatch", tasks={task.id: task.status})

        state = self._dispatch(task, state)

        self.assertEqual(state.gate, "escalation")
        self.assertEqual(state.tasks["01-a"], "needs-escalation")
        self.assertEqual(state.approved_cmds, [])
        request_id = next(iter(state.escalations))
        request = escalation.load_request(self.layout, request_id)
        self.assertEqual(request.kind, "cmd-flagged")
        self.assertEqual(request.exact, FLAGGED_HARMLESS_CMD)
        self.assertEqual(request.target, FLAGGED_HARMLESS_CMD)

    def test_approve_then_redispatch_runs_the_approved_cmd(self) -> None:
        task = _task("01-a", [ExitCriterion(check="cmd", value=FLAGGED_HARMLESS_CMD)])
        write_task(self.layout, task)
        state = State(phase="dispatch", tasks={task.id: task.status})
        state = self._dispatch(task, state)
        request_id = next(iter(state.escalations))

        approved = escalation.approve(self.layout, state, request_id)

        self.assertEqual(approved.status, "approved")
        self.assertIn(FLAGGED_HARMLESS_CMD, state.approved_cmds)
        self.assertIsNone(state.gate)
        self.assertEqual(state.tasks["01-a"], "pending")

        flagged, _ = escalation.screen_cmd(FLAGGED_HARMLESS_CMD, state.approved_cmds)
        self.assertFalse(flagged)  # screen skips an approved exact command

        pending_task = next(iter(load_tasks(self.layout)))
        state = self._dispatch(pending_task, state)

        self.assertIsNone(state.gate)
        self.assertEqual(state.tasks["01-a"], "done")
        final_task = next(iter(load_tasks(self.layout)))
        self.assertEqual(final_task.status, "done")

    def test_reject_blocks_task_and_never_runs_cmd(self) -> None:
        task = _task("01-a", [ExitCriterion(check="cmd", value=FLAGGED_HARMLESS_CMD)])
        write_task(self.layout, task)
        state = State(phase="dispatch", tasks={task.id: task.status})
        state = self._dispatch(task, state)
        request_id = next(iter(state.escalations))

        rejected = escalation.reject(self.layout, state, request_id, "not approved for this run")

        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(state.tasks["01-a"], "blocked")
        self.assertEqual(state.gate, "blocked")  # terminal: no other escalation open
        self.assertEqual(state.approved_cmds, [])

        on_disk = next(iter(load_tasks(self.layout)))
        self.assertEqual(on_disk.status, "blocked")

        scratch = (self.layout.scratch_dir / "01-a.md").read_text(encoding="utf-8")
        self.assertIn("Escalation rejected", scratch)
        self.assertIn("not approved for this run", scratch)

        events = [
            json.loads(line) for line in self.layout.events.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(any(e["kind"] == "escalation_reject" for e in events))


class TestScreenCmd(unittest.TestCase):
    def test_flagged_when_not_approved(self) -> None:
        flagged, reasons = escalation.screen_cmd("sudo rm -rf /", [])
        self.assertTrue(flagged)
        self.assertTrue(reasons)

    def test_clean_when_exact_string_approved(self) -> None:
        flagged, reasons = escalation.screen_cmd("sudo true", ["sudo true"])
        self.assertFalse(flagged)
        self.assertEqual(reasons, ())

    def test_clean_when_never_flagged(self) -> None:
        flagged, reasons = escalation.screen_cmd("echo hi", [])
        self.assertFalse(flagged)
        self.assertEqual(reasons, ())


class TestStateRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-roundtrip")
        self.layout.scaffold()

    def test_escalations_and_approved_cmds_survive_to_from_dict(self) -> None:
        state = State(
            phase="dispatch",
            gate="escalation",
            escalations={"esc-01-a-1": "pending"},
            approved_cmds=["git push origin main"],
        )
        save_state(self.layout, state)

        loaded = load_state(self.layout)

        self.assertEqual(loaded.escalations, {"esc-01-a-1": "pending"})
        self.assertEqual(loaded.approved_cmds, ["git push origin main"])
        self.assertEqual(loaded.gate, "escalation")

    def test_needs_escalation_task_status_round_trips(self) -> None:
        task = _task("01-a")
        task.status = "needs-escalation"
        write_task(self.layout, task)

        reloaded = next(iter(load_tasks(self.layout)))

        self.assertEqual(reloaded.status, "needs-escalation")


class TestEscalationCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.run_id = "run-cli"
        self.layout = RunLayout(repo=self.repo, run_id=self.run_id)
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")
        self.layout.link_current()

        task = _task("01-a", [ExitCriterion(check="cmd", value=FLAGGED_HARMLESS_CMD)])
        write_task(self.layout, task)
        state = State(phase="dispatch", tasks={task.id: task.status}, adapter="stub")
        state = dispatch.run(self.layout, _cfg(), _AlwaysDoneAdapter(), state, [task])
        save_state(self.layout, state)
        self.request_id = next(iter(state.escalations))

    def _argv(self, *args: str) -> list[str]:
        return ["--repo", str(self.repo), "--run-id", self.run_id, *args]

    def test_escalations_lists_open_request(self) -> None:
        exit_code = cli.main(self._argv("escalations"))
        self.assertEqual(exit_code, 0)

        state = load_state(self.layout)
        self.assertEqual(escalation.list_open(state), [self.request_id])

    def test_escalate_approve_resumes_task(self) -> None:
        exit_code = cli.main(self._argv("escalate-approve", self.request_id, "--no-continue"))
        self.assertEqual(exit_code, 0)

        state = load_state(self.layout)
        self.assertIsNone(state.gate)
        self.assertEqual(state.tasks["01-a"], "pending")
        self.assertIn(FLAGGED_HARMLESS_CMD, state.approved_cmds)

    def test_escalate_reject_blocks_task(self) -> None:
        exit_code = cli.main(
            self._argv("escalate-reject", self.request_id, "no thanks", "--no-continue")
        )
        self.assertEqual(exit_code, 0)

        state = load_state(self.layout)
        self.assertEqual(state.gate, "blocked")
        self.assertEqual(state.tasks["01-a"], "blocked")


if __name__ == "__main__":
    unittest.main()
