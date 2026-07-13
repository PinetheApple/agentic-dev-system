"""Containment sandbox (ticket 011): the DRIVER-owned jail floor wrapped
around every `run()` and every `cmd` gate.

Mechanism: a `bwrap` FS jail (`--unshare-net` egress-deny, `--clearenv` +
allowlist, read-only system binds, tmpfs-masked secret paths, one rw bind at
the caller's `cwd`) optionally nested inside a `systemd-run --scope`
cgroups-v2 resource scope when resource caps are configured. Policy is a
single `SandboxPolicy` built from `harness.toml`'s `[sandbox]` table
(`ads/config.py` -> `policy_from_harness` here) and applied identically
regardless of which harness is driving (`ads/adapters/claude_code.py`,
`ads/adapters/opencode.py`) or which gate is executing (`ads/validate.py`'s
`cmd` criterion) — selected by policy/capability, never an `if claude-code`.

Deliberately OUT of scope this slice (see the ticket): the `needs-escalation`
driver approval state machine (dec 6) and driver-brokered deps/WebFetch
(dec 3). `classify_cmd` below is advisory-only — it never blocks execution;
routing a flagged command to human escalation is exactly that deferred dec-6
machinery.
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
import sys
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ads.config import HarnessConfig

# The capability flag a harness sets when it jails `run()` itself (dec 9);
# neither `claude-code` nor `opencode` advertises this today, so every real
# adapter currently takes the driver-wrap branch. Specified-but-unvalidated:
# no harness this slice actually self-jails, so this path has no live cover.
SANDBOX_NATIVE_CAPABILITY = "sandbox-native"

DEFAULT_RO_PATHS: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")
# `/etc` minus secrets is a known simplification (fine-grained /etc masking,
# e.g. shadow/ssh host keys, is fog for a later slice).
DEFAULT_MASK_PATH_TEMPLATES: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.config/gh",
    "~/.gnupg",
)
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR")
# Common toolchain/cache dirs a real task needs read access to (rustup,
# cargo, npm, pyenv, pip/uv caches, nvm). Deliberately conservative and
# read-only-bound only — never anything under ~/.config, ~/.ssh, ~/.aws, or
# other credential-bearing dirs (those stay covered by DEFAULT_MASK_PATH_TEMPLATES).
DEFAULT_RO_HOME_PATHS: tuple[str, ...] = (
    "~/.cargo",
    "~/.rustup",
    "~/.npm",
    "~/.pyenv",
    "~/.cache",
    "~/.local/share/uv",
    "~/.nvm",
)

_SCOPE_PROBE_TIMEOUT_SECONDS = 5


class SandboxUnavailable(RuntimeError):
    """Raised by `require()` when a policy demands containment but the host
    is missing the tools to build it — fail-closed, never run unwrapped."""


@dataclass(frozen=True)
class SandboxPolicy:
    enabled: bool
    deny_egress: bool = True
    ro_paths: tuple[str, ...] = DEFAULT_RO_PATHS
    ro_home_paths: tuple[str, ...] = ()
    mask_paths: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    mem_max: str | None = None
    cpu_quota: str | None = None
    tasks_max: int | None = None
    tmpfs_size: str | None = None
    wall_clock_seconds: int | None = None
    caps_required: bool = False


@dataclass(frozen=True)
class CmdVerdict:
    flagged: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


def is_available() -> bool:
    return shutil.which("bwrap") is not None and shutil.which("systemd-run") is not None


def require(policy: SandboxPolicy) -> None:
    """Fail-closed: a configured jail that can't be built must not silently
    run unwrapped. No-op when the policy is disabled. This only checks that
    `bwrap`/`systemd-run` exist on PATH at all — with no FS jail the policy
    can never be honored, regardless of `caps_required`. The softer
    "caps requested but the systemd *user* scope isn't usable" case is
    handled separately by `wrap_command` (see `scope_available`)."""
    if policy.enabled and not is_available():
        raise SandboxUnavailable(
            "sandbox policy is enabled but `bwrap`/`systemd-run` are not both on PATH"
        )


@functools.lru_cache(maxsize=1)
def scope_available() -> bool:
    """Impure, cached probe: can we start a rootless `systemd-run --user
    --scope`? False on hosts with no systemd user manager (headless/CI),
    without raising."""
    try:
        proc = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "true"],
            capture_output=True,
            timeout=_SCOPE_PROBE_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _cap_properties(policy: SandboxPolicy) -> list[str]:
    props: list[str] = []
    if policy.mem_max is not None:
        props.append(f"--property=MemoryMax={policy.mem_max}")
    if policy.cpu_quota is not None:
        props.append(f"--property=CPUQuota={policy.cpu_quota}")
    if policy.tasks_max is not None:
        props.append(f"--property=TasksMax={policy.tasks_max}")
    return props


def wrap(
    argv: list[str],
    cwd: Path,
    policy: SandboxPolicy,
    env: Mapping[str, str],
    *,
    enable_scope: bool = True,
) -> list[str]:
    """Pure: build the fully-wrapped argv for `argv` run at `cwd`. Identity
    passthrough when `policy.enabled` is False, so every non-sandbox
    environment (and every test that doesn't opt in) is unaffected.

    `enable_scope` gates whether a set of resource caps actually gets a
    `systemd-run --user --scope` layer — this function stays pure and never
    probes the host itself; callers that need the "is a user scope even
    usable here" decision should go through `wrap_command` instead."""
    if not policy.enabled:
        return list(argv)

    cwd_str = str(cwd)

    scope_prefix: list[str] = []
    cap_properties = _cap_properties(policy)
    if enable_scope and cap_properties:
        # Only nest under systemd-run when a resource cap is actually set —
        # environments without cgroup delegation still get the bwrap FS/net
        # jail with no cgroups layer. `--user` targets the per-user systemd
        # manager so this works rootless without polkit/root.
        scope_prefix = ["systemd-run", "--user", "--scope", "--quiet", *cap_properties]

    bwrap_cmd: list[str] = ["bwrap", "--die-with-parent", "--unshare-pid"]
    if policy.deny_egress:
        bwrap_cmd.append("--unshare-net")
    bwrap_cmd.append("--clearenv")
    for key in policy.env_allowlist:
        if key in env:
            bwrap_cmd += ["--setenv", key, env[key]]
    # `--proc`/`--dev` are bwrap-synthesized minimal virtual mounts (not host
    # /dev), and mandatory for the jail to be usable: nearly every real
    # command touches /dev/null or /proc.
    bwrap_cmd += ["--proc", "/proc", "--dev", "/dev"]
    for path in policy.ro_paths:
        if Path(path).exists():
            bwrap_cmd += ["--ro-bind", path, path]
    for path in policy.ro_home_paths:
        if Path(path).exists():
            bwrap_cmd += ["--ro-bind", path, path]
    if policy.tmpfs_size is not None:
        # bwrap's `--size BYTES` sizes the *next* `--tmpfs` arg and wants a
        # raw byte count, not a human suffix like "2G" — callers must
        # pre-compute bytes; documented here rather than silently truncated.
        bwrap_cmd += ["--size", policy.tmpfs_size, "--tmpfs", "/tmp"]
    else:
        bwrap_cmd += ["--tmpfs", "/tmp"]
    for path in policy.mask_paths:
        bwrap_cmd += ["--tmpfs", path]
    bwrap_cmd += ["--bind", cwd_str, cwd_str, "--chdir", cwd_str, "--", *argv]

    return [*scope_prefix, *bwrap_cmd]


def _resolve_enable_scope(policy: SandboxPolicy) -> bool:
    """Impure: decide whether the systemd-run scope layer should be emitted.
    Caps unset -> irrelevant (wrap() only emits the layer when caps are
    present anyway). Caps set: probe `scope_available()`; if it's usable,
    enable it; if not, degrade (warn) or fail-closed per `caps_required`."""
    if not _cap_properties(policy):
        return False
    if scope_available():
        return True
    if policy.caps_required:
        raise SandboxUnavailable(
            "sandbox policy requires resource caps (caps_required=True) but no "
            "systemd --user scope is available on this host"
        )
    warnings.warn(
        "resource caps requested but systemd user scope unavailable; running "
        "bwrap FS/net jail without cgroup caps",
        stacklevel=3,
    )
    print(
        "[sandbox] resource caps requested but systemd user scope unavailable; "
        "running bwrap FS/net jail without cgroup caps",
        file=sys.stderr,
    )
    return False


def wrap_command(
    argv: list[str], cwd: Path, policy: SandboxPolicy, env: Mapping[str, str]
) -> list[str]:
    """Impure composer: the entry point callers should use instead of raw
    `wrap`. Resolves whether the cgroup scope layer is actually usable on
    this host (`scope_available`) and applies the fail-closed/degrade
    decision (`SandboxPolicy.caps_required`) before delegating to the pure
    `wrap`. Identity passthrough when `policy.enabled` is False."""
    if not policy.enabled:
        return list(argv)
    enable_scope = _resolve_enable_scope(policy)
    return wrap(argv, cwd, policy, env, enable_scope=enable_scope)


def wrap_shell(
    command: str, cwd: Path, policy: SandboxPolicy, env: Mapping[str, str]
) -> tuple[list[str], bool]:
    """For the `cmd` gate, which today runs `command` with `shell=True`.

    Returns `(to_run, use_shell)`: when disabled, `([command], True)` —
    identical to today's `subprocess.run(command, shell=True, ...)`. When
    enabled, `(wrap_command(["/bin/sh", "-c", command], ...), False)` — the
    caller must switch to `shell=False` since the shell is now explicit
    inside the jailed argv. Delegates to `wrap_command` so the cmd gate gets
    the same scope-availability degrade/fail-closed treatment as `run()`.
    """
    if not policy.enabled:
        return [command], True
    return wrap_command(["/bin/sh", "-c", command], cwd, policy, env), False


def resolve_env(policy: SandboxPolicy, environ: Mapping[str, str]) -> dict[str, str]:
    """Pure allowlist filter: `env_allowlist ∩ environ`."""
    return {k: environ[k] for k in policy.env_allowlist if k in environ}


def policy_from_harness(harness: HarnessConfig, *, home: Path | None = None) -> SandboxPolicy:
    """Translate `harness.toml`'s raw `[sandbox]` knobs (`HarnessConfig.sandbox`)
    into a concrete `SandboxPolicy`: fills in `sandbox.py`'s built-in defaults
    for any knob the config left at `()`, expands `~` in mask paths against
    `home` (default `Path.home()`), and filters mask paths to those that
    actually exist. Config carries raw knobs only — this module owns
    defaults + path resolution, so the default path lists live in exactly
    one place."""
    cfg = harness.sandbox
    if not cfg.enabled:
        return SandboxPolicy(enabled=False)

    home_dir = home if home is not None else Path.home()

    def _expand(raw: str) -> str:
        return str(home_dir / raw[2:]) if raw.startswith("~/") else raw

    mask_templates = cfg.mask_paths or DEFAULT_MASK_PATH_TEMPLATES
    expanded_masks = tuple(_expand(raw) for raw in mask_templates)
    existing_masks = tuple(p for p in expanded_masks if Path(p).exists())

    ro_home_templates = cfg.ro_home_paths or DEFAULT_RO_HOME_PATHS
    expanded_ro_home = tuple(_expand(raw) for raw in ro_home_templates)
    existing_ro_home = tuple(p for p in expanded_ro_home if Path(p).exists())

    return SandboxPolicy(
        enabled=True,
        deny_egress=cfg.deny_egress,
        ro_paths=cfg.ro_paths or DEFAULT_RO_PATHS,
        ro_home_paths=existing_ro_home,
        mask_paths=existing_masks,
        env_allowlist=cfg.env_allowlist or DEFAULT_ENV_ALLOWLIST,
        mem_max=cfg.mem_max,
        cpu_quota=cfg.cpu_quota,
        tasks_max=cfg.tasks_max,
        tmpfs_size=cfg.tmpfs_size,
        wall_clock_seconds=cfg.wall_clock,
        caps_required=cfg.caps_required,
    )


# ---------------------------------------------------------------------------
# cmd classifier (dec 7 soft screen) — pure, advisory-only.
#
# This never blocks execution; the bwrap floor above is the real boundary.
# Routing a flagged command to human approval is the deferred dec-6
# `needs-escalation` state machine — out of scope this slice.
# ---------------------------------------------------------------------------

_RM_RF_RE = re.compile(r"\brm\s+(?:-\S+\s+)*-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+(\S+)")
_RM_FR_RE = re.compile(r"\brm\s+(?:-\S+\s+)*-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*\s+(\S+)")
_FORK_BOMB_RE = re.compile(r":\s*\(\)\s*{\s*:\s*\|\s*:\s*&?\s*}\s*;\s*:")
_PIPE_TO_SHELL_RE = re.compile(r"\b(curl|wget)\b[^|;]*\|\s*(sudo\s+)?(sh|bash|zsh)\b")
_BASE64_TO_SHELL_RE = re.compile(
    r"base64\b[^|;]*(-d|--decode)[^|;]*\|\s*(sudo\s+)?(sh|bash|zsh)\b"
)
_ECHO_TO_SHELL_RE = re.compile(r"\becho\b[^|;]*\|\s*(sudo\s+)?(sh|bash|zsh|base64)\b")
_HIGH_ENTROPY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"
)

