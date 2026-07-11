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
    tools: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Config:
    harness: HarnessConfig
    base: str
    experts: dict[str, PromptDoc]
    phases: dict[str, PromptDoc]


TOOLS_KEY = "tools"


def _parse_tools_list(value: str) -> tuple[str, ...]:
    """`tools: [Read, Write, Edit, Bash]` inline-list syntax."""
    inner = value.strip().removeprefix("[").removesuffix("]")
    return tuple(item.strip() for item in inner.split(",") if item.strip())


def _split_simple_frontmatter(text: str) -> tuple[dict[str, str], str, tuple[str, ...] | None]:
    """Optional `--- key: value ... ---` header followed by prose body.

    Only scalar `key: value` lines are supported here — experts/phases don't
    need the richer list/map shape that task frontmatter does — except for
    the single `tools: [A, B]` inline-list key an expert may declare.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return {}, text, None
    meta: dict[str, str] = {}
    tools: tuple[str, ...] | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == FRONTMATTER_DELIM:
            body = "\n".join(lines[idx + 1 :]).lstrip("\n")
            return meta, body, tools
        key, _, value = lines[idx].partition(":")
        key = key.strip()
        if key == TOOLS_KEY:
            tools = _parse_tools_list(value)
        elif key:
            meta[key] = value.strip()
    return {}, text, None


def _load_prompt_doc(path: Path) -> PromptDoc:
    meta, body, tools = _split_simple_frontmatter(path.read_text(encoding="utf-8"))
    return PromptDoc(meta=meta, body=body, tools=tools)


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
