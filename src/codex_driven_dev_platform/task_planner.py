from __future__ import annotations

from collections.abc import Callable, Mapping
import threading
from typing import Any
from uuid import UUID

from .entry_lease_planner import EntryLeasePlanner
from .models import (
    EntryLeaseDecision,
    EntryLeasePlan,
    LeaseStatus,
    TaskRequest,
    TaskStatus,
)
from .scheduler import TaskScheduler


ReconsiderationPolicy = Callable[[TaskRequest, EntryLeaseDecision], bool]


class TaskPlanner:
    """Own task-level policy while Scheduler owns leasing and execution."""

    def __init__(
        self,
        entry_lease_planner: EntryLeasePlanner,
        scheduler: TaskScheduler,
        *,
        reconsideration_policy: ReconsiderationPolicy | None = None,
        max_generations: int | None = 8,
    ) -> None:
        self.entry_lease_planner = entry_lease_planner
        self.scheduler = scheduler
        self.reconsideration_policy = (
            reconsideration_policy or _default_reconsideration_policy
        )
        self.max_generations = max_generations
        self._lock = threading.RLock()
        self._requests: dict[UUID, TaskRequest] = {}
        entry_lease_planner.set_plan_listener(self.on_plan_created)
        scheduler.set_decision_listener(self.on_lease_decision)
        scheduler.set_cancellation_requester(entry_lease_planner.cancel)

    def submit(
        self,
        prompt: str,
        *,
        executor_options: Mapping[str, Any] | None = None,
    ) -> tuple[TaskRequest, EntryLeasePlan]:
        request = TaskRequest(prompt)
        with self._lock:
            self._requests[request.task_id] = request
        self.scheduler.register(request)
        plan = self.entry_lease_planner.create_and_submit(
            request,
            executor_options=executor_options,
        )
        return request, plan

    def get_request(self, task_id: UUID | str) -> TaskRequest:
        identifier = UUID(str(task_id))
        with self._lock:
            return self._requests[identifier]

    def on_plan_created(
        self,
        request: TaskRequest,
        plan: EntryLeasePlan,
    ) -> None:
        request.add_plan(plan)

    def on_lease_decision(
        self,
        request: TaskRequest,
        decision: EntryLeaseDecision,
    ) -> None:
        if decision.status not in {LeaseStatus.INVALID, LeaseStatus.WAITING}:
            return
        if request.task_status not in {TaskStatus.OPEN, TaskStatus.PLANNING}:
            return
        if (
            self.max_generations is not None
            and decision.plan.generation >= self.max_generations
        ):
            return
        if self.reconsideration_policy(request, decision):
            self.entry_lease_planner.reconsider(request, decision)


def _default_reconsideration_policy(
    request: TaskRequest,
    decision: EntryLeaseDecision,
) -> bool:
    del request
    return decision.status in {LeaseStatus.INVALID, LeaseStatus.WAITING}
