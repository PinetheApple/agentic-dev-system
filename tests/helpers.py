"""Shared test scaffolding: a real temp git repo, no mocking of git/filesystem."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@contextmanager
def temp_git_repo() -> Generator[Path]:
    """A real `git init`-ed repo with one commit, so `git diff`/`git rev-parse
    HEAD` in `ads/validate.py`/`ads/driver.py` exercise real git mechanics."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _run_git(repo, "init", "-q")
        _run_git(repo, "config", "user.email", "test@example.com")
        _run_git(repo, "config", "user.name", "Test")
        (repo / "README.md").write_text("# test repo\n", encoding="utf-8")
        _run_git(repo, "add", "README.md")
        _run_git(repo, "commit", "-q", "-m", "initial commit")
        yield repo
