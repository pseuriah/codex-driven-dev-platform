from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import TypeAlias

from .taskspace import TaskspaceGenerator


Fingerprint: TypeAlias = tuple[str, int | str]
ScopeSnapshot: TypeAlias = tuple[tuple[str, Fingerprint], ...]


class StagedTaskspaceError(RuntimeError):
    """Raised when a staged taskspace cannot be committed safely."""


class StagedTaskspaceConflictError(StagedTaskspaceError):
    """Raised when leased project data changed outside the executor."""


@dataclass
class StagedTaskspace:
    """Disposable workspace used when Codex cannot mount writable files.

    Codex receives only the leased entries. After a successful run, only the
    writable scopes are synchronized back to the original project.
    """

    project_root: Path
    readable_entries: tuple[str, ...]
    writable_entries: tuple[str, ...]

    def __post_init__(self) -> None:
        self.project_root = self.project_root.resolve(strict=True)
        self._temporary_directory, self._temporary_parent = (
            _create_temporary_directory(self.project_root)
        )
        self.workspace_root = Path(self._temporary_directory.name)
        self.writable_entries = _minimal_writable_scopes(self.writable_entries)
        self._exact_targets: dict[str, Path] = {}
        self._snapshots = {
            pattern: _snapshot_scope(self.project_root, pattern)
            for pattern in self.writable_entries
        }
        self._materialize()
        self._initialize_repository()

    @staticmethod
    def required(writable_entries: tuple[str, ...]) -> bool:
        return any(not _is_subtree(pattern) for pattern in writable_entries)

    def codex_overrides(self) -> tuple[str, ...]:
        # The taskspace contains only leased inputs and commit() copies back only
        # leased writable scopes. Making this disposable projection writable as
        # one root avoids Codex's Linux sandbox having to synthesize writable
        # children beneath a read-only root (which fails for exact file roots).
        generator = TaskspaceGenerator(
            self.workspace_root,
            reserved_entries=(),
        )
        return generator.generate_codex_overrides(
            (),
            (".",),
            private_temporary_directory=True,
        )

    def commit(self) -> None:
        for pattern, expected in self._snapshots.items():
            current = _snapshot_scope(self.project_root, pattern)
            if current != expected:
                raise StagedTaskspaceConflictError(
                    f"Leased entry changed while the task was running: {pattern}"
                )

        for pattern in self.writable_entries:
            if _is_subtree(pattern):
                _sync_subtree(
                    self.workspace_root / _subtree_base(pattern),
                    self.project_root / _subtree_base(pattern),
                )
            else:
                _sync_exact(
                    self._exact_targets[pattern],
                    self.project_root / pattern,
                )

    def close(self) -> None:
        self._temporary_directory.cleanup()
        try:
            self._temporary_parent.rmdir()
        except OSError:
            pass

    def __enter__(self) -> StagedTaskspace:
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()

    def _materialize(self) -> None:
        for pattern in sorted(
            dict.fromkeys(self.readable_entries),
            key=lambda value: (value.count("/"), value),
        ):
            _copy_scope(
                self.project_root,
                self.workspace_root,
                pattern,
                writable=False,
            )

        for pattern in self.writable_entries:
            if _is_subtree(pattern):
                _copy_scope(
                    self.project_root,
                    self.workspace_root,
                    pattern,
                    writable=True,
                )
            else:
                self._materialize_exact_write(pattern)

    def _materialize_exact_write(self, pattern: str) -> None:
        source = self.project_root / pattern
        if source.is_symlink():
            raise StagedTaskspaceError(
                f"Writable file cannot be a symlink: {pattern}"
            )
        if source.is_dir():
            raise StagedTaskspaceError(
                f"Writable directory must use a subtree ending in '/**': {pattern}"
            )
        if source.exists() and not source.is_file():
            raise StagedTaskspaceError(f"Unsupported taskspace entry: {pattern}")

        target = self.workspace_root / pattern
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, target)
        self._exact_targets[pattern] = target

    def _initialize_repository(self) -> None:
        git = shutil.which("git")
        if git is None:
            return
        commands = (
            (git, "init", "-q"),
            (git, "add", "-A"),
            (
                git,
                "-c",
                "user.name=task-executor",
                "-c",
                "user.email=task-executor@localhost",
                "commit",
                "-qm",
                "taskspace baseline",
                "--allow-empty",
            ),
        )
        try:
            for command in commands:
                subprocess.run(
                    command,
                    cwd=self.workspace_root,
                    check=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except (OSError, subprocess.CalledProcessError):
            shutil.rmtree(self.workspace_root / ".git", ignore_errors=True)


def _copy_scope(
    source_root: Path,
    destination_root: Path,
    pattern: str,
    *,
    writable: bool,
) -> None:
    relative = _subtree_base(pattern) if _is_subtree(pattern) else Path(pattern)
    source = source_root / relative
    destination = destination_root / relative

    if _is_subtree(pattern):
        if source.is_symlink():
            if writable:
                raise StagedTaskspaceError(
                    f"Writable subtree cannot be a symlink: {pattern}"
                )
            source = source.resolve(strict=True)
        if source.exists() and not source.is_dir():
            raise StagedTaskspaceError(
                f"Subtree entry is not a directory: {pattern}"
            )
        if writable and source.is_dir():
            unsafe_link = next(
                (path for path in source.rglob("*") if path.is_symlink()),
                None,
            )
            if unsafe_link is not None:
                raise StagedTaskspaceError(
                    "Writable subtree contains a symlink that cannot be "
                    f"staged safely: {unsafe_link.relative_to(source_root)}"
                )
        if source.is_dir():
            shutil.copytree(
                source,
                destination,
                dirs_exist_ok=True,
                symlinks=False,
            )
        elif writable:
            destination.mkdir(parents=True, exist_ok=True)
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        if writable:
            raise StagedTaskspaceError(
                f"Writable file cannot be a symlink: {pattern}"
            )
        source = source.resolve(strict=True)
    if source.is_file():
        shutil.copy2(source, destination)
    elif source.is_dir():
        if writable:
            raise StagedTaskspaceError(
                f"Writable directory must use a subtree ending in '/**': {pattern}"
            )
        destination.mkdir(exist_ok=True)
    elif source.exists():
        raise StagedTaskspaceError(f"Unsupported taskspace entry: {pattern}")


def _snapshot_scope(root: Path, pattern: str) -> ScopeSnapshot:
    relative = _subtree_base(pattern) if _is_subtree(pattern) else Path(pattern)
    path = root / relative
    if not _is_subtree(pattern):
        return ((".", _fingerprint(path)),)
    if not path.exists() and not path.is_symlink():
        return ((".", ("absent", 0)),)
    if not path.is_dir() or path.is_symlink():
        return ((".", _fingerprint(path)),)

    snapshots: list[tuple[str, Fingerprint]] = [(".", _fingerprint(path))]
    for directory, directory_names, file_names in os.walk(
        path,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        for name in sorted(directory_names + file_names):
            child = current / name
            snapshots.append(
                (child.relative_to(path).as_posix(), _fingerprint(child))
            )
    return tuple(sorted(snapshots))


def _fingerprint(path: Path) -> Fingerprint:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ("absent", 0)
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return ("symlink", os.readlink(path))
    if stat.S_ISDIR(metadata.st_mode):
        return ("directory", mode)
    if stat.S_ISREG(metadata.st_mode):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return ("file", f"{mode}:{digest.hexdigest()}")
    return ("unsupported", mode)


def _sync_exact(source: Path, destination: Path) -> None:
    if source.is_symlink() or (source.exists() and not source.is_file()):
        raise StagedTaskspaceError(
            f"Writable file became an unsupported type: {destination}"
        )
    if not source.exists():
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        return
    _atomic_copy(source, destination)


def _sync_subtree(source: Path, destination: Path) -> None:
    if source.is_symlink():
        raise StagedTaskspaceError(
            f"Writable subtree became a symlink: {destination}"
        )
    if not source.exists():
        if destination.exists():
            shutil.rmtree(destination)
        return
    if not source.is_dir():
        raise StagedTaskspaceError(
            f"Writable subtree became a non-directory: {destination}"
        )

    for path in source.rglob("*"):
        if path.is_symlink():
            raise StagedTaskspaceError(
                f"Writable subtree contains a symlink: {path.relative_to(source)}"
            )

    destination.mkdir(parents=True, exist_ok=True)
    source_paths = {
        path.relative_to(source).as_posix(): path for path in source.rglob("*")
    }
    destination_paths = {
        path.relative_to(destination).as_posix(): path
        for path in destination.rglob("*")
    }

    for relative, source_path in sorted(source_paths.items()):
        destination_path = destination / relative
        if source_path.is_dir():
            if destination_path.exists() and not destination_path.is_dir():
                destination_path.unlink()
            destination_path.mkdir(parents=True, exist_ok=True)
        elif source_path.is_file():
            if destination_path.is_dir():
                shutil.rmtree(destination_path)
            _atomic_copy(source_path, destination_path)
        else:
            raise StagedTaskspaceError(
                f"Writable subtree contains an unsupported entry: {relative}"
            )

    for relative, destination_path in sorted(
        destination_paths.items(),
        key=lambda item: item[0].count("/"),
        reverse=True,
    ):
        if relative in source_paths:
            continue
        if destination_path.is_dir() and not destination_path.is_symlink():
            destination_path.rmdir()
        else:
            destination_path.unlink()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".task-executor-write-",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _minimal_writable_scopes(patterns: tuple[str, ...]) -> tuple[str, ...]:
    minimal: list[str] = []
    for pattern in sorted(dict.fromkeys(patterns), key=lambda value: (value.count("/"), value)):
        if any(_subtree_contains(existing, pattern) for existing in minimal):
            continue
        minimal.append(pattern)
    return tuple(minimal)


def _subtree_contains(subtree: str, pattern: str) -> bool:
    if not _is_subtree(subtree):
        return False
    base = _subtree_base(subtree).as_posix()
    return pattern == base or pattern.startswith(base.rstrip("/") + "/")


def _is_subtree(pattern: str) -> bool:
    return pattern == "**" or pattern.endswith("/**")


def _subtree_base(pattern: str) -> Path:
    if pattern == "**":
        return Path(".")
    return Path(pattern[:-3])


def _create_temporary_directory(
    project_root: Path,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    candidates: list[Path] = []
    if hasattr(os, "getuid"):
        runtime_directory = Path("/run/user") / str(os.getuid())
        if runtime_directory.is_dir():
            candidates.append(runtime_directory / "codex-driven-dev-platform")
    candidates.extend(
        (
            Path("/var/tmp/codex-driven-dev-platform"),
            project_root.parent / ".codex-driven-dev-platform-taskspaces",
        )
    )

    errors: list[OSError] = []
    for parent in candidates:
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = parent.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise OSError(f"Unsafe staging parent: {parent}")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise OSError(f"Staging parent has a different owner: {parent}")
            directory = tempfile.TemporaryDirectory(
                prefix="taskspace-",
                dir=parent,
            )
            return directory, parent
        except OSError as error:
            errors.append(error)

    details = "; ".join(str(error) for error in errors)
    raise StagedTaskspaceError(
        f"Could not create an isolated taskspace: {details}"
    )
