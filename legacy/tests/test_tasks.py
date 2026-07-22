import unittest

from ads.tasks import (
    CycleError,
    ExitCriterion,
    Task,
    check_acyclic,
    parse_task,
    ready_batch,
    serialize_task,
)


class TestFrontmatterRoundTrip(unittest.TestCase):
    def test_round_trip_stable(self) -> None:
        task = Task(
            id="01-implement",
            status="pending",
            depends_on=["00-setup"],
            owns=["ads/foo.py", "ads/bar.py"],
            exit_criteria=[
                ExitCriterion(check="cmd", value="pytest tests/test_foo.py"),
                ExitCriterion(check="judgment", value="follows SOLID"),
            ],
            expert="python-expert",
            critical=True,
            tier="standard",
            parent=None,
            body="Implement the foo module.\n\nHonor the Bar interface.",
        )
        text1 = serialize_task(task)
        parsed1 = parse_task(text1)
        text2 = serialize_task(parsed1)
        parsed2 = parse_task(text2)

        self.assertEqual(text1, text2)
        self.assertEqual(parsed1, parsed2)
        self.assertEqual(parsed1.id, "01-implement")
        self.assertEqual(parsed1.depends_on, ["00-setup"])
        self.assertEqual(parsed1.owns, ["ads/foo.py", "ads/bar.py"])
        self.assertEqual(len(parsed1.exit_criteria), 2)
        self.assertEqual(parsed1.exit_criteria[0].check, "cmd")
        self.assertTrue(parsed1.critical)

    def test_null_parent_and_empty_lists(self) -> None:
        task = Task(id="00-setup", status="done", parent=None)
        parsed = parse_task(serialize_task(task))
        self.assertIsNone(parsed.parent)
        self.assertEqual(parsed.depends_on, [])
        self.assertEqual(parsed.owns, [])
        self.assertEqual(parsed.exit_criteria, [])

    def test_body_preserved(self) -> None:
        text = (
            "---\n"
            "id: x\n"
            "status: pending\n"
            "depends_on: []\n"
            "owns: []\n"
            "exit_criteria: []\n"
            "expert: python-expert\n"
            "critical: false\n"
            "tier: fast\n"
            "parent: null\n"
            "---\n"
            "# Objective\n\nDo the thing.\n"
        )
        task = parse_task(text)
        self.assertIn("Do the thing.", task.body)


class TestAcyclicity(unittest.TestCase):
    def _task(self, task_id: str, depends_on: list[str]) -> Task:
        return Task(id=task_id, status="pending", depends_on=depends_on)

    def test_accepts_dag(self) -> None:
        tasks = [
            self._task("a", []),
            self._task("b", ["a"]),
            self._task("c", ["a", "b"]),
        ]
        check_acyclic(tasks)  # must not raise

    def test_rejects_cycle(self) -> None:
        tasks = [
            self._task("a", ["c"]),
            self._task("b", ["a"]),
            self._task("c", ["b"]),
        ]
        with self.assertRaises(CycleError):
            check_acyclic(tasks)

    def test_rejects_self_cycle(self) -> None:
        tasks = [self._task("a", ["a"])]
        with self.assertRaises(CycleError):
            check_acyclic(tasks)


class TestReadyBatch(unittest.TestCase):
    def test_disjoint_owns_batched_together(self) -> None:
        tasks = [
            Task(id="a", status="pending", owns=["x.py"]),
            Task(id="b", status="pending", owns=["y.py"]),
        ]
        batch = ready_batch(tasks)
        self.assertEqual({t.id for t in batch}, {"a", "b"})

    def test_overlapping_owns_only_one_batched(self) -> None:
        tasks = [
            Task(id="a", status="pending", owns=["shared.py"]),
            Task(id="b", status="pending", owns=["shared.py"]),
        ]
        batch = ready_batch(tasks)
        self.assertEqual(len(batch), 1)

    def test_deps_gate_readiness(self) -> None:
        tasks = [
            Task(id="a", status="pending", owns=["a.py"]),
            Task(id="b", status="pending", owns=["b.py"], depends_on=["a"]),
        ]
        batch = ready_batch(tasks)
        self.assertEqual({t.id for t in batch}, {"a"})

    def test_done_and_active_excluded(self) -> None:
        tasks = [
            Task(id="a", status="done", owns=["a.py"]),
            Task(id="b", status="pending", owns=["b.py"], depends_on=["a"]),
            Task(id="c", status="active", owns=["c.py"]),
        ]
        batch = ready_batch(tasks)
        self.assertEqual({t.id for t in batch}, {"b"})


if __name__ == "__main__":
    unittest.main()
