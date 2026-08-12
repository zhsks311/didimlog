"""Project evidence artifact path and binding verification."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath

from didimlog.project.git_exclude import _git_environment
from didimlog.project.record import (
    ARTIFACT_PATH_MAX,
    GIT_OID_RE,
    GitUnavailable,
    PolicyError,
    SchemaError,
)

_GIT_TIMEOUT_SECONDS = 10
_GIT_METADATA_MAX_BYTES = 4096
_GIT_FD_EXEC = (
    "import os,sys;"
    "fd=int(sys.argv[1]);git=sys.argv[2];"
    "os.fchdir(fd);"
    "os.execv(git,[git,'--git-dir=..',*sys.argv[3:]])"
)
_HASH_CHUNK_SIZE = 64 * 1024


def _invalid_artifact_path(record_id: str) -> SchemaError:
    return SchemaError("INVALID_ARTIFACT_PATH {}".format(record_id))


def _artifact_path_escape(record_id: str, artifact_path: object) -> PolicyError:
    return PolicyError(
        "ARTIFACT_PATH_ESCAPE {} {}".format(record_id, artifact_path)
    )


def check_artifact_path_format(value, record_id):
    """Return a canonical project-relative path or raise a schema error.

    This validates the path scalar used by either exclusive evidence binding
    mode. Filesystem placement below ``knowledge/raw/`` is enforced separately
    by :func:`check_artifact_path_policy`.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= ARTIFACT_PATH_MAX:
        raise _invalid_artifact_path(record_id)
    if "\\" in value or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise _invalid_artifact_path(record_id)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _invalid_artifact_path(record_id) from error

    path = PurePosixPath(value)
    parts = value.split("/")
    if (
        path.is_absolute()
        or value.endswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or path.as_posix() != value
    ):
        raise _invalid_artifact_path(record_id)
    return value


def check_artifact_path_policy(
    workspace,
    artifact_path,
    record_id,
    *,
    workspace_descriptor: int | None = None,
):
    """Return a ``knowledge/raw/`` path that cannot escape through a symlink.

    The caller must first apply :func:`check_artifact_path_format` when
    validating record schema. Policy failures deliberately remain exit-code 3
    errors when this lower-level function is called directly. A supplied
    workspace descriptor is borrowed and pins the root for descriptor-relative
    artifact access.
    """
    if not isinstance(artifact_path, str):
        raise _artifact_path_escape(record_id, artifact_path)
    try:
        artifact_path.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _artifact_path_escape(record_id, artifact_path) from error

    parts = artifact_path.split("/")
    if (
        os.path.isabs(artifact_path)
        or not 1 <= len(artifact_path) <= ARTIFACT_PATH_MAX
        or len(parts) < 3
        or parts[:2] != ["knowledge", "raw"]
        or any(part in ("", ".", "..") for part in parts)
        or "\\" in artifact_path
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in artifact_path
        )
    ):
        raise _artifact_path_escape(record_id, artifact_path)

    try:
        workspace_path = os.path.abspath(os.fspath(workspace))
        full_path = os.path.abspath(os.path.join(workspace_path, *parts))
        if os.path.commonpath((workspace_path, full_path)) != workspace_path:
            raise _artifact_path_escape(record_id, artifact_path)

        if workspace_descriptor is not None:
            descriptor = _open_workspace(
                workspace,
                record_id,
                artifact_path,
                workspace_descriptor,
            )
            try:
                for index, part in enumerate(parts):
                    try:
                        linked = os.stat(
                            part,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        break
                    if stat.S_ISLNK(linked.st_mode):
                        raise _artifact_path_escape(record_id, artifact_path)
                    if index == len(parts) - 1:
                        break
                    child: int | None = None
                    try:
                        child = os.open(
                            part,
                            _directory_flags(),
                            dir_fd=descriptor,
                        )
                        opened = os.fstat(child)
                    except BaseException:
                        if child is not None:
                            os.close(child)
                        raise
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or opened.st_dev != linked.st_dev
                        or opened.st_ino != linked.st_ino
                    ):
                        os.close(child)
                        raise _artifact_path_escape(record_id, artifact_path)
                    os.close(descriptor)
                    descriptor = child
            finally:
                os.close(descriptor)
            return full_path

        if os.path.islink(workspace_path):
            raise _artifact_path_escape(record_id, artifact_path)
        candidate = workspace_path
        for part in parts:
            candidate = os.path.join(candidate, part)
            try:
                candidate_stat = os.lstat(candidate)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(candidate_stat.st_mode):
                raise _artifact_path_escape(record_id, artifact_path)
    except PolicyError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise _artifact_path_escape(record_id, artifact_path) from error

    return full_path
