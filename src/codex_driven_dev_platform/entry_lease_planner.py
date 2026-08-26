from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Protocol
from uuid import UUID

from .models import EntryLeaseDecision, EntryLeasePlan, TaskRequest
from .scheduler import TaskScheduler
from .taskspace import Entry


PlanListener = Callable[[TaskRequest, EntryLeasePlan], None]


class EntryLeasePlanningError(RuntimeError):
    pass


class EntryLeasePlanningCancelled(EntryLeasePlanningError):
    pass


@dataclass(frozen=True, slots=True)
class EntryLeaseProposal:
    readable_entries: tuple[str, ...]
    writable_entries: tuple[str, ...]
    rationale: str = ""


class EntryLeasePlanningAgent(Protocol):
    def create_proposal(
        self,
        request: TaskRequest,
        *,
        generation: int,
        feedback: EntryLeaseDecision | None,
    ) -> EntryLeaseProposal: ...

    def cancel(self, task_id: UUID) -> None: ...


class CodexEntryLeasePlanningAgent:
    """Run a read-only Codex process acting as ``entry-lease-planner``."""

    def __init__(
        self,
        project_root: Entry | None = None,
        *,
        codex_command: Sequence[str] = ("codex",),
        model: str | None = None,
        timeout: float | None = None,
        termination_grace_seconds: float = 5.0,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        root = Path.cwd() if project_root is None else Path(project_root)
        self.project_root = root.resolve(strict=True)
        if not self.project_root.is_dir():
            raise ValueError("project_root must be a directory")
        if not codex_command:
            raise ValueError("codex_command must not be empty")
        self.codex_command = tuple(codex_command)
        self.model = model
        self.timeout = timeout
        self.termination_grace_seconds = termination_grace_seconds
        self.environment = dict(environment or {})
        self.schema_path = Path(__file__).with_name(
            "entry_lease_plan.schema.json"
        )
        self._lock = threading.RLock()
        self._processes: dict[UUID, subprocess.Popen[str]] = {}

    def create_proposal(
        self,
        request: TaskRequest,
        *,
        generation: int,
        feedback: EntryLeaseDecision | None,
    ) -> EntryLeaseProposal:
        output_descriptor, output_name = tempfile.mkstemp(
            prefix="entry-lease-plan-",
            suffix=".json",
        )
        os.close(output_descriptor)
        output_path = Path(output_name)
        command = [
            *self.codex_command,
            "exec",
            "--ignore-user-config",
            "--strict-config",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--output-schema",
            os.fspath(self.schema_path),
            "--output-last-message",
            os.fspath(output_path),
            "-C",
            os.fspath(self.project_root),
        ]
        if self.model is not None:
            command.extend(("--model", self.model))
        command.append("-")

        child_environment = os.environ.copy()
        child_environment.update(self.environment)
        prompt = _planning_prompt(request, generation, feedback)
        process: subprocess.Popen[str] | None = None
        try:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    env=child_environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=False,
                )
            except OSError as error:
                raise EntryLeasePlanningError(
                    "Could not start entry-lease-planner"
                ) from error
            with self._lock:
                self._processes[request.task_id] = process

            try:
                stdout, stderr = process.communicate(prompt, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._stop_process(process)
                raise EntryLeasePlanningError(
                    f"entry-lease-planner timed out for task {request.task_id}"
                ) from None

            if process.returncode != 0:
                if process.returncode in {-15, -9}:
                    raise EntryLeasePlanningCancelled(
                        f"entry-lease-planner was cancelled for task {request.task_id}"
                    )
                raise EntryLeasePlanningError(
                    "entry-lease-planner failed with exit code "
                    f"{process.returncode}: {stderr or stdout}"
                )

            try:
                payload = json.loads(output_path.read_text())
            except (OSError, json.JSONDecodeError) as error:
                raise EntryLeasePlanningError(
                    "entry-lease-planner did not produce a valid plan file"
                ) from error
            return _proposal_from_payload(payload)
        finally:
            with self._lock:
                if (
                    process is not None
                    and self._processes.get(request.task_id) is process
                ):
                    del self._processes[request.task_id]
            output_path.unlink(missing_ok=True)

    def cancel(self, task_id: UUID) -> None:
        with self._lock:
            process = self._processes.get(UUID(str(task_id)))
        if process is not None:
            self._stop_process(process)

    def _stop_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=self.termination_grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class EntryLeasePlanner:
    """Create plans, return them to TaskPlanner, and fan them out to Scheduler."""

    def __init__(
        self,
        agent: EntryLeasePlanningAgent,
        scheduler: TaskScheduler,
        *,
        max_parallel_reconsiderations: int = 4,
    ) -> None:
        self.agent = agent
        self.scheduler = scheduler
        self._pool = ThreadPoolExecutor(
            max_workers=max_parallel_reconsiderations,
            thread_name_prefix="entry-lease-planner",
        )
        self._lock = threading.RLock()
        self._generations: dict[UUID, int] = {}
        self._active: dict[UUID, Future[EntryLeasePlan]] = {}
        self._pending_feedback: dict[
            UUID, tuple[TaskRequest, EntryLeaseDecision]
        ] = {}
        self._cancelled_tasks: set[UUID] = set()
        self._executor_options: dict[UUID, dict[str, object]] = {}
        self._plan_listener: PlanListener | None = None

    def set_plan_listener(self, listener: PlanListener | None) -> None:
        self._plan_listener = listener

    def create_and_submit(
        self,
        request: TaskRequest,
        *,
        executor_options: Mapping[str, object] | None = None,
    ) -> EntryLeasePlan:
        if executor_options is not None:
            self._executor_options[request.task_id] = dict(executor_options)
        return self._create_and_submit(request, feedback=None)

    def reconsider(
        self,
        request: TaskRequest,
        feedback: EntryLeaseDecision,
    ) -> Future[EntryLeasePlan]:
        with self._lock:
            active = self._active.get(request.task_id)
            if active is not None and not active.done():
                self._pending_feedback[request.task_id] = (request, feedback)
                return active
            self._cancelled_tasks.discard(request.task_id)
            future = self._pool.submit(
                self._create_and_submit,
                request,
                feedback,
            )
            self._active[request.task_id] = future
            future.add_done_callback(
                lambda completed, task_id=request.task_id: self._clear_active(
                    task_id, completed
                )
            )
            return future

    def cancel(self, task_id: UUID) -> None:
        identifier = UUID(str(task_id))
        with self._lock:
            self._cancelled_tasks.add(identifier)
            self._pending_feedback.pop(identifier, None)
        self.agent.cancel(identifier)
        with self._lock:
            future = self._active.get(identifier)
        if future is not None:
            future.cancel()

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)

    def _create_and_submit(
        self,
        request: TaskRequest,
        feedback: EntryLeaseDecision | None,
    ) -> EntryLeasePlan:
        generation = self._next_generation(request.task_id)
        proposal = self.agent.create_proposal(
            request,
            generation=generation,
            feedback=feedback,
        )
        plan = EntryLeasePlan(
            task_id=request.task_id,
            executor_id=_executor_id_for(request, generation),
            generation=generation,
            readable_entries=proposal.readable_entries,
            writable_entries=proposal.writable_entries,
            rationale=proposal.rationale,
        )
        listener = self._plan_listener
        if listener is None:
            request.add_plan(plan)
        else:
            listener(request, plan)
        self.scheduler.submit(
            request,
            plan,
            executor_options=self._executor_options.get(request.task_id),
        )
        return plan

    def _next_generation(self, task_id: UUID) -> int:
        with self._lock:
            generation = self._generations.get(task_id, 0) + 1
            self._generations[task_id] = generation
            return generation

    def _clear_active(
        self,
        task_id: UUID,
        completed: Future[EntryLeasePlan],
    ) -> None:
        pending: tuple[TaskRequest, EntryLeaseDecision] | None = None
        with self._lock:
            if self._active.get(task_id) is completed:
                del self._active[task_id]
                if task_id not in self._cancelled_tasks:
                    pending = self._pending_feedback.pop(task_id, None)
        if pending is not None:
            self.reconsider(*pending)


