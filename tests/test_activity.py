"""`ads/activity.py`: the observability choke point every long-running
`adapter.run()` call goes through — sets/clears `state.current_activity`
around the call, writes `run_start`/`run_end` events, and routes the
`activity_log` path into the adapter."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ads.activity import activity_log_path, run_with_activity
from ads.adapters.base import RunResult, StructuredPayload
from ads.layout import RunLayout
from ads.state import State, load_state, save_state
from ads.tasks import TaskTier


class _SnapshottingAdapter:
    """Records `state.current_activity` at the moment `run()` is called, and
    the `activity_log` path it was given, so the caller can assert the
    heartbeat was set BEFORE the run and cleared AFTER."""

    def __init__(self, state: State) -> None:
        self._state = state
        self.seen_activity: dict[str, str] | None = None
        self.seen_activity_log: Path | None = None

    def resolve_model(self, tier: TaskTier) -> str:
        return "scripted-model"

    def capabilities(self) -> list[str]:
        return []

    def sync(self) -> None:
        pass

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
        *,
        activity_log: Path | None = None,
    ) -> RunResult:
        self.seen_activity = (
            dict(self._state.current_activity) if self._state.current_activity else None
        )
        self.seen_activity_log = activity_log
        payload: StructuredPayload = {"status": "done", "summary": "ok"}
        return RunResult(text=json.dumps(payload), structured=payload, exit_status="ok")


class _RaisingAdapter:
    def resolve_model(self, tier: TaskTier) -> str:
        return "scripted-model"

    def capabilities(self) -> list[str]:
        return []

    def sync(self) -> None:
        pass

    def run(
        self,
        prompt: str,
        cwd: Path,
        allowed_tools: list[str] | None = None,
        tier: TaskTier = "standard",
        *,
        activity_log: Path | None = None,
    ) -> RunResult:
        raise RuntimeError("boom")


class TestRunWithActivity(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.layout = RunLayout(repo=self.repo, run_id="run-1")
        self.layout.scaffold()

    def test_sets_current_activity_before_run_and_clears_after(self) -> None:
        state = State(phase="dispatch")
        adapter = _SnapshottingAdapter(state)

        result = run_with_activity(
            adapter,
            self.layout,
            state,
            label="01-a",
            kind="dispatch",
            prompt="do the thing",
            cwd=self.repo,
            tier="standard",
        )

        self.assertEqual(result.exit_status, "ok")
        assert adapter.seen_activity is not None
        self.assertEqual(adapter.seen_activity["label"], "01-a")
        self.assertEqual(adapter.seen_activity["kind"], "dispatch")
        self.assertEqual(adapter.seen_activity["model"], "scripted-model")
        self.assertTrue(adapter.seen_activity["started_at"])
        self.assertIsNone(state.current_activity)  # cleared after the call

    def test_clears_current_activity_even_when_adapter_raises(self) -> None:
        state = State(phase="dispatch")
        adapter = _RaisingAdapter()

        with self.assertRaises(RuntimeError):
            run_with_activity(
                adapter,
                self.layout,
                state,
                label="01-a",
                kind="dispatch",
                prompt="do the thing",
                cwd=self.repo,
                tier="standard",
            )

        self.assertIsNone(state.current_activity)

    def test_routes_activity_log_path_to_the_adapter(self) -> None:
        state = State(phase="dispatch")
        adapter = _SnapshottingAdapter(state)

        run_with_activity(
            adapter,
            self.layout,
            state,
            label="01-a",
            kind="dispatch",
            prompt="do the thing",
            cwd=self.repo,
            tier="standard",
        )

        self.assertEqual(adapter.seen_activity_log, activity_log_path(self.layout, "01-a"))

    def test_writes_run_start_and_run_end_events(self) -> None:
        state = State(phase="dispatch")
        adapter = _SnapshottingAdapter(state)

        run_with_activity(
            adapter,
            self.layout,
            state,
            label="01-a",
            kind="dispatch",
            prompt="do the thing",
            cwd=self.repo,
            tier="standard",
        )

        lines = self.layout.events.read_text(encoding="utf-8").splitlines()
        kinds = [json.loads(line)["kind"] for line in lines]
        self.assertIn("run_start", kinds)
        self.assertIn("run_end", kinds)

    def test_state_json_reflects_the_heartbeat_persisted_and_cleared(self) -> None:
        state = State(phase="dispatch")
        adapter = _SnapshottingAdapter(state)

        run_with_activity(
            adapter,
            self.layout,
            state,
            label="01-a",
            kind="dispatch",
            prompt="do the thing",
            cwd=self.repo,
            tier="standard",
        )

        reloaded = load_state(self.layout)
        self.assertIsNone(reloaded.current_activity)

    def test_label_with_unsafe_chars_is_sanitized_into_a_valid_filename(self) -> None:
        path = activity_log_path(self.layout, "some/label:with*chars")
        self.assertEqual(path.name, "some_label_with_chars.log")
        self.assertTrue(path.parent.exists())


class TestCurrentActivityRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.layout = RunLayout(repo=Path(self._tmp.name), run_id="run-1")

    def test_current_activity_round_trips_through_to_dict_from_dict(self) -> None:
        state = State(
            phase="dispatch",
            current_activity={
                "label": "01-a",
                "kind": "dispatch",
                "model": "claude-sonnet-5",
                "started_at": "2026-07-14T00:00:00Z",
            },
        )
        save_state(self.layout, state)

        reloaded = load_state(self.layout)

        self.assertEqual(reloaded.current_activity, state.current_activity)

    def test_current_activity_defaults_to_none(self) -> None:
        state = State(phase="dispatch")
        save_state(self.layout, state)

        reloaded = load_state(self.layout)

        self.assertIsNone(reloaded.current_activity)


if __name__ == "__main__":
    unittest.main()