_SIMPLE_DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bsudo\b", "sudo"),
    (r"\bmkfs\b", "mkfs"),
    (r"\bdd\s+[^|;]*\bof=/dev/", "dd writing to a /dev block device"),
    (r">\s*/dev/sd\w*", "write redirected to /dev/sd*"),
    (r"\b(shutdown|reboot)\b", "shutdown/reboot"),
    (r"\bchmod\s+-R\s+777\s+/(?:\s|$)", "chmod -R 777 /"),
    (r"\bnpm\s+publish\b", "npm publish"),
    (r"\bcargo\s+publish\b", "cargo publish"),
    (r"\btwine\s+upload\b", "twine upload"),
    (r"\bpip\b[^|;]*\bupload\b", "pip upload"),
    (r"\bgh\s+release\b", "gh release"),
    (r"\bgit\s+push\b", "git push"),
    (r"--no-preserve-root", "rm --no-preserve-root"),
)

_SIMPLE_OBFUSCATION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_PIPE_TO_SHELL_RE, "curl/wget piped directly into a shell"),
    (_BASE64_TO_SHELL_RE, "base64-decoded payload piped into a shell"),
    (_ECHO_TO_SHELL_RE, "echoed payload piped into a shell/decoder"),
    (re.compile(r"\beval\b"), "eval of an assembled string"),
)


