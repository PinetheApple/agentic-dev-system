"""Task file I/O (ticket 003) and scratch-file feedback appends (ticket 004)."""

from __future__ import annotations

from pathlib import Path

from ads.layout import RunLayout
from ads.tasks import Task, parse_task, serialize_task


def task_path(layout: RunLayout, task_id: str) -> Path:
    return layout.tasks_dir / f"{task_id}.md"


def load_tasks(layout: RunLayout) -> list[Task]:
    return [
        parse_task(p.read_text(encoding="utf-8")) for p in sorted(layout.tasks_dir.glob("*.md"))
    ]


def write_task(layout: RunLayout, task: Task) -> None:
    task_path(layout, task.id).write_text(serialize_task(task), encoding="utf-8")


def append_scratch(layout: RunLayout, task_id: str, text: str) -> None:
    """Append validation feedback to a task's scratch file — a resume read-set,
    not a transcript (ticket 004): the next attempt reads it without
    inheriting the author's context."""
    layout.scratch_dir.mkdir(parents=True, exist_ok=True)
    scratch_path = layout.scratch_dir / f"{task_id}.md"
    with scratch_path.open("a", encoding="utf-8") as fh:
        fh.write(text)