def _artifact_missing(record_id: str, artifact_path: str) -> PolicyError:
    return PolicyError(
        "ARTIFACT_MISSING {} {}".format(record_id, artifact_path)
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_workspace(
    workspace,
    record_id: str,
    artifact_path: str,
    workspace_descriptor: int | None = None,
) -> int:
    absolute = os.path.abspath(os.fspath(workspace))
    if workspace_descriptor is not None:
        descriptor: int | None = None
        try:
            descriptor = os.dup(workspace_descriptor)
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("workspace descriptor is not a directory")
        except (OSError, TypeError, ValueError) as error:
            if descriptor is not None:
                os.close(descriptor)
            raise _artifact_path_escape(record_id, artifact_path) from error
        return descriptor
    try:
        linked = os.lstat(absolute)
        descriptor = os.open(absolute, _directory_flags())
    except (OSError, TypeError, ValueError) as error:
        raise _artifact_path_escape(record_id, artifact_path) from error
    opened = os.fstat(descriptor)
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != linked.st_dev
        or opened.st_ino != linked.st_ino
    ):
        os.close(descriptor)
        raise _artifact_path_escape(record_id, artifact_path)
    return descriptor


def _open_artifact(
    workspace,
    artifact_path: str,
    record_id: str,
    workspace_descriptor: int | None = None,
) -> int:
    parts = artifact_path.split("/")
    descriptor = _open_workspace(
        workspace,
        record_id,
        artifact_path,
        workspace_descriptor,
    )
    try:
        for part in parts[:-1]:
            try:
                linked = os.stat(
                    part,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError as error:
                raise _artifact_missing(record_id, artifact_path) from error
            if stat.S_ISLNK(linked.st_mode):
                raise _artifact_path_escape(record_id, artifact_path)
            try:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
            except OSError as error:
                raise _artifact_missing(record_id, artifact_path) from error
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or opened.st_dev != linked.st_dev
                or opened.st_ino != linked.st_ino
            ):
                os.close(child)
                raise _artifact_path_escape(record_id, artifact_path)
            os.close(descriptor)
            descriptor = child

        name = parts[-1]
        try:
            linked = os.stat(
                name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError as error:
            raise _artifact_missing(record_id, artifact_path) from error
        if stat.S_ISLNK(linked.st_mode):
            raise _artifact_path_escape(record_id, artifact_path)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            artifact_descriptor = os.open(
                name,
                flags,
                dir_fd=descriptor,
            )
        except OSError as error:
            raise _artifact_missing(record_id, artifact_path) from error
        opened = os.fstat(artifact_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
        ):
            os.close(artifact_descriptor)
            raise _artifact_missing(record_id, artifact_path)
        return artifact_descriptor
    finally:
        os.close(descriptor)




def verify_artifact_local(
    workspace,
    artifact_path,
    expected_sha256,
    record_id,
    *,
    workspace_descriptor: int | None = None,
):
    """Verify the sole local-mode binding to a regular file and SHA-256.

    The corresponding evidence record must select exactly this mode (an
    ``artifact_sha256`` and no ``artifact_git``); ``record.validate_frontmatter``
    owns that schema invariant.
    """
    if workspace_descriptor is None:
        full_path = check_artifact_path_policy(
            workspace,
            artifact_path,
            record_id,
        )
    else:
        full_path = check_artifact_path_policy(
            workspace,
            artifact_path,
            record_id,
            workspace_descriptor=workspace_descriptor,
        )
    descriptor = _open_artifact(
        workspace,
        artifact_path,
        record_id,
        workspace_descriptor,
    )

    digest = hashlib.sha256()
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if digest.hexdigest() != expected_sha256:
        raise PolicyError(
            "ARTIFACT_DIGEST_MISMATCH {} {}".format(record_id, artifact_path)
        )
    return full_path


def _run_git(git: str, workspace, arguments: list[str], record_id: str):
    try:
        return subprocess.run(
            [git, "-C", os.fspath(workspace), *arguments],
            env=_git_environment(),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        raise GitUnavailable("GIT_UNVERIFIABLE {}".format(record_id)) from error


class _GitMetadataNotRepository(Exception):
    pass


class _GitMetadataUnverifiable(Exception):
    pass


def _same_entry(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _open_git_directory_component(descriptor: int, name: bytes) -> int:
    try:
        linked = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError as error:
        raise _GitMetadataNotRepository from error
    except (OSError, TypeError, ValueError) as error:
        raise _GitMetadataUnverifiable from error
    if not stat.S_ISDIR(linked.st_mode):
        raise _GitMetadataNotRepository
    child: int | None = None
    try:
        child = os.open(name, _directory_flags(), dir_fd=descriptor)
        opened = os.fstat(child)
    except (OSError, TypeError, ValueError) as error:
        if child is not None:
            os.close(child)
        raise _GitMetadataUnverifiable from error
    if not stat.S_ISDIR(opened.st_mode) or not _same_entry(linked, opened):
        os.close(child)
        raise _GitMetadataUnverifiable
    return child


def _open_git_directory_path(descriptor: int, path: bytes) -> int:
    if not path or b"\0" in path or b"\r" in path or b"\n" in path:
        raise _GitMetadataNotRepository
    absolute = path.startswith(b"/")
    parts = path.split(b"/")
    if absolute:
        parts = parts[1:]
    if any(not part for part in parts):
        raise _GitMetadataNotRepository
    try:
        current = (
            os.open(b"/", _directory_flags()) if absolute else os.dup(descriptor)
        )
    except (OSError, TypeError, ValueError) as error:
        raise _GitMetadataUnverifiable from error
    try:
        for part in parts:
            child = _open_git_directory_component(current, part)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _read_stable_git_metadata(descriptor: int) -> bytes:
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > _GIT_METADATA_MAX_BYTES
        ):
            raise _GitMetadataNotRepository
        chunks = []
        remaining = _GIT_METADATA_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, _HASH_CHUNK_SIZE))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
    except _GitMetadataNotRepository:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise _GitMetadataUnverifiable from error
    if (
        not _same_entry(before, after)
        or before.st_mode != after.st_mode
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    ):
        raise _GitMetadataUnverifiable
    if len(content) > _GIT_METADATA_MAX_BYTES:
        raise _GitMetadataNotRepository
    return content


