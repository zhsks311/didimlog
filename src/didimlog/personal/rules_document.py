"""Create the user-owned ``MY-RULES.md`` document without overwriting it."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path

from .paths import data_home


USER_RULES_TEMPLATE = """# 내 규칙

여기에 모든 프로젝트에서 항상 지킬 개인 규칙만 적는다.
이 파일은 설치 프로그램이 덮어쓰거나 생성 index를 자동으로 불러오지 않는다.
"""


class RulesDocumentError(ValueError):
    """Raised when the user rules destination is unsafe."""


class RulesConcurrentModification(RulesDocumentError):
    """Raised when another writer creates the rules document concurrently."""


def _require_regular_directory(directory: Path) -> None:
    try:
        info = directory.lstat()
    except OSError as exc:
        raise RulesDocumentError("MY-RULES.md parent must be a regular directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RulesDocumentError("MY-RULES.md parent must be a regular directory")


def _existing_regular_file(target: Path) -> bool:
    try:
        info = target.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RulesDocumentError("MY-RULES.md destination is unsafe") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RulesDocumentError("MY-RULES.md must be a regular file")
    return True


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise RulesDocumentError("MY-RULES.md parent is unsafe") from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_user_rules(home=None) -> Path:
    """Create ``~/knowledge/MY-RULES.md`` once and return its path.

    An existing regular file is user-owned and is returned unchanged. Creation
    publishes a synced temporary inode with a no-replace hard link so a file
    appearing concurrently is never overwritten.
    """
    directory = data_home(home)
    target = directory / "MY-RULES.md"

    _require_regular_directory(directory)
    if _existing_regular_file(target):
        return target

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".my-rules-",
        suffix=".tmp",
        dir=directory,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor = -1
        with handle:
            handle.write(USER_RULES_TEMPLATE)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise RulesConcurrentModification(
                "MY-RULES.md appeared during creation"
            ) from exc
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise RulesConcurrentModification(
                    "MY-RULES.md appeared during creation"
                ) from exc
            raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass

    _sync_directory(directory)
    return target
