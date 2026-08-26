from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import TextIO

from .entry_lease_planner import (
    CodexEntryLeasePlanningAgent,
    EntryLeasePlanner,
)
from .executor import CodexTaskExecutor
from .leases import EntryManager
from .models import (
    EntryLeaseDecision,
    EntryLeasePlan,
    TaskExecutionResult,
    TaskRequest,
)
from .scheduler import TaskScheduler
from .task_planner import TaskPlanner


EXIT_ERROR = 1
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED = 130
EXIT_RELEASE_ERROR = 74


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-driven-dev-platform",
        description=(
            "Plan entry permissions, acquire an exclusive lease, and run a "
            "Codex task."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="plan, lease, and execute one task",
    )
    run_parser.add_argument(
        "prompt",
        nargs="?",
        help="task prompt; read from stdin when omitted",
    )
    run_parser.add_argument(
        "-C",
        "--project",
        default=".",
        metavar="PATH",
        help="target project root (default: current directory)",
    )
    run_parser.add_argument(
        "--prompt-file",
        metavar="PATH",
        help="read the task prompt from a UTF-8 file; use '-' for stdin",
    )
    run_parser.add_argument(
        "--codex",
        default="codex",
        metavar="PATH",
        help="Codex CLI executable (default: codex)",
    )
    run_parser.add_argument(
        "--model",
        help="model used by the task executor",
    )
    run_parser.add_argument(
        "--planner-model",
        help="model used to produce entry lease plans",
    )
    run_parser.add_argument(
        "--timeout",
        type=_positive_float,
        metavar="SECONDS",
        help="task executor timeout",
    )
    run_parser.add_argument(
        "--planner-timeout",
        type=_positive_float,
        metavar="SECONDS",
        help="timeout for each entry lease planning attempt",
    )
    run_parser.add_argument(
        "--max-generations",
        type=_generation_limit,
        default=8,
        metavar="N|unlimited",
        help="maximum lease-plan generations (default: 8)",
    )
    run_parser.add_argument(
        "--max-workers",
        type=_positive_int,
        default=4,
        metavar="N",
        help="maximum parallel task executors (default: 4)",
    )
    run_parser.add_argument(
        "--max-parallel-reconsiderations",
        type=_positive_int,
        default=4,
        metavar="N",
        help="maximum parallel lease replanning agents (default: 4)",
    )
    run_parser.add_argument(
        "--no-ephemeral",
        action="store_true",
        help="allow the task executor to persist its Codex session",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="write one machine-readable result object to stdout",
    )
    run_parser.add_argument(
        "--debug",
        action="store_true",
        help="show a live scheduler TUI on stderr (event log when not a TTY)",
    )
    run_parser.set_defaults(handler=_run_task)

    leases_parser = subparsers.add_parser(
        "leases",
        help="show active leases for a project",
    )
    leases_parser.add_argument(
        "-C",
        "--project",
        default=".",
        metavar="PATH",
        help="target project root (default: current directory)",
    )
    leases_parser.add_argument(
        "--json",
        action="store_true",
        help="write a machine-readable lease array",
    )
    leases_parser.set_defaults(handler=_show_leases)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return arguments.handler(arguments, parser, sys.stdin, sys.stdout, sys.stderr)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR


