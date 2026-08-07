"""OS-owned locks for serializing Didimlog writes in one directory."""

from __future__ import annotations

import fcntl
import os
import stat


_LOCK_NAME = ".didimlog.lock"


def acquire_directory_lock(parent_descriptor: int) -> int:
    """Acquire and return the Didimlog lock below an already-open directory."""

    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(
        _LOCK_NAME,
        flags,
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        opened = os.fstat(descriptor)
        linked = os.stat(
            _LOCK_NAME,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
        ):
            raise OSError("Didimlog lock is not a stable regular file")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
