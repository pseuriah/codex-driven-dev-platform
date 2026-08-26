from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import fnmatch
import json
import os
from pathlib import Path
from typing import Literal, TypeAlias


Entry: TypeAlias = str | os.PathLike[str]
Access: TypeAlias = Literal["read", "write"]
EntryAccess: TypeAlias = tuple[Path, str]

DEFAULT_LEASE_REGISTRY = Path(".task-executor-leases.jsonl")
_GLOB_MAGIC = "*?["


class ReservedEntryError(ValueError):
    """Raised when an entry could include task-executor management state."""


@dataclass(frozen=True, slots=True)
class TaskspaceEntry:
    pattern: str
    access: Access

    @property
    def path(self) -> Path:
        return Path(self.pattern)


def patterns_may_overlap(left: str | Path, right: str | Path) -> bool:
    """Conservatively determine whether two project-relative globs may overlap.

    Literal mismatches and a wildcard that cannot match the opposing literal
    prove disjointness. Two wildcard components are considered overlapping
    unless a future, stricter matcher can prove otherwise.
    """

    left_parts = Path(left).parts
    right_parts = Path(right).parts

    left_index = 0
    right_index = 0
    while left_index < len(left_parts) and right_index < len(right_parts):
        left_part = left_parts[left_index]
        right_part = right_parts[right_index]

        if left_part == "**" or right_part == "**":
            return True

        left_magic = _has_magic(left_part)
        right_magic = _has_magic(right_part)
        if not left_magic and not right_magic:
            if left_part != right_part:
                return False
        elif left_magic and not right_magic:
            if not fnmatch.fnmatchcase(right_part, left_part):
                return False
        elif right_magic and not left_magic:
            if not fnmatch.fnmatchcase(left_part, right_part):
                return False

        left_index += 1
        right_index += 1

    # An exhausted side denotes either the same entry or an ancestor scope.
    return True


