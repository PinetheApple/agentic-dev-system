"""Observability feed (ticket 005). Adapts `music_app`'s `loop_fmt.py` — its
`render()` tag-per-line dispatch, footer, `human_num`/`human_time`, `STATS`
usage tracking are lifted, not rewritten — and extends `render()` to also
dispatch the ADS 5-field envelope (`{ts, seq, phase, type, task, data}`).

`rich` is the one blessed runtime-dependency exception (SPEC §7), import-
guarded here: the token-free test suite runs without it (no pinned footer,
plain-print lines only). Non-TTY output (redirected/piped) degrades the same
way even when rich is installed.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from collections.abc import Iterator
from typing import Any, TextIO, cast


# Dynamic import (rather than a static `from rich... import ...`) so pyright
# doesn't need rich resolvable to type-check this module: `rich` is the one
# blessed runtime-dependency exception (SPEC §7), and the token-free test
# suite must run without it installed at all — degrade to plain-print.
def _try_import_rich() -> tuple[Any, Any, Any, Any, bool]:
    try:
        console = importlib.import_module("rich.console")
        live = importlib.import_module("rich.live")
        panel = importlib.import_module("rich.panel")
        text = importlib.import_module("rich.text")
        return console, live, panel, text, True
    except ImportError:
        return None, None, None, None, False


_rich_console, _rich_live, _rich_panel, _rich_text, _RICH_AVAILABLE = _try_import_rich()

RESET = "\033[0m"

ADS_TAGS: dict[str, str] = {
    "run:start": "1;35",
    "phase:enter": "1;34",
    "plan:done": "36",
    "review:gate": "1;33",
    "gate_open": "1;33",
    "gate_close": "33",
    "task:start": "30;43",
    "task:done": "30;42",
    "validate:verdict": "35",
    "halt": "1;31",
    "done": "1;32",
    "gap_decided": "33",
    "error": "91",
}

TOOL_TAGS = {
    "Bash": ("BASH", "30;43"),
    "Read": ("READ", "30;44"),
    "Grep": ("GREP", "30;44"),
    "Glob": ("GLOB", "30;44"),
    "Edit": ("EDIT", "30;42"),
    "Write": ("WRITE", "30;42"),
}
SUBAGENT_TOOLS = {"Task", "Agent"}

TOOL_INDENT = "  "
RESULT_INDENT = "      "

# Running totals across the adapter stream, printed in the footer/summary.
STATS: dict[str, float] = {"context": 0, "in": 0, "out": 0, "cache_read": 0, "cost": 0.0}


def sgr(code: str, text: str) -> str:
    return f"\033[{code}m{text}{RESET}"


def human_num(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def human_time(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60:02d}s"


def track_usage(usage: dict[str, Any]) -> None:
    STATS["in"] += usage.get("input_tokens", 0)
    STATS["out"] += usage.get("output_tokens", 0)
    STATS["cache_read"] += usage.get("cache_read_input_tokens", 0)
    window = (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )
    STATS["context"] = max(STATS["context"], window)


def tag(label: str, code: str) -> str:
    return sgr(code, f" {label} ")


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def stamp() -> str:
    return sgr("90", f"[{time.strftime('%H:%M:%S')}]") + " "


def _tool_line(block: dict[str, Any]) -> str:
    name = block.get("name", "?")
    args = block.get("input", {})
    if name in SUBAGENT_TOOLS:
        agent = args.get("subagent_type", "?")
        detail = args.get("description") or args.get("prompt") or ""
        return stamp() + tag(f"TASK -> {agent}", "1;30;46") + " " + clip(detail, 90)
    label, code = TOOL_TAGS.get(name, (name.upper(), "30;47"))
    detail = (
        args.get("command") or args.get("description") or args.get("file_path") or json.dumps(args)
    )
    return stamp() + tag(label, code) + " " + clip(detail, 140)


def _result_body(content: object) -> str:
    if isinstance(content, list):
        parts = cast("list[dict[str, Any]]", content)
        return " ".join(str(part.get("text", "")) for part in parts)
    return str(content)


def _render_ads_envelope(event: dict[str, Any]) -> Iterator[str]:
    event_type = str(event.get("type", "?"))
    task = event.get("task")
    code = ADS_TAGS.get(event_type, "97;40")
    detail = f" [{task}]" if task else ""
    data = cast("dict[str, Any]", event.get("data") or {})
    if data:
        detail += " " + clip(json.dumps(data, sort_keys=True), 140)
    yield stamp() + tag(event_type.upper(), code) + detail


def _render_claude_stream(event: dict[str, Any]) -> Iterator[str]:
    etype = event.get("type")
    if etype == "system" and event.get("subtype") == "init":
        yield stamp() + tag("START", "1;35") + " session"
    elif etype == "assistant":
        track_usage(event["message"].get("usage", {}))
        for block in event["message"]["content"]:
            if block["type"] == "text" and block["text"].strip():
                yield "\n" + stamp() + tag("NOTE", "97;45") + " " + clip(block["text"], 200)
            elif block["type"] == "tool_use":
                yield TOOL_INDENT + _tool_line(block)
    elif etype == "user":
        for block in event["message"]["content"]:
            if block.get("type") == "tool_result":
                body = clip(_result_body(block.get("content", "")), 130)
                errored = block.get("is_error")
                mark = "x" if errored else "L"
                yield RESULT_INDENT + sgr("91" if errored else "90", f"{mark} {body}")
    elif etype == "result":
        track_usage(event.get("usage", {}))
        STATS["cost"] = event.get("total_cost_usd", STATS["cost"])
        secs = int(event.get("duration_ms", 0) / 1000)
        cost = round(event.get("total_cost_usd", 0), 2)
        summary = f"done {event.get('subtype', 'end')} - {human_time(secs)} - ${cost}"
        yield "\n" + sgr("1;32", summary)


def render(event: dict[str, Any]) -> Iterator[str]:
    """Dispatch by shape: an ADS envelope (has `seq`+`type`+`phase`) renders a
    colored ADS tag line; a Claude `stream-json` event (`type` in
    assistant/user/result/system) renders the existing loop_fmt tag line."""
    if "seq" in event and "phase" in event and "type" in event:
        yield from _render_ads_envelope(event)
    else:
        yield from _render_claude_stream(event)


class Feed:
    """The live feed process the driver drives: a line sink plus a pinned
    footer. Degrades to plain-print with no footer when `rich` is absent or
    output isn't a TTY (SPEC §7's blessed exception, import-guarded)."""

    def __init__(self, *, out: TextIO | None = None) -> None:
        self._out = out or sys.stdout
        self._start = time.monotonic()
        self._phase = ""
        self._task = ""
        self._done = 0
        self._total = 0
        self._live: Any | None = None
        self._console: Any | None = None
        if _RICH_AVAILABLE and self._out.isatty():
            console = _rich_console.Console(file=self._out)
            live = _rich_live.Live(
                self._footer(), console=console, refresh_per_second=4, transient=True
            )
            live.start()
            self._console = console
            self._live = live

    def set_progress(self, phase: str, task: str | None, done: int, total: int) -> None:
        self._phase = phase
        self._task = task or ""
        self._done = done
        self._total = total
        if self._live is not None:
            self._live.update(self._footer())

    def emit_line(self, line: str) -> None:
        if self._console is not None:
            self._console.print(_rich_text.Text.from_ansi(line))
        else:
            print(line, file=self._out)

    def emit_event(self, event: dict[str, Any]) -> None:
        for line in render(event):
            self.emit_line(line)

    def close(self) -> None:
        if self._live is not None:
            self._live.stop()

    def _footer(self) -> Any:
        secs = int(time.monotonic() - self._start)
        line = _rich_text.Text()
        line.append("elapsed ", style="dim")
        line.append(human_time(secs), style="bold cyan")
        line.append("  phase ", style="dim")
        line.append(self._phase + (f"·{self._task}" if self._task else "") or "-", style="bold")
        line.append("  tasks ", style="dim")
        line.append(f"{self._done}/{self._total}", style="bold")
        line.append("  context ", style="dim")
        line.append(human_num(STATS["context"]), style="bold")
        line.append("  $", style="dim")
        line.append(f"{STATS['cost']:.2f}", style="bold")
        return _rich_panel.Panel(line, style="green", expand=True)
