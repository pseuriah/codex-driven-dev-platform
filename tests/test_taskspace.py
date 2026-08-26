import os
from pathlib import Path
import tempfile
import tomllib
import unittest

from codex_driven_dev_platform.taskspace import (
    ReservedEntryError,
    TaskspaceGenerator,
    patterns_may_overlap,
)


class TaskspaceGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "src").mkdir()
        (self.root / "docs").mkdir()
        self.generator = TaskspaceGenerator(self.root)

    def test_generates_current_codex_permissions_and_write_wins(self) -> None:
        config = self.generator.generate_taskspace_config(
            [Path("src/**"), Path("docs/guide.md")],
            [Path("src/**")],
        )

        parsed = tomllib.loads(config)
        roots = parsed["permissions"]["task-executor"]["filesystem"][
            ":workspace_roots"
        ]
        self.assertEqual(parsed["approval_policy"], "never")
        self.assertEqual(parsed["default_permissions"], "task-executor")
        self.assertEqual(
            roots,
            {
                "src/**": "write",
                "docs/guide.md": "read",
            },
        )

    def test_rejects_globs_unsupported_by_codex_permissions(self) -> None:
        for entry in (
            Path("docs/*.md"),
            Path("src/**/*.py"),
            Path("src/file?.py"),
        ):
            with self.subTest(entry=entry):
                with self.assertRaisesRegex(
                    ValueError,
                    "exact path or a directory subtree",
                ):
                    self.generator.generate_taskspace_config([entry], [])

    def test_generates_parseable_codex_cli_overrides(self) -> None:
        overrides = self.generator.generate_codex_overrides(
            [Path("src/**")], [Path("docs/**")]
        )

        parsed = [tomllib.loads(f"value={item.split('=', 1)[1]}")["value"] for item in overrides]
        self.assertEqual(parsed[0], "never")
        self.assertEqual(parsed[1], "task-executor")
        self.assertEqual(
            parsed[2]["filesystem"][":workspace_roots"],
            {"src/**": "read", "docs/**": "write"},
        )

    def test_rejects_absolute_entry(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be relative"):
            self.generator.generate_taskspace_config([self.root / "src"], [])

    def test_rejects_parent_traversal(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the project"):
            self.generator.generate_taskspace_config([Path("../secret")], [])

    def test_rejects_reserved_registry_and_matching_scopes(self) -> None:
        for entry in (
            Path(".task-executor-leases.jsonl"),
            Path(".task-executor-*.jsonl"),
            Path("."),
            Path("**"),
        ):
            with self.subTest(entry=entry):
                with self.assertRaises(ReservedEntryError):
                    self.generator.generate_taskspace_config([entry], [])

    def test_rejects_symlink_to_outside_project(self) -> None:
        outside = Path(self.temporary_directory.name).parent
        os.symlink(outside, self.root / "external")

        with self.assertRaisesRegex(ValueError, "outside the project"):
            self.generator.generate_taskspace_config([Path("external/**")], [])

    def test_rejects_external_symlink_selected_by_recursive_glob(self) -> None:
        outside = Path(self.temporary_directory.name).parent
        (self.root / "links").mkdir()
        os.symlink(outside, self.root / "links" / "external")

        with self.assertRaisesRegex(ValueError, "symlink outside"):
            self.generator.generate_taskspace_config([Path("links/**/*.py")], [])

    def test_allows_symlink_with_target_inside_project(self) -> None:
        os.symlink(self.root / "src", self.root / "source")

        config = self.generator.generate_taskspace_config([Path("source/**")], [])

        roots = tomllib.loads(config)["permissions"]["task-executor"][
            "filesystem"
        ][":workspace_roots"]
        self.assertEqual(roots["source/**"], "read")

    def test_glob_does_not_reject_unrelated_external_symlink(self) -> None:
        outside = Path(self.temporary_directory.name).parent
        (self.root / "src" / "safe.py").touch()
        os.symlink(outside, self.root / "docs" / "external")

        config = self.generator.generate_taskspace_config(
            [Path("src/safe.py")], []
        )

        roots = tomllib.loads(config)["permissions"]["task-executor"][
            "filesystem"
        ][":workspace_roots"]
        self.assertEqual(roots["src/safe.py"], "read")

    def test_overlap_detection_is_conservative(self) -> None:
        self.assertFalse(patterns_may_overlap("src/**", "docs/**"))
        self.assertTrue(patterns_may_overlap("src/**", "src/file.py"))
        self.assertTrue(patterns_may_overlap("src/*.py", "src/*.txt"))
        self.assertFalse(patterns_may_overlap("src/*.py", "src/readme.txt"))


if __name__ == "__main__":
    unittest.main()
