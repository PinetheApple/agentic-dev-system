"""Table-driven unit tests for ticket 003's DAG mechanics: segment-aware
`owns` overlap and 3-color-DFS acyclicity."""

from __future__ import annotations

import unittest
from typing import ClassVar

from ads.tasks import (
    CycleError,
    Task,
    TaskParseError,
    _owns_overlap,  # pyright: ignore[reportPrivateUsage]
    check_acyclic,
    ready_batch,
)


class OwnsOverlapTest(unittest.TestCase):
    CASES: ClassVar[list[tuple[str, list[str], list[str], bool]]] = [
        ("identical path", ["src/tui"], ["src/tui"], True),
        ("dir prefixes file", ["src/tui"], ["src/tui/app.py"], True),
        ("file prefixed by dir", ["src/tui/app.py"], ["src/tui"], True),
        ("sibling dir with shared string prefix", ["src/tui"], ["src/tui-old"], False),
        ("disjoint dirs", ["src/a"], ["src/b"], False),
        ("no overlap, multiple entries", ["src/a", "src/b"], ["src/c", "src/d"], False),
        ("overlap among many", ["src/a", "src/b"], ["src/b/inner.py"], True),
        ("empty vs anything", [], ["src/a"], False),
        ("empty vs empty", [], [], False),
    ]

    def test_cases(self) -> None:
        for name, a, b, expected in self.CASES:
            with self.subTest(name=name):
                self.assertEqual(_owns_overlap(a, b), expected)
                self.assertEqual(_owns_overlap(b, a), expected)


def _task(task_id: str, depends_on: list[str] | None = None, owns: list[str] | None = None) -> Task:
    return Task(id=task_id, depends_on=depends_on or [], owns=owns or [])


class CheckAcyclicTest(unittest.TestCase):
    def test_dag_passes(self) -> None:
        tasks = [_task("a"), _task("b", depends_on=["a"]), _task("c", depends_on=["a", "b"])]
        check_acyclic(tasks)  # no raise

    def test_direct_cycle_raises(self) -> None:
        tasks = [_task("a", depends_on=["b"]), _task("b", depends_on=["a"])]
        with self.assertRaises(CycleError):
            check_acyclic(tasks)

    def test_indirect_cycle_raises(self) -> None:
        tasks = [
            _task("a", depends_on=["c"]),
            _task("b", depends_on=["a"]),
            _task("c", depends_on=["b"]),
        ]
        with self.assertRaises(CycleError):
            check_acyclic(tasks)

    def test_unknown_dependency_raises(self) -> None:
        tasks = [_task("a", depends_on=["ghost"])]
        with self.assertRaises(TaskParseError):
            check_acyclic(tasks)

    def test_self_loop_raises(self) -> None:
        tasks = [_task("a", depends_on=["a"])]
        with self.assertRaises(CycleError):
            check_acyclic(tasks)


class ReadyBatchTest(unittest.TestCase):
    def test_orders_by_dependency(self) -> None:
        tasks = [_task("a", owns=["src/a"]), _task("b", depends_on=["a"], owns=["src/b"])]
        self.assertEqual([t.id for t in ready_batch(tasks)], ["a"])

    def test_batches_disjoint_owns_but_not_overlapping(self) -> None:
        tasks = [
            _task("a", owns=["src/tui"]),
            _task("b", owns=["src/tui/app.py"]),
            _task("c", owns=["src/other"]),
        ]
        batch_ids = [t.id for t in ready_batch(tasks)]
        self.assertIn("a", batch_ids)
        self.assertIn("c", batch_ids)
        self.assertNotIn("b", batch_ids)

    def test_done_deps_unblock_dependents(self) -> None:
        a = _task("a", owns=["src/a"])
        a.status = "done"
        b = _task("b", depends_on=["a"], owns=["src/b"])
        self.assertEqual([t.id for t in ready_batch([a, b])], ["b"])

    def test_unknown_dependency_raises(self) -> None:
        tasks = [_task("a", depends_on=["ghost"])]
        with self.assertRaises(TaskParseError):
            ready_batch(tasks)


if __name__ == "__main__":
    unittest.main()
