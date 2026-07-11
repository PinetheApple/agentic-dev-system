"""On-disk layout for a run — the filesystem IS the state (spine B, ticket 001)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

AGENT_DIR = ".agent"
CONFIG_DIRNAME = "config"
RUNS_DIRNAME = "runs"


@dataclass(frozen=True)
class RunLayout:
    """Paths for one run under `<repo>/.agent/runs/<run-id>/`."""

    repo: Path
    run_id: str

    @property
    def agent(self) -> Path:
        return self.repo / AGENT_DIR

    @property
    def config(self) -> Path:
        return self.agent / CONFIG_DIRNAME

    @property
    def root(self) -> Path:
        return self.agent / RUNS_DIRNAME / self.run_id

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def intent(self) -> Path:
        return self.root / "intent.md"

    @property
    def spec(self) -> Path:
        return self.root / "spec.md"

    @property
    def design(self) -> Path:
        return self.root / "design.md"

    @property
    def tasks_dir(self) -> Path:
        return self.root / "tasks"

    @property
    def scratch_dir(self) -> Path:
        return self.root / "scratch"

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def current_link(self) -> Path:
        return self.agent / RUNS_DIRNAME / "current"

    def scaffold(self) -> None:
        """Create the run directory tree. Idempotent."""
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.scratch_dir.mkdir(parents=True, exist_ok=True)

    def link_current(self) -> None:
        """Point `runs/current` -> this run (best-effort symlink)."""
        link = self.current_link
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(Path(self.run_id))
        except OSError:
            pass  # non-fatal: symlink is a convenience, state.json is truth
