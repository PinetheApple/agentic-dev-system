"""Ticket 004/011 gap: `_run_dispatch` must forward an expert's declared
`tools` frontmatter to `adapter.run(..., allowed_tools=...)`. Without this,
real `claude` dispatch calls get no --allowedTools flag and every
file-writing task self-reports blocked."""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

from ads.adapters.base import RunResult
from ads.adapters.stub import StubAdapter
from ads.config import Config, HarnessConfig, PromptDoc, SandboxConfig, load_config
from ads.driver import (
    _run_dispatch,  # pyright: ignore[reportPrivateUsage]
    approve,
    run_until_halt,
    start_run,
)
from ads.layout import RunLayout
from ads.state import State
from ads.tasks import ExitCriterion, Task, TaskTier, serialize_task

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_CONFIG = REPO_ROOT / "examples" / "demo" / ".agent" / "config"


class RecordingAdapter(StubAdapter):
    """Delegates to StubAdapter but records every `(prompt, allowed_tools)` it
    was called with, so callers can isolate dispatch-phase calls from the
    validate phase's judgment/integration critic calls (ticket 007), which
    carry the critic's own `tools` and are not part of what this test
    covers."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, list[str] | None]] = []

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
    ) -> RunResult:
        self.calls.append((prompt, allowed_tools))
        return super().run(prompt, cwd, allowed_tools=allowed_tools, tier=tier)


class TestDispatchForwardsExpertTools(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        shutil.copytree(DEMO_CONFIG, self.repo / ".agent" / "config")
        self.layout = RunLayout(repo=self.repo, run_id="run-test")
        self.cfg = load_config(self.layout.config)
        # ticket 011: the demo config opts real runs into the sandbox, but
        # this test exercises tool-forwarding via real `cmd` exit criteria
        # (`true`) with no bwrap/systemd-run assumption — keep it jail-free.
        self.cfg = dataclasses.replace(
            self.cfg, harness=dataclasses.replace(self.cfg.harness, sandbox=SandboxConfig())
        )

    def test_python_expert_declared_tools_reach_adapter_run(self) -> None:
        adapter = RecordingAdapter()
        start_run(self.layout, "Build a thing.")
        run_until_halt(self.layout, self.cfg, adapter)  # -> review (spec)
        approve(self.layout)  # -> review (design)
        run_until_halt(self.layout, self.cfg, adapter)
        approve(self.layout)  # -> dispatch

        adapter.calls.clear()
        run_until_halt(self.layout, self.cfg, adapter)  # dispatch -> validate -> done

        dispatch_calls = [tools for prompt, tools in adapter.calls if "PHASE:dispatch" in prompt]
        self.assertTrue(dispatch_calls, "expected at least one dispatch call with tools recorded")
        for call in dispatch_calls:
            self.assertEqual(call, ["Read", "Write", "Edit", "Bash"])

    def test_expert_with_no_declared_tools_yields_none(self) -> None:
        self.layout.scaffold()
        self.layout.design.write_text("# Design\n", encoding="utf-8")
        task = Task(
            id="01-silent",
            status="pending",
            depends_on=[],
            owns=["ads/thing.py"],
            exit_criteria=[ExitCriterion(check="cmd", value="true")],
            expert="silent-expert",
            critical=True,
            tier="standard",
            body="Do a thing.",
        )
        (self.layout.tasks_dir).mkdir(parents=True, exist_ok=True)
        (self.layout.tasks_dir / f"{task.id}.md").write_text(serialize_task(task), encoding="utf-8")

        cfg_no_tools = Config(
            harness=HarnessConfig(
                tier_model={"fast": "x", "standard": "x", "deep": "x"}, run_cmd=[], capabilities=[]
            ),
            base="base",
            experts={"silent-expert": PromptDoc(meta={}, body="silent expert body", tools=None)},
            phases={"dispatch": PromptDoc(meta={}, body="dispatch {task}")},
        )
        adapter = RecordingAdapter()
        state = State(phase="dispatch", tasks={"01-silent": "pending"})

        _run_dispatch(self.layout, cfg_no_tools, adapter, state)

        self.assertEqual([tools for _, tools in adapter.calls], [None])


if __name__ == "__main__":
    unittest.main()
