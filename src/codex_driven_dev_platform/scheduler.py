from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
import itertools
import threading
import time
from typing import Any, Protocol
from uuid import UUID

from .leases import EntryManager
from .models import (
    EntryLeaseDecision,
    EntryLeasePlan,
    LeaseDecisionCode,
    LeaseStatus,
    TaskExecutionResult,
    TaskRequest,
    TaskStatus,
    WaitRegistration,
)


class TaskExecutor(Protocol):
    def run(
        self,
        prompt: str,
        plan: EntryLeasePlan,
        **options: Any,
    ) -> Any: ...


DecisionListener = Callable[[TaskRequest, EntryLeaseDecision], None]
ExecutionStartListener = Callable[[TaskRequest, EntryLeasePlan], None]
ExecutionListener = Callable[[TaskExecutionResult], None]
CancellationRequester = Callable[[UUID], None]


class TaskScheduler:
    """The sole component that applies EntryLeasePlans to EntryManager."""

    def __init__(
        self,
        entry_manager: EntryManager,
        task_executor: TaskExecutor,
        *,
        max_workers: int = 4,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.entry_manager = entry_manager
        self.task_executor = task_executor
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="task-executor",
        )
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._sequence = itertools.count()
        self._requests: dict[UUID, TaskRequest] = {}
        self._wait_queue: dict[UUID, WaitRegistration] = {}
        self._execution_options: dict[UUID, dict[str, Any]] = {}
        self._futures: dict[UUID, Future[TaskExecutionResult]] = {}
        self._results: dict[UUID, TaskExecutionResult] = {}
        self._decision_listeners: list[DecisionListener] = []
        self._execution_start_listeners: list[ExecutionStartListener] = []
        self._execution_listeners: list[ExecutionListener] = []
        self._cancellation_requester: CancellationRequester | None = None
        self._callback_errors: list[Exception] = []

    def set_decision_listener(self, listener: DecisionListener | None) -> None:
        with self._lock:
            self._decision_listeners = [] if listener is None else [listener]

    def add_decision_listener(self, listener: DecisionListener) -> None:
        with self._lock:
            self._decision_listeners.append(listener)

    def add_execution_start_listener(
        self, listener: ExecutionStartListener
    ) -> None:
        with self._lock:
            self._execution_start_listeners.append(listener)

    def set_execution_listener(self, listener: ExecutionListener | None) -> None:
        with self._lock:
            self._execution_listeners = [] if listener is None else [listener]

    def add_execution_listener(self, listener: ExecutionListener) -> None:
        with self._lock:
            self._execution_listeners.append(listener)

    def set_cancellation_requester(
        self, requester: CancellationRequester | None
    ) -> None:
        self._cancellation_requester = requester

    def register(self, request: TaskRequest) -> None:
        with self._lock:
            existing = self._requests.get(request.task_id)
            if existing is not None and existing is not request:
                raise ValueError(f"Task ID is already registered: {request.task_id}")
            self._requests[request.task_id] = request

    def submit(
        self,
        request: TaskRequest,
        plan: EntryLeasePlan,
        *,
        executor_options: Mapping[str, Any] | None = None,
    ) -> EntryLeaseDecision:
        self.register(request)
        if plan.task_id != request.task_id:
            raise ValueError("EntryLeasePlan belongs to a different task")

        with self._lock:
            request.add_plan(plan)
            if executor_options is not None:
                self._execution_options[plan.plan_id] = dict(executor_options)

            if request.selected_plan_id == plan.plan_id:
                decision = self._latest_decision(request, plan.plan_id)
                launch = False
            elif self._task_is_claimed(request):
                decision = self._supersede_locked(
                    plan, "Task has already selected another plan"
                )
                request.add_decision(decision)
                launch = False
            elif plan.plan_id in self._wait_queue:
                decision = self._latest_decision(request, plan.plan_id)
                launch = False
            else:
                decision, launch = self._apply_locked(request, plan)

        self._publish_decision(request, decision)
        if launch:
            self._launch(request, plan)
        return decision

    def queued_plans(self) -> tuple[EntryLeasePlan, ...]:
        with self._lock:
            registrations = sorted(
                self._wait_queue.values(), key=lambda item: item.sequence
            )
            return tuple(item.plan for item in registrations)

    def callback_errors(self) -> tuple[Exception, ...]:
        with self._lock:
            return tuple(self._callback_errors)

    def wait(self, task_id: UUID | str, timeout: float | None = None) -> TaskExecutionResult:
        identifier = UUID(str(task_id))
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while identifier not in self._results:
                if identifier not in self._requests:
                    raise KeyError(f"Unknown task: {identifier}")
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"Task did not finish: {identifier}")
                self._condition.wait(remaining)
            return self._results[identifier]

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)

    def _apply_locked(
        self, request: TaskRequest, plan: EntryLeasePlan
    ) -> tuple[EntryLeaseDecision, bool]:
        decision = self.entry_manager.acquire(plan)
        request.add_decision(decision)

        if decision.status is LeaseStatus.WAITING:
            if not decision.conflicts:
                raise RuntimeError("WAITING decision must identify conflicts")
            self._wait_queue[plan.plan_id] = WaitRegistration(
                plan,
                decision.blocker_executor_ids,
                next(self._sequence),
            )
            return decision, False

        self._wait_queue.pop(plan.plan_id, None)
        if decision.status is LeaseStatus.LEASED:
            self._claim_locked(request, plan)
            return decision, True
        return decision, False

    def _claim_locked(self, request: TaskRequest, plan: EntryLeasePlan) -> None:
        if request.selected_plan_id not in (None, plan.plan_id):
            raise RuntimeError("Task acquired more than one plan")
        request.selected_plan_id = plan.plan_id
        request.task_status = TaskStatus.RUNNING

        for plan_id, registration in tuple(self._wait_queue.items()):
            if registration.plan.task_id != request.task_id:
                continue
            del self._wait_queue[plan_id]
            if registration.plan.plan_id != plan.plan_id:
                registration.plan.apply_decision(
                    LeaseStatus.SUPERSEDED,
                    reasons=(f"Task selected plan {plan.plan_id}",),
                )
        self._condition.notify_all()

    def _launch(self, request: TaskRequest, plan: EntryLeasePlan) -> None:
        options = self._execution_options.get(plan.plan_id, {})
        try:
            future = self._pool.submit(
                self._execute_and_release,
                request,
                plan,
                options,
            )
        except Exception as error:
            self._record_launch_failure(request, plan, error)
            return
        with self._condition:
            self._futures[request.task_id] = future
            self._condition.notify_all()

        requester = self._cancellation_requester
        if requester is not None:
            self._safe_callback(requester, request.task_id)

    def _record_launch_failure(
        self,
        request: TaskRequest,
        plan: EntryLeasePlan,
        execution_error: Exception,
    ) -> None:
        release_error: BaseException | None = None
        try:
            self.entry_manager.release(plan.plan_id)
        except BaseException as error:
            release_error = error
        result = TaskExecutionResult(
            request,
            plan,
            execution_error=execution_error,
            release_error=release_error,
        )
        with self._condition:
            request.task_status = TaskStatus.FAILED
            self._results[request.task_id] = result
            self._condition.notify_all()
        self._publish_execution(result)
        if release_error is None:
            self._retry_waiting(plan.executor_id)

    def _execute_and_release(
        self,
        request: TaskRequest,
        plan: EntryLeasePlan,
        options: Mapping[str, Any],
    ) -> TaskExecutionResult:
        self._publish_execution_started(request, plan)
        result: Any | None = None
        execution_error: BaseException | None = None
        release_error: BaseException | None = None
        try:
            result = self.task_executor.run(
                request.prompt,
                plan,
                **options,
            )
        except BaseException as error:
            execution_error = error

        try:
            self.entry_manager.release(plan.plan_id)
        except BaseException as error:
            release_error = error

        execution_result = TaskExecutionResult(
            request,
            plan,
            result=result,
            execution_error=execution_error,
            release_error=release_error,
        )

        with self._condition:
            if execution_error is not None or release_error is not None:
                request.task_status = TaskStatus.FAILED
            elif getattr(result, "returncode", 0) != 0:
                request.task_status = TaskStatus.FAILED
            else:
                request.task_status = TaskStatus.COMPLETED
            self._results[request.task_id] = execution_result
            self._condition.notify_all()

        self._publish_execution(execution_result)

        if release_error is None:
            self._retry_waiting(plan.executor_id)
        return execution_result

    def _retry_waiting(self, released_executor_id: UUID) -> None:
        with self._lock:
            candidates = sorted(
                (
                    registration
                    for registration in self._wait_queue.values()
                    if released_executor_id
                    in registration.blocker_executor_ids
                ),
                key=lambda item: item.sequence,
            )

        for registration in candidates:
            plan = registration.plan
            with self._lock:
                current = self._wait_queue.get(plan.plan_id)
                request = self._requests.get(plan.task_id)
                if current is not registration or request is None:
                    continue
                if self._task_is_claimed(request):
                    self._wait_queue.pop(plan.plan_id, None)
                    decision = self._supersede_locked(
                        plan, "Task has already selected another plan"
                    )
                    request.add_decision(decision)
                    launch = False
                else:
                    decision, launch = self._apply_locked(request, plan)

            self._publish_decision(request, decision)
            if launch:
                self._launch(request, plan)

    @staticmethod
    def _task_is_claimed(request: TaskRequest) -> bool:
        return request.selected_plan_id is not None or request.task_status in {
            TaskStatus.RUNNING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }

    @staticmethod
    def _supersede_locked(
        plan: EntryLeasePlan, reason: str
    ) -> EntryLeaseDecision:
        plan.apply_decision(LeaseStatus.SUPERSEDED, reasons=(reason,))
        return EntryLeaseDecision(
            plan,
            LeaseStatus.SUPERSEDED,
            LeaseDecisionCode.TASK_ALREADY_CLAIMED,
            reasons=(reason,),
        )

    @staticmethod
    def _latest_decision(
        request: TaskRequest, plan_id: UUID
    ) -> EntryLeaseDecision:
        for decision in reversed(request.decisions):
            if decision.plan.plan_id == plan_id:
                return decision
        raise RuntimeError(f"No decision has been recorded for plan {plan_id}")

    def _publish_decision(
        self, request: TaskRequest, decision: EntryLeaseDecision
    ) -> None:
        with self._lock:
            listeners = tuple(self._decision_listeners)
        for listener in listeners:
            self._safe_callback(listener, request, decision)

    def _publish_execution_started(
        self, request: TaskRequest, plan: EntryLeasePlan
    ) -> None:
        with self._lock:
            listeners = tuple(self._execution_start_listeners)
        for listener in listeners:
            self._safe_callback(listener, request, plan)

    def _publish_execution(self, result: TaskExecutionResult) -> None:
        with self._lock:
            listeners = tuple(self._execution_listeners)
        for listener in listeners:
            self._safe_callback(listener, result)

    def _safe_callback(self, callback: Callable[..., Any], *args: Any) -> None:
        try:
            callback(*args)
        except Exception as error:
            with self._lock:
                self._callback_errors.append(error)
