"""Regression coverage for the `claude -p --output-format json` envelope
parse path: the model's phase-shaped JSON answer lives nested inside the
CLI's own JSON envelope's `result` field, not at the envelope's top level."""

from __future__ import annotations

import json
import unittest

from ads.adapters.claude_code import parse_claude_stdout


class TestParseClaudeStdout(unittest.TestCase):
    def test_dict_envelope_result_field_parses_into_structured(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "result": '{"status": "done", "summary": "did the thing"}',
                "total_cost_usd": 0.01,
                "session_id": "abc123",
            }
        )

        text, structured = parse_claude_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertEqual(structured.get("status"), "done")
        self.assertEqual(structured.get("summary"), "did the thing")
        self.assertEqual(text, '{"status": "done", "summary": "did the thing"}')

    def test_result_wrapped_in_markdown_fence_still_parses(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "result": '```json\n{"pass": true, "notes": "looks fine"}\n```',
            }
        )

        _, structured = parse_claude_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertIs(structured.get("pass"), True)

    def test_array_of_stream_events_extracts_trailing_result_entry(self) -> None:
        """Observed live: hook/plugin-heavy sessions emit a JSON array of the
        full event stream instead of a single result object; the terminal
        `type: "result"` entry still carries the model's answer."""
        stdout = json.dumps(
            [
                {"type": "system", "subtype": "init"},
                {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 5},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}},
                {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
                {
                    "type": "result",
                    "subtype": "success",
                    "result": '{"status": "blocked", "summary": "missing dependency"}',
                    "total_cost_usd": 0.02,
                },
            ]
        )

        _, structured = parse_claude_stdout(stdout)

        self.assertIsNotNone(structured)
        assert structured is not None
        self.assertEqual(structured.get("status"), "blocked")

    def test_non_json_result_text_yields_no_structured_payload(self) -> None:
        stdout = json.dumps({"type": "result", "result": "I could not comply with that request."})

        text, structured = parse_claude_stdout(stdout)

        self.assertIsNone(structured)
        self.assertEqual(text, "I could not comply with that request.")

    def test_unparseable_stdout_falls_back_to_raw_text(self) -> None:
        stdout = "not json at all"

        text, structured = parse_claude_stdout(stdout)

        self.assertIsNone(structured)
        self.assertEqual(text, stdout)

    def test_array_with_no_result_entry_yields_no_structured_payload(self) -> None:
        stdout = json.dumps([{"type": "system", "subtype": "init"}])

        text, structured = parse_claude_stdout(stdout)

        self.assertIsNone(structured)
        self.assertEqual(text, stdout)


if __name__ == "__main__":
    unittest.main()
