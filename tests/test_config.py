"""Ticket 009: the config loader fills `{placeholder}` markers via literal
substring replacement, so literal JSON braces in the phase templates must
survive untouched, and `compose()` layers sections in a fixed order."""

from __future__ import annotations

import unittest
from pathlib import Path

from ads.config import load_base, load_optional, render_phase
from ads.prompt import compose

REPO_CONFIG = Path(__file__).resolve().parent.parent / ".agent" / "config"


class RenderPhaseTest(unittest.TestCase):
    def test_placeholders_are_substituted(self) -> None:
        text = render_phase(REPO_CONFIG, "plan", {"intent": "build the thing"})
        self.assertIn("build the thing", text)
        self.assertNotIn("{intent}", text)

    def test_execute_placeholders_all_fill(self) -> None:
        text = render_phase(
            REPO_CONFIG,
            "execute",
            {"task_id": "01-implement", "owns": "src/thing.py", "task": "Implement thing."},
        )
        self.assertIn("TASK_ID: 01-implement", text)
        self.assertIn("src/thing.py", text)
        self.assertIn("Implement thing.", text)
        self.assertNotIn("{task_id}", text)
        self.assertNotIn("{owns}", text)
        self.assertNotIn("{task}", text)

    def test_validate_placeholders_all_fill(self) -> None:
        text = render_phase(
            REPO_CONFIG, "validate", {"criterion": "does the thing", "diff": "diff --git a/x b/x"}
        )
        self.assertIn("does the thing", text)
        self.assertIn("diff --git a/x b/x", text)
        self.assertNotIn("{criterion}", text)
        self.assertNotIn("{diff}", text)

    def test_literal_json_braces_survive_substitution(self) -> None:
        text = render_phase(REPO_CONFIG, "execute", {"task_id": "x", "owns": "x", "task": "x"})
        self.assertIn('"status": "complete" | "blocked"', text)

    def test_load_optional_missing_file_returns_empty(self) -> None:
        self.assertEqual(load_optional(REPO_CONFIG / "does-not-exist.md"), "")

    def test_load_base_reads_file(self) -> None:
        self.assertIn("Core Engineering Principles", load_base(REPO_CONFIG))


class ComposeLayeringTest(unittest.TestCase):
    def test_sections_appear_in_order(self) -> None:
        prompt = compose(
            base="BASE_MARKER",
            expert_body="",
            design="DESIGN_MARKER",
            task_body="TASK_MARKER",
            spec="SPEC_MARKER",
        )
        order = [
            prompt.index(marker)
            for marker in ("BASE_MARKER", "SPEC_MARKER", "DESIGN_MARKER", "TASK_MARKER")
        ]
        self.assertEqual(order, sorted(order))

    def test_empty_sections_are_omitted(self) -> None:
        prompt = compose(base="BASE_MARKER", expert_body="", design="", task_body="TASK_MARKER")
        self.assertNotIn("## Design", prompt)
        self.assertNotIn("## Spec", prompt)


if __name__ == "__main__":
    unittest.main()
