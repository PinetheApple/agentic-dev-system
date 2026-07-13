"""Task file I/O shared between the dispatch-phase runners (ticket 001/006).

Split out of `ads/driver.py` so `ads/dispatch.py` (worktree-isolated
dispatch) and `ads/driver.py` (plan/validate) can both use it without a
circular import between the two phase-runner modules.
"""

from __future__ import annotations

from pathlib import Path

from ads.adapters.base import RunResult
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


def write_scratch(layout: RunLayout, task: Task, result: RunResult) -> None:
    """Append the run's outcome to scratch/<id>.md. Appends rather than
    overwrites (ticket 005 Rule 2): the file was scaffolded with the
    Objective/Done/Remaining/Decisions skeleton before run(), and the task
    may have edited it in place while running — this call must not erase
    that checkpoint."""
    scratch_path = layout.scratch_dir / f"{task.id}.md"
    footer = f"\n## Run result ({task.status})\n\n{result.text}\n"
    with scratch_path.open("a", encoding="utf-8") as fh:
        fh.write(footer)


def clear_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.glob("*.md"):
        child.unlink()