def _run_task(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    prompt = _read_prompt(arguments, parser, stdin)
    project = _project_root(arguments.project, parser)
    codex_command = (arguments.codex,)
    tui = _SchedulerTui(stderr, enabled=arguments.debug)
    tui.write("start", project=project)

    manager = EntryManager(project)
    executor = CodexTaskExecutor(manager, codex_command=codex_command)
    scheduler = TaskScheduler(
        manager,
        executor,
        max_workers=arguments.max_workers,
    )
    agent = CodexEntryLeasePlanningAgent(
        project,
        codex_command=codex_command,
        model=arguments.planner_model,
        timeout=arguments.planner_timeout,
    )
    lease_planner = EntryLeasePlanner(
        agent,
        scheduler,
        max_parallel_reconsiderations=arguments.max_parallel_reconsiderations,
    )
    planner = TaskPlanner(
        lease_planner,
        scheduler,
        max_generations=arguments.max_generations,
    )
    scheduler.add_decision_listener(tui.on_lease_decision)
    scheduler.add_execution_start_listener(tui.on_execution_started)
    scheduler.add_execution_listener(tui.on_execution_finished)
    tui.set_queue_provider(scheduler.queued_plans)

    def publish_plan(request: TaskRequest, plan: EntryLeasePlan) -> None:
        planner.on_plan_created(request, plan)
        tui.on_plan_created(request, plan)

    lease_planner.set_plan_listener(publish_plan)
    tui.write("components-ready")

    execution: TaskExecutionResult | None = None
    try:
        if not arguments.json and not arguments.debug:
            print(f"Planning task in {project}...", file=stderr, flush=True)
        request, initial_plan = planner.submit(
            prompt,
            executor_options={
                "timeout": arguments.timeout,
                "model": arguments.model,
                "ephemeral": not arguments.no_ephemeral,
            },
        )
        if not arguments.json and not arguments.debug:
            print(
                f"Task {request.task_id}: initial plan is "
                f"{initial_plan.lease_status.value}; waiting for completion...",
                file=stderr,
                flush=True,
            )
        execution = scheduler.wait(request.task_id)
    finally:
        lease_planner.shutdown()
        scheduler.shutdown()
        tui.write(
            "shutdown",
            lease_status=(
                execution.plan.lease_status.value if execution is not None else "unknown"
            ),
        )

    if arguments.json:
        json.dump(_execution_payload(execution), stdout, ensure_ascii=False)
        stdout.write("\n")
    else:
        _write_execution_output(execution, stdout, stderr)
    return _execution_exit_code(execution)


def _show_leases(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    del stdin, stderr
    project = _project_root(arguments.project, parser)
    records = EntryManager(project).active_leases()
    payload = [
        {
            "plan_id": str(record.plan_id),
            "task_id": str(record.task_id),
            "executor_id": str(record.executor_id),
            "owner_pid": record.owner_pid,
            "acquired_at": record.acquired_at,
            "readable_entries": list(record.readable_entries),
            "writable_entries": list(record.writable_entries),
        }
        for record in records
    ]
    if arguments.json:
        json.dump(payload, stdout, ensure_ascii=False)
        stdout.write("\n")
        return 0

    if not records:
        print("No active leases.", file=stdout)
        return 0

    for record in records:
        print(
            f"plan={record.plan_id} task={record.task_id} "
            f"executor={record.executor_id} pid={record.owner_pid}",
            file=stdout,
        )
        print(f"  acquired: {record.acquired_at}", file=stdout)
        for entry in record.readable_entries:
            print(f"  read:  {entry}", file=stdout)
        for entry in record.writable_entries:
            print(f"  write: {entry}", file=stdout)
    return 0


def _read_prompt(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
    stdin: TextIO,
) -> str:
    if arguments.prompt is not None and arguments.prompt_file is not None:
        parser.error("PROMPT and --prompt-file cannot be used together")

    if arguments.prompt_file is not None:
        if arguments.prompt_file == "-":
            prompt = stdin.read()
        else:
            try:
                prompt = Path(arguments.prompt_file).read_text(encoding="utf-8")
            except OSError as error:
                parser.error(f"could not read prompt file: {error}")
    elif arguments.prompt is not None:
        prompt = arguments.prompt
    elif stdin.isatty():
        parser.error("PROMPT is required when stdin is a terminal")
    else:
        prompt = stdin.read()

    if not prompt.strip():
        parser.error("PROMPT must not be empty")
    return prompt


def _project_root(value: str, parser: argparse.ArgumentParser) -> Path:
    try:
        project = Path(value).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        parser.error(f"invalid project root: {error}")
    if not project.is_dir():
        parser.error(f"project root is not a directory: {project}")
    return project


def _execution_payload(execution: TaskExecutionResult) -> dict[str, object]:
    process = execution.result
    return {
        "task_id": str(execution.request.task_id),
        "task_status": execution.request.task_status.value,
        "plan_id": str(execution.plan.plan_id),
        "executor_id": str(execution.plan.executor_id),
        "lease_status": execution.plan.lease_status.value,
        "generation": execution.plan.generation,
        "readable_entries": list(execution.plan.readable_entries),
        "writable_entries": list(execution.plan.writable_entries),
        "returncode": getattr(process, "returncode", None),
        "stdout": getattr(process, "stdout", None),
        "stderr": getattr(process, "stderr", None),
        "execution_error": _error_text(execution.execution_error),
        "release_error": _error_text(execution.release_error),
        "decisions": [
            _decision_payload(decision) for decision in execution.request.decisions
        ],
    }


def _decision_payload(decision: EntryLeaseDecision) -> dict[str, object]:
    return {
        "plan_id": str(decision.plan.plan_id),
        "generation": decision.plan.generation,
        "status": decision.status.value,
        "code": decision.code.value,
        "reasons": list(decision.reasons),
        "blocker_executor_ids": [
            str(identifier) for identifier in decision.blocker_executor_ids
        ],
    }


def _write_execution_output(
    execution: TaskExecutionResult,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    process = execution.result
    process_stdout = getattr(process, "stdout", "") or ""
    process_stderr = getattr(process, "stderr", "") or ""
    stdout.write(process_stdout)
    stderr.write(process_stderr)
    if process_stdout and not process_stdout.endswith("\n"):
        stdout.write("\n")
    if process_stderr and not process_stderr.endswith("\n"):
        stderr.write("\n")

    if execution.execution_error is not None:
        print(f"Task execution failed: {execution.execution_error}", file=stderr)
    if execution.release_error is not None:
        print(f"Lease release failed: {execution.release_error}", file=stderr)
    print(
        f"Task {execution.request.task_id}: "
        f"{execution.request.task_status.value}",
        file=stderr,
    )


def _execution_exit_code(execution: TaskExecutionResult) -> int:
    if execution.release_error is not None:
        return EXIT_RELEASE_ERROR
    if isinstance(execution.execution_error, subprocess.TimeoutExpired):
        return EXIT_TIMEOUT
    if execution.execution_error is not None:
        return EXIT_ERROR
    returncode = getattr(execution.result, "returncode", 0)
    if returncode == 0:
        return 0
    if isinstance(returncode, int) and 1 <= returncode <= 255:
        return returncode
    return EXIT_ERROR


def _error_text(error: BaseException | None) -> str | None:
    if error is None:
        return None
    return f"{type(error).__name__}: {error}"


class _SchedulerTui:
    """Render live scheduler state on a TTY and ordered events otherwise."""

    def __init__(self, stream: TextIO, *, enabled: bool) -> None:
        self._stream = stream
        self._enabled = enabled
        self._sequence = 0
        self._lock = threading.RLock()
        isatty = getattr(stream, "isatty", lambda: False)
        self._dynamic = bool(
            enabled
            and isatty()
            and os.environ.get("TERM", "") not in {"", "dumb"}
        )
        self._rendered_lines = 0
        self._task_id = "-"
        self._task_status = "planning"
        self._project = "-"
        self._plans: dict[str, dict[str, object]] = {}
        self._events: list[str] = []
        self._queue_provider: Callable[[], Sequence[EntryLeasePlan]] = lambda: ()

    def set_queue_provider(
        self, provider: Callable[[], Sequence[EntryLeasePlan]]
    ) -> None:
        self._queue_provider = provider

    def on_plan_created(
        self, request: TaskRequest, plan: EntryLeasePlan
    ) -> None:
        with self._lock:
            self._task_id = str(request.task_id)
            self._task_status = request.task_status.value
            self._update_plan(plan)
            self.write(
                "plan-created",
                generation=plan.generation,
                plan_id=plan.plan_id,
                readable=plan.readable_entries,
                writable=plan.writable_entries,
            )

    def on_lease_decision(
        self, request: TaskRequest, decision: EntryLeaseDecision
    ) -> None:
        with self._lock:
            self._task_id = str(request.task_id)
            self._task_status = request.task_status.value
            self._update_plan(decision.plan)
            self.write(
                "lease-decision",
                generation=decision.plan.generation,
                status=decision.status.value,
                code=decision.code.value,
                blockers=decision.blocker_executor_ids,
            )

    def on_execution_started(
        self, request: TaskRequest, plan: EntryLeasePlan
    ) -> None:
        with self._lock:
            self._task_id = str(request.task_id)
            self._task_status = request.task_status.value
            self._update_plan(plan)
            self.write(
                "executor-started",
                generation=plan.generation,
                executor_id=plan.executor_id,
            )

    def on_execution_finished(self, execution: TaskExecutionResult) -> None:
        with self._lock:
            self._task_id = str(execution.request.task_id)
            self._task_status = execution.request.task_status.value
            self._update_plan(execution.plan)
            self.write(
                "execution-finished",
                task_status=execution.request.task_status.value,
                returncode=getattr(execution.result, "returncode", None),
                lease_status=execution.plan.lease_status.value,
            )

    def write(self, event: str, **fields: object) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._sequence += 1
            if event == "start" and "project" in fields:
                self._project = str(fields["project"])
            if "task_status" in fields:
                self._task_status = str(fields["task_status"])
            details = " ".join(
                f"{name}={_debug_value(value)}" for name, value in fields.items()
            )
            suffix = f" {details}" if details else ""
            event_line = f"[{self._sequence:02d}] {event}{suffix}"
            self._events.append(event_line)
            if self._dynamic:
                self._render()
            else:
                print(
                    f"[debug {self._sequence:02d}] {event}{suffix}",
                    file=self._stream,
                    flush=True,
                )

    def _update_plan(self, plan: EntryLeasePlan) -> None:
        self._plans[str(plan.plan_id)] = {
            "generation": plan.generation,
            "status": plan.lease_status.value,
            "executor": str(plan.executor_id),
            "read": len(plan.readable_entries),
            "write": len(plan.writable_entries),
        }

    def _render(self) -> None:
        try:
            queued = tuple(self._queue_provider())
        except Exception:
            queued = ()
        lines = [
            "┌─ codex-driven-dev-platform · Scheduler TUI",
            f"│ Project  {_compact(self._project, 68)}",
            f"│ Task     {_compact(self._task_id, 68)}",
            f"│ Status   {self._task_status:<12}  Queue {len(queued)}",
            "├─ Plans",
        ]
        if self._plans:
            plans = sorted(
                self._plans.values(), key=lambda item: int(item["generation"])
            )
            for plan in plans[-6:]:
                lines.append(
                    "│ "
                    f"g{plan['generation']:<2} {plan['status']:<10} "
                    f"R{plan['read']} W{plan['write']} "
                    f"executor={_compact(str(plan['executor']), 12)}"
                )
        else:
            lines.append("│ planning entries…")
        lines.append("├─ Recent events")
        lines.extend(f"│ {line}" for line in self._events[-8:])
        lines.append("└─ Ctrl-C to interrupt")

        width = max(40, shutil.get_terminal_size(fallback=(100, 24)).columns)
        if self._rendered_lines:
            self._stream.write(f"\x1b[{self._rendered_lines}A")
        for line in lines:
            self._stream.write("\x1b[2K" + _compact(line, width - 1) + "\n")
        if self._rendered_lines > len(lines):
            for _ in range(self._rendered_lines - len(lines)):
                self._stream.write("\x1b[2K\n")
        self._rendered_lines = max(self._rendered_lines, len(lines))
        self._stream.flush()


def _compact(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


def _debug_value(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return json.dumps([str(item) for item in value], ensure_ascii=False)
    return str(value)


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _generation_limit(value: str) -> int | None:
    if value == "unlimited":
        return None
    return _positive_int(value)