def _executor_id_for(request: TaskRequest, generation: int) -> UUID:
    # A logical executor identity exists before its OS process starts.
    from uuid import uuid5

    return uuid5(request.task_id, f"executor-generation-{generation}")


def _proposal_from_payload(payload: object) -> EntryLeaseProposal:
    if not isinstance(payload, dict):
        raise EntryLeasePlanningError("Plan output must be a JSON object")
    readable = payload.get("readable_entries")
    writable = payload.get("writable_entries")
    rationale = payload.get("rationale", "")
    if (
        not isinstance(readable, list)
        or not all(isinstance(item, str) for item in readable)
        or not isinstance(writable, list)
        or not all(isinstance(item, str) for item in writable)
        or not isinstance(rationale, str)
    ):
        raise EntryLeasePlanningError("Plan output has an invalid schema")
    return EntryLeaseProposal(tuple(readable), tuple(writable), rationale)


def _planning_prompt(
    request: TaskRequest,
    generation: int,
    feedback: EntryLeaseDecision | None,
) -> str:
    feedback_payload: dict[str, object] | None = None
    if feedback is not None:
        feedback_payload = {
            "previous_plan_id": str(feedback.plan.plan_id),
            "status": feedback.status.value,
            "code": feedback.code.value,
            "reasons": list(feedback.reasons),
            "blocker_executor_ids": [
                str(identifier) for identifier in feedback.blocker_executor_ids
            ],
        }
    context = {
        "task_id": str(request.task_id),
        "generation": generation,
        "prompt": request.prompt,
        "feedback": feedback_payload,
    }
    return (
        "You are the entry-lease-planner agent. Inspect the project read-only and "
        "return the minimal project-relative glob entries needed to perform the "
        "task. Separate readable_entries from writable_entries. Never include "
        "absolute paths, parent traversal, or task-executor control files. "
        "For read or write access, each entry must be either an exact path or "
        "a directory subtree ending in '/**'. Patterns such as '*.py', "
        "'src/**/*.py', and other wildcard forms are unsupported. "
        "Respond only with the requested JSON schema.\n\n"
        + json.dumps(context, ensure_ascii=False, indent=2)
    )