def _open_stable_git_metadata_file(
    descriptor: int,
    name: bytes,
    linked: os.stat_result,
) -> int:
    if not stat.S_ISREG(linked.st_mode):
        raise _GitMetadataNotRepository
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    metadata: int | None = None
    try:
        metadata = os.open(name, flags, dir_fd=descriptor)
        opened = os.fstat(metadata)
    except (OSError, TypeError, ValueError) as error:
        if metadata is not None:
            os.close(metadata)
        raise _GitMetadataUnverifiable from error
    if not stat.S_ISREG(opened.st_mode) or not _same_entry(linked, opened):
        os.close(metadata)
        raise _GitMetadataUnverifiable
    return metadata


def _single_git_metadata_line(content: bytes) -> bytes:
    if content.endswith(b"\n"):
        content = content[:-1]
    if not content or b"\0" in content or b"\r" in content or b"\n" in content:
        raise _GitMetadataNotRepository
    return content


def _open_pinned_git_directory(workspace_descriptor: int) -> int:
    name = b".git"
    try:
        linked = os.stat(
            name,
            dir_fd=workspace_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise _GitMetadataNotRepository from error
    except (OSError, TypeError, ValueError) as error:
        raise _GitMetadataUnverifiable from error
    if stat.S_ISDIR(linked.st_mode):
        git_directory: int | None = None
        try:
            git_directory = os.open(
                name,
                _directory_flags(),
                dir_fd=workspace_descriptor,
            )
            opened = os.fstat(git_directory)
        except (OSError, TypeError, ValueError) as error:
            if git_directory is not None:
                os.close(git_directory)
            raise _GitMetadataUnverifiable from error
        if not stat.S_ISDIR(opened.st_mode) or not _same_entry(linked, opened):
            os.close(git_directory)
            raise _GitMetadataUnverifiable
        return git_directory
    metadata = _open_stable_git_metadata_file(
        workspace_descriptor,
        name,
        linked,
    )
    try:
        line = _single_git_metadata_line(_read_stable_git_metadata(metadata))
    finally:
        os.close(metadata)
    prefix = b"gitdir: "
    if not line.startswith(prefix) or len(line) == len(prefix):
        raise _GitMetadataNotRepository
    return _open_git_directory_path(workspace_descriptor, line[len(prefix) :])


def _directory_revision(descriptor: int) -> tuple[int, ...]:
    current = os.fstat(descriptor)
    if not stat.S_ISDIR(current.st_mode):
        raise _GitMetadataUnverifiable
    return (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )


def _reject_git_alternates_in_info(info_descriptor: int) -> None:
    for name in (b"alternates", b"http-alternates"):
        try:
            os.stat(
                name,
                dir_fd=info_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        except (OSError, TypeError, ValueError) as error:
            raise _GitMetadataUnverifiable from error
        raise _GitMetadataUnverifiable


@dataclass(frozen=True)
class _PinnedGitObjectDatabase:
    descriptor: int
    watch_descriptor: int
    object_database_revision: tuple[int, ...]
    info_exists: bool
    watch_revision: tuple[int, ...]

    def close(self) -> None:
        os.close(self.watch_descriptor)
        os.close(self.descriptor)


def _validate_pinned_git_object_database(
    object_database: _PinnedGitObjectDatabase,
) -> None:
    try:
        if (
            _directory_revision(object_database.descriptor)
            != object_database.object_database_revision
        ):
            raise _GitMetadataUnverifiable
        if (
            _directory_revision(object_database.watch_descriptor)
            != object_database.watch_revision
        ):
            raise _GitMetadataUnverifiable
        if object_database.info_exists:
            _reject_git_alternates_in_info(
                object_database.watch_descriptor,
            )
        else:
            try:
                os.stat(
                    b"info",
                    dir_fd=object_database.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise _GitMetadataUnverifiable
        if (
            _directory_revision(object_database.watch_descriptor)
            != object_database.watch_revision
        ):
            raise _GitMetadataUnverifiable
        if (
            _directory_revision(object_database.descriptor)
            != object_database.object_database_revision
        ):
            raise _GitMetadataUnverifiable
    except _GitMetadataUnverifiable:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise _GitMetadataUnverifiable from error


def _open_pinned_objects_directory(
    repository_descriptor: int,
) -> _PinnedGitObjectDatabase:
    object_database = _open_git_directory_component(
        repository_descriptor,
        b"objects",
    )
    watch_descriptor: int | None = None
    try:
        try:
            object_database_revision = _directory_revision(object_database)
        except (OSError, TypeError, ValueError) as error:
            raise _GitMetadataUnverifiable from error
        try:
            linked = os.stat(
                b"info",
                dir_fd=object_database,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            try:
                watch_descriptor = os.dup(object_database)
            except (OSError, TypeError, ValueError) as error:
                raise _GitMetadataUnverifiable from error
            info_exists = False
        except (OSError, TypeError, ValueError) as error:
            raise _GitMetadataUnverifiable from error
        else:
            if not stat.S_ISDIR(linked.st_mode):
                raise _GitMetadataUnverifiable
            watch_descriptor = _open_git_directory_component(
                object_database,
                b"info",
            )
            info_exists = True

        try:
            watch_revision = _directory_revision(watch_descriptor)
        except (OSError, TypeError, ValueError) as error:
            raise _GitMetadataUnverifiable from error
        pinned = _PinnedGitObjectDatabase(
            descriptor=object_database,
            watch_descriptor=watch_descriptor,
            info_exists=info_exists,
            watch_revision=watch_revision,
            object_database_revision=object_database_revision,
        )
        _validate_pinned_git_object_database(pinned)
        return pinned
    except BaseException:
        if watch_descriptor is not None:
            os.close(watch_descriptor)
        os.close(object_database)
        raise


def _open_pinned_git_object_database(
    workspace_descriptor: int,
) -> _PinnedGitObjectDatabase:
    workspace: int | None = None
    try:
        workspace = os.dup(workspace_descriptor)
        if not stat.S_ISDIR(os.fstat(workspace).st_mode):
            raise _GitMetadataNotRepository
    except _GitMetadataNotRepository:
        if workspace is not None:
            os.close(workspace)
        raise
    except (OSError, TypeError, ValueError) as error:
        if workspace is not None:
            os.close(workspace)
        raise _GitMetadataUnverifiable from error
    try:
        repository_descriptor = _open_pinned_git_directory(workspace)
    finally:
        os.close(workspace)

    try:
        try:
            linked = os.stat(
                b"commondir",
                dir_fd=repository_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            linked = None
        except (OSError, TypeError, ValueError) as error:
            raise _GitMetadataUnverifiable from error

        if linked is not None:
            metadata = _open_stable_git_metadata_file(
                repository_descriptor,
                b"commondir",
                linked,
            )
            try:
                common_path = _single_git_metadata_line(
                    _read_stable_git_metadata(metadata)
                )
            finally:
                os.close(metadata)
            common_directory = _open_git_directory_path(
                repository_descriptor,
                common_path,
            )
            os.close(repository_descriptor)
            repository_descriptor = common_directory

        return _open_pinned_objects_directory(repository_descriptor)
    finally:
        os.close(repository_descriptor)


def _pinned_git_environment() -> dict[str, str]:
    environment = _git_environment()
    environment["GIT_OBJECT_DIRECTORY"] = "."
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    return environment


def _run_git_pinned(
    git: str,
    object_database: _PinnedGitObjectDatabase,
    arguments: list[str],
    record_id: str,
):
    child_descriptor: int | None = None
    try:
        _validate_pinned_git_object_database(object_database)
        child_descriptor = os.dup(object_database.descriptor)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _GIT_FD_EXEC,
                str(child_descriptor),
                git,
                *arguments,
            ],
            pass_fds=(child_descriptor,),
            env=_pinned_git_environment(),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
        _validate_pinned_git_object_database(object_database)
        return result
    except _GitMetadataUnverifiable as error:
        raise GitUnavailable("GIT_UNVERIFIABLE {}".format(record_id)) from error
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        raise GitUnavailable("GIT_UNVERIFIABLE {}".format(record_id)) from error
    finally:
        if child_descriptor is not None:
            os.close(child_descriptor)


def _run_artifact_git(
    git: str,
    workspace,
    object_database_descriptor: _PinnedGitObjectDatabase | None,
    arguments: list[str],
    record_id: str,
):
    if object_database_descriptor is None:
        return _run_git(git, workspace, arguments, record_id)
    return _run_git_pinned(
        git,
        object_database_descriptor,
        arguments,
        record_id,
    )


def _require_workspace_identity(
    workspace,
    workspace_descriptor: int,
    record_id: str,
    artifact_path: str,
) -> None:
    try:
        linked = os.lstat(os.path.abspath(os.fspath(workspace)))
        opened = os.fstat(workspace_descriptor)
    except (OSError, TypeError, ValueError) as error:
        raise _artifact_path_escape(record_id, artifact_path) from error
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISDIR(linked.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or linked.st_dev != opened.st_dev
        or linked.st_ino != opened.st_ino
    ):
        raise _artifact_path_escape(record_id, artifact_path)


def _git_tree_entry_is_regular_blob(output: bytes, artifact_path: str) -> bool:
    expected_path = artifact_path.encode("utf-8")
    for entry in output.split(b"\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition(b"\t")
        fields = metadata.split()
        if (
            separator
            and path == expected_path
            and len(fields) == 3
            and fields[0] in (b"100644", b"100755")
            and fields[1] == b"blob"
        ):
            return True
    return False


def verify_artifact_git(
    workspace,
    artifact_path,
    object_id,
    record_id,
    *,
    workspace_descriptor: int | None = None,
):
    """Verify the sole Git-mode binding to an exact commit blob path.

    The corresponding evidence record must select exactly this mode (an
    ``artifact_git`` and no ``artifact_sha256``); ``record.validate_frontmatter``
    owns that schema invariant. Git is invoked only with an argv list and a
    bounded timeout. A supplied ``workspace_descriptor`` pins both artifact
    traversal and the Git object database used by every command.
    """
    if workspace_descriptor is not None:
        _require_workspace_identity(
            workspace,
            workspace_descriptor,
            record_id,
            artifact_path,
        )
    object_database_descriptor: _PinnedGitObjectDatabase | None = None
    try:
        if workspace_descriptor is None:
            check_artifact_path_policy(
                workspace,
                artifact_path,
                record_id,
            )
        else:
            check_artifact_path_policy(
                workspace,
                artifact_path,
                record_id,
                workspace_descriptor=workspace_descriptor,
            )

        git = shutil.which("git")
        if git is None:
            raise GitUnavailable("GIT_UNAVAILABLE {}".format(record_id))

        if workspace_descriptor is not None:
            try:
                object_database_descriptor = _open_pinned_git_object_database(
                    workspace_descriptor
                )
            except _GitMetadataNotRepository as error:
                raise GitUnavailable(
                    "GIT_NOT_A_REPOSITORY {}".format(record_id)
                ) from error
            except _GitMetadataUnverifiable as error:
                raise GitUnavailable(
                    "GIT_UNVERIFIABLE {}".format(record_id)
                ) from error

        repository_arguments = (
            ["rev-parse", "--is-inside-work-tree"]
            if object_database_descriptor is None
            else ["rev-parse", "--git-dir"]
        )
        repository = _run_artifact_git(
            git,
            workspace,
            object_database_descriptor,
            repository_arguments,
            record_id,
        )
        if (
            repository.returncode != 0
            or (
                object_database_descriptor is None
                and repository.stdout.strip() != b"true"
            )
        ):
            raise GitUnavailable("GIT_NOT_A_REPOSITORY {}".format(record_id))

        if not isinstance(object_id, str) or not GIT_OID_RE.fullmatch(object_id):
            raise PolicyError(
                "ARTIFACT_GIT_MISSING {} {}".format(record_id, object_id)
            )
        object_type = _run_artifact_git(
            git,
            workspace,
            object_database_descriptor,
            ["cat-file", "-t", object_id],
            record_id,
        )
        if object_type.returncode != 0 or object_type.stdout.strip() != b"commit":
            raise PolicyError(
                "ARTIFACT_GIT_MISSING {} {}".format(record_id, object_id)
            )

        binding = _run_artifact_git(
            git,
            workspace,
            object_database_descriptor,
            ["ls-tree", "-z", object_id, "--", artifact_path],
            record_id,
        )
        if binding.returncode != 0 or not _git_tree_entry_is_regular_blob(
            binding.stdout, artifact_path
        ):
            raise PolicyError(
                "ARTIFACT_GIT_PATH {} {}".format(record_id, artifact_path)
            )
    finally:
        if object_database_descriptor is not None:
            object_database_descriptor.close()
        if workspace_descriptor is not None:
            _require_workspace_identity(
                workspace,
                workspace_descriptor,
                record_id,
                artifact_path,
            )
