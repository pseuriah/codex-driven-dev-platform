from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
from typing import Any
from uuid import UUID

from .models import (
    EntryConflict,
    EntryLeaseDecision,
    EntryLeasePlan,
    LeaseDecisionCode,
    LeaseStatus,
)
from .taskspace import DEFAULT_LEASE_REGISTRY, Entry, TaskspaceGenerator


_REGISTRY_VERSION = 2


class LeaseError(RuntimeError):
    """Base class for EntryManager errors."""


class LeaseRegistryCorruptionError(LeaseError):
    """Raised when the durable lease log cannot be replayed safely."""


class PlanStateError(LeaseError):
    """Raised when an operation is incompatible with the plan state."""


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    plan_id: UUID
    task_id: UUID
    executor_id: UUID
    owner_pid: int
    acquired_at: str
    readable_entries: tuple[str, ...]
    writable_entries: tuple[str, ...]

    @property
    def entries(self) -> tuple[str, ...]:
        return self.readable_entries + self.writable_entries


class EntryManager:
    """Mechanically validates, acquires, records, and releases entry leases.

    The manager has no planning or scheduling policy. ``acquire`` atomically
    decides and persists a plan lease while holding the registry flock.
    """

    def __init__(
        self,
        project_root: Entry | None = None,
        *,
        registry_path: Entry = DEFAULT_LEASE_REGISTRY,
    ) -> None:
        self.generator = TaskspaceGenerator(
            project_root, reserved_entries=(registry_path,)
        )
        relative_registry = self.generator._normalize_entry(Path(registry_path))
        self.project_root = self.generator.project_root
        self.registry_relative_path = relative_registry
        self.registry_path = self.project_root / relative_registry
        self._known_plans: dict[UUID, EntryLeasePlan] = {}

    def acquire(self, plan: EntryLeasePlan) -> EntryLeaseDecision:
        if plan.lease_status in {
            LeaseStatus.INVALID,
            LeaseStatus.RELEASED,
            LeaseStatus.SUPERSEDED,
        }:
            raise PlanStateError(
                f"Cannot acquire plan {plan.plan_id} in state {plan.lease_status}"
            )

        try:
            prepared = self.generator.prepare_entries(
                plan.readable_entries, plan.writable_entries
            )
        except ValueError as error:
            reason = str(error)
            plan.apply_decision(LeaseStatus.INVALID, reasons=(reason,))
            return EntryLeaseDecision(
                plan,
                LeaseStatus.INVALID,
                LeaseDecisionCode.INVALID,
                reasons=(reason,),
            )

        readable = tuple(
            entry.pattern for entry in prepared if entry.access == "read"
        )
        writable = tuple(
            entry.pattern for entry in prepared if entry.access == "write"
        )
        plan.readable_entries = readable
        plan.writable_entries = writable

        with self._locked_registry(exclusive=True) as registry:
            active = self._read_active_records(
                registry, truncate_incomplete=True
            )

            existing_plan = active.get(plan.plan_id)
            if existing_plan is not None:
                self._ensure_idempotent_match(plan, existing_plan)
                plan.apply_decision(LeaseStatus.LEASED)
                self._known_plans[plan.plan_id] = plan
                return EntryLeaseDecision(
                    plan,
                    LeaseStatus.LEASED,
                    LeaseDecisionCode.ALREADY_LEASED,
                )

            claimed_task = next(
                (
                    record
                    for record in active.values()
                    if record.task_id == plan.task_id
                ),
                None,
            )
            if claimed_task is not None:
                reason = (
                    f"Task {plan.task_id} is already leased by plan "
                    f"{claimed_task.plan_id}"
                )
                plan.apply_decision(
                    LeaseStatus.SUPERSEDED, reasons=(reason,)
                )
                return EntryLeaseDecision(
                    plan,
                    LeaseStatus.SUPERSEDED,
                    LeaseDecisionCode.TASK_ALREADY_CLAIMED,
                    reasons=(reason,),
                )

            claimed_executor = next(
                (
                    record
                    for record in active.values()
                    if record.executor_id == plan.executor_id
                ),
                None,
            )
            if claimed_executor is not None:
                reason = (
                    f"Executor {plan.executor_id} is already assigned to plan "
                    f"{claimed_executor.plan_id}"
                )
                plan.apply_decision(LeaseStatus.INVALID, reasons=(reason,))
                return EntryLeaseDecision(
                    plan,
                    LeaseStatus.INVALID,
                    LeaseDecisionCode.INVALID,
                    reasons=(reason,),
                )

            requested_record = _record_from_plan(plan)
            conflicts = self._find_conflicts(
                requested_record, active.values()
            )
            if conflicts:
                blockers = tuple(
                    dict.fromkeys(conflict.executor_id for conflict in conflicts)
                )
                reasons = tuple(
                    f"{conflict.requested_pattern!r} conflicts with "
                    f"{conflict.active_pattern!r} held by "
                    f"executor {conflict.executor_id}"
                    for conflict in conflicts
                )
                plan.apply_decision(
                    LeaseStatus.WAITING,
                    reasons=reasons,
                    blocker_executor_ids=blockers,
                )
                return EntryLeaseDecision(
                    plan,
                    LeaseStatus.WAITING,
                    LeaseDecisionCode.CONFLICT,
                    reasons=reasons,
                    conflicts=tuple(conflicts),
                )

            self._append_event(registry, _acquire_event(requested_record))

        plan.apply_decision(LeaseStatus.LEASED)
        self._known_plans[plan.plan_id] = plan
        return EntryLeaseDecision(
            plan,
            LeaseStatus.LEASED,
            LeaseDecisionCode.ACQUIRED,
        )

    def release(self, plan_id: UUID | str) -> bool:
        identifier = UUID(str(plan_id))
        with self._locked_registry(exclusive=True) as registry:
            active = self._read_active_records(
                registry, truncate_incomplete=True
            )
            record = active.get(identifier)
            if record is None:
                known = self._known_plans.get(identifier)
                if known is not None and known.lease_status is LeaseStatus.LEASED:
                    known.apply_decision(LeaseStatus.RELEASED)
                return False

            self._append_event(
                registry,
                {
                    "version": _REGISTRY_VERSION,
                    "operation": "release",
                    "plan_id": str(identifier),
                    "task_id": str(record.task_id),
                    "executor_id": str(record.executor_id),
                    "released_at": _utc_now(),
                },
            )

            if len(active) == 1:
                registry.seek(0)
                registry.truncate(0)
                registry.flush()
                os.fsync(registry.fileno())

        known = self._known_plans.get(identifier)
        if known is not None:
            known.apply_decision(LeaseStatus.RELEASED)
        return True

    def active_leases(self) -> tuple[LeaseRecord, ...]:
        with self._locked_registry(exclusive=False) as registry:
            return tuple(self._read_active_records(registry).values())

    def get_active_lease(self, plan_id: UUID | str) -> LeaseRecord | None:
        identifier = UUID(str(plan_id))
        with self._locked_registry(exclusive=False) as registry:
            return self._read_active_records(registry).get(identifier)

    def assert_leased(self, plan: EntryLeasePlan) -> LeaseRecord:
        record = self.get_active_lease(plan.plan_id)
        if record is None:
            raise PlanStateError(f"Plan {plan.plan_id} has no active lease")
        self._ensure_idempotent_match(plan, record)
        if plan.lease_status is not LeaseStatus.LEASED:
            raise PlanStateError(
                f"Plan {plan.plan_id} is not marked as leased"
            )
        return record

    def _find_conflicts(
        self,
        requested: LeaseRecord,
        active_records: Iterable[LeaseRecord],
    ) -> list[EntryConflict]:
        conflicts: list[EntryConflict] = []
        for active in active_records:
            for requested_pattern in requested.entries:
                for active_pattern in active.entries:
                    if self.generator.entries_may_overlap(
                        requested_pattern, active_pattern
                    ):
                        conflicts.append(
                            EntryConflict(
                                active.plan_id,
                                active.task_id,
                                active.executor_id,
                                requested_pattern,
                                active_pattern,
                            )
                        )
        return conflicts

    @staticmethod
    def _ensure_idempotent_match(
        plan: EntryLeasePlan, record: LeaseRecord
    ) -> None:
        expected = (
            plan.task_id,
            plan.executor_id,
            plan.readable_entries,
            plan.writable_entries,
        )
        actual = (
            record.task_id,
            record.executor_id,
            record.readable_entries,
            record.writable_entries,
        )
        if expected != actual:
            raise PlanStateError(
                f"Plan identifier {plan.plan_id} is already used by "
                "different lease data"
            )

    @contextmanager
    def _locked_registry(self, *, exclusive: bool) -> Iterator[Any]:
        parent = self.registry_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = parent.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise LeaseError(f"Cannot create lease registry: {parent}") from error

        try:
            resolved_parent.relative_to(self.project_root)
        except ValueError as error:
            raise LeaseError("Lease registry resolves outside the project") from error

        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.registry_path, flags, 0o600)
        except OSError as error:
            raise LeaseError(
                f"Cannot open lease registry safely: {self.registry_path}"
            ) from error

        registry = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise LeaseError("Lease registry must be a regular, unlinked file")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise LeaseError("Lease registry must be owned by the current user")
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)

            lock = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(descriptor, lock)
            yield registry
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                registry.close()

    def _read_active_records(
        self,
        registry: Any,
        *,
        truncate_incomplete: bool = False,
    ) -> dict[UUID, LeaseRecord]:
        registry.seek(0)
        raw = registry.read()
        if not isinstance(raw, bytes):
            raise LeaseRegistryCorruptionError("Lease registry is not binary")

        complete = raw
        if complete and not complete.endswith(b"\n"):
            final_newline = complete.rfind(b"\n")
            complete = b"" if final_newline < 0 else complete[: final_newline + 1]
            if truncate_incomplete:
                registry.seek(len(complete))
                registry.truncate()
                registry.flush()
                os.fsync(registry.fileno())

        active: dict[UUID, LeaseRecord] = {}
        for line_number, raw_line in enumerate(complete.splitlines(), start=1):
            try:
                event = json.loads(raw_line)
                self._apply_event(active, event)
            except (
                AttributeError,
                KeyError,
                TypeError,
                UnicodeDecodeError,
                ValueError,
            ) as error:
                raise LeaseRegistryCorruptionError(
                    f"Invalid lease registry event on line {line_number}"
                ) from error
        return active

    def _apply_event(
        self, active: dict[UUID, LeaseRecord], event: object
    ) -> None:
        if not isinstance(event, dict) or event.get("version") != _REGISTRY_VERSION:
            raise ValueError("Unsupported lease registry event")

        operation = _event_string(event, "operation")
        plan_id = UUID(_event_string(event, "plan_id"))
        task_id = UUID(_event_string(event, "task_id"))
        executor_id = UUID(_event_string(event, "executor_id"))

        if operation == "acquire":
            if plan_id in active:
                raise ValueError("Duplicate active plan identifier")
            if any(record.task_id == task_id for record in active.values()):
                raise ValueError("Task has more than one active plan")
            if any(
                record.executor_id == executor_id for record in active.values()
            ):
                raise ValueError("Executor has more than one active plan")
            readable = _string_tuple(event["readable_entries"])
            writable = _string_tuple(event["writable_entries"])
            owner_pid = event["owner_pid"]
            acquired_at = event["acquired_at"]
            if type(owner_pid) is not int or owner_pid <= 0:
                raise TypeError("Invalid owner PID")
            if not isinstance(acquired_at, str):
                raise TypeError("Invalid acquisition time")
            prepared = self.generator.prepare_entries(readable, writable)
            normalized_readable = tuple(
                item.pattern for item in prepared if item.access == "read"
            )
            normalized_writable = tuple(
                item.pattern for item in prepared if item.access == "write"
            )
            if readable != normalized_readable or writable != normalized_writable:
                raise ValueError("Lease event contains non-normalized entries")
            active[plan_id] = LeaseRecord(
                plan_id,
                task_id,
                executor_id,
                owner_pid,
                acquired_at,
                readable,
                writable,
            )
        elif operation == "release":
            released_at = event.get("released_at")
            record = active.get(plan_id)
            if (
                not isinstance(released_at, str)
                or record is None
                or record.task_id != task_id
                or record.executor_id != executor_id
            ):
                raise ValueError("Release event has no matching acquisition")
            del active[plan_id]
        else:
            raise ValueError(f"Unknown lease registry operation: {operation}")

    @staticmethod
    def _append_event(registry: Any, event: dict[str, Any]) -> None:
        payload = (
            json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode()
        registry.seek(0, os.SEEK_END)
        remaining = memoryview(payload)
        while remaining:
            written = registry.write(remaining)
            if not written:
                raise LeaseError("Could not append to the lease registry")
            remaining = remaining[written:]
        registry.flush()
        os.fsync(registry.fileno())


def _record_from_plan(plan: EntryLeasePlan) -> LeaseRecord:
    return LeaseRecord(
        plan_id=plan.plan_id,
        task_id=plan.task_id,
        executor_id=plan.executor_id,
        owner_pid=os.getpid(),
        acquired_at=_utc_now(),
        readable_entries=plan.readable_entries,
        writable_entries=plan.writable_entries,
    )


def _acquire_event(record: LeaseRecord) -> dict[str, Any]:
    return {
        "version": _REGISTRY_VERSION,
        "operation": "acquire",
        "plan_id": str(record.plan_id),
        "task_id": str(record.task_id),
        "executor_id": str(record.executor_id),
        "owner_pid": record.owner_pid,
        "acquired_at": record.acquired_at,
        "readable_entries": list(record.readable_entries),
        "writable_entries": list(record.writable_entries),
    }


def _event_string(event: dict[str, Any], key: str) -> str:
    value = event[key]
    if not isinstance(value, str):
        raise TypeError(f"Expected string field: {key}")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError("Expected a list of entry strings")
    return tuple(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
