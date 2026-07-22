"""Driver-side phase-JSON parsing (ticket 007 moves this out of the adapter;
ticket 002 owns the per-phase envelope/shapes). The stub adapter emits the
same JSON text a real adapter would, so this is the single parse path
exercised token-free.
"""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict, cast

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def strip_json_fence(text: str) -> str:
    """Model was instructed to answer bare JSON, but may still wrap it in a
    markdown fence — handle both."""
    match = _JSON_FENCE_RE.match(text.strip())
    return match.group(1) if match else text


def _parse_json_object(text: str) -> dict[str, Any]:
    for candidate in (text, strip_json_fence(text)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return cast(dict[str, Any], parsed)
    raise ValueError(f"could not parse a JSON object from adapter text: {text[:200]!r}")


class ExitCriterionPayload(TypedDict):
    check: str
    value: str


class _TaskPayloadRequired(TypedDict):
    id: str


class TaskPayload(_TaskPayloadRequired, total=False):
    """One entry in the plan phase's `tasks` array. `filename` is the plan
    prompt's own bookkeeping hint; never read back."""

    filename: str
    depends_on: list[str]
    owns: list[str]
    exit_criteria: list[ExitCriterionPayload]
    body: str


class GapPayload(TypedDict):
    ambiguous: bool
    question: str
    decision: str


class _PlanPayloadRequired(TypedDict):
    spec: str
    design: str | None
    tasks: list[TaskPayload]


class PlanPayload(_PlanPayloadRequired, total=False):
    """`gap` is optional: the plan phase's classification of an unambiguous
    gap (decide + record, `decision` set) vs an ambiguous one (a `question`
    to stop for) — ticket 006's gap branch."""

    gap: GapPayload


class CommandResult(TypedDict):
    cmd: str
    exit: int


class IssuePayload(TypedDict):
    desc: str
    blocking: bool


class ExecuteHandoff(TypedDict):
    task_id: str
    status: str
    commands: list[CommandResult]
    undone: list[str]
    issues: list[IssuePayload]


JudgmentVerdict = TypedDict(
    "JudgmentVerdict", {"pass": bool, "evidence": str, "cited_paths": list[str]}
)


def parse_plan_payload(text: str) -> PlanPayload:
    data = _parse_json_object(text)
    missing = [k for k in ("spec", "tasks") if k not in data]
    if missing:
        raise ValueError(f"plan payload missing keys {missing}: {data!r}")
    payload = cast(
        PlanPayload,
        {
            "spec": data["spec"],
            "design": data.get("design"),
            "tasks": data.get("tasks", []),
        },
    )
    if "gap" in data:
        payload["gap"] = cast("GapPayload", data["gap"])
    return payload


def parse_execute_handoff(text: str) -> ExecuteHandoff:
    data = _parse_json_object(text)
    missing = [k for k in ("task_id", "status") if k not in data]
    if missing:
        raise ValueError(f"execute handoff missing keys {missing}: {data!r}")
    return cast(
        ExecuteHandoff,
        {
            "task_id": data["task_id"],
            "status": data["status"],
            "commands": data.get("commands", []),
            "undone": data.get("undone", []),
            "issues": data.get("issues", []),
        },
    )


def parse_judgment_verdict(text: str) -> JudgmentVerdict:
    data = _parse_json_object(text)
    return cast(
        JudgmentVerdict,
        {
            "pass": bool(data.get("pass", False)),
            "evidence": str(data.get("evidence", "")),
            "cited_paths": list(data.get("cited_paths", [])),
        },
    )
