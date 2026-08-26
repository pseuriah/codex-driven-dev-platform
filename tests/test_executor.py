import json
from pathlib import Path
import stat
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

from codex_driven_dev_platform.executor import CodexTaskExecutor
from codex_driven_dev_platform.leases import EntryManager, PlanStateError
from codex_driven_dev_platform.models import (
    EntryLeasePlan,
    LeaseStatus,
    TaskRequest,
    TaskStatus,
)
from codex_driven_dev_platform.scheduler import TaskScheduler


_FAKE_CODEX = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import signal
import sys
import time

if os.environ.get("FAKE_CODEX_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
prompt = sys.stdin.read()
time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "0")))
write_relative = os.environ.get("FAKE_CODEX_WRITE_RELATIVE")
if write_relative:
    target = Path(write_relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(os.environ.get("FAKE_CODEX_WRITE_CONTENT", "changed"))
unleased_relative = os.environ.get("FAKE_CODEX_WRITE_UNLEASED")
if unleased_relative:
    target = Path(unleased_relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("must not be committed")
print(json.dumps({
    "executor_id": os.environ.get("CODEX_TASK_EXECUTOR_ID"),
    "plan_id": os.environ.get("CODEX_ENTRY_LEASE_PLAN_ID"),
    "task_id": os.environ.get("CODEX_TASK_ID"),
    "prompt": prompt,
    "arguments": sys.argv[1:],
}))
raise SystemExit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
"""


class _InterruptingProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.communications = 0

    def communicate(self, input=None, timeout=None):
        del input, timeout
        self.communications += 1
        if self.communications == 1:
            raise KeyboardInterrupt()
        self.returncode = -15
        return "", ""

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class CodexTaskExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        self.fake_codex = self.root / "fake-codex"
        self.fake_codex.write_text(_FAKE_CODEX)
        self.fake_codex.chmod(self.fake_codex.stat().st_mode | stat.S_IXUSR)
        self.manager = EntryManager(self.root)
        self.executor = CodexTaskExecutor(
            self.manager,
            codex_command=(str(self.fake_codex),),
            termination_grace_seconds=0.05,
        )

    def make_plan(self) -> EntryLeasePlan:
        from uuid import uuid4

        return EntryLeasePlan(
            task_id=uuid4(),
            executor_id=uuid4(),
            readable_entries=("src/**",),
            writable_entries=("docs/**",),
        )

    def test_uses_preacquired_plan_without_releasing_it(self) -> None:
        plan = self.make_plan()
        self.manager.acquire(plan)

        result = self.executor.run("perform task", plan, ephemeral=True)

        payload = json.loads(result.stdout)
        self.assertEqual(payload["executor_id"], str(plan.executor_id))
        self.assertEqual(payload["plan_id"], str(plan.plan_id))
        self.assertEqual(payload["task_id"], str(plan.task_id))
        self.assertEqual(payload["prompt"], "perform task")
        self.assertIn("--ignore-user-config", result.args)
        self.assertIn("--strict-config", result.args)
        self.assertIn("--ephemeral", result.args)
        self.assertEqual(plan.lease_status, LeaseStatus.LEASED)
        self.assertIsNotNone(self.manager.get_active_lease(plan.plan_id))

        overrides = [
            result.args[index + 1]
            for index, argument in enumerate(result.args)
            if argument == "-c"
        ]
        permissions = tomllib.loads(
            "value=" + overrides[2].split("=", 1)[1]
        )["value"]
        self.assertEqual(
            permissions["filesystem"][":workspace_roots"],
            {"src/**": "read", "docs/**": "write"},
        )
        self.assertEqual(permissions["filesystem"][":tmpdir"], "write")
        self.assertEqual(permissions["filesystem"][":slash_tmp"], "write")

    def test_real_codex_uses_private_tmp_bwrap_wrapper_on_linux(self) -> None:
        with mock.patch(
            "codex_driven_dev_platform.executor.sys.platform",
            "linux",
        ), mock.patch(
            "codex_driven_dev_platform.executor.shutil.which",
            return_value="/usr/bin/bwrap",
        ):
            executor = CodexTaskExecutor(
                self.manager,
                codex_command=("codex",),
            )

        self.assertEqual(executor.private_tmp_wrapper[0], "/usr/bin/bwrap")
        self.assertIn("--tmpfs", executor.private_tmp_wrapper)
        self.assertIn("/tmp", executor.private_tmp_wrapper)

    def test_rejects_plan_without_active_lease(self) -> None:
        plan = self.make_plan()

        with self.assertRaises(PlanStateError):
            self.executor.run("task", plan)

    def test_nonzero_result_does_not_release_scheduler_owned_lease(self) -> None:
        plan = self.make_plan()
        self.manager.acquire(plan)

        result = self.executor.run(
            "fail",
            plan,
            environment={"FAKE_CODEX_EXIT": "7"},
        )

        self.assertEqual(result.returncode, 7)
        self.assertEqual(plan.lease_status, LeaseStatus.LEASED)

    def test_exact_writable_file_uses_staging_and_commits_only_lease(self) -> None:
        target = self.root / "docs" / "guide.md"
        target.write_text("before")
        plan = self.make_plan()
        plan.writable_entries = ("docs/guide.md",)
        self.manager.acquire(plan)

        result = self.executor.run(
            "edit exact file",
            plan,
            environment={
                "FAKE_CODEX_WRITE_RELATIVE": "docs/guide.md",
                "FAKE_CODEX_WRITE_CONTENT": "after",
                "FAKE_CODEX_WRITE_UNLEASED": "src/unleased.py",
            },
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "after")
        self.assertFalse((self.root / "src" / "unleased.py").exists())
        workspace = Path(result.args[result.args.index("-C") + 1])
        self.assertNotEqual(workspace, self.root)
        self.assertFalse(workspace.exists())
        self.assertIn("--skip-git-repo-check", result.args)
        self.assertIsNotNone(self.manager.get_active_lease(plan.plan_id))

    def test_nonzero_exact_file_execution_discards_staged_changes(self) -> None:
        target = self.root / "docs" / "guide.md"
        target.write_text("before")
        plan = self.make_plan()
        plan.writable_entries = ("docs/guide.md",)
        self.manager.acquire(plan)

        result = self.executor.run(
            "fail staged edit",
            plan,
            environment={
                "FAKE_CODEX_WRITE_RELATIVE": "docs/guide.md",
                "FAKE_CODEX_WRITE_CONTENT": "after",
                "FAKE_CODEX_EXIT": "7",
            },
        )

        self.assertEqual(result.returncode, 7)
        self.assertEqual(target.read_text(), "before")

    def test_timeout_stops_child_but_does_not_release_lease(self) -> None:
        plan = self.make_plan()
        self.manager.acquire(plan)

        with self.assertRaises(subprocess.TimeoutExpired):
            self.executor.run(
                "slow",
                plan,
                timeout=0.05,
                environment={
                    "FAKE_CODEX_IGNORE_TERM": "1",
                    "FAKE_CODEX_SLEEP": "5",
                },
            )

        self.assertEqual(plan.lease_status, LeaseStatus.LEASED)
        self.assertIsNotNone(self.manager.get_active_lease(plan.plan_id))

    def test_scheduler_releases_after_normal_and_nonzero_exit(self) -> None:
        scheduler = TaskScheduler(self.manager, self.executor)
        self.addCleanup(scheduler.shutdown)

        normal_request = TaskRequest("normal")
        normal = EntryLeasePlan(
            normal_request.task_id,
            normal_request.task_id,
            ("src/**",),
            (),
        )
        scheduler.submit(normal_request, normal)
        normal_result = scheduler.wait(normal_request.task_id, timeout=2)
        self.assertIsNone(normal_result.execution_error)
        self.assertEqual(normal.lease_status, LeaseStatus.RELEASED)

        failure_request = TaskRequest("failure")
        failure = EntryLeasePlan(
            failure_request.task_id,
            failure_request.task_id,
            ("src/**",),
            (),
        )
        scheduler.submit(
            failure_request,
            failure,
            executor_options={"environment": {"FAKE_CODEX_EXIT": "9"}},
        )
        failure_result = scheduler.wait(failure_request.task_id, timeout=2)
        self.assertEqual(failure_result.result.returncode, 9)
        self.assertEqual(failure_request.task_status, TaskStatus.FAILED)
        self.assertEqual(failure.lease_status, LeaseStatus.RELEASED)

    def test_scheduler_releases_after_spawn_failure_and_timeout(self) -> None:
        missing_executor = CodexTaskExecutor(
            self.manager,
            codex_command=(str(self.root / "missing-codex"),),
        )
        spawn_scheduler = TaskScheduler(self.manager, missing_executor)
        self.addCleanup(spawn_scheduler.shutdown)
        spawn_request = TaskRequest("spawn")
        spawn_plan = EntryLeasePlan(
            spawn_request.task_id,
            spawn_request.task_id,
            ("src/**",),
            (),
        )

        spawn_scheduler.submit(spawn_request, spawn_plan)
        spawn_result = spawn_scheduler.wait(spawn_request.task_id, timeout=2)
        self.assertIsInstance(spawn_result.execution_error, FileNotFoundError)
        self.assertEqual(spawn_plan.lease_status, LeaseStatus.RELEASED)

        timeout_scheduler = TaskScheduler(self.manager, self.executor)
        self.addCleanup(timeout_scheduler.shutdown)
        timeout_request = TaskRequest("timeout")
        timeout_plan = EntryLeasePlan(
            timeout_request.task_id,
            timeout_request.task_id,
            ("src/**",),
            (),
        )
        timeout_scheduler.submit(
            timeout_request,
            timeout_plan,
            executor_options={
                "timeout": 0.05,
                "environment": {
                    "FAKE_CODEX_IGNORE_TERM": "1",
                    "FAKE_CODEX_SLEEP": "5",
                },
            },
        )
        timeout_result = timeout_scheduler.wait(timeout_request.task_id, timeout=2)
        self.assertIsInstance(
            timeout_result.execution_error,
            subprocess.TimeoutExpired,
        )
        self.assertEqual(timeout_plan.lease_status, LeaseStatus.RELEASED)

    def test_scheduler_releases_after_interrupt(self) -> None:
        scheduler = TaskScheduler(self.manager, self.executor)
        self.addCleanup(scheduler.shutdown)
        request = TaskRequest("interrupt")
        plan = EntryLeasePlan(
            request.task_id,
            request.task_id,
            ("src/**",),
            (),
        )
        process = _InterruptingProcess()

        with mock.patch(
            "codex_driven_dev_platform.executor.subprocess.Popen",
            return_value=process,
        ):
            scheduler.submit(request, plan)
            result = scheduler.wait(request.task_id, timeout=2)

        self.assertIsInstance(result.execution_error, KeyboardInterrupt)
        self.assertTrue(process.terminated)
        self.assertEqual(plan.lease_status, LeaseStatus.RELEASED)


if __name__ == "__main__":
    unittest.main()
