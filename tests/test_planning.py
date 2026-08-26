from concurrent.futures import Future
import json
from pathlib import Path
import queue
import stat
import tempfile
import threading
import time
import unittest

from codex_driven_dev_platform.entry_lease_planner import (
    CodexEntryLeasePlanningAgent,
    EntryLeasePlanner,
    EntryLeasePlanningCancelled,
    EntryLeaseProposal,
)
from codex_driven_dev_platform.leases import EntryManager
from codex_driven_dev_platform.models import (
    EntryLeaseDecision,
    EntryLeasePlan,
    LeaseDecisionCode,
    LeaseStatus,
    TaskRequest,
)
from codex_driven_dev_platform.scheduler import TaskScheduler
from codex_driven_dev_platform.task_planner import TaskPlanner


class _RecordingScheduler:
    def __init__(self) -> None:
        self.submissions = []

    def submit(self, request, plan, *, executor_options=None):
        self.submissions.append((request, plan, executor_options))
        return None


class _SequenceAgent:
    def __init__(self, proposals) -> None:
        self.proposals = list(proposals)
        self.calls = []
        self.cancelled = []

    def create_proposal(self, request, *, generation, feedback):
        self.calls.append((request, generation, feedback))
        return self.proposals.pop(0)

    def cancel(self, task_id):
        self.cancelled.append(task_id)


class _BlockingAgent:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def create_proposal(self, request, *, generation, feedback):
        del request, generation, feedback
        self.started.set()
        self.cancelled.wait(2)
        raise EntryLeasePlanningCancelled("cancelled")

    def cancel(self, task_id):
        del task_id
        self.cancelled.set()


class _NoopExecutor:
    def run(self, prompt, plan, **options):
        del prompt, plan, options
        return _Result(0)


class _Result:
    returncode = 0


class _ControlledExecutor:
    def __init__(self) -> None:
        self.started = queue.Queue()
        self._release = {}
        self._lock = threading.Lock()

    def run(self, prompt, plan, **options):
        del prompt, options
        release = threading.Event()
        with self._lock:
            self._release[plan.plan_id] = release
        self.started.put(plan)
        if not release.wait(3):
            raise TimeoutError("test executor was not released")
        return _Result()

    def allow(self, plan):
        with self._lock:
            release = self._release[plan.plan_id]
        release.set()


class _InitialThenBlockingAgent:
    def __init__(self) -> None:
        self.calls = 0
        self.reconsidering = threading.Event()
        self.cancelled = threading.Event()

    def create_proposal(self, request, *, generation, feedback):
        del request, generation
        self.calls += 1
        if feedback is None:
            return EntryLeaseProposal(("src/**",), (), "initial")
        self.reconsidering.set()
        self.cancelled.wait(3)
        raise EntryLeasePlanningCancelled("cancelled")

    def cancel(self, task_id):
        del task_id
        self.cancelled.set()


_FAKE_PLANNER = """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

arguments = sys.argv[1:]
output = Path(arguments[arguments.index("--output-last-message") + 1])
prompt = sys.stdin.read()
output.write_text(json.dumps({
    "readable_entries": ["src/**"],
    "writable_entries": ["tests/**"],
    "rationale": "fake plan"
}))
print(prompt)
"""


