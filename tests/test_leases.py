import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from codex_driven_dev_platform.leases import (
    EntryManager,
    LeaseRegistryCorruptionError,
    PlanStateError,
)
from codex_driven_dev_platform.models import (
    EntryLeasePlan,
    LeaseDecisionCode,
    LeaseStatus,
)


def _plan(readable, writable=(), *, task_id=None, executor_id=None):
    return EntryLeasePlan(
        task_id=task_id or uuid4(),
        executor_id=executor_id or uuid4(),
        readable_entries=tuple(readable),
        writable_entries=tuple(writable),
    )


def _parallel_acquire_worker(root: str, start, release, results) -> None:
    manager = EntryManager(root)
    plan = _plan(["src/**"])
    start.wait()
    decision = manager.acquire(plan)
    results.put(decision.status.value)
    if decision.accepted:
        release.wait(5)
        manager.release(plan.plan_id)


class EntryManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        self.manager = EntryManager(self.root)

    def test_acquire_updates_plan_and_registry_then_releases(self) -> None:
        plan = _plan(["src/**"], ["docs/**"])

        decision = self.manager.acquire(plan)

        self.assertTrue(decision.accepted)
        self.assertEqual(plan.lease_status, LeaseStatus.LEASED)
        record = self.manager.active_leases()[0]
        self.assertEqual(record.plan_id, plan.plan_id)
        self.assertEqual(record.task_id, plan.task_id)
        self.assertEqual(record.executor_id, plan.executor_id)
        self.assertTrue(self.manager.release(plan.plan_id))
        self.assertFalse(self.manager.release(plan.plan_id))
        self.assertEqual(plan.lease_status, LeaseStatus.RELEASED)
        self.assertEqual(self.manager.registry_path.read_bytes(), b"")

    def test_same_plan_acquisition_is_idempotent(self) -> None:
        plan = _plan(["src/**"])
        self.manager.acquire(plan)

        decision = self.manager.acquire(plan)

        self.assertEqual(decision.code, LeaseDecisionCode.ALREADY_LEASED)
        self.assertEqual(len(self.manager.active_leases()), 1)

    def test_conflict_marks_plan_waiting_with_blockers(self) -> None:
        active = _plan(["src/**"])
        waiting = _plan(["src/file.py"])
        self.manager.acquire(active)

        decision = self.manager.acquire(waiting)

        self.assertEqual(decision.status, LeaseStatus.WAITING)
        self.assertEqual(waiting.blocker_executor_ids, (active.executor_id,))
        self.assertEqual(len(decision.conflicts), 1)
        self.assertEqual(len(self.manager.active_leases()), 1)

    def test_waiting_plan_can_be_retried_after_release(self) -> None:
        active = _plan(["src/**"])
        waiting = _plan(["src/**"])
        self.manager.acquire(active)
        self.manager.acquire(waiting)
        self.manager.release(active.plan_id)

        decision = self.manager.acquire(waiting)

        self.assertEqual(decision.status, LeaseStatus.LEASED)
        self.assertEqual(waiting.lease_status, LeaseStatus.LEASED)

    def test_same_task_can_have_only_one_active_plan(self) -> None:
        task_id = uuid4()
        first = _plan(["src/**"], task_id=task_id)
        second = _plan(["docs/**"], task_id=task_id)
        self.manager.acquire(first)

        decision = self.manager.acquire(second)

        self.assertEqual(decision.status, LeaseStatus.SUPERSEDED)
        self.assertEqual(
            decision.code,
            LeaseDecisionCode.TASK_ALREADY_CLAIMED,
        )

    def test_executor_identifier_cannot_be_shared_by_active_plans(self) -> None:
        executor_id = uuid4()
        first = _plan(["src/**"], executor_id=executor_id)
        second = _plan(["docs/**"], executor_id=executor_id)
        self.manager.acquire(first)

        decision = self.manager.acquire(second)

        self.assertEqual(decision.status, LeaseStatus.INVALID)
        self.assertIn("already assigned", decision.reasons[0])

    def test_provably_disjoint_plans_can_be_leased(self) -> None:
        first = _plan(["src/**"])
        second = _plan(["docs/**"])

        self.assertTrue(self.manager.acquire(first).accepted)
        self.assertTrue(self.manager.acquire(second).accepted)
        self.assertEqual(len(self.manager.active_leases()), 2)

    def test_invalid_plan_is_not_written_or_queued_by_manager(self) -> None:
        plan = _plan(["../outside"])

        decision = self.manager.acquire(plan)

        self.assertEqual(decision.status, LeaseStatus.INVALID)
        self.assertFalse(self.manager.registry_path.exists())

    def test_rejects_acquisition_from_terminal_state(self) -> None:
        plan = _plan(["src/**"])
        plan.apply_decision(LeaseStatus.SUPERSEDED)

        with self.assertRaises(PlanStateError):
            self.manager.acquire(plan)

    def test_incomplete_tail_is_ignored_and_repaired_on_update(self) -> None:
        first = _plan(["src/**"])
        self.manager.acquire(first)
        with self.manager.registry_path.open("ab") as registry:
            registry.write(b'{"version":2,"operation":"release"')

        self.assertEqual(self.manager.active_leases()[0].plan_id, first.plan_id)
        second = _plan(["docs/**"])
        self.assertTrue(self.manager.acquire(second).accepted)
        self.assertEqual(len(self.manager.active_leases()), 2)

    def test_complete_corrupt_event_fails_closed(self) -> None:
        self.manager.registry_path.write_bytes(b"{}\n")

        with self.assertRaises(LeaseRegistryCorruptionError):
            self.manager.active_leases()

    def test_does_not_reclaim_stale_owner_pid(self) -> None:
        plan = _plan(["src/**"])
        event = {
            "version": 2,
            "operation": "acquire",
            "plan_id": str(plan.plan_id),
            "task_id": str(plan.task_id),
            "executor_id": str(plan.executor_id),
            "owner_pid": 999999999,
            "acquired_at": "2026-01-01T00:00:00+00:00",
            "readable_entries": ["src/**"],
            "writable_entries": [],
        }
        self.manager.registry_path.write_text(json.dumps(event) + "\n")

        active = self.manager.active_leases()

        self.assertEqual(active[0].owner_pid, 999999999)

    def test_parallel_overlapping_acquisition_has_one_winner(self) -> None:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        release = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_parallel_acquire_worker,
                args=(str(self.root), start, release, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        start.set()

        outcomes = sorted(results.get(timeout=5) for _ in processes)
        release.set()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)

        self.assertEqual(outcomes, ["leased", "waiting"])


if __name__ == "__main__":
    unittest.main()
