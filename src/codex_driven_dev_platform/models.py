from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class LeaseStatus(StrEnum):
    PROPOSED = "proposed"
    INVALID = "invalid"
    WAITING = "waiting"
    LEASED = "leased"
    RELEASED = "released"
    SUPERSEDED = "superseded"


class LeaseDecisionCode(StrEnum):
    ACQUIRED = "acquired"
    CONFLICT = "conflict"
    INVALID = "invalid"
    ALREADY_LEASED = "already_leased"
    TASK_ALREADY_CLAIMED = "task_already_claimed"


class TaskStatus(StrEnum):
    PLANNING = "planning"
    OPEN = "open"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class EntryConflict:
    plan_id: UUID
    task_id: UUID
    executor_id: UUID
    requested_pattern: str
    active_pattern: str


@dataclass(slots=True)
class EntryLeasePlan:
    task_id: UUID
    executor_id: UUID
    readable_entries: tuple[str, ...]
    writable_entries: tuple[str, ...]
    generation: int = 1
    rationale: str = ""
    plan_id: UUID = field(default_factory=uuid4)
    lease_status: LeaseStatus = LeaseStatus.PROPOSED
    revision: int = 0
    reasons: tuple[str, ...] = ()
    blocker_executor_ids: tuple[UUID, ...] = ()
    created_at: str = field(default_factory=lambda: _utc_now())

    def __post_init__(self) -> None:
        self.task_id = UUID(str(self.task_id))
        self.executor_id = UUID(str(self.executor_id))
        self.plan_id = UUID(str(self.plan_id))
        self.readable_entries = tuple(map(str, self.readable_entries))
        self.writable_entries = tuple(map(str, self.writable_entries))
        if self.generation < 1:
            raise ValueError("generation must be positive")

    @property
    def entries(self) -> tuple[str, ...]:
        return self.readable_entries + self.writable_entries

    def apply_decision(
        self,
        status: LeaseStatus,
        *,
        reasons: tuple[str, ...] = (),
        blocker_executor_ids: tuple[UUID, ...] = (),
    ) -> None:
        allowed = _LEASE_TRANSITIONS[self.lease_status]
        if status not in allowed:
            raise ValueError(
                f"Invalid lease transition: {self.lease_status} -> {status}"
            )
        self.lease_status = status
        self.reasons = reasons
        self.blocker_executor_ids = blocker_executor_ids
        self.revision += 1


@dataclass(frozen=True, slots=True)
class EntryLeaseDecision:
    plan: EntryLeasePlan
    status: LeaseStatus
    code: LeaseDecisionCode
    reasons: tuple[str, ...] = ()
    conflicts: tuple[EntryConflict, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status is LeaseStatus.LEASED

    @property
    def blocker_executor_ids(self) -> tuple[UUID, ...]:
        return tuple(dict.fromkeys(conflict.executor_id for conflict in self.conflicts))


@dataclass(slots=True)
class TaskRequest:
    prompt: str
    task_id: UUID = field(default_factory=uuid4)
    task_status: TaskStatus = TaskStatus.PLANNING
    selected_plan_id: UUID | None = None
    plans: dict[UUID, EntryLeasePlan] = field(default_factory=dict)
    decisions: list[EntryLeaseDecision] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: _utc_now())

    def __post_init__(self) -> None:
        self.task_id = UUID(str(self.task_id))
        if not isinstance(self.prompt, str):
            raise TypeError("prompt must be a string")

    def add_plan(self, plan: EntryLeasePlan) -> None:
        if plan.task_id != self.task_id:
            raise ValueError("EntryLeasePlan belongs to a different task")
        self.plans[plan.plan_id] = plan
        if self.task_status is TaskStatus.PLANNING:
            self.task_status = TaskStatus.OPEN

    def add_decision(self, decision: EntryLeaseDecision) -> None:
        self.add_plan(decision.plan)
        self.decisions.append(decision)


@dataclass(slots=True)
class WaitRegistration:
    plan: EntryLeasePlan
    blocker_executor_ids: tuple[UUID, ...]
    sequence: int
    enqueued_at: str = field(default_factory=lambda: _utc_now())


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    request: TaskRequest
    plan: EntryLeasePlan
    result: Any | None = None
    execution_error: BaseException | None = None
    release_error: BaseException | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_LEASE_TRANSITIONS: dict[LeaseStatus, frozenset[LeaseStatus]] = {
    LeaseStatus.PROPOSED: frozenset(
        {
            LeaseStatus.PROPOSED,
            LeaseStatus.INVALID,
            LeaseStatus.WAITING,
            LeaseStatus.LEASED,
            LeaseStatus.SUPERSEDED,
        }
    ),
    LeaseStatus.WAITING: frozenset(
        {
            LeaseStatus.WAITING,
            LeaseStatus.INVALID,
            LeaseStatus.LEASED,
            LeaseStatus.SUPERSEDED,
        }
    ),
    LeaseStatus.LEASED: frozenset(
        {LeaseStatus.LEASED, LeaseStatus.RELEASED}
    ),
    LeaseStatus.INVALID: frozenset(
        {LeaseStatus.INVALID, LeaseStatus.SUPERSEDED}
    ),
    LeaseStatus.SUPERSEDED: frozenset({LeaseStatus.SUPERSEDED}),
    LeaseStatus.RELEASED: frozenset({LeaseStatus.RELEASED}),
}