class EntryLeasePlannerTests(unittest.TestCase):
    def test_returns_and_submits_the_same_plan_object(self) -> None:
        scheduler = _RecordingScheduler()
        agent = _SequenceAgent(
            [EntryLeaseProposal(("src/**",), ("tests/**",), "reason")]
        )
        planner = EntryLeasePlanner(agent, scheduler)
        self.addCleanup(planner.shutdown)
        request = TaskRequest("task")

        returned = planner.create_and_submit(
            request,
            executor_options={"timeout": 30},
        )

        submitted_request, submitted_plan, options = scheduler.submissions[0]
        self.assertIs(submitted_request, request)
        self.assertIs(submitted_plan, returned)
        self.assertIs(request.plans[returned.plan_id], returned)
        self.assertEqual(options, {"timeout": 30})

    def test_reconsideration_passes_structured_reason_and_new_generation(self) -> None:
        scheduler = _RecordingScheduler()
        agent = _SequenceAgent(
            [EntryLeaseProposal(("docs/**",), (), "alternative")]
        )
        planner = EntryLeasePlanner(agent, scheduler)
        self.addCleanup(planner.shutdown)
        request = TaskRequest("task")
        previous = EntryLeasePlan(
            task_id=request.task_id,
            executor_id=request.task_id,
            generation=1,
            readable_entries=("src/**",),
            writable_entries=(),
        )
        previous.apply_decision(
            LeaseStatus.WAITING,
            reasons=("conflict",),
        )
        feedback = EntryLeaseDecision(
            previous,
            LeaseStatus.WAITING,
            LeaseDecisionCode.CONFLICT,
            reasons=("conflict",),
        )

        future = planner.reconsider(request, feedback)
        plan = future.result(timeout=2)

        self.assertEqual(plan.generation, 1)
        self.assertIs(agent.calls[0][2], feedback)
        self.assertIs(scheduler.submissions[0][1], plan)

    def test_cancel_stops_active_agent(self) -> None:
        scheduler = _RecordingScheduler()
        agent = _BlockingAgent()
        planner = EntryLeasePlanner(agent, scheduler)
        self.addCleanup(planner.shutdown)
        request = TaskRequest("task")
        plan = EntryLeasePlan(
            task_id=request.task_id,
            executor_id=request.task_id,
            readable_entries=("src/**",),
            writable_entries=(),
        )
        feedback = EntryLeaseDecision(
            plan,
            LeaseStatus.WAITING,
            LeaseDecisionCode.CONFLICT,
        )

        future = planner.reconsider(request, feedback)
        self.assertTrue(agent.started.wait(1))
        planner.cancel(request.task_id)

        with self.assertRaises(EntryLeasePlanningCancelled):
            future.result(timeout=2)

    def test_codex_agent_uses_schema_and_plan_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "src").mkdir()
            fake = root / "fake-planner"
            fake.write_text(_FAKE_PLANNER)
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            agent = CodexEntryLeasePlanningAgent(
                root,
                codex_command=(str(fake),),
            )

            proposal = agent.create_proposal(
                TaskRequest("inspect task"),
                generation=1,
                feedback=None,
            )

            self.assertEqual(proposal.readable_entries, ("src/**",))
            self.assertEqual(proposal.writable_entries, ("tests/**",))

    def test_codex_output_schema_uses_supported_array_keywords(self) -> None:
        schema_path = (
            Path(__file__).parents[1]
            / "src"
            / "codex_driven_dev_platform"
            / "entry_lease_plan.schema.json"
        )
        schema = json.loads(schema_path.read_text())

        for property_name in ("readable_entries", "writable_entries"):
            array_schema = schema["properties"][property_name]
            self.assertEqual(
                array_schema,
                {"type": "array", "items": {"type": "string"}},
            )


