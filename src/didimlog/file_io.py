"""Small fd-relative primitives for fail-closed filesystem reads."""

from __future__ import annotations

import os
import secrets
import stat


class UnsafePathError(OSError):
    """A path component changed or was not the required filesystem type."""


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _check_child_name(name: str) -> None:
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise UnsafePathError("unsafe path component")


def open_child_directory(parent_descriptor: int, name: str) -> int:
    """Open one unchanged, non-symlink directory below an open directory."""
    _check_child_name(name)
    descriptor = None
    try:
        linked = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
            raise UnsafePathError("unsafe directory component")
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _file_revision(opened) != _file_revision(linked)
        ):
            raise UnsafePathError("directory component changed")
        return descriptor
    except UnsafePathError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise UnsafePathError("unable to open directory component") from error


def _file_revision(info: os.stat_result) -> tuple[int, ...]:
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


def _conditional_replace_revision(info: os.stat_result) -> tuple[int, ...]:
    """Return fields that remain stable when the same inode is renamed."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
    )


def read_regular_file_at_with_stat(
    parent_descriptor: int,
    name: str,
    maximum_bytes: int,
) -> tuple[bytes, os.stat_result]:
    """Read bounded bytes and return the unchanged file's final metadata."""
    _check_child_name(name)
    if not isinstance(maximum_bytes, int) or isinstance(maximum_bytes, bool):
        raise TypeError("maximum_bytes must be an integer")
    if maximum_bytes < 0:
        raise ValueError("maximum_bytes must not be negative")

    descriptor = None
    try:
        linked = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
            raise UnsafePathError("unsafe file component")
        descriptor = os.open(name, _file_flags(), dir_fd=parent_descriptor)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _file_revision(opened) != _file_revision(linked)
        ):
            raise UnsafePathError("file component changed")
        limit = maximum_bytes + 1
        chunks = bytearray()
        while len(chunks) < limit:
            chunk = os.read(descriptor, min(64 * 1024, limit - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        finished = os.fstat(descriptor)
        if _file_revision(finished) != _file_revision(opened):
            raise UnsafePathError("file changed while it was read")
        return bytes(chunks), finished
    except UnsafePathError:
        raise
    except OSError as error:
        raise UnsafePathError("unable to read regular file") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_regular_file_at(
    parent_descriptor: int,
    name: str,
    maximum_bytes: int,
) -> bytes:
    """Read at most ``maximum_bytes + 1`` bytes from one unchanged regular file."""
    data, _ = read_regular_file_at_with_stat(
        parent_descriptor,
        name,
        maximum_bytes,
    )
    return data


def _create_temporary_file_at(
    parent_descriptor: int,
    prefix: str,
    mode: int,
) -> tuple[str, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(32):
        name = prefix + secrets.token_hex(12) + ".tmp"
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
    raise UnsafePathError("unable to allocate temporary file")


def _write_all_and_sync(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]
    os.fsync(descriptor)


def _rollback_published_file_at(
    parent_descriptor: int,
    name: str,
    backup_name: str,
    published_identity: tuple[int, int],
) -> bool:
    """Preserve a concurrent public entry while restoring after failed publish."""
    recovery_name: str | None = None
    keep_recovery = False
    try:
        try:
            recovery_name, recovery_descriptor = _create_temporary_file_at(
                parent_descriptor,
                ".didim-recovery-",
                0o600,
            )
        except OSError:
            return False
        os.close(recovery_descriptor)

        try:
            os.rename(
                name,
                recovery_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            os.unlink(recovery_name, dir_fd=parent_descriptor)
            recovery_name = None
            try:
                os.link(
                    backup_name,
                    name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            except OSError:
                return False
            return True
        except OSError:
            return False

        try:
            quarantined = os.stat(
                recovery_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except OSError:
            keep_recovery = True
            return False

        if (quarantined.st_dev, quarantined.st_ino) == published_identity:
            try:
                os.link(
                    backup_name,
                    name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            except OSError:
                keep_recovery = True
                return False
            return True

        try:
            os.link(
                recovery_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            keep_recovery = True
        except OSError:
            keep_recovery = True
            return False
        return True
    finally:
        if recovery_name is not None and not keep_recovery:
            try:
                os.unlink(recovery_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass


def replace_regular_file_at_if_unchanged(
    parent_descriptor: int,
    name: str,
    expected: bytes,
    replacement: bytes,
    mode: int,
    *,
    expected_info: os.stat_result | None = None,
) -> bool:
    """Publish replacement without overwriting a concurrent pathname writer."""
    _check_child_name(name)
    if not isinstance(expected, bytes) or not isinstance(replacement, bytes):
        raise TypeError("expected and replacement must be bytes")

    temporary_name: str | None = None
    backup_name: str | None = None
    backup_moved = False
    published_identity: tuple[int, int] | None = None
    try:
        temporary_name, temporary_descriptor = _create_temporary_file_at(
            parent_descriptor,
            ".didim-replacement-",
            mode,
        )
        try:
            _write_all_and_sync(temporary_descriptor, replacement)
            temporary_info = os.fstat(temporary_descriptor)
            published_identity = (temporary_info.st_dev, temporary_info.st_ino)
        finally:
            os.close(temporary_descriptor)

        backup_name, backup_descriptor = _create_temporary_file_at(
            parent_descriptor,
            ".didim-backup-",
            0o600,
        )
        os.close(backup_descriptor)
        try:
            os.rename(
                name,
                backup_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return False
        backup_moved = True

        moved_data, moved_info = read_regular_file_at_with_stat(
            parent_descriptor,
            backup_name,
            len(expected),
        )
        if (
            moved_data != expected
            or (
                expected_info is not None
                and _conditional_replace_revision(moved_info)
                != _conditional_replace_revision(expected_info)
            )
        ):
            return False

        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            return False

        published = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        moved_after, moved_after_info = read_regular_file_at_with_stat(
            parent_descriptor,
            backup_name,
            len(expected),
        )
        if (
            (published.st_dev, published.st_ino) != published_identity
            or moved_after != expected
            or (
                expected_info is not None
                and _conditional_replace_revision(moved_after_info)
                != _conditional_replace_revision(expected_info)
            )
        ):
            return False

        os.unlink(backup_name, dir_fd=parent_descriptor)
        backup_name = None
        backup_moved = False
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        temporary_name = None
        os.fsync(parent_descriptor)
        return True
    except UnsafePathError:
        raise
    except OSError as error:
        raise UnsafePathError("unable to replace regular file safely") from error
    finally:
        if (
            backup_moved
            and backup_name is not None
            and published_identity is not None
            and not _rollback_published_file_at(
                parent_descriptor,
                name,
                backup_name,
                published_identity,
            )
        ):
            backup_name = None
        if backup_name is not None:
            try:
                os.unlink(backup_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        if backup_moved:
            try:
                os.fsync(parent_descriptor)
            except OSError:
                pass


def open_directory_path(path: os.PathLike[str] | str) -> int:
    """Open one unchanged, non-symlink directory path."""
    absolute = os.path.abspath(os.fspath(path))
    descriptor = None
    try:
        linked = os.lstat(absolute)
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
            raise UnsafePathError("unsafe directory path")
        descriptor = os.open(absolute, _directory_flags())
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _file_revision(opened) != _file_revision(linked)
        ):
            raise UnsafePathError("directory path changed")
        return descriptor
    except UnsafePathError:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise UnsafePathError("unable to open directory path") from error


def read_regular_file_beneath(
    root: os.PathLike[str] | str,
    relative_path: os.PathLike[str] | str,
    maximum_bytes: int,
) -> bytes:
    """Read a regular file by traversing every component below ``root``."""
    relative = os.fspath(relative_path)
    parts = relative.split(os.sep)
    if (
        os.path.isabs(relative)
        or not parts
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise UnsafePathError("unsafe relative path")

    descriptors = [open_directory_path(root)]
    try:
        for component in parts[:-1]:
            descriptors.append(open_child_directory(descriptors[-1], component))
        return read_regular_file_at(
            descriptors[-1],
            parts[-1],
            maximum_bytes,
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
