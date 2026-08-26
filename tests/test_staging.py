from pathlib import Path
import tempfile
import tomllib
import unittest

from codex_driven_dev_platform.staging import (
    StagedTaskspace,
    StagedTaskspaceConflictError,
)


class StagedTaskspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("old")
        (self.root / "README.md").write_text("read me")

    def test_exact_file_commit_ignores_unleased_changes(self) -> None:
        with StagedTaskspace(
            self.root,
            ("README.md",),
            ("src/app.py",),
        ) as taskspace:
            (taskspace.workspace_root / "src" / "app.py").write_text("new")
            (taskspace.workspace_root / "README.md").write_text("changed copy")
            (taskspace.workspace_root / "unleased.txt").write_text("discard")
            taskspace.commit()

        self.assertEqual((self.root / "src" / "app.py").read_text(), "new")
        self.assertEqual((self.root / "README.md").read_text(), "read me")
        self.assertFalse((self.root / "unleased.txt").exists())

    def test_subtree_commit_mirrors_additions_and_deletions(self) -> None:
        (self.root / "src" / "remove.py").write_text("remove")

        with StagedTaskspace(self.root, (), ("src/**",)) as taskspace:
            (taskspace.workspace_root / "src" / "app.py").unlink()
            (taskspace.workspace_root / "src" / "remove.py").unlink()
            (taskspace.workspace_root / "src" / "package").mkdir()
            (taskspace.workspace_root / "src" / "package" / "new.py").write_text(
                "new"
            )
            taskspace.commit()

        self.assertFalse((self.root / "src" / "app.py").exists())
        self.assertFalse((self.root / "src" / "remove.py").exists())
        self.assertEqual(
            (self.root / "src" / "package" / "new.py").read_text(),
            "new",
        )

    def test_exact_file_deletion_is_committed(self) -> None:
        with StagedTaskspace(self.root, (), ("src/app.py",)) as taskspace:
            (taskspace.workspace_root / "src" / "app.py").unlink()
            taskspace.commit()

        self.assertFalse((self.root / "src" / "app.py").exists())

    def test_external_change_prevents_commit(self) -> None:
        with StagedTaskspace(self.root, (), ("src/app.py",)) as taskspace:
            (taskspace.workspace_root / "src" / "app.py").write_text("agent")
            (self.root / "src" / "app.py").write_text("external")

            with self.assertRaises(StagedTaskspaceConflictError):
                taskspace.commit()

        self.assertEqual((self.root / "src" / "app.py").read_text(), "external")

    def test_staged_profile_writes_only_the_disposable_workspace_root(self) -> None:
        with StagedTaskspace(
            self.root,
            ("README.md",),
            ("src/app.py",),
        ) as taskspace:
            overrides = taskspace.codex_overrides()

        permissions = tomllib.loads(
            "value=" + overrides[2].split("=", 1)[1]
        )["value"]
        filesystem = permissions["filesystem"]
        self.assertEqual(filesystem[":workspace_roots"], {".": "write"})
        self.assertEqual(filesystem[":tmpdir"], "write")
        self.assertEqual(filesystem[":slash_tmp"], "write")


if __name__ == "__main__":
    unittest.main()
