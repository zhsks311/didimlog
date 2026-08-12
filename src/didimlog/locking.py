"""OS-owned locks for serializing Didimlog access to one directory."""

from __future__ import annotations
from contextlib import contextmanager

import fcntl
import os
from pathlib import Path
import stat
from didimlog.file_io import open_child_directory, open_directory_path


def acquire_directory_lock(
    parent_descriptor: int,
    *,
    shared: bool = False,
    blocking: bool = True,
) -> int:
    """Lock and return a duplicate of an already-open directory descriptor."""
    descriptor = os.dup(parent_descriptor)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("Didimlog lock target is not a directory")
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(descriptor, operation)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def path_lock(
    path: os.PathLike[str] | str,
    *,
    shared: bool = False,
    blocking: bool = True,
):
    """Lock one directory and its stable parent namespace for one transaction."""
    target = Path(os.path.abspath(path))
    if target.parent == target:
        raise OSError("Didimlog path lock target must have a parent directory")

    parent_descriptor = open_directory_path(target.parent)
    parent_lock: int | None = None
    target_descriptor: int | None = None
    target_lock: int | None = None
    try:
        parent_lock = acquire_directory_lock(
            parent_descriptor,
            shared=shared,
            blocking=blocking,
        )
        target_descriptor = open_child_directory(parent_descriptor, target.name)
        target_lock = acquire_directory_lock(
            target_descriptor,
            shared=shared,
            blocking=blocking,
        )
        yield target_lock
    finally:
        if target_lock is not None:
            os.close(target_lock)
        if target_descriptor is not None:
            os.close(target_descriptor)
        if parent_lock is not None:
            os.close(parent_lock)
        os.close(parent_descriptor)
