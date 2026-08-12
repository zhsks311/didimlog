"""Package에 포함된 Claude 지침만 안전하게 materialize한다."""

from __future__ import annotations

import importlib.resources
import os
import secrets
import stat
from pathlib import Path


_RESOURCE_NAMES = (
    "KNOWLEDGE_USAGE.md",
    "LESSON_WRITING_RULES.md",
)
_RESOURCE_PACKAGE = "didimlog.resources.personal"


def _open_directory(path: Path) -> int:
    try:
        entry = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise ValueError("Claude config must be an existing directory") from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise ValueError("Claude config must be a regular directory")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ValueError("Claude config must be a safe regular directory") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != entry.st_dev
        or opened.st_ino != entry.st_ino
    ):
        os.close(descriptor)
        raise ValueError("Claude config directory changed while it was opened")
    return descriptor


def _open_managed_directory(config_descriptor: int) -> int:
    try:
        entry = os.stat("didimlog", dir_fd=config_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir("didimlog", mode=0o700, dir_fd=config_descriptor)
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValueError("Didimlog resource directory could not be created safely") from exc
        else:
            os.fsync(config_descriptor)
        try:
            entry = os.stat("didimlog", dir_fd=config_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("Didimlog resource directory could not be opened safely") from exc
    except OSError as exc:
        raise ValueError("Didimlog resource directory is unsafe") from exc

    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise ValueError("Didimlog resource directory must be a regular directory")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open("didimlog", flags, dir_fd=config_descriptor)
        opened = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ValueError("Didimlog resource directory is unsafe") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != entry.st_dev
        or opened.st_ino != entry.st_ino
    ):
        os.close(descriptor)
        raise ValueError("Didimlog resource directory changed while it was opened")
    return descriptor


def _read_regular_file(directory_descriptor: int, name: str) -> bytes | None:
    try:
        entry = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"managed resource target is unsafe: {name}") from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise ValueError(f"managed resource target must be a regular file: {name}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != entry.st_dev
            or opened.st_ino != entry.st_ino
        ):
            raise ValueError(f"managed resource target changed while it was opened: {name}")
        chunks = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.extend(chunk)
        return bytes(chunks)
    except OSError as exc:
        raise ValueError(f"managed resource target could not be read safely: {name}") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


def _temporary_name(directory_descriptor: int, target_name: str) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(16):
        name = f".{target_name}.{secrets.token_hex(8)}.tmp"
        try:
            return name, os.open(name, flags, 0o600, dir_fd=directory_descriptor)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ValueError(f"managed resource temporary file is unsafe: {target_name}") from exc
    raise ValueError(f"managed resource temporary name is unavailable: {target_name}")


def _atomic_replace(directory_descriptor: int, name: str, content: bytes) -> None:
    temporary_name, descriptor = _temporary_name(directory_descriptor, name)
    try:
        try:
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        try:
            current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                raise ValueError(f"managed resource target must be a regular file: {name}")
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"managed resource could not be replaced safely: {name}") from exc
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass


def materialize_resources(config: Path) -> tuple[Path, Path]:
    """Atomically install the two canonical package resources below ``config``.

    Existing byte-identical regular files are not touched, preserving their inode
    and timestamps. No personal lesson, docs, or book content is inspected.
    """

    config_path = Path(config)
    resource_root = importlib.resources.files(_RESOURCE_PACKAGE)
    packaged = tuple(
        (name, resource_root.joinpath(name).read_bytes())
        for name in _RESOURCE_NAMES
    )

    config_descriptor = _open_directory(config_path)
    try:
        managed_descriptor = _open_managed_directory(config_descriptor)
        try:
            for name, content in packaged:
                if _read_regular_file(managed_descriptor, name) != content:
                    _atomic_replace(managed_descriptor, name, content)
        finally:
            os.close(managed_descriptor)
    finally:
        os.close(config_descriptor)

    managed_path = config_path / "didimlog"
    return tuple(managed_path / name for name in _RESOURCE_NAMES)
