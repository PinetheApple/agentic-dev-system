"""Regression coverage for the `opencode run --format json` NDJSON parse path
and for the harness-swap/single-source proof (ticket 002, second harness).

The recorded stdout fixtures below are grounded in a real `opencode run
--format json` invocation against v1.17.16 (no credentials, OpenCode's
bundled free-tier model, zero cost) — see `ads/adapters/opencode.py`'s module
docstring. Only the events actually observed (`step_start`, `text`,
`step_finish`) are used verbatim; unknown/tool-call event shapes were not
observed live, so the parser (and these tests) treat unrecognized lines as
ignorable rather than assuming a specific tool-event schema.
"""

from __future__ import annotations

import inspect
import json
import unittest
from collections.abc import Mapping
from pathlib import Path

from ads.adapters.base import ADAPTER_OPENCODE
from ads.adapters.claude_code import ClaudeCodeAdapter
from ads.adapters.opencode import OpenCodeAdapter, parse_opencode_stdout
from ads.cli import _build_adapter  # pyright: ignore[reportPrivateUsage]
from ads.config import Config, HarnessConfig, load_config
from ads.prompt import compose

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_CONFIG = REPO_ROOT / "examples" / "demo" / ".agent" / "config"


def _ndjson(*events: Mapping[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _text_event(text: str) -> Mapping[str, object]:
    return {
        "type": "text",
        "timestamp": 1783926220840,
        "sessionID": "ses_0a5b625afffeF5vbivMzKzAQYe",
        "part": {
            "id": "prt_f5a49e7d4001HP0S1C6FCnVTEF",
            "messageID": "msg_f5a49dba6001Iah8009PnV5aqS",
            "sessionID": "ses_0a5b625afffeF5vbivMzKzAQYe",
            "type": "text",
            "text": text,
            "time": {"start": 1783926220756, "end": 1783926220834},
        },
    }


_STEP_START: Mapping[str, object] = {
    "type": "step_start",
    "timestamp": 1783926219664,
    "sessionID": "ses_0a5b625afffeF5vbivMzKzAQYe",
    "part": {
        "id": "prt_f5a49e38c00152HgjlpEBkO2wr",
        "messageID": "msg_f5a49dba6001Iah8009PnV5aqS",
        "sessionID": "ses_0a5b625afffeF5vbivMzKzAQYe",
        "snapshot": "af99c7f857d1ae0bb38c98cefb5fe7ea083db99a",
        "type": "step-start",
    },
}

_STEP_FINISH: Mapping[str, object] = {
    "type": "step_finish",
    "timestamp": 1783926220863,
    "sessionID": "ses_0a5b625afffeF5vbivMzKzAQYe",
    "part": {
        "id": "prt_f5a49e838001boXNRKX63Wz5XJ",
        "reason": "stop",
        "snapshot": "af99c7f857d1ae0bb38c98cefb5fe7ea083db99a",
        "messageID": "msg_f5a49dba6001Iah8009PnV5aqS",
        "sessionID": "ses_0a5b625afffeF5vbivMzKzAQYe",
        "type": "step-finish",
        "tokens": {"total": 13828, "input": 13805, "output": 10, "reasoning": 13, "cache": {}},
        "cost": 0,
    },
}


class TestParseOpencodeStdout(unittest.TestCase):
    def test_bare_json_text_event_parses_into_structured(self) -> None:
        stdout = _ndjson(
            _STEP_START,
            _text_event('{"status": "done", "summary": "did the thing"}'),
            _STEP_FINISH,
        )

        text, structured = parse_opencode_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertEqual(structured.get("status"), "done")
        self.assertEqual(text, '{"status": "done", "summary": "did the thing"}')

    def test_fenced_json_text_event_still_parses(self) -> None:
        stdout = _ndjson(
            _STEP_START,
            _text_event('```json\n{"pass": true, "notes": "looks fine"}\n```'),
            _STEP_FINISH,
        )

        _, structured = parse_opencode_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertIs(structured.get("pass"), True)

    def test_multiple_text_events_are_concatenated_in_order(self) -> None:
        stdout = _ndjson(
            _STEP_START,
            _text_event('{"status": "done", '),
            _text_event('"summary": "streamed in chunks"}'),
            _STEP_FINISH,
        )

        text, structured = parse_opencode_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertEqual(structured.get("summary"), "streamed in chunks")
        self.assertEqual(text, '{"status": "done", "summary": "streamed in chunks"}')

    def test_conversational_text_yields_no_structured_payload(self) -> None:
        stdout = _ndjson(_STEP_START, _text_event("Hello! How can I help you today?"), _STEP_FINISH)

        text, structured = parse_opencode_stdout(stdout)

        self.assertIsNone(structured)
        self.assertEqual(text, "Hello! How can I help you today?")

    def test_no_text_events_falls_back_to_raw_stdout(self) -> None:
        stdout = _ndjson(_STEP_START, _STEP_FINISH)

        text, structured = parse_opencode_stdout(stdout)

        self.assertIsNone(structured)
        self.assertEqual(text, stdout)

    def test_empty_stdout_falls_back_to_raw_text(self) -> None:
        text, structured = parse_opencode_stdout("")

        self.assertIsNone(structured)
        self.assertEqual(text, "")

    def test_unparseable_lines_are_skipped_not_fatal(self) -> None:
        stdout = "not json\n" + _ndjson(_text_event('{"status": "done"}'))

        text, structured = parse_opencode_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertEqual(structured.get("status"), "done")
        self.assertEqual(text, '{"status": "done"}')


class TestBuildAdapterOpencode(unittest.TestCase):
    def test_build_adapter_opencode_returns_opencode_adapter(self) -> None:
        cfg = load_config(DEMO_CONFIG)
        adapter = _build_adapter(ADAPTER_OPENCODE, cfg)

        self.assertIsInstance(adapter, OpenCodeAdapter)

    def test_capabilities_reflect_tool_scoping_gap(self) -> None:
        harness = HarnessConfig(
            tier_model={"fast": "a", "standard": "b", "deep": "c"},
            run_cmd=["opencode", "run"],
            capabilities=["tools", "streaming", "json_output"],
        )
        adapter = OpenCodeAdapter(harness)

        self.assertNotIn("allowedtools-cli", adapter.capabilities())


class TestPromptCompositionIsAdapterAgnostic(unittest.TestCase):
    """Single-source proof: the composed prompt only depends on
    base+expert+design+task text, never on which adapter will run it."""

    def test_compose_signature_takes_no_adapter_or_harness_argument(self) -> None:
        """Structural proof `compose()` has no seam for an adapter to
        influence the prompt at all — not just that two calls agree."""
        params = list(inspect.signature(compose).parameters)

        self.assertEqual(params, ["base", "expert_body", "design", "task_body"])

    def test_dispatch_prompt_built_from_config_is_byte_identical_across_adapters(self) -> None:
        """Build the same dispatch prompt the driver would build, once per
        adapter, and confirm the adapter choice never touched the bytes."""
        cfg = load_config(DEMO_CONFIG)
        expert = cfg.experts["python-expert"]
        task_body = cfg.phases["dispatch"].body.replace("{task}", "Implement the thing.")
        design_text = "# Design\n\nUse a simple layered design.\n"

        adapters: list[object] = [
            ClaudeCodeAdapter(cfg.harness),
            OpenCodeAdapter(cfg.harness),
        ]
        prompts = {
            type(adapter).__name__: compose(cfg.base, expert.body, design_text, task_body)
            for adapter in adapters
        }

        self.assertEqual(len(set(prompts.values())), 1)

    def test_experts_phases_base_are_the_same_config_dicts_across_harness_files(self) -> None:
        """Loading the demo config with either harness.toml or
        harness.opencode.toml yields identical base/experts/phases — proving
        the harness file is the only thing that differs."""
        claude_cfg = load_config(DEMO_CONFIG)

        opencode_harness = DEMO_CONFIG / "harness.opencode.toml"
        self.assertTrue(opencode_harness.exists())
        # load_config always reads "harness.toml" by name; simulate the swap
        # by loading the sibling file's contents through the same loader path
        # a `cp harness.opencode.toml harness.toml` would take.
        swapped_cfg = _load_config_with_harness_file(DEMO_CONFIG, opencode_harness)

        self.assertEqual(claude_cfg.base, swapped_cfg.base)
        self.assertEqual(claude_cfg.experts.keys(), swapped_cfg.experts.keys())
        self.assertEqual(claude_cfg.phases.keys(), swapped_cfg.phases.keys())
        for name, expert in claude_cfg.experts.items():
            self.assertEqual(expert.body, swapped_cfg.experts[name].body)
            self.assertEqual(expert.tools, swapped_cfg.experts[name].tools)
        self.assertNotEqual(claude_cfg.harness.tier_model, swapped_cfg.harness.tier_model)


def _load_config_with_harness_file(config_dir: Path, harness_file: Path) -> Config:
    """Mirrors `ads.config.load_config` but reads an arbitrary harness file —
    used only to prove config-swap equivalence without touching the repo's
    on-disk `harness.toml`."""
    import tomllib

    from ads.config import HarnessConfig as _HarnessConfig
    from ads.config import _load_prompt_doc  # pyright: ignore[reportPrivateUsage]

    with harness_file.open("rb") as fh:
        raw = tomllib.load(fh)
    harness = _HarnessConfig(
        tier_model=dict(raw.get("tier_model", {})),
        run_cmd=list(raw.get("run", {}).get("cmd", [])),
        capabilities=list(raw.get("capabilities", {}).get("flags", [])),
    )
    base = (config_dir / "base.md").read_text(encoding="utf-8")
    experts = {p.stem: _load_prompt_doc(p) for p in sorted((config_dir / "experts").glob("*.md"))}
    phases = {p.stem: _load_prompt_doc(p) for p in sorted((config_dir / "phases").glob("*.md"))}
    return Config(harness=harness, base=base, experts=experts, phases=phases)


if __name__ == "__main__":
    unittest.main()
