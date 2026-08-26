from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .leases import EntryManager
from .models import EntryLeasePlan
from .staging import StagedTaskspace
from .taskspace import TaskspaceGenerator


class CodexTaskExecutor:
    """Run Codex with a lease that was already acquired by TaskScheduler."""

    def __init__(
        self,
        entry_manager: EntryManager,
        *,
        codex_command: Sequence[str] = ("codex",),
        generator: TaskspaceGenerator | None = None,
        termination_grace_seconds: float = 5.0,
        private_tmp_wrapper: Sequence[str] | None = None,
    ) -> None:
        if not codex_command:
            raise ValueError("codex_command must not be empty")
        if termination_grace_seconds < 0:
            raise ValueError("termination_grace_seconds must not be negative")

        self.entry_manager = entry_manager
        self.generator = generator or entry_manager.generator
        if self.generator.project_root != entry_manager.project_root:
            raise ValueError("Generator and EntryManager must use the same project root")

        self.project_root = entry_manager.project_root
        self.codex_command = tuple(codex_command)
        self._uses_codex_linux_sandbox = (
            sys.platform.startswith("linux")
            and Path(self.codex_command[0]).name == "codex"
        )
        self.termination_grace_seconds = termination_grace_seconds
        self.private_tmp_wrapper = self._resolve_private_tmp_wrapper(
            private_tmp_wrapper
        )

    def run(
        self,
        prompt: str,
        plan: EntryLeasePlan,
        *,
        timeout: float | None = None,
        model: str | None = None,
        ephemeral: bool = False,
        check: bool = False,
        environment: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")

        # Read-only verification: TaskExecutor never applies for a lease.
        self.entry_manager.assert_leased(plan)
        child_environment = os.environ.copy()
        if environment is not None:
            child_environment.update(environment)
        child_environment["CODEX_TASK_EXECUTOR_ID"] = str(plan.executor_id)
        child_environment["CODEX_ENTRY_LEASE_PLAN_ID"] = str(plan.plan_id)
        child_environment["CODEX_TASK_ID"] = str(plan.task_id)

        if StagedTaskspace.required(plan.writable_entries):
            with StagedTaskspace(
                self.project_root,
                plan.readable_entries,
                plan.writable_entries,
            ) as taskspace:
                staged_environment = dict(child_environment)
                staged_environment.update(
                    {"TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp"}
                )
                result = self._run_codex(
                    prompt,
                    taskspace.codex_overrides(),
                    taskspace.workspace_root,
                    staged_environment,
                    timeout=timeout,
                    model=model,
                    ephemeral=ephemeral,
                    skip_git_repo_check=True,
                    private_tmp=True,
                )
                if result.returncode == 0:
                    taskspace.commit()
        else:
            overrides = self.generator.generate_codex_overrides(
                plan.readable_entries,
                plan.writable_entries,
                private_temporary_directory=bool(plan.writable_entries),
            )
            result = self._run_codex(
                prompt,
                overrides,
                self.project_root,
                child_environment,
                timeout=timeout,
                model=model,
                ephemeral=ephemeral,
                skip_git_repo_check=False,
                private_tmp=bool(plan.writable_entries),
            )
        if check:
            result.check_returncode()
        return result

    def _run_codex(
        self,
        prompt: str,
        overrides: Sequence[str],
        workspace_root: os.PathLike[str],
        child_environment: Mapping[str, str],
        *,
        timeout: float | None,
        model: str | None,
        ephemeral: bool,
        skip_git_repo_check: bool,
        private_tmp: bool,
    ) -> subprocess.CompletedProcess[str]:
        command = self._build_command(
            overrides,
            workspace_root=workspace_root,
            model=model,
            ephemeral=ephemeral,
            skip_git_repo_check=skip_git_repo_check,
        )
        if private_tmp:
            if not self.private_tmp_wrapper and self._uses_codex_linux_sandbox:
                raise RuntimeError(
                    "Writable entries require bwrap to provide an "
                    "executor-private /tmp on Linux"
                )
            if self.private_tmp_wrapper:
                command = [*self.private_tmp_wrapper, *command]
        process = subprocess.Popen(
            command,
            cwd=workspace_root,
            env=child_environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        stdout, stderr = self._communicate(
            process, prompt, command=command, timeout=timeout
        )
        return subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )

    def _resolve_private_tmp_wrapper(
        self,
        configured: Sequence[str] | None,
    ) -> tuple[str, ...]:
        if configured is not None:
            return tuple(configured)
        if not self._uses_codex_linux_sandbox:
            return ()
        bwrap = shutil.which("bwrap")
        if bwrap is None:
            return ()
        return (
            bwrap,
            "--die-with-parent",
            "--bind",
            "/",
            "/",
            "--dev-bind",
            "/dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--",
        )

    def _build_command(
        self,
        overrides: Sequence[str],
        *,
        workspace_root: os.PathLike[str],
        model: str | None,
        ephemeral: bool,
        skip_git_repo_check: bool,
    ) -> list[str]:
        command = [
            *self.codex_command,
            "exec",
            "--ignore-user-config",
            "--strict-config",
            "-C",
            os.fspath(workspace_root),
        ]
        for override in overrides:
            command.extend(("-c", override))
        if model is not None:
            command.extend(("--model", model))
        if ephemeral:
            command.append("--ephemeral")
        if skip_git_repo_check:
            command.append("--skip-git-repo-check")
        command.append("-")
        return command

    def _communicate(
        self,
        process: subprocess.Popen[str],
        prompt: str,
        *,
        command: Sequence[str],
        timeout: float | None,
    ) -> tuple[str, str]:
        try:
            return process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            stdout, stderr = self._stop_process(process)
            raise subprocess.TimeoutExpired(
                command, timeout, output=stdout, stderr=stderr
            ) from None
        except BaseException:
            self._stop_process(process)
            raise

    def _stop_process(self, process: subprocess.Popen[str]) -> tuple[str, str]:
        if process.poll() is None:
            process.terminate()
        try:
            return process.communicate(timeout=self.termination_grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.communicate()
