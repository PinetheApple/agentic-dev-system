"""Task = markdown file with machine-read frontmatter (ticket 003).

No pyyaml available, so this module ships a tight YAML-subset parser for the
exact frontmatter shape we need: plain scalars, flow lists (`[a, b]`), a
block list of flow-maps (`- {check: cmd, value: "..."}`), booleans and null.
It is not a general YAML parser — it deliberately only covers this shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast, get_args

from ads._literal import validate_literal

FRONTMATTER_DELIM = "---"

TaskStatus = Literal[
    "pending", "active", "done", "split", "blocked", "needs-escalation", "aborted"
]
TaskTier = Literal["fast", "standard", "deep"]
ExitCriterionCheck = Literal["cmd", "judgment"]

TASK_STATUSES: tuple[TaskStatus, ...] = get_args(TaskStatus)
TASK_TIERS: tuple[TaskTier, ...] = get_args(TaskTier)
EXIT_CRITERION_CHECKS: tuple[ExitCriterionCheck, ...] = get_args(ExitCriterionCheck)

FRONTMATTER_KEYS = (
    "id",
    "status",
    "depends_on",
    "owns",
    "exit_criteria",
    "expert",
    "critical",
    "tier",
    "parent",
)

# The value shape this hand-rolled parser can ever produce for one frontmatter
# key: a plain scalar, a flow list of strings, or a block list of flow-maps
# (only `exit_criteria` uses the last shape).
FrontmatterValue = str | bool | None | list[str] | list[dict[str, str]]


class TaskParseError(ValueError):
    pass


class CycleError(ValueError):
    """Raised when the task dependency graph is not a DAG."""


@dataclass
class ExitCriterion:
    check: ExitCriterionCheck
    value: str

    def to_dict(self) -> dict[str, str]:
        return {"check": self.check, "value": self.value}


@dataclass
class Task:
    id: str
    status: TaskStatus = "pending"
    depends_on: list[str] = field(default_factory=list[str])
    owns: list[str] = field(default_factory=list[str])
    exit_criteria: list[ExitCriterion] = field(default_factory=list[ExitCriterion])
    expert: str = ""
    critical: bool = False
    tier: TaskTier = "standard"
    parent: str | None = None
    body: str = ""


# ---------------------------------------------------------------------------
# scalar tokenizing helpers
# ---------------------------------------------------------------------------


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` at brace/quote depth 0."""
    parts: list[str] = []
    depth = 0
    in_quote: str | None = None
    current: list[str] = []
    for ch in text:
        if in_quote:
            current.append(ch)
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            current.append(ch)
            continue
        if ch in "{[":
            depth += 1
            current.append(ch)
            continue
        if ch in "}]":
            depth -= 1
            current.append(ch)
            continue
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current or parts:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _parse_scalar(raw: str) -> str | bool | None:
    raw = raw.strip()
    if raw in ("null", "~", ""):
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _parse_flow_list(raw: str) -> list[str]:
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        raise TaskParseError(f"expected flow list, got: {raw!r}")
    inner = raw[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    for part in _split_top_level(inner):
        value = _parse_scalar(part)
        items.append(value if isinstance(value, str) else str(value))
    return items


def _parse_flow_map(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if not (raw.startswith("{") and raw.endswith("}")):
        raise TaskParseError(f"expected flow map, got: {raw!r}")
    inner = raw[1:-1].strip()
    result: dict[str, str] = {}
    for part in _split_top_level(inner):
        key, _, value = part.partition(":")
        parsed = _parse_scalar(value)
        result[key.strip()] = parsed if isinstance(parsed, str) else str(parsed)
    return result


# ---------------------------------------------------------------------------
# frontmatter <-> Task
# ---------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        raise TaskParseError("task file must start with '---' frontmatter delimiter")
    for idx in range(1, len(lines)):
        if lines[idx].strip() == FRONTMATTER_DELIM:
            front = "\n".join(lines[1:idx])
            body = "\n".join(lines[idx + 1 :]).lstrip("\n")
            return front, body
    raise TaskParseError("unterminated frontmatter block")


def _parse_frontmatter(front: str) -> dict[str, FrontmatterValue]:
    raw_lines = front.splitlines()
    data: dict[str, FrontmatterValue] = {}
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        if not line.strip() or line.strip().startswith("#"):
            i += 1
            continue
        if line.startswith((" ", "\t")):
            raise TaskParseError(f"unexpected indented line: {line!r}")
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest:
            if rest.startswith("["):
                data[key] = _parse_flow_list(rest)
            else:
                data[key] = _parse_scalar(rest)
            i += 1
            continue
        # empty value on this line -> block list of flow-maps follows, indented
        items: list[dict[str, str]] = []
        j = i + 1
        while j < len(raw_lines) and raw_lines[j].strip().startswith("- "):
            item_raw = raw_lines[j].strip()[2:].strip()
            items.append(_parse_flow_map(item_raw))
            j += 1
        data[key] = items
        i = j
    return data


def _require_str(data: dict[str, FrontmatterValue], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TaskParseError(f"{key!r} must be a string, got {value!r}")
    return value


def _optional_str(value: FrontmatterValue) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise TaskParseError(f"expected a string or null, got {value!r}")


def _str_or_default(value: FrontmatterValue, default: str) -> str:
    """Like `value or default`, but type-safe: an empty scalar (`key:` with
    nothing after it) parses as `[]` in this frontmatter shape, so a bare
    truthiness check is what the original parser relied on here."""
    return value if isinstance(value, str) and value else default


def _as_str_list(value: FrontmatterValue) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        # `all()` doesn't narrow for pyright; the check above is the real guard.
        return cast(list[str], value)
    raise TaskParseError(f"expected a list of strings, got {value!r}")


def _parse_exit_criteria(value: FrontmatterValue) -> list[ExitCriterion]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TaskParseError(f"expected a list of exit criteria, got {value!r}")
    criteria: list[ExitCriterion] = []
    for item in value:
        if not isinstance(item, dict):
            raise TaskParseError(f"expected an exit criterion map, got {item!r}")
        check = cast(
            ExitCriterionCheck,
            validate_literal(
                item["check"],
                EXIT_CRITERION_CHECKS,
                field="exit_criteria.check",
                error=TaskParseError,
            ),
        )
        criteria.append(ExitCriterion(check=check, value=item["value"]))
    return criteria


def parse_task(text: str) -> Task:
    front, body = split_frontmatter(text)
    data = _parse_frontmatter(front)
    missing = [k for k in ("id", "status") if k not in data or data[k] is None]
    if missing:
        raise TaskParseError(f"frontmatter missing required keys: {missing}")
    status = cast(
        TaskStatus,
        validate_literal(data["status"], TASK_STATUSES, field="status", error=TaskParseError),
    )
    tier_value = data.get("tier") or "standard"
    tier = cast(
        TaskTier, validate_literal(tier_value, TASK_TIERS, field="tier", error=TaskParseError)
    )
    return Task(
        id=_require_str(data, "id"),
        status=status,
        depends_on=_as_str_list(data.get("depends_on")),
        owns=_as_str_list(data.get("owns")),
        exit_criteria=_parse_exit_criteria(data.get("exit_criteria")),
        expert=_str_or_default(data.get("expert"), ""),
        critical=bool(data.get("critical") or False),
        tier=tier,
        parent=_optional_str(data.get("parent")),
        body=body,
    )


def _fmt_scalar(value: str | bool | None) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _fmt_flow_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def serialize_task(task: Task) -> str:
    lines = [FRONTMATTER_DELIM]
    lines.append(f"id: {task.id}")
    lines.append(f"status: {task.status}")
    lines.append(f"depends_on: {_fmt_flow_list(task.depends_on)}")
    lines.append(f"owns: {_fmt_flow_list(task.owns)}")
    if task.exit_criteria:
        lines.append("exit_criteria:")
        for ec in task.exit_criteria:
            lines.append(f'  - {{check: {ec.check}, value: "{ec.value}"}}')
    else:
        lines.append("exit_criteria: []")
    lines.append(f"expert: {task.expert}")
    lines.append(f"critical: {_fmt_scalar(task.critical)}")
    lines.append(f"tier: {task.tier}")
    lines.append(f"parent: {_fmt_scalar(task.parent)}")
    lines.append(FRONTMATTER_DELIM)
    front = "\n".join(lines)
    if task.body:
        return front + "\n" + task.body.rstrip("\n") + "\n"
    return front + "\n"


# ---------------------------------------------------------------------------
# DAG checks + concurrency
# ---------------------------------------------------------------------------


def check_acyclic(tasks: list[Task]) -> None:
    """Raise CycleError if depends_on edges form a cycle."""
    by_id = {t.id: t for t in tasks}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {t.id: WHITE for t in tasks}

    def visit(task_id: str, path: list[str]) -> None:
        color[task_id] = GRAY
        for dep in by_id[task_id].depends_on:
            if dep not in by_id:
                raise TaskParseError(f"{task_id} depends_on unknown task {dep!r}")
            if color[dep] == GRAY:
                cycle = " -> ".join([*path, dep])
                raise CycleError(f"cycle detected: {cycle}")
            if color[dep] == WHITE:
                visit(dep, [*path, dep])
        color[task_id] = BLACK

    for t in tasks:
        if color[t.id] == WHITE:
            visit(t.id, [t.id])


def _owns_overlap(a: list[str], b: list[str]) -> bool:
    return bool(set(a) & set(b))


def ready_batch(tasks: list[Task]) -> list[Task]:
    """Pending tasks whose deps are all done, greedily filtered to disjoint `owns`.

    Concurrency is derived from disjoint `owns`, not a declared flag: two ready
    tasks that touch overlapping paths cannot be batched together.

    Ticket 010: `aborted` is terminal and never `pending`, so an aborted task
    is naturally excluded here; a dependent of an aborted task also never
    becomes ready because its `depends_on` id never reaches `done` — abort
    blocks dependents by construction, no extra bookkeeping needed.
    """
    done_ids = {t.id for t in tasks if t.status == "done"}
    dep_ready = [
        t for t in tasks if t.status == "pending" and all(dep in done_ids for dep in t.depends_on)
    ]
    batch: list[Task] = []
    claimed: list[str] = []
    for task in dep_ready:
        if _owns_overlap(task.owns, claimed):
            continue
        batch.append(task)
        claimed.extend(task.owns)
    return batch
