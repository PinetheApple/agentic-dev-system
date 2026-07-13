"""Harness adapter contract (ticket 002) — the entire portable seam.

Everything above this layer (driver, phases, prompts) is harness-agnostic.
Everything below it (subprocess calls, CLI flags, model ids) is adapter-owned.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict, get_args

from ads.tasks import ExitCriterionCheck, TaskStatus, TaskTier

AdapterName = Literal["claude-code", "stub", "opencode"]

ADAPTER_CLAUDE_CODE: AdapterName = "claude-code"
ADAPTER_STUB: AdapterName = "stub"
ADAPTER_OPENCODE: AdapterName = "opencode"
ADAPTER_NAMES: tuple[AdapterName, ...] = get_args(AdapterName)

ExitStatus = Literal["ok", "error"]


class ExitCriterionPayload(TypedDict):
    check: ExitCriterionCheck
    value: str


class _TaskPayloadRequired(TypedDict):
    id: str


class TaskPayload(_TaskPayloadRequired, total=False):
    """One entry of the `plan` phase's `tasks` array.

    `filename` is emitted by the plan prompt (see `.agent/config/phases/plan.md`)
    for the adapter's own bookkeeping; the driver never reads it back.
    """

    filename: str
    depends_on: list[str]
    owns: list[str]
    exit_criteria: list[ExitCriterionPayload]
    expert: str
    critical: bool
    tier: TaskTier
    parent: str | None
    body: str


# Every phase's `run()` call returns JSON shaped for that phase, but the
# `Adapter` contract is phase-agnostic — so this TypedDict is the union of all
# fields any phase may populate (`total=False`: each call only sets a subset).
# Functional syntax is required for the `pass` key (a Python keyword).
# `op`/`target`/`exact` (ticket 011 dec 6): the structured escalation request
# an agent emits alongside `status: "needs-escalation"` — the outward op it
# wants (e.g. "git-push"), what it targets (e.g. "origin/main"), and the
# exact cmd or unified diff the driver would run/apply on approval. Reason
# text reuses the existing `summary` field.
StructuredPayload = TypedDict(
    "StructuredPayload",
    {
        "status": TaskStatus,
        "summary": str,
        "spec": str,
        "design": str,
        "tasks": list[TaskPayload],
        "pass": bool,
        "evidence": str,
        "cited_paths": list[str],
        "op": str,
        "target": str,
        "exact": str,
    },
    total=False,
)


@dataclass(frozen=True)
class RunResult:
    text: str
    structured: StructuredPayload | None
    exit_status: ExitStatus


class Adapter(Protocol):
    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
    ) -> RunResult: ...

    def capabilities(self) -> list[str]: ...

    def resolve_model(self, tier: TaskTier) -> str: ...

    def sync(self) -> None:
        """Optional: reconcile any harness-side session/session-file state."""
        ...
