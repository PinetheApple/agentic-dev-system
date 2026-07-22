"""Loads prompt building-blocks from `<repo>/.agent/config/` (ticket 009).

Substitution is plain `str.replace`, not `str.format` — the phase templates
contain literal JSON braces (`{ }`) in their example payloads that must
survive untouched.
"""

from __future__ import annotations

from pathlib import Path

PHASES_DIRNAME = "phases"


def _read(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    return path.read_text(encoding="utf-8")


def load_base(config_dir: Path) -> str:
    return _read(config_dir / "base.md")


def load_optional(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def render_phase(config_dir: Path, name: str, substitutions: dict[str, str]) -> str:
    """Load `phases/<name>.md` and fill `{placeholder}` markers via literal
    substring replacement (never `str.format`)."""
    text = _read(config_dir / PHASES_DIRNAME / f"{name}.md")
    for key, value in substitutions.items():
        text = text.replace(f"{{{key}}}", value)
    return text