class TaskspaceGenerator:
    def __init__(
        self,
        project_root: Entry | None = None,
        *,
        reserved_entries: Sequence[Entry] = (DEFAULT_LEASE_REGISTRY,),
    ) -> None:
        root = Path.cwd() if project_root is None else Path(project_root)
        try:
            self.project_root = root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError(f"Invalid project root: {root}") from error

        if not self.project_root.is_dir():
            raise ValueError(f"Project root is not a directory: {root}")

        normalized_reserved = [
            self._normalize_entry(Path(entry)).as_posix()
            for entry in reserved_entries
        ]
        self.reserved_entries = tuple(normalized_reserved)

    def prepare_entries(
        self,
        readable_entries: Sequence[Entry],
        writable_entries: Sequence[Entry],
    ) -> tuple[TaskspaceEntry, ...]:
        unique: dict[str, Access] = {}

        for raw_entry in readable_entries:
            path = self._normalize_entry(Path(raw_entry))
            pattern = path.as_posix()
            unique.setdefault(pattern, "read")

        for raw_entry in writable_entries:
            path = self._normalize_entry(Path(raw_entry))
            pattern = path.as_posix()
            unique[pattern] = "write"

        symlinks = tuple(self._symlinks_in_project())
        prepared: list[TaskspaceEntry] = []
        for pattern, access in unique.items():
            self._validate_reserved_entry(pattern)
            self._validate_entry(Path(pattern), symlinks)
            self._validate_permission_pattern(pattern)
            prepared.append(TaskspaceEntry(pattern, access))

        return tuple(prepared)

    def generate_taskspace_config(
        self,
        readable_entries: Sequence[Entry],
        writable_entries: Sequence[Entry],
    ) -> str:
        entries = self.prepare_entries(readable_entries, writable_entries)
        workspace_entries = "\n".join(
            f"{_toml_string(entry.pattern)} = {_toml_string(entry.access)}"
            for entry in entries
        )

        config = """approval_policy = "never"
default_permissions = "task-executor"

[permissions.task-executor.filesystem]
":root" = "deny"
":minimal" = "read"
":tmpdir" = "deny"
":slash_tmp" = "deny"

[permissions.task-executor.filesystem.":workspace_roots"]
"""
        return config + workspace_entries + "\n"

    def generate_codex_overrides(
        self,
        readable_entries: Sequence[Entry],
        writable_entries: Sequence[Entry],
        *,
        private_temporary_directory: bool = False,
    ) -> tuple[str, ...]:
        entries = self.prepare_entries(readable_entries, writable_entries)
        workspace_entries = ", ".join(
            f"{_toml_string(entry.pattern)} = {_toml_string(entry.access)}"
            for entry in entries
        )
        filesystem = (
            "{ "
            '":root" = "deny", '
            '":minimal" = "read", '
            f'":tmpdir" = "{_temporary_access(private_temporary_directory)}", '
            f'":slash_tmp" = "{_temporary_access(private_temporary_directory)}", '
            f'":workspace_roots" = {{ {workspace_entries} }}'
            " }"
        )
        return (
            'approval_policy="never"',
            'default_permissions="task-executor"',
            f"permissions.task-executor={{ filesystem = {filesystem} }}",
        )

    def resolved_scopes(self, pattern: str | Path) -> tuple[Path, ...]:
        normalized = self._normalize_entry(Path(pattern))
        normalized_pattern = normalized.as_posix()
        matches = self._glob_matches(normalized_pattern)
        if not _has_magic(normalized_pattern):
            matches.append(self.project_root / normalized)
        else:
            static_prefix = _static_prefix(normalized)
            if static_prefix:
                matches.append(self.project_root.joinpath(*static_prefix))

        scopes: dict[Path, None] = {}
        for match in matches:
            try:
                resolved = match.resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise ValueError(
                    f"Taskspace entry cannot be resolved safely: {pattern}"
                ) from error
            self._ensure_within_project(resolved, normalized.as_posix())
            scopes[resolved] = None
        return tuple(scopes)

    def entries_may_overlap(
        self, left: str | Path, right: str | Path
    ) -> bool:
        left_pattern = self._normalize_entry(Path(left)).as_posix()
        right_pattern = self._normalize_entry(Path(right)).as_posix()
        if patterns_may_overlap(left_pattern, right_pattern):
            return True

        left_scopes = self.resolved_scopes(left_pattern)
        right_scopes = self.resolved_scopes(right_pattern)
        return any(
            _paths_overlap(left_scope, right_scope)
            for left_scope in left_scopes
            for right_scope in right_scopes
        )

    def _unique_entries(
        self, entry_access: Iterable[EntryAccess]
    ) -> list[EntryAccess]:
        readable: list[Path] = []
        writable: list[Path] = []
        for entry, access in entry_access:
            if access == "read":
                readable.append(entry)
            elif access == "write":
                writable.append(entry)
            else:
                raise ValueError(f"Unsupported access level: {access}")

        return [
            (Path(entry.pattern), entry.access)
            for entry in self.prepare_entries(readable, writable)
        ]

    def _normalize_entry(self, entry: Path) -> Path:
        if "\0" in os.fspath(entry):
            raise ValueError("Taskspace entry contains a null byte")
        if entry.is_absolute():
            raise ValueError(
                f"Taskspace entry must be relative to the project root: {entry}"
            )

        value = os.path.normpath(os.fspath(entry))
        if value == os.pardir or value.startswith(os.pardir + os.sep):
            raise ValueError(f"Taskspace entry points outside the project: {entry}")
        return Path(value)

    def _validate_reserved_entry(self, pattern: str) -> None:
        for reserved in self.reserved_entries:
            if patterns_may_overlap(pattern, reserved):
                raise ReservedEntryError(
                    f"Taskspace entry {pattern!r} may include reserved state: "
                    f"{reserved}"
                )

    @staticmethod
    def _validate_permission_pattern(pattern: str) -> None:
        if not _has_magic(pattern):
            return

        parts = Path(pattern).parts
        if (
            parts
            and parts[-1] == "**"
            and not any(_has_magic(part) for part in parts[:-1])
        ):
            return

        raise ValueError(
            f"Taskspace entry {pattern!r} is not supported by Codex "
            "filesystem permissions; use an exact path or a directory "
            "subtree ending in '/**'"
        )

    def _validate_entry(self, entry: Path, symlinks: Iterable[Path]) -> None:
        pattern = entry.as_posix()
        matches = self._glob_matches(pattern)

        prefix_parts = _static_prefix(entry)

        paths_to_check = list(matches)
        if prefix_parts:
            paths_to_check.append(self.project_root.joinpath(*prefix_parts))

        for path in paths_to_check:
            try:
                resolved = path.resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise ValueError(
                    f"Taskspace entry cannot be resolved safely: {pattern}"
                ) from error
            self._ensure_within_project(resolved, pattern)

        for link in symlinks:
            try:
                target = link.resolve(strict=False)
            except (OSError, RuntimeError) as error:
                raise ValueError(f"Unsafe symlink in taskspace entry: {link}") from error

            if self._is_within_project(target):
                continue

            relative_link = link.relative_to(self.project_root)
            if patterns_may_overlap(pattern, relative_link.as_posix()):
                raise ValueError(
                    f"Taskspace entry {pattern!r} includes a symlink outside "
                    f"the project: {relative_link}"
                )

    def _glob_matches(self, pattern: str) -> list[Path]:
        if pattern == ".":
            return [self.project_root]
        try:
            return list(self.project_root.glob(pattern, recurse_symlinks=False))
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError(f"Invalid taskspace glob: {pattern}") from error

    def _ensure_within_project(self, path: Path, entry: str) -> None:
        if not self._is_within_project(path):
            raise ValueError(
                f"Taskspace entry {entry!r} points outside the project: {path}"
            )

    def _is_within_project(self, path: Path) -> bool:
        try:
            path.relative_to(self.project_root)
        except ValueError:
            return False
        return True

    def _symlinks_in_project(self) -> Iterable[Path]:
        def raise_walk_error(error: OSError) -> None:
            raise ValueError(
                f"Cannot inspect project entries safely: {error.filename}"
            ) from error

        for directory, directory_names, file_names in os.walk(
            self.project_root, followlinks=False, onerror=raise_walk_error
        ):
            parent = Path(directory)
            for name in (*directory_names, *file_names):
                path = parent / name
                if path.is_symlink():
                    yield path

    @staticmethod
    def _glob_and_path_overlap(
        pattern_parts: tuple[str, ...], path_parts: tuple[str, ...]
    ) -> bool:
        return patterns_may_overlap(Path(*pattern_parts), Path(*path_parts))


def _has_magic(value: str) -> bool:
    return any(character in value for character in _GLOB_MAGIC)


def _static_prefix(pattern: Path) -> list[str]:
    prefix: list[str] = []
    for part in pattern.parts:
        if _has_magic(part):
            break
        prefix.append(part)
    return prefix


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _temporary_access(private_temporary_directory: bool) -> Access | Literal["deny"]:
    return "write" if private_temporary_directory else "deny"
