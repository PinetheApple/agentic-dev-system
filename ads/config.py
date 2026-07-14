"""Loads the committed `.agent/config/` tree (ticket 002).

`harness.toml` is the ONLY harness-aware file. Everything else (base.md,
experts/*.md, phases/*.md) is plain prose, composed in-memory by ads/prompt.py.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

FRONTMATTER_DELIM = "---"

# `[capabilities] max_parallel` in harness.toml is numeric tuning fog — wire
# the knob, default a small cap when unset (ticket 006).
DEFAULT_MAX_PARALLEL = 4


@dataclass(frozen=True)
class SandboxConfig:
    """Raw `[sandbox]` knobs from harness.toml (ticket 011). Every list/tuple
    field defaults to `()`, meaning "use `ads/sandbox.py`'s built-in
    defaults" — this dataclass is a pass-through of the config file's shape;
    `ads/sandbox.py`'s `policy_from_harness` owns the actual default values,
    `~` expansion, and existence filtering so those lists live in one place."""

    enabled: bool = False
    deny_egress: bool = True
    mem_max: str | None = None
    cpu_quota: str | None = None
    tmpfs_size: str | None = None
    tasks_max: int | None = None
    wall_clock: int | None = None
    ro_paths: tuple[str, ...] = ()
    ro_home_paths: tuple[str, ...] = ()
    rw_paths: tuple[str, ...] = ()
    mask_paths: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = ()
    caps_required: bool = False


@dataclass(frozen=True)
class NativeConfig:
    """Raw `[native]` knobs from harness.toml (ticket 011 dec-9). Deliberately
    a SEPARATE table from `[sandbox]`: `[sandbox]` is the driver-wrap jail
    policy (a host-level FS/net/cgroup boundary the driver builds itself);
    `[native]` only applies when the harness advertises
    `SANDBOX_NATIVE_CAPABILITY` and the driver does NOT wrap `run()` — in
    that posture, these knobs become `claude` CLI flags applied INSIDE the
    harness process for defense-in-depth, not a standalone jail. See
    `ads/adapters/claude_code.py`'s `run()` for exactly how they're used."""

    permission_mode: str | None = None
    disallowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class HarnessConfig:
    tier_model: dict[str, str]
    run_cmd: list[str]
    capabilities: list[str]
    max_parallel: int = DEFAULT_MAX_PARALLEL
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    native: NativeConfig = field(default_factory=NativeConfig)


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
    sandbox_section = cast(dict[str, Any], raw.get("sandbox", {}))
    native_section = cast(dict[str, Any], raw.get("native", {}))
    return HarnessConfig(
        tier_model=dict(raw.get("tier_model", {})),
        run_cmd=list(run_section.get("cmd", [])),
        capabilities=list(capabilities_section.get("flags", [])),
        max_parallel=int(capabilities_section.get("max_parallel", DEFAULT_MAX_PARALLEL)),
        sandbox=_load_sandbox(sandbox_section),
        native=_load_native(native_section),
    )


def _load_sandbox(section: dict[str, Any]) -> SandboxConfig:
    return SandboxConfig(
        enabled=bool(section.get("enabled", False)),
        deny_egress=bool(section.get("deny_egress", True)),
        mem_max=section.get("mem_max"),
        cpu_quota=section.get("cpu_quota"),
        tmpfs_size=section.get("tmpfs_size"),
        tasks_max=section.get("tasks_max"),
        wall_clock=section.get("wall_clock"),
        ro_paths=tuple(section.get("ro_paths", ())),
        ro_home_paths=tuple(section.get("ro_home_paths", ())),
        rw_paths=tuple(section.get("rw_paths", ())),
        mask_paths=tuple(section.get("mask_paths", ())),
        env_allowlist=tuple(section.get("env_allowlist", ())),
        caps_required=bool(section.get("caps_required", False)),
    )


def _load_native(section: dict[str, Any]) -> NativeConfig:
    return NativeConfig(
        permission_mode=section.get("permission_mode"),
        disallowed_tools=tuple(section.get("disallowed_tools", ())),
    )


def load_config(config_dir: Path) -> Config:
    harness = _load_harness(config_dir / "harness.toml")
    base = (config_dir / "base.md").read_text(encoding="utf-8")
    experts = {p.stem: _load_prompt_doc(p) for p in sorted((config_dir / "experts").glob("*.md"))}
    phases = {p.stem: _load_prompt_doc(p) for p in sorted((config_dir / "phases").glob("*.md"))}
    return Config(harness=harness, base=base, experts=experts, phases=phases)
