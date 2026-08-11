"""Fail-closed reads and conditional writes for one regular file."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import stat

from didimlog.file_io import (
    UnsafePathError,
    open_directory_path,
    read_regular_file_at_with_stat,
    replace_regular_file_at_if_unchanged,
)
from didimlog.locking import acquire_directory_lock


def _target_path(path: Path) -> Path:
    try:
        target = Path(path)
    except (OSError, TypeError) as error:
        raise ValueError("target path is invalid") from error
    if (
        not target.is_absolute()
        or ".." in target.parts
        or target.name in ("", ".", "..")
        or "/" in target.name
        or "\\" in target.name
        or "\x00" in target.name
    ):
        raise ValueError("target path must be absolute with one final file name")
    return target


def _open_parent(target: Path) -> int:
    try:
        return open_directory_path(target.parent)
    except (OSError, RuntimeError) as error:
        raise ValueError("target parent could not be opened safely") from error


def _revision(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_target(
    parent_descriptor: int,
    name: str,
    maximum_bytes: int,
) -> tuple[bytes, os.stat_result] | None:
    try:
        linked = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("target could not be inspected safely") from error
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
        raise ValueError("target must be a regular file")

    try:
        data, finished = read_regular_file_at_with_stat(
            parent_descriptor,
            name,
            maximum_bytes,
        )
    except UnsafePathError as error:
        raise ValueError("target could not be read safely") from error
    if len(data) > maximum_bytes or _revision(finished) != _revision(linked):
        raise ValueError("target changed or exceeded the read limit")
    return data, finished


def _verify_parent(target: Path, parent_descriptor: int) -> None:
    verification_descriptor = _open_parent(target)
    try:
        opened = os.fstat(parent_descriptor)
        current = os.fstat(verification_descriptor)
    except OSError as error:
        raise ValueError("target parent could not be rechecked") from error
    finally:
        os.close(verification_descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or opened.st_dev != current.st_dev
        or opened.st_ino != current.st_ino
    ):
        raise ValueError("target parent changed before write")


def _temporary_file(parent_descriptor: int, mode: int) -> tuple[str, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(32):
        name = ".didimlog-" + secrets.token_hex(12) + ".tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name,
                flags,
                mode,
                dir_fd=parent_descriptor,
            )
            os.fchmod(descriptor, mode)
            return name, descriptor
        except FileExistsError:
            continue
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                finally:
                    try:
                        os.unlink(name, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
            raise
    raise ValueError("temporary file name could not be allocated")


def _write_all_and_sync(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]
    os.fsync(descriptor)


def read_optional_regular_file(
    path: Path,
    maximum_bytes: int,
) -> bytes | None:
    """Return bounded bytes for one regular file, or ``None`` if only it is absent."""
    target = _target_path(path)
    if not isinstance(maximum_bytes, int) or isinstance(maximum_bytes, bool):
        raise TypeError("maximum_bytes must be an integer")
    if maximum_bytes < 0:
        raise ValueError("maximum_bytes must not be negative")

    parent_descriptor = _open_parent(target)
    try:
        current = _read_target(parent_descriptor, target.name, maximum_bytes)
        return None if current is None else current[0]
    finally:
        os.close(parent_descriptor)


def write_regular_file_if_unchanged(
    path: Path,
    original: bytes | None,
    intended: bytes | None,
) -> None:
    """Publish intended bytes only while the planned regular file is unchanged."""
    target = _target_path(path)
    if original is not None and not isinstance(original, bytes):
        raise ValueError("original content must be bytes or None")
    if intended is not None and not isinstance(intended, bytes):
        raise ValueError("intended content must be bytes or None")
    if original is not None and intended is None:
        raise ValueError("intended None does not delete an existing file")

    parent_descriptor = _open_parent(target)
    lock_descriptor: int | None = None
    temporary_name: str | None = None
    try:
        lock_descriptor = acquire_directory_lock(parent_descriptor)
        maximum_bytes = 0 if original is None else len(original)
        current = _read_target(parent_descriptor, target.name, maximum_bytes)
        if original is None:
            if current is not None:
                raise ValueError("target was created after planning")
            if intended is None:
                return
            mode = 0o600
        else:
            if current is None or current[0] != original:
                raise ValueError("target changed after planning")
            if intended == original:
                return
            mode = stat.S_IMODE(current[1].st_mode)

        _verify_parent(target, parent_descriptor)
        rechecked = _read_target(parent_descriptor, target.name, maximum_bytes)
        if original is None:
            if rechecked is not None:
                raise ValueError("target was created before write")
            temporary_name, temporary_descriptor = _temporary_file(
                parent_descriptor,
                mode,
            )
            try:
                _write_all_and_sync(temporary_descriptor, intended)
            finally:
                os.close(temporary_descriptor)
            os.link(
                temporary_name,
                target.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            os.unlink(temporary_name, dir_fd=parent_descriptor)
            temporary_name = None
            os.fsync(parent_descriptor)
            return

        if (
            rechecked is None
            or rechecked[0] != original
            or _revision(rechecked[1]) != _revision(current[1])
        ):
            raise ValueError("target changed before write")
        replaced = replace_regular_file_at_if_unchanged(
            parent_descriptor,
            target.name,
            original,
            intended,
            mode,
            expected_info=current[1],
        )
        if not replaced:
            raise ValueError("target changed before write")
    except ValueError:
        raise
    except OSError as error:
        raise ValueError("target could not be written atomically") from error
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(parent_descriptor)
