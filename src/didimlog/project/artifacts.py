"""Project evidence artifact path and binding verification."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import PurePosixPath

from didimlog.project.record import (
    ARTIFACT_PATH_MAX,
    GIT_OID_RE,
    GitUnavailable,
    PolicyError,
    SchemaError,
)

_GIT_TIMEOUT_SECONDS = 10
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
    mode. Filesystem placement below ``artifacts/`` is enforced separately by
    :func:`check_artifact_path_policy`.
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


def check_artifact_path_policy(workspace, artifact_path, record_id):
    """Return an ``artifacts/`` path that cannot escape through a symlink.

    The caller must first apply :func:`check_artifact_path_format` when
    validating record schema. Policy failures deliberately remain exit-code 3
    errors when this lower-level function is called directly.
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
        or len(parts) < 2
        or parts[0] != "artifacts"
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
        if os.path.islink(workspace_path):
            raise _artifact_path_escape(record_id, artifact_path)
        full_path = os.path.abspath(os.path.join(workspace_path, *parts))
        if os.path.commonpath((workspace_path, full_path)) != workspace_path:
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


def _open_workspace(workspace, record_id: str, artifact_path: str) -> int:
    absolute = os.path.abspath(os.fspath(workspace))
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
) -> int:
    parts = artifact_path.split("/")
    descriptor = _open_workspace(workspace, record_id, artifact_path)
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




def verify_artifact_local(workspace, artifact_path, expected_sha256, record_id):
    """Verify the sole local-mode binding to a regular file and SHA-256.

    The corresponding evidence record must select exactly this mode (an
    ``artifact_sha256`` and no ``artifact_git``); ``record.validate_frontmatter``
    owns that schema invariant.
    """
    full_path = check_artifact_path_policy(
        workspace,
        artifact_path,
        record_id,
    )
    descriptor = _open_artifact(workspace, artifact_path, record_id)

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
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
        raise GitUnavailable("GIT_UNVERIFIABLE {}".format(record_id)) from error


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


def verify_artifact_git(workspace, artifact_path, object_id, record_id):
    """Verify the sole Git-mode binding to an exact commit blob path.

    The corresponding evidence record must select exactly this mode (an
    ``artifact_git`` and no ``artifact_sha256``); ``record.validate_frontmatter``
    owns that schema invariant. Git is invoked only with an argv list and a
    bounded timeout.
    """
    check_artifact_path_policy(workspace, artifact_path, record_id)

    git = shutil.which("git")
    if git is None:
        raise GitUnavailable("GIT_UNAVAILABLE {}".format(record_id))

    repository = _run_git(
        git, workspace, ["rev-parse", "--is-inside-work-tree"], record_id
    )
    if repository.returncode != 0 or repository.stdout.strip() != b"true":
        raise GitUnavailable("GIT_NOT_A_REPOSITORY {}".format(record_id))

    if not isinstance(object_id, str) or not GIT_OID_RE.fullmatch(object_id):
        raise PolicyError(
            "ARTIFACT_GIT_MISSING {} {}".format(record_id, object_id)
        )
    object_type = _run_git(git, workspace, ["cat-file", "-t", object_id], record_id)
    if object_type.returncode != 0 or object_type.stdout.strip() != b"commit":
        raise PolicyError(
            "ARTIFACT_GIT_MISSING {} {}".format(record_id, object_id)
        )

    binding = _run_git(
        git,
        workspace,
        ["ls-tree", "-z", object_id, "--", artifact_path],
        record_id,
    )
    if binding.returncode != 0 or not _git_tree_entry_is_regular_blob(
        binding.stdout, artifact_path
    ):
        raise PolicyError(
            "ARTIFACT_GIT_PATH {} {}".format(record_id, artifact_path)
        )