class TaskPlannerIntegrationTests(unittest.TestCase):
    def test_conflict_feedback_replans_and_alternative_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "src").mkdir()
            (root / "docs").mkdir()
            manager = EntryManager(root)
            scheduler = TaskScheduler(manager, _NoopExecutor())
            self.addCleanup(scheduler.shutdown)

            blocker_request = TaskRequest("blocker")
            blocker = EntryLeasePlan(
                task_id=blocker_request.task_id,
                executor_id=blocker_request.task_id,
                readable_entries=("src/**",),
                writable_entries=(),
            )
            blocker_decision = manager.acquire(blocker)
            self.assertTrue(blocker_decision.accepted)

            agent = _SequenceAgent(
                [
                    EntryLeaseProposal(("src/**",), (), "initial"),
                    EntryLeaseProposal(("docs/**",), (), "alternative"),
                ]
            )
            lease_planner = EntryLeasePlanner(agent, scheduler)
            self.addCleanup(lease_planner.shutdown)
            planner = TaskPlanner(lease_planner, scheduler)

            request, initial = planner.submit("target")
            result = scheduler.wait(request.task_id, timeout=2)

            self.assertEqual(initial.lease_status, LeaseStatus.SUPERSEDED)
            self.assertEqual(result.plan.generation, 2)
            self.assertEqual(result.plan.lease_status, LeaseStatus.RELEASED)
            self.assertIsNotNone(request.selected_plan_id)
            self.assertEqual(agent.calls[1][2].status, LeaseStatus.WAITING)
            manager.release(blocker.plan_id)

    def test_invalid_feedback_replans_and_valid_plan_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "docs").mkdir()
            manager = EntryManager(root)
            scheduler = TaskScheduler(manager, _NoopExecutor())
            self.addCleanup(scheduler.shutdown)
            agent = _SequenceAgent(
                [
                    EntryLeaseProposal(("../outside",), (), "invalid"),
                    EntryLeaseProposal(("docs/**",), (), "corrected"),
                ]
            )
            lease_planner = EntryLeasePlanner(agent, scheduler)
            self.addCleanup(lease_planner.shutdown)
            planner = TaskPlanner(lease_planner, scheduler)

            request, initial = planner.submit("target")
            result = scheduler.wait(request.task_id, timeout=2)

            self.assertEqual(initial.lease_status, LeaseStatus.INVALID)
            self.assertEqual(result.plan.generation, 2)
            self.assertEqual(result.plan.lease_status, LeaseStatus.RELEASED)
            feedback = agent.calls[1][2]
            self.assertEqual(feedback.status, LeaseStatus.INVALID)
            self.assertEqual(feedback.code, LeaseDecisionCode.INVALID)
            self.assertTrue(feedback.reasons)

    def test_task_planner_observes_plan_before_scheduler_decision(self) -> None:
        events = []

        class ObservingScheduler(_RecordingScheduler):
            def register(self, request):
                del request

            def set_decision_listener(self, listener):
                del listener

            def set_cancellation_requester(self, requester):
                del requester

            def submit(self, request, plan, *, executor_options=None):
                events.append(("scheduler", plan.plan_id in request.plans))
                return super().submit(
                    request,
                    plan,
                    executor_options=executor_options,
                )

        scheduler = ObservingScheduler()
        agent = _SequenceAgent([EntryLeaseProposal(("src/**",), (), "plan")])
        lease_planner = EntryLeasePlanner(agent, scheduler)
        self.addCleanup(lease_planner.shutdown)
        planner = TaskPlanner(lease_planner, scheduler)

        request, plan = planner.submit("task")

        self.assertIs(request.plans[plan.plan_id], plan)
        self.assertEqual(events, [("scheduler", True)])

    def test_each_conflicting_generation_triggers_next_reconsideration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "src").mkdir()
            manager = EntryManager(root)
            blocker_request = TaskRequest("blocker")
            blocker = EntryLeasePlan(
                task_id=blocker_request.task_id,
                executor_id=blocker_request.task_id,
                readable_entries=("src/**",),
                writable_entries=(),
            )
            manager.acquire(blocker)
            scheduler = TaskScheduler(manager, _NoopExecutor())
            self.addCleanup(scheduler.shutdown)
            agent = _SequenceAgent(
                [
                    EntryLeaseProposal(("src/**",), (), "one"),
                    EntryLeaseProposal(("src/**",), (), "two"),
                    EntryLeaseProposal(("src/**",), (), "three"),
                ]
            )
            lease_planner = EntryLeasePlanner(agent, scheduler)
            self.addCleanup(lease_planner.shutdown)
            planner = TaskPlanner(
                lease_planner,
                scheduler,
                max_generations=3,
            )

            request, _ = planner.submit("target")
            deadline = time.monotonic() + 2
            while len(agent.calls) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertEqual([call[1] for call in agent.calls], [1, 2, 3])
            self.assertEqual(len(scheduler.queued_plans()), 3)
            self.assertTrue(
                all(
                    plan.lease_status is LeaseStatus.WAITING
                    for plan in request.plans.values()
                )
            )
            manager.release(blocker.plan_id)

    def test_waiting_plan_wins_then_stops_active_reconsideration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "src").mkdir()
            manager = EntryManager(root)
            executor = _ControlledExecutor()
            scheduler = TaskScheduler(manager, executor)
            self.addCleanup(scheduler.shutdown)

            blocker_request = TaskRequest("blocker")
            blocker = EntryLeasePlan(
                task_id=blocker_request.task_id,
                executor_id=blocker_request.task_id,
                readable_entries=("src/**",),
                writable_entries=(),
            )
            scheduler.submit(blocker_request, blocker)
            self.assertIs(executor.started.get(timeout=2), blocker)

            agent = _InitialThenBlockingAgent()
            lease_planner = EntryLeasePlanner(agent, scheduler)
            self.addCleanup(lease_planner.shutdown)
            planner = TaskPlanner(lease_planner, scheduler)
            request, waiting = planner.submit("target")
            self.assertEqual(waiting.lease_status, LeaseStatus.WAITING)
            self.assertTrue(agent.reconsidering.wait(1))

            executor.allow(blocker)
            scheduler.wait(blocker_request.task_id, timeout=2)
            self.assertIs(executor.started.get(timeout=2), waiting)
            self.assertTrue(agent.cancelled.wait(1))
            self.assertEqual(waiting.lease_status, LeaseStatus.LEASED)

            executor.allow(waiting)
            result = scheduler.wait(request.task_id, timeout=2)
            self.assertIsNone(result.execution_error)
            self.assertEqual(waiting.lease_status, LeaseStatus.RELEASED)


if __name__ == "__main__":
    unittest.main()
