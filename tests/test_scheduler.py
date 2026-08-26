from pathlib import Path
import queue
import tempfile
import threading
import unittest
from uuid import uuid4

from codex_driven_dev_platform.leases import EntryManager
from codex_driven_dev_platform.models import (
    EntryLeasePlan,
    LeaseStatus,
    TaskRequest,
    TaskStatus,
)
from codex_driven_dev_platform.scheduler import TaskScheduler


class _ControlledExecutor:
    def __init__(self) -> None:
        self.started: queue.Queue[EntryLeasePlan] = queue.Queue()
        self._release: dict[object, threading.Event] = {}
        self._lock = threading.Lock()

    def run(self, prompt, plan, **options):
        del prompt, options
        release = threading.Event()
        with self._lock:
            self._release[plan.plan_id] = release
        self.started.put(plan)
        if not release.wait(5):
            raise TimeoutError("test executor was not released")
        return _Result(0)

    def allow(self, plan: EntryLeasePlan) -> None:
        with self._lock:
            release = self._release[plan.plan_id]
        release.set()


class _FailingExecutor:
    def run(self, prompt, plan, **options):
        del prompt, plan, options
        raise RuntimeError("executor failed to start")


class _Result:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def _plan(request, readable, writable=(), generation=1):
    return EntryLeasePlan(
        task_id=request.task_id,
        executor_id=uuid4(),
        generation=generation,
        readable_entries=tuple(readable),
        writable_entries=tuple(writable),
    )


class TaskSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        self.manager = EntryManager(self.root)
        self.executor = _ControlledExecutor()
        self.scheduler = TaskScheduler(self.manager, self.executor)
        self.addCleanup(self.scheduler.shutdown)
        self.decisions = []
        self.cancellations = []
        self.scheduler.set_decision_listener(
            lambda request, decision: self.decisions.append(
                (request.task_id, decision)
            )
        )
        self.scheduler.set_cancellation_requester(self.cancellations.append)

    def test_immediate_lease_starts_executor_and_scheduler_releases(self) -> None:
        request = TaskRequest("task")
        plan = _plan(request, ["src/**"])

        decision = self.scheduler.submit(request, plan)
        started = self.executor.started.get(timeout=2)

        self.assertTrue(decision.accepted)
        self.assertIs(started, plan)
        self.assertEqual(request.task_status, TaskStatus.RUNNING)
        self.assertEqual(request.selected_plan_id, plan.plan_id)
        self.assertEqual(self.cancellations, [request.task_id])

        self.executor.allow(plan)
        result = self.scheduler.wait(request.task_id, timeout=2)
        self.assertIsNone(result.execution_error)
        self.assertEqual(plan.lease_status, LeaseStatus.RELEASED)
        self.assertEqual(request.task_status, TaskStatus.COMPLETED)
        self.assertEqual(self.manager.active_leases(), ())

    def test_duplicate_selected_plan_is_idempotent(self) -> None:
        request = TaskRequest("task")
        plan = _plan(request, ["src/**"])
        first = self.scheduler.submit(request, plan)
        self.executor.started.get(timeout=2)

        second = self.scheduler.submit(request, plan)

        self.assertIs(second, first)
        self.assertEqual(plan.lease_status, LeaseStatus.LEASED)
        with self.assertRaises(queue.Empty):
            self.executor.started.get(timeout=0.1)
        self.executor.allow(plan)
        self.scheduler.wait(request.task_id, timeout=2)

    def test_listener_failure_does_not_prevent_executor_start(self) -> None:
        self.scheduler.set_decision_listener(
            lambda request, decision: (_ for _ in ()).throw(
                RuntimeError("listener failed")
            )
        )
        request = TaskRequest("task")
        plan = _plan(request, ["src/**"])

        self.scheduler.submit(request, plan)

        self.assertIs(self.executor.started.get(timeout=2), plan)
        self.assertIsInstance(self.scheduler.callback_errors()[0], RuntimeError)
        self.executor.allow(plan)
        self.scheduler.wait(request.task_id, timeout=2)

    def test_waiting_queue_contains_only_conflicting_plans(self) -> None:
        first_request = TaskRequest("first")
        first = _plan(first_request, ["src/**"])
        self.scheduler.submit(first_request, first)
        self.executor.started.get(timeout=2)

        waiting_request = TaskRequest("waiting")
        waiting = _plan(waiting_request, ["src/**"])
        decision = self.scheduler.submit(waiting_request, waiting)

        self.assertEqual(decision.status, LeaseStatus.WAITING)
        self.assertEqual(self.scheduler.queued_plans(), (waiting,))
        self.assertTrue(decision.conflicts)

        invalid_request = TaskRequest("invalid")
        invalid = _plan(invalid_request, ["../outside"])
        invalid_decision = self.scheduler.submit(invalid_request, invalid)
        self.assertEqual(invalid_decision.status, LeaseStatus.INVALID)
        self.assertEqual(self.scheduler.queued_plans(), (waiting,))

        self.executor.allow(first)
        self.scheduler.wait(first_request.task_id, timeout=2)
        started = self.executor.started.get(timeout=2)
        self.assertIs(started, waiting)
        self.executor.allow(waiting)
        self.scheduler.wait(waiting_request.task_id, timeout=2)

    def test_release_retries_only_affected_plan_and_recomputes_blockers(self) -> None:
        source_request = TaskRequest("source")
        source = _plan(source_request, ["src/**"])
        docs_request = TaskRequest("docs")
        docs = _plan(docs_request, ["docs/**"])
        self.scheduler.submit(source_request, source)
        self.scheduler.submit(docs_request, docs)
        self.executor.started.get(timeout=2)
        self.executor.started.get(timeout=2)

        waiting_request = TaskRequest("both")
        waiting = _plan(waiting_request, ["src/**", "docs/**"])
        decision = self.scheduler.submit(waiting_request, waiting)
        self.assertEqual(
            set(decision.blocker_executor_ids),
            {source.executor_id, docs.executor_id},
        )

        self.executor.allow(source)
        self.scheduler.wait(source_request.task_id, timeout=2)
        self.assertEqual(waiting.lease_status, LeaseStatus.WAITING)
        self.assertEqual(waiting.blocker_executor_ids, (docs.executor_id,))
        with self.assertRaises(queue.Empty):
            self.executor.started.get(timeout=0.1)

        self.executor.allow(docs)
        self.scheduler.wait(docs_request.task_id, timeout=2)
        self.assertIs(self.executor.started.get(timeout=2), waiting)
        self.executor.allow(waiting)
        self.scheduler.wait(waiting_request.task_id, timeout=2)

    def test_reconsidered_plan_can_win_and_supersede_waiting_plan(self) -> None:
        blocker_request = TaskRequest("blocker")
        blocker = _plan(blocker_request, ["src/**"])
        self.scheduler.submit(blocker_request, blocker)
        self.executor.started.get(timeout=2)

        request = TaskRequest("target")
        waiting = _plan(request, ["src/**"], generation=1)
        alternative = _plan(request, ["docs/**"], generation=2)
        self.scheduler.submit(request, waiting)
        decision = self.scheduler.submit(request, alternative)

        self.assertEqual(decision.status, LeaseStatus.LEASED)
        self.assertEqual(waiting.lease_status, LeaseStatus.SUPERSEDED)
        self.assertEqual(self.scheduler.queued_plans(), ())
        self.assertIs(self.executor.started.get(timeout=2), alternative)

        self.executor.allow(blocker)
        self.scheduler.wait(blocker_request.task_id, timeout=2)
        with self.assertRaises(queue.Empty):
            self.executor.started.get(timeout=0.1)

        self.executor.allow(alternative)
        self.scheduler.wait(request.task_id, timeout=2)

    def test_execution_failure_still_releases_and_retries_waiter(self) -> None:
        scheduler = TaskScheduler(self.manager, _FailingExecutor())
        self.addCleanup(scheduler.shutdown)
        request = TaskRequest("failing")
        plan = _plan(request, ["src/**"])

        scheduler.submit(request, plan)
        result = scheduler.wait(request.task_id, timeout=2)

        self.assertIsInstance(result.execution_error, RuntimeError)
        self.assertIsNone(result.release_error)
        self.assertEqual(plan.lease_status, LeaseStatus.RELEASED)
        self.assertEqual(self.manager.active_leases(), ())

    def test_scheduler_pool_failure_releases_preacquired_plan(self) -> None:
        scheduler = TaskScheduler(self.manager, self.executor)
        scheduler.shutdown()
        request = TaskRequest("cannot launch")
        plan = _plan(request, ["src/**"])

        decision = scheduler.submit(request, plan)
        result = scheduler.wait(request.task_id, timeout=2)

        self.assertTrue(decision.accepted)
        self.assertIsInstance(result.execution_error, RuntimeError)
        self.assertEqual(plan.lease_status, LeaseStatus.RELEASED)
        self.assertEqual(self.manager.active_leases(), ())


if __name__ == "__main__":
    unittest.main()