def _rm_rf_reason(command: str) -> str | None:
    for pattern in (_RM_RF_RE, _RM_FR_RE):
        match = pattern.search(command)
        if match is None:
            continue
        target = match.group(1)
        if target.startswith("/") or target.startswith("~"):
            return f"rm -rf targeting {target!r}"
    return None


def classify_cmd(command: str) -> CmdVerdict:
    """Pure, conservative dec-7 soft screen over two axes: dangerous intent
    (destructive/exfiltrating shapes) and obfuscation (the obfuscation is
    itself the signal, regardless of what it decodes to)."""
    reasons: list[str] = []

    rm_reason = _rm_rf_reason(command)
    if rm_reason is not None:
        reasons.append(rm_reason)
    if _FORK_BOMB_RE.search(command):
        reasons.append("fork-bomb shape")
    for pattern, reason in _SIMPLE_DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            reasons.append(reason)
    for compiled, reason in _SIMPLE_OBFUSCATION_PATTERNS:
        if compiled.search(command):
            reasons.append(reason)
    if _HIGH_ENTROPY_TOKEN_RE.search(command):
        reasons.append("high-entropy/base64-shaped blob present")

    return CmdVerdict(flagged=bool(reasons), reasons=tuple(dict.fromkeys(reasons)))


__all__ = [
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_MASK_PATH_TEMPLATES",
    "DEFAULT_RO_HOME_PATHS",
    "DEFAULT_RO_PATHS",
    "SANDBOX_NATIVE_CAPABILITY",
    "CmdVerdict",
    "SandboxPolicy",
    "SandboxUnavailable",
    "classify_cmd",
    "is_available",
    "policy_from_harness",
    "require",
    "resolve_env",
    "scope_available",
    "wrap",
    "wrap_command",
    "wrap_shell",
]
