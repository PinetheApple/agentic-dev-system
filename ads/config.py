"""Loads the committed `.agent/config/` tree (ticket 002).

`harness.toml` is the ONLY harness-aware file. Everything else (base.md,
experts/*.md, phases/*.md) is plain prose, composed in-memory by ads/prompt.py.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

FRONTMATTER_DELIM = "---"


@dataclass(frozen=True)
class HarnessConfig:
    tier_model: dict[str, str]
    run_cmd: list[str]
    capabilities: list[str]


@dataclass(frozen=True)
class PromptDoc:
    """A prose config file with optional simple `key: value` frontmatter."""

    meta: dict[str, str]
    body: str


@dataclass(frozen=True)
class Config:
    harness: HarnessConfig
    base: str
    experts: dict[str, PromptDoc]
    phases: dict[str, PromptDoc]


def _split_simple_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Optional `--- key: value ... ---` header followed by prose body.

    Only scalar `key: value` lines are supported here — experts/phases don't
    need the richer list/map shape that task frontmatter does.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return {}, text
    meta: dict[str, str] = {}
    for idx in range(1, len(lines)):
        if lines[idx].strip() == FRONTMATTER_DELIM:
            body = "\n".join(lines[idx + 1 :]).lstrip("\n")
            return meta, body
        key, _, value = lines[idx].partition(":")
        if key.strip():
            meta[key.strip()] = value.strip()
    return {}, text


def _load_prompt_doc(path: Path) -> PromptDoc:
    meta, body = _split_simple_frontmatter(path.read_text(encoding="utf-8"))
    return PromptDoc(meta=meta, body=body)


def _load_harness(path: Path) -> HarnessConfig:
    # tomllib.load() returns dict[str, Any] by design (arbitrary TOML shape);
    # this is the boundary where we assert our expected schema.
    with path.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)
    run_section = cast(dict[str, Any], raw.get("run", {}))
    capabilities_section = cast(dict[str, Any], raw.get("capabilities", {}))
    return HarnessConfig(
        tier_model=dict(raw.get("tier_model", {})),
        run_cmd=list(run_section.get("cmd", [])),
        capabilities=list(capabilities_section.get("flags", [])),
    )


def load_config(config_dir: Path) -> Config:
    harness = _load_harness(config_dir / "harness.toml")
    base = (config_dir / "base.md").read_text(encoding="utf-8")
    experts = {p.stem: _load_prompt_doc(p) for p in sorted((config_dir / "experts").glob("*.md"))}
    phases = {p.stem: _load_prompt_doc(p) for p in sorted((config_dir / "phases").glob("*.md"))}
    return Config(harness=harness, base=base, experts=experts, phases=phases)
