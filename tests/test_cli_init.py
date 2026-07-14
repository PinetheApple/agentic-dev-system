"""`driver init` scaffolds a claude-code-ready `.agent/config/` into a target
repo — the anti-footgun regression: sandbox must ship ENABLED with network
allowed (see ads/templates/starter/.agent/config/harness.toml's comment): a
disabled jail throws away all containment, and `deny_egress = false` (not
`--unshare-net`) is what keeps `claude -p`'s own API call alive."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from ads.cli import main
from ads.config import load_config


class TestDriverInit(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_init_scaffolds_a_loadable_config(self) -> None:
        exit_code = main(["--repo", str(self.repo), "init"])
        self.assertEqual(exit_code, 0)

        config_dir = self.repo / ".agent" / "config"
        self.assertTrue((config_dir / "harness.toml").is_file())
        self.assertTrue((config_dir / "base.md").is_file())
        self.assertTrue((config_dir / "experts" / "coder.md").is_file())
        self.assertTrue((config_dir / "experts" / "plan.md").is_file())
        self.assertTrue((config_dir / "experts" / "critic.md").is_file())
        self.assertTrue((config_dir / "experts" / "reconcile.md").is_file())
        for phase in ("plan", "dispatch", "validate", "validate-integration", "reconcile"):
            self.assertTrue((config_dir / "phases" / f"{phase}.md").is_file())

        # proves packaging/importlib.resources wiring works, not just that
        # files landed on disk
        cfg = load_config(config_dir)
        self.assertIn("coder", cfg.experts)
        self.assertEqual(cfg.harness.tier_model["standard"], "claude-sonnet-5")

    def test_scaffolded_harness_toml_has_sandbox_enabled_with_network(self) -> None:
        main(["--repo", str(self.repo), "init"])

        raw = (self.repo / ".agent" / "config" / "harness.toml").read_bytes()
        with_sandbox = tomllib.loads(raw.decode("utf-8"))
        sandbox_table = with_sandbox["sandbox"]
        self.assertTrue(sandbox_table["enabled"])
        self.assertFalse(sandbox_table["deny_egress"])
        self.assertIn("~/.claude", sandbox_table["rw_paths"])

    def test_second_init_without_force_refuses(self) -> None:
        main(["--repo", str(self.repo), "init"])

        with self.assertRaises(SystemExit):
            main(["--repo", str(self.repo), "init"])

    def test_second_init_with_force_overwrites(self) -> None:
        main(["--repo", str(self.repo), "init"])
        harness_path = self.repo / ".agent" / "config" / "harness.toml"
        harness_path.write_text("# tampered\n", encoding="utf-8")

        exit_code = main(["--repo", str(self.repo), "init", "--force"])

        self.assertEqual(exit_code, 0)
        self.assertNotIn("tampered", harness_path.read_text(encoding="utf-8"))

    def test_opencode_adapter_still_scaffolds_claude_shaped_starter(self) -> None:
        exit_code = main(["--repo", str(self.repo), "--adapter", "opencode", "init"])

        self.assertEqual(exit_code, 0)
        self.assertTrue((self.repo / ".agent" / "config" / "harness.toml").is_file())


if __name__ == "__main__":
    unittest.main()
