"""Deterministic resume support (ticket 005, increment A: Rules 2 and 4).

Rule 2 — scratch skeleton: `scaffold_scratch` writes `scratch/<id>.md` with a
fixed skeleton (Objective / Done / Remaining / Decisions) before a task's
`run()`, if and only if the file doesn't already exist. This guarantees the
file is always present to checkpoint against, and a re-dispatch never
clobbers progress a prior attempt already recorded there.

Rule 4 — resume read-set: `assemble_resume_context` returns the markdown
block a (re)dispatch prompt must carry when prior work is detected for a
task — its scratch checkpoint plus the on-disk diff of its `owns` paths,
with an explicit instruction that the diff overrides the scratch file on
conflict (scratch can lag a crash; the filesystem cannot). Returns `None` on
a genuinely fresh task (empty scratch `Done`, nothing changed on disk) so a
first dispatch never carries resume boilerplate.

Rule 3 note: there is deliberately no mid-task summarize/compaction step
anywhere in this system. Checkpoint-to-scratch (Rule 2) plus a fresh, cold
`run()` on redispatch (this module's Rule 4 read-set) IS the compaction —
whatever a harness does natively mid-transcript (if anything) is a
non-load-bearing accelerator ADS never depends on or drives.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ads import worktree
from ads.layout import RunLayout
from ads.tasks import Task

DONE_HEADING = "## Done"

RESUME_INSTRUCTIONS = (
    "Prior work was detected for this task: its scratch checkpoint has `Done` "
    "entries and/or its `owns` paths already changed on disk. Read the scratch "
    "checkpoint and the on-disk diff below before doing anything else. The "
    "on-disk diff OVERRIDES the scratch checkpoint on any conflict — scratch "
    "can lag a crash, the filesystem cannot. Continue only on items still "
    "listed under `Remaining` in the scratch checkpoint; do not redo work "
    "already reflected as `Done` there or already present in the diff."
)


# ---------------------------------------------------------------------------
# Rule 2 — scratch skeleton
# ---------------------------------------------------------------------------


def render_scratch_skeleton(task: Task) -> str:
    remaining = "\n".join(f"- {ec.value}" for ec in task.exit_criteria)
    lines = [
        "## Objective",
        task.body.strip(),
        "",
        "## Done",
        "",
        "<!-- one line per completed exit-criterion: <criterion> — <where: path:line/artifact> -->",
        "",
        "## Remaining",
        remaining,
        "",
        "## Decisions / gotchas",
        "",
    ]
    return "\n".join(lines) + "\n"


def scratch_path(layout: RunLayout, task_id: str) -> Path:
    return layout.scratch_dir / f"{task_id}.md"


def scaffold_scratch(layout: RunLayout, task: Task) -> None:
    """Create `scratch/<id>.md` from the fixed skeleton if absent. Never
    overwrites an existing file — that would erase a prior attempt's
    checkpointed progress."""
    path = scratch_path(layout, task.id)
    if path.exists():
        return
    path.write_text(render_scratch_skeleton(task), encoding="utf-8")


def _has_done_entries(scratch_text: str) -> bool:
    lines = scratch_text.splitlines()
    try:
        start = lines.index(DONE_HEADING) + 1
    except ValueError:
        return False
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("##"):
            break
        if not stripped or stripped.startswith("<!--"):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Rule 4 — on-disk owns-diff (ground truth)
# ---------------------------------------------------------------------------


def _git_owns_diff(repo: Path, owns: list[str]) -> str:
    """Diff of `owns` paths from the repo's root commit through to the
    current working tree (staged + unstaged included), so the result is
    ground truth of everything that ever landed in `owns` regardless of how
    many merges/commits happened across prior dispatch attempts."""
    root = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    root_shas = root.stdout.split()
    if root.returncode != 0 or not root_shas:
        return ""  # no commits yet -> nothing to diff against
    diff = subprocess.run(
        ["git", "diff", root_shas[0], "--", *owns],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return diff.stdout


def _fallback_owns_diff(repo: Path, owns: list[str]) -> str:
    """Non-git targets have no diff mechanism, so this documents what's on
    disk instead — an honest, explicitly-labeled substitute, not a diff."""
    lines: list[str] = []
    for entry in owns:
        candidate = repo / entry
        paths = [candidate] if candidate.is_file() else sorted(candidate.rglob("*"))
        for path in paths:
            if path.is_file():
                stat = path.stat()
                rel = path.relative_to(repo)
                lines.append(f"{rel}: {stat.st_size} bytes, mtime={stat.st_mtime:.0f}")
    if not lines:
        return ""
    return "no git history available (non-git target); existing owns files on disk:\n" + "\n".join(
        lines
    )


def owns_diff(repo: Path, owns: list[str]) -> str:
    """Best-effort ground truth of what's physically changed in `owns`."""
    if not owns:
        return ""
    if worktree.is_git_repo(repo):
        return _git_owns_diff(repo, owns)
    return _fallback_owns_diff(repo, owns)


# ---------------------------------------------------------------------------
# Rule 4 — read-set assembly
# ---------------------------------------------------------------------------


def has_prior_work(scratch_text: str, diff_text: str) -> bool:
    return bool(diff_text.strip()) or _has_done_entries(scratch_text)


def resume_block(task_id: str, scratch_text: str, diff_text: str) -> str:
    parts = [
        RESUME_INSTRUCTIONS,
        "",
        f"### scratch/{task_id}.md (checkpoint)",
        "",
        scratch_text.strip() or "(empty)",
    ]
    if diff_text.strip():
        parts += ["", "### on-disk diff of owns paths (ground truth)", "", diff_text.strip()]
    return "\n".join(parts) + "\n"


def assemble_resume_context(layout: RunLayout, task: Task) -> str | None:
    """The Rule-4 read-set for `task`, or `None` when nothing indicates prior
    work (fresh dispatch) — callers must not inject an empty resume block."""
    path = scratch_path(layout, task.id)
    scratch_text = path.read_text(encoding="utf-8") if path.exists() else ""
    diff_text = owns_diff(layout.repo, task.owns)
    if not has_prior_work(scratch_text, diff_text):
        return None
    return resume_block(task.id, scratch_text, diff_text)
