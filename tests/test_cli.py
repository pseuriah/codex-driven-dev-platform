from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

from codex_driven_dev_platform.cli import main
from codex_driven_dev_platform.leases import EntryManager
from codex_driven_dev_platform.models import EntryLeasePlan


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


_FAKE_CODEX = """#!/usr/bin/env python3
import json
from pathlib import Path
import sys

arguments = sys.argv[1:]
prompt = sys.stdin.read()
if "--output-last-message" in arguments:
    index = arguments.index("--output-last-message")
    Path(arguments[index + 1]).write_text(json.dumps({
        "readable_entries": ["src/**"],
        "writable_entries": ["docs/**"],
        "rationale": "fake plan",
    }))
    raise SystemExit(0)

print(json.dumps({"prompt": prompt, "arguments": arguments}))
if prompt == "fail":
    print("requested failure", file=sys.stderr)
    raise SystemExit(7)
"""


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        self.fake_codex = self.root / "fake-codex"
        self.fake_codex.write_text(_FAKE_CODEX)
        self.fake_codex.chmod(
            self.fake_codex.stat().st_mode | stat.S_IXUSR
        )

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_run_executes_task_and_emits_json(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "run",
            "-C",
            str(self.root),
            "--codex",
            str(self.fake_codex),
            "--json",
            "update docs",
        )

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["task_status"], "completed")
        self.assertEqual(payload["lease_status"], "released")
        self.assertEqual(payload["returncode"], 0)
        self.assertEqual(payload["readable_entries"], ["src/**"])
        self.assertEqual(payload["writable_entries"], ["docs/**"])
        self.assertEqual(
            json.loads(payload["stdout"])["prompt"],
            "update docs",
        )
        self.assertEqual(EntryManager(self.root).active_leases(), ())

    def test_run_forwards_executor_exit_code(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "run",
            "-C",
            str(self.root),
            "--codex",
            str(self.fake_codex),
            "fail",
        )

        self.assertEqual(exit_code, 7)
        self.assertEqual(json.loads(stdout)["prompt"], "fail")
        self.assertIn("requested failure", stderr)
        self.assertIn("failed", stderr)
        self.assertEqual(EntryManager(self.root).active_leases(), ())

    def test_run_reads_prompt_file(self) -> None:
        prompt_file = self.root / "prompt.txt"
        prompt_file.write_text("prompt from file", encoding="utf-8")

        exit_code, stdout, stderr = self.invoke(
            "run",
            "-C",
            str(self.root),
            "--codex",
            str(self.fake_codex),
            "--json",
            "--prompt-file",
            str(prompt_file),
        )

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(
            json.loads(payload["stdout"])["prompt"],
            "prompt from file",
        )

    def test_run_debug_shows_ordered_sequence_on_stderr(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "run",
            "-C",
            str(self.root),
            "--codex",
            str(self.fake_codex),
            "--json",
            "--debug",
            "update docs",
        )

        self.assertEqual(exit_code, 0, stderr)
        json.loads(stdout)
        events = [
            "start",
            "components-ready",
            "plan-created",
            "lease-decision",
            "executor-started",
            "execution-finished",
            "shutdown",
        ]
        positions = [stderr.index(event) for event in events]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("readable=[\"src/**\"]", stderr)
        self.assertIn("writable=[\"docs/**\"]", stderr)
        self.assertIn("lease_status=released", stderr)

    def test_run_debug_uses_live_tui_when_stderr_is_a_terminal(self) -> None:
        stdout = io.StringIO()
        stderr = _TtyStringIO()
        with mock.patch.dict("os.environ", {"TERM": "xterm"}), redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            exit_code = main(
                (
                    "run",
                    "-C",
                    str(self.root),
                    "--codex",
                    str(self.fake_codex),
                    "--json",
                    "--debug",
                    "update docs",
                )
            )

        self.assertEqual(exit_code, 0, stderr.getvalue())
        json.loads(stdout.getvalue())
        rendered = stderr.getvalue()
        self.assertIn("Scheduler TUI", rendered)
        self.assertIn("Recent events", rendered)
        self.assertIn("executor-started", rendered)
        self.assertIn("\x1b[2K", rendered)

    def test_leases_lists_active_records_as_json(self) -> None:
        manager = EntryManager(self.root)
        plan = EntryLeasePlan(
            task_id=uuid4(),
            executor_id=uuid4(),
            readable_entries=("src/**",),
            writable_entries=("docs/**",),
        )
        manager.acquire(plan)
        self.addCleanup(manager.release, plan.plan_id)

        exit_code, stdout, stderr = self.invoke(
            "leases",
            "-C",
            str(self.root),
            "--json",
        )

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["plan_id"], str(plan.plan_id))
        self.assertEqual(payload[0]["readable_entries"], ["src/**"])
        self.assertEqual(payload[0]["writable_entries"], ["docs/**"])


if __name__ == "__main__":
    unittest.main()
