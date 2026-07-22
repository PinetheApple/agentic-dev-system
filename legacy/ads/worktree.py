"""Git worktree isolation for parallel-safe dispatch (ticket 006).

Each ready task in a dispatch batch runs in its own `git worktree`, on its
own branch, branched off the target repo's current HEAD (the "integration
branch"). The task's `run()` executes with `cwd` set to that worktree, so
file writes land there — fully isolated from every other task in the batch
and from the integration branch itself.

Merge-back runs two tripwires before a branch folds into the integration
branch:

1. **write-set audit** — `git diff --name-only` of the task branch vs its
   base is the actual changed-file set. Every changed file must be covered
   by the task's declared `owns` (see `_covers`: exact path, directory
   prefix, or fnmatch glob). An uncovered file is an out-of-bounds
   violation — the silent-drift failure mode, caught even when the merge
   itself would be textually clean.
2. **git merge conflict** — a textual conflict on `git merge --no-ff`.

Either tripwire trips: the merge is refused, `git merge --abort` guarantees
the integration branch is never left half-merged, and the worktree/branch
are left on disk for inspection (`MergeOutcome.merged is False`). Clean
tasks (audit passes, merge clean) merge automatically.

Worktrees live under the system temp dir (`tempfile.gettempdir()`), never
inside the target repo tree, so they can't pollute `git status`/gitignore
for the repo being worked on.
"""

from __future__ import annotations

import fnmatch
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

WORKTREE_BRANCH_PREFIX = "ads-task"
MERGE_VIOLATION_OUT_OF_BOUNDS = "out_of_bounds"
MERGE_VIOLATION_CONFLICT = "conflict"


class GitError(RuntimeError):
    """A git subprocess call failed unexpectedly — a hard error, not a tripwire."""


@dataclass(frozen=True)
class TaskWorktree:
    task_id: str
    path: Path
    branch: str
    base_sha: str


@dataclass(frozen=True)
class MergeOutcome:
    task_id: str
    merged: bool
    violation: str | None  # one of the MERGE_VIOLATION_* constants, or None
    changed_files: list[str] = field(default_factory=list[str])
    uncovered_files: list[str] = field(default_factory=list[str])
    diff_text: str = ""  # task branch vs base — always captured
    merge_output: str = ""  # git merge stdout/stderr — only set on conflict


def is_git_repo(repo: Path) -> bool:
    """True only if `repo` is itself a git worktree root (has its own
    `.git`), not merely nested inside an ancestor repo's tree. Being nested
    (e.g. `examples/demo` inside this repo) is treated the same as "not a
    git repo": there's no sane branch/worktree boundary to isolate against,
    so dispatch falls back to the pre-006 sequential in-place behavior.
    """
    return (repo / ".git").exists()


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _require_git(args: list[str], cwd: Path, what: str) -> subprocess.CompletedProcess[str]:
    proc = _git(args, cwd)
    if proc.returncode != 0:
        raise GitError(f"{what} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc


def head_sha(repo: Path) -> str:
    """The integration branch's current HEAD — the base every task branch
    in this dispatch batch is created from."""
    return _require_git(["rev-parse", "HEAD"], repo, "rev-parse HEAD").stdout.strip()


def create_worktree(repo: Path, base_sha: str, run_id: str, task_id: str) -> TaskWorktree:
    branch = f"{WORKTREE_BRANCH_PREFIX}/{run_id}/{task_id}"
    base_dir = Path(tempfile.gettempdir()) / "ads-worktrees" / run_id
    base_dir.mkdir(parents=True, exist_ok=True)
    path = base_dir / f"{task_id}-{uuid.uuid4().hex[:8]}"
    _require_git(
        ["worktree", "add", "-b", branch, str(path), base_sha],
        repo,
        f"worktree add ({task_id})",
    )
    return TaskWorktree(task_id=task_id, path=path, branch=branch, base_sha=base_sha)


def commit_all(wt: TaskWorktree, message: str) -> bool:
    """Stage + commit everything the task's run() wrote in its worktree.
    Returns False (no-op, nothing to commit) if the task made no changes."""
    _require_git(["add", "-A"], wt.path, f"add ({wt.task_id})")
    staged = _git(["diff", "--cached", "--quiet"], wt.path)
    if staged.returncode == 0:
        return False
    _require_git(["commit", "--quiet", "-m", message], wt.path, f"commit ({wt.task_id})")
    return True


def changed_files(wt: TaskWorktree) -> list[str]:
    proc = _require_git(
        ["diff", "--name-only", wt.base_sha, "HEAD"], wt.path, f"diff --name-only ({wt.task_id})"
    )
    return [line for line in proc.stdout.splitlines() if line]


def full_diff(wt: TaskWorktree) -> str:
    return _require_git(["diff", wt.base_sha, "HEAD"], wt.path, f"diff ({wt.task_id})").stdout


def covers(changed_path: str, owns_entry: str) -> bool:
    """One `owns` entry covers a changed path if the path equals it, sits
    under it as a directory prefix, or matches it as an fnmatch glob. This
    is the owns-coverage rule for the write-set audit: `owns` entries are
    treated as path prefixes/globs, not only exact file paths. Also reused
    by validate's integration-critic path attribution (ticket 007)."""
    normalized = owns_entry.rstrip("/")
    if changed_path == normalized or changed_path.startswith(normalized + "/"):
        return True
    return any(c in owns_entry for c in "*?[") and fnmatch.fnmatch(changed_path, owns_entry)


def uncovered_files(paths: list[str], owns: list[str]) -> list[str]:
    return [p for p in paths if not any(covers(p, entry) for entry in owns)]


def merge_task_branch(repo: Path, wt: TaskWorktree, owns: list[str]) -> MergeOutcome:
    """Run both tripwires and merge if clean. A failed `git merge` is always
    aborted, so the integration branch is never left half-merged."""
    diffed = changed_files(wt)
    diff_text = full_diff(wt)
    missing = uncovered_files(diffed, owns)
    if missing:
        return MergeOutcome(
            task_id=wt.task_id,
            merged=False,
            violation=MERGE_VIOLATION_OUT_OF_BOUNDS,
            changed_files=diffed,
            uncovered_files=missing,
            diff_text=diff_text,
        )

    proc = _git(["merge", "--no-ff", "--quiet", "-m", f"ads: merge {wt.task_id}", wt.branch], repo)
    if proc.returncode != 0:
        _git(["merge", "--abort"], repo)
        return MergeOutcome(
            task_id=wt.task_id,
            merged=False,
            violation=MERGE_VIOLATION_CONFLICT,
            changed_files=diffed,
            diff_text=diff_text,
            merge_output=proc.stdout + proc.stderr,
        )
    return MergeOutcome(
        task_id=wt.task_id, merged=True, violation=None, changed_files=diffed, diff_text=diff_text
    )


def remove_worktree(repo: Path, wt: TaskWorktree) -> bool:
    """Best-effort cleanup of a worktree that merged cleanly (or whose task
    never wrote anything). Returns False if either git call failed so the
    caller can warn instead of silently leaking a worktree/branch."""
    removed = _git(["worktree", "remove", "--force", str(wt.path)], repo)
    deleted = _git(["branch", "-D", wt.branch], repo)
    return removed.returncode == 0 and deleted.returncode == 0
