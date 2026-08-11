"""Plan and create a project Knowledge Harness scaffold without overwrites."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat

from didimlog.errors import DidimError, EXIT_POLICY
from didimlog.file_io import (
    UnsafePathError,
    read_regular_file_at,
    read_regular_file_at_with_stat,
    read_regular_file_beneath,
    replace_regular_file_at_if_unchanged,
)
from didimlog.locking import acquire_directory_lock, path_lock
from didimlog.project.resources import read_project_resource


_DIRECTORY_NAMES = (
    "knowledge",
    "knowledge/records",
    "knowledge/records/observation",
    "knowledge/records/experiment",
    "knowledge/records/evidence",
    "knowledge/raw",
    "knowledge/index",
    "knowledge/schema",
    "knowledge/active",
)
_RESOURCE_TARGETS = (
    ("knowledge/README.md", "README.md"),
    ("knowledge/POINTER.md", "POINTER.md"),
    ("knowledge/schema/record.schema.json", "record.schema.json"),
    ("knowledge/active/harness.md", "active-harness.md"),
)
_LEGACY_README_SIZE = 16_336
_LEGACY_README_SHA256 = (
    "6347d06afaab04f94c9f409717e0539add7252d000e5b0d51ea68d00036b0961"
)


@dataclass(frozen=True)
class ScaffoldPlan:
    """The complete, deterministic set of scaffold paths and file bytes."""

    directories: tuple[Path, ...]
    files: tuple[tuple[Path, bytes], ...]
    updates: tuple[tuple[Path, bytes, bytes], ...] = ()


def _policy_error(token: str, path: Path) -> DidimError:
    return DidimError(f"{token} {path}", exit_code=EXIT_POLICY)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _require_workspace(workspace: Path) -> Path:
    candidate = Path(workspace)
    metadata = _lstat(candidate)
    if metadata is None or not stat.S_ISDIR(metadata.st_mode):
        raise _policy_error("SCAFFOLD_WORKSPACE_INVALID", candidate)
    if stat.S_ISLNK(metadata.st_mode):
        raise _policy_error("PATH_ESCAPE", candidate)
    return Path(os.path.abspath(candidate))


def _expected_plan(workspace: Path) -> ScaffoldPlan:
    directories = tuple(workspace / relative for relative in _DIRECTORY_NAMES)
    files = tuple(
        (workspace / relative, read_project_resource(resource))
        for relative, resource in _RESOURCE_TARGETS
    )
    return ScaffoldPlan(directories=directories, files=files)


def _require_directory(path: Path) -> None:
    metadata = _lstat(path)
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise _policy_error("PATH_ESCAPE", path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise _policy_error("SCAFFOLD_CONFLICT", path)


def _require_file(
    path: Path,
    expected: bytes,
    alternate: bytes | None = None,
) -> None:
    metadata = _lstat(path)
    if metadata is None:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise _policy_error("PATH_ESCAPE", path)
    if not stat.S_ISREG(metadata.st_mode):
        raise _policy_error("SCAFFOLD_CONFLICT", path)
    maximum_bytes = len(expected)
    if alternate is not None:
        maximum_bytes = max(maximum_bytes, len(alternate))
    try:
        actual = read_regular_file_beneath(
            path.parent,
            path.name,
            maximum_bytes,
        )
    except UnsafePathError as error:
        raise _policy_error("SCAFFOLD_CONFLICT", path) from error
    if actual != expected and actual != alternate:
        raise _policy_error("SCAFFOLD_CONFLICT", path)


def _legacy_readme(path: Path, expected: bytes) -> bytes | None:
    metadata = _lstat(path)
    if metadata is None:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        raise _policy_error("PATH_ESCAPE", path)
    if not stat.S_ISREG(metadata.st_mode):
        raise _policy_error("SCAFFOLD_CONFLICT", path)
    try:
        actual = read_regular_file_beneath(
            path.parent,
            path.name,
            max(len(expected), _LEGACY_README_SIZE),
        )
    except UnsafePathError as error:
        raise _policy_error("SCAFFOLD_CONFLICT", path) from error
    if actual == expected:
        return None
    if (
        len(actual) == _LEGACY_README_SIZE
        and hashlib.sha256(actual).hexdigest() == _LEGACY_README_SHA256
    ):
        return actual
    raise _policy_error("SCAFFOLD_CONFLICT", path)


def _preflight(plan: ScaffoldPlan) -> None:
    workspace = plan.directories[0].parent
    _require_workspace(workspace)
    updates = {
        path: (original, intended)
        for path, original, intended in plan.updates
    }
    for path in plan.directories:
        _require_directory(path)
    for path, expected in plan.files:
        update = updates.get(path)
        if update is None:
            _require_file(path, expected)
        else:
            _require_file(path, update[0], update[1])


def _validate_plan(plan: ScaffoldPlan) -> Path:
    if not isinstance(plan, ScaffoldPlan) or not plan.directories:
        raise DidimError("SCAFFOLD_PLAN_INVALID", exit_code=EXIT_POLICY)

    workspace = plan.directories[0].parent
    canonical_workspace = _require_workspace(workspace)
    if workspace != canonical_workspace:
        raise _policy_error("PATH_ESCAPE", workspace)

    expected = _expected_plan(workspace)
    if plan.directories != expected.directories:
        raise DidimError("SCAFFOLD_PLAN_INVALID", exit_code=EXIT_POLICY)
    if plan.files != expected.files:
        raise DidimError("SCAFFOLD_PLAN_INVALID", exit_code=EXIT_POLICY)
    if not isinstance(plan.updates, tuple) or len(plan.updates) > 1:
        raise DidimError("SCAFFOLD_PLAN_INVALID", exit_code=EXIT_POLICY)
    if plan.updates:
        readme_path, current_readme = expected.files[0]
        update = plan.updates[0]
        if not isinstance(update, tuple) or len(update) != 3:
            raise DidimError("SCAFFOLD_PLAN_INVALID", exit_code=EXIT_POLICY)
        path, original, intended = update
        if (
            path != readme_path
            or not isinstance(original, bytes)
            or intended != current_readme
            or len(original) != _LEGACY_README_SIZE
            or hashlib.sha256(original).hexdigest() != _LEGACY_README_SHA256
        ):
            raise DidimError("SCAFFOLD_PLAN_INVALID", exit_code=EXIT_POLICY)
    return workspace


def plan_scaffold(workspace: Path) -> ScaffoldPlan:
    """Return a write-free scaffold plan after validating all existing targets."""
    canonical_workspace = _require_workspace(workspace)
    expected = _expected_plan(canonical_workspace)
    for path in expected.directories:
        _require_directory(path)
    updates = []
    for index, (path, content) in enumerate(expected.files):
        if index == 0:
            legacy = _legacy_readme(path, content)
            if legacy is not None:
                updates.append((path, legacy, content))
        else:
            _require_file(path, content)
    return ScaffoldPlan(
        directories=expected.directories,
        files=expected.files,
        updates=tuple(updates),
    )


def _directory_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    return flags | getattr(os, "O_NOFOLLOW", 0)


def _open_directory_entry(
    parent_descriptor: int,
    path: Path,
) -> tuple[int, tuple[int, int]]:
    try:
        descriptor = os.open(
            path.name,
            _directory_flags(),
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise _policy_error("PATH_ESCAPE", path) from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise _policy_error("SCAFFOLD_CONFLICT", path)
    return descriptor, (metadata.st_dev, metadata.st_ino)


def _validate_open_directories(
    workspace: Path,
    target_parent: Path,
    descriptors: dict[Path, int],
    identities: dict[Path, tuple[int, int]],
) -> None:
    workspace_metadata = _lstat(workspace)
    if (
        workspace_metadata is None
        or stat.S_ISLNK(workspace_metadata.st_mode)
        or (workspace_metadata.st_dev, workspace_metadata.st_ino)
        != identities[workspace]
    ):
        raise _policy_error("PATH_ESCAPE", workspace)

    chain = []
    current = target_parent
    while current != workspace:
        chain.append(current)
        current = current.parent
    for path in reversed(chain):
        descriptor, identity = _open_directory_entry(descriptors[path.parent], path)
        os.close(descriptor)
        if identity != identities[path]:
            raise _policy_error("PATH_ESCAPE", path)


def _entry_identity(
    parent_descriptor: int,
    name: str,
    *,
    directory: bool,
) -> tuple[int, int] | None:
    flags = _directory_flags() if directory else (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_type(metadata.st_mode):
            return None
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def _file_revision(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _entry_revision(
    parent_descriptor: int,
    name: str,
) -> tuple[int, ...] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return _file_revision(metadata)
    finally:
        os.close(descriptor)


def _rollback(
    created_files: list[tuple[str, tuple[int, ...], int]],
    created_directories: list[tuple[str, tuple[int, int], int]],
) -> None:
    for name, revision, parent_descriptor in reversed(created_files):
        try:
            if _entry_revision(parent_descriptor, name) == revision:
                os.unlink(name, dir_fd=parent_descriptor)
        except OSError:
            pass
        finally:
            os.close(parent_descriptor)
    for name, identity, parent_descriptor in reversed(created_directories):
        try:
            if _entry_identity(parent_descriptor, name, directory=True) == identity:
                os.rmdir(name, dir_fd=parent_descriptor)
        except OSError:
            pass
        finally:
            os.close(parent_descriptor)


def _require_file_at(
    parent_descriptor: int,
    path: Path,
    expected: bytes,
) -> None:
    try:
        actual = read_regular_file_at(
            parent_descriptor,
            path.name,
            len(expected),
        )
    except UnsafePathError as error:
        raise _policy_error("SCAFFOLD_CONFLICT", path) from error
    if actual != expected:
        raise _policy_error("SCAFFOLD_CONFLICT", path)


def _create_file(
    path: Path,
    content: bytes,
    parent_descriptor: int,
) -> tuple[int, ...] | None:
    parent_metadata = _lstat(path.parent)
    opened_parent = os.fstat(parent_descriptor)
    if (
        parent_metadata is None
        or stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
        or (parent_metadata.st_dev, parent_metadata.st_ino)
        != (opened_parent.st_dev, opened_parent.st_ino)
    ):
        raise _policy_error("PATH_ESCAPE", path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(
            path.name,
            flags,
            0o666,
            dir_fd=parent_descriptor,
        )
    except FileExistsError:
        _require_file_at(parent_descriptor, path, content)
        return None
    except OSError as error:
        raise _policy_error("SCAFFOLD_CREATE_FAILED", path) from error

    identity: tuple[int, int]
    revision: tuple[int, ...]
    try:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            revision = _file_revision(os.fstat(stream.fileno()))
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            if _entry_identity(
                parent_descriptor,
                path.name,
                directory=False,
            ) == identity:
                os.unlink(path.name, dir_fd=parent_descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise _policy_error("SCAFFOLD_CREATE_FAILED", path) from error
    return revision


def _update_scaffold_file_at(
    parent_descriptor: int,
    path: Path,
    original: bytes,
    intended: bytes,
) -> None:
    """Conditionally update one regular file through a pinned directory."""
    lock_descriptor = acquire_directory_lock(parent_descriptor)
    try:
        maximum_bytes = max(len(original), len(intended))
        current, current_info = read_regular_file_at_with_stat(
            parent_descriptor,
            path.name,
            maximum_bytes,
        )
        if current == intended:
            return
        if current != original:
            raise ValueError("scaffold target changed after planning")
        replaced = replace_regular_file_at_if_unchanged(
            parent_descriptor,
            path.name,
            original,
            intended,
            stat.S_IMODE(current_info.st_mode),
            expected_info=current_info,
        )
        if replaced:
            return
        current, _ = read_regular_file_at_with_stat(
            parent_descriptor,
            path.name,
            maximum_bytes,
        )
        if current != intended:
            raise ValueError("scaffold target changed before write")
    finally:
        os.close(lock_descriptor)


def _apply_scaffold_updates(
    plan: ScaffoldPlan,
    knowledge_descriptor: int | None = None,
) -> None:
    """Apply validated updates through a pinned, locked knowledge directory."""
    if knowledge_descriptor is None:
        workspace = _validate_plan(plan)
    else:
        workspace = plan.directories[0].parent
    lock_context = (
        path_lock(workspace / "knowledge")
        if knowledge_descriptor is None
        else nullcontext(knowledge_descriptor)
    )
    with lock_context as pinned_knowledge:
        for path, original, intended in plan.updates:
            try:
                _update_scaffold_file_at(
                    pinned_knowledge,
                    path,
                    original,
                    intended,
                )
            except (OSError, UnsafePathError, ValueError) as error:
                raise _policy_error("SCAFFOLD_CONFLICT", path) from error


def apply_scaffold(plan: ScaffoldPlan) -> None:
    """Create missing targets, then conditionally apply validated updates."""
    workspace = _validate_plan(plan)
    _preflight(plan)
    update_paths = {path for path, _, _ in plan.updates}

    try:
        workspace_descriptor = os.open(workspace, _directory_flags())
    except OSError as error:
        raise _policy_error("PATH_ESCAPE", workspace) from error
    workspace_metadata = os.fstat(workspace_descriptor)
    descriptors = {workspace: workspace_descriptor}
    identities = {
        workspace: (workspace_metadata.st_dev, workspace_metadata.st_ino)
    }
    created_directories: list[tuple[str, tuple[int, int], int]] = []
    created_files: list[tuple[str, tuple[int, ...], int]] = []
    try:
        for path in plan.directories:
            _validate_open_directories(
                workspace,
                path.parent,
                descriptors,
                identities,
            )
            parent_descriptor = descriptors[path.parent]
            created = False
            try:
                os.mkdir(path.name, dir_fd=parent_descriptor)
                created = True
            except FileExistsError:
                pass
            except OSError as error:
                raise _policy_error("SCAFFOLD_CREATE_FAILED", path) from error

            descriptor, identity = _open_directory_entry(parent_descriptor, path)
            descriptors[path] = descriptor
            identities[path] = identity
            if created:
                created_directories.append(
                    (path.name, identity, os.dup(parent_descriptor))
                )

        for path, content in plan.files:
            if path in update_paths:
                continue
            _validate_open_directories(
                workspace,
                path.parent,
                descriptors,
                identities,
            )
            parent_descriptor = descriptors[path.parent]
            revision = _create_file(path, content, parent_descriptor)
            if revision is not None:
                created_files.append(
                    (path.name, revision, os.dup(parent_descriptor))
                )
        if plan.updates:
            _apply_scaffold_updates(
                plan,
                descriptors[workspace / "knowledge"],
            )
            _validate_open_directories(
                workspace,
                workspace / "knowledge",
                descriptors,
                identities,
            )
    except BaseException:
        _rollback(created_files, created_directories)
        raise
    else:
        for _, _, descriptor in created_files:
            os.close(descriptor)
        for _, _, descriptor in created_directories:
            os.close(descriptor)
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)
