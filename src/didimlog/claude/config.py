"""Claude 전역 지침과 SessionStart hook의 Didimlog 소유 부분만 갱신한다."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any
from didimlog.file_io import (
    UnsafePathError,
    read_regular_file_at_with_stat,
    replace_regular_file_at_if_unchanged,
)
from didimlog.locking import acquire_directory_lock


_START = b"<!-- DIDIMLOG:START version=1 -->\n"
_END = b"<!-- DIDIMLOG:END -->\n"
_START_PREFIX = b"<!-- DIDIMLOG:START"
_END_PREFIX = b"<!-- DIDIMLOG:END"
_SESSION_START_SUFFIX = " hook session-start"
_MISSING = object()
_TARGET_MAX_BYTES = 4 * 1024 * 1024


def _absolute_path_bytes(path: Path, *, label: str) -> bytes:
    try:
        candidate = Path(path)
    except (OSError, TypeError) as exc:
        raise ValueError(f"{label} path is invalid") from exc
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"{label} path must be absolute")
    try:
        encoded = str(candidate).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} path must be UTF-8") from exc
    if b"\n" in encoded or b"\r" in encoded:
        raise ValueError(f"{label} path must fit on one line")
    return encoded


def render_managed_block(config: Path) -> bytes:
    """Render the exact Didimlog-owned ``CLAUDE.md`` block."""

    config_bytes = _absolute_path_bytes(config, label="Claude config")
    resource_root = config_bytes + b"/didimlog/"
    return (
        _START
        + b"@~/knowledge/MY-RULES.md\n"
        + b"@"
        + resource_root
        + b"KNOWLEDGE_USAGE.md\n"
        + b"@"
        + resource_root
        + b"LESSON_WRITING_RULES.md\n"
        + _END
    )


def plan_claude_md(original: bytes, config: Path) -> bytes:
    """Return a byte-preserving ``CLAUDE.md`` mutation plan."""

    if not isinstance(original, bytes):
        raise ValueError("CLAUDE.md content must be bytes")

    start_markers = original.count(_START_PREFIX)
    end_markers = original.count(_END_PREFIX)
    if start_markers == 0 and end_markers == 0:
        if not original:
            return render_managed_block(config)
        separator = b"\n" if original.endswith(b"\n") else b"\n\n"
        return original + separator + render_managed_block(config)

    if (
        start_markers != 1
        or end_markers != 1
        or original.count(_START) != 1
        or original.count(_END) != 1
    ):
        raise ValueError("CLAUDE.md has invalid Didimlog markers")

    begin = original.index(_START)
    end_begin = original.index(_END)
    if end_begin < begin + len(_START):
        raise ValueError("CLAUDE.md has mismatched Didimlog markers")
    finish = end_begin + len(_END)
    return original[:begin] + render_managed_block(config) + original[finish:]


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("settings.json contains duplicate keys")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"settings.json contains invalid constant: {value}")


def _load_settings(original: bytes) -> dict[str, Any]:
    if not original:
        return {}
    try:
        text = original.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("settings.json is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("settings.json root must be an object")
    return value


def _is_managed_session_start_hook(hook: dict[str, Any], command: str) -> bool:
    candidate = hook.get("command")
    if hook.get("type") != "command" or not isinstance(candidate, str):
        return False
    if candidate == command:
        return True
    if not candidate.endswith(_SESSION_START_SUFFIX):
        return False
    launcher = candidate[: -len(_SESSION_START_SUFFIX)]
    if "\n" in launcher or "\r" in launcher:
        return False
    launcher_path = Path(launcher)
    return launcher_path.is_absolute() and launcher_path.name == "didim"


def plan_settings(original: bytes, launcher: Path) -> bytes:
    """Return canonical JSON with one absolute Didimlog SessionStart hook."""

    if not isinstance(original, bytes):
        raise ValueError("settings.json content must be bytes")
    launcher_bytes = _absolute_path_bytes(launcher, label="launcher")
    launcher_text = launcher_bytes.decode("utf-8")
    command = launcher_text + _SESSION_START_SUFFIX

    value = _load_settings(original)
    hooks_value = value.get("hooks", _MISSING)
    if hooks_value is _MISSING:
        hooks: dict[str, Any] = {}
        value["hooks"] = hooks
    elif not isinstance(hooks_value, dict):
        raise ValueError("settings.json hooks must be an object")
    else:
        hooks = hooks_value

    session_value = hooks.get("SessionStart", _MISSING)
    if session_value is _MISSING:
        session_start: list[dict[str, Any]] = []
        hooks["SessionStart"] = session_start
    elif not isinstance(session_value, list):
        raise ValueError("settings.json SessionStart must be an array")
    else:
        session_start = session_value

    planned_matchers: list[dict[str, Any]] = []
    for matcher in session_start:
        if not isinstance(matcher, dict):
            raise ValueError("settings.json SessionStart entries must be objects")
        matcher_hooks = matcher.get("hooks", _MISSING)
        if not isinstance(matcher_hooks, list):
            raise ValueError("settings.json SessionStart hooks must be arrays")
        if any(not isinstance(hook, dict) for hook in matcher_hooks):
            raise ValueError("settings.json SessionStart hooks must be objects")

        remaining_hooks = [
            hook
            for hook in matcher_hooks
            if not _is_managed_session_start_hook(hook, command)
        ]
        removed_managed_hook = len(remaining_hooks) != len(matcher_hooks)
        if removed_managed_hook and not remaining_hooks and set(matcher) == {"hooks"}:
            continue
        if removed_managed_hook:
            matcher = dict(matcher)
            matcher["hooks"] = remaining_hooks
        planned_matchers.append(matcher)

    planned_matchers.append(
        {"hooks": [{"type": "command", "command": command}]}
    )
    hooks["SessionStart"] = planned_matchers
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _open_parent(path: Path) -> int:
    try:
        entry = path.parent.lstat()
    except (OSError, RuntimeError) as exc:
        raise ValueError("target parent must be an existing directory") from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        raise ValueError("target parent must be a regular directory")

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path.parent, flags)
        opened = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ValueError("target parent could not be opened safely") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != entry.st_dev
        or opened.st_ino != entry.st_ino
    ):
        os.close(descriptor)
        raise ValueError("target parent changed while it was opened")
    return descriptor


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
    parent_descriptor: int, name: str
) -> tuple[bytes, os.stat_result] | None:
    try:
        entry = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("target could not be inspected safely") from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise ValueError("target must be a regular file")

    try:
        data, finished = read_regular_file_at_with_stat(
            parent_descriptor,
            name,
            _TARGET_MAX_BYTES,
        )
        if len(data) > _TARGET_MAX_BYTES or _revision(finished) != _revision(entry):
            raise ValueError("target changed while it was read")
        return data, finished
    except UnsafePathError as exc:
        raise ValueError("target could not be read safely") from exc


def _same_content(current: bytes, expected: bytes) -> bool:
    return (
        hashlib.sha256(current).digest() == hashlib.sha256(expected).digest()
        and current == expected
    )


def _temporary_file(parent_descriptor: int, mode: int) -> tuple[str, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(32):
        name = ".didimlog-" + secrets.token_hex(12) + ".tmp"
        try:
            descriptor = os.open(name, flags, mode, dir_fd=parent_descriptor)
            os.fchmod(descriptor, mode)
            return name, descriptor
        except FileExistsError:
            continue
        except OSError as exc:
            raise ValueError("temporary file could not be created safely") from exc
    raise ValueError("temporary file name could not be allocated")


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]
    os.fsync(descriptor)


def _verify_parent(path: Path, descriptor: int) -> None:
    try:
        current = path.parent.lstat()
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise ValueError("target parent could not be rechecked") from exc
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise ValueError("target parent changed before write")


def write_if_unchanged(
    path: Path, original: bytes | None, intended: bytes
) -> None:
    """Atomically write only while target type and planned bytes are unchanged."""

    try:
        target = Path(path)
    except (OSError, TypeError) as exc:
        raise ValueError("target path is invalid") from exc
    if not target.is_absolute() or ".." in target.parts or not target.name:
        raise ValueError("target path must be absolute and must not escape its parent")
    if original is not None and not isinstance(original, bytes):
        raise ValueError("original content must be bytes or None")
    if not isinstance(intended, bytes):
        raise ValueError("intended content must be bytes")

    parent_descriptor = _open_parent(target)
    lock_descriptor: int | None = None
    temporary_name: str | None = None
    try:
        lock_descriptor = acquire_directory_lock(parent_descriptor)
        current = _read_target(parent_descriptor, target.name)
        if original is None:
            if current is not None:
                raise ValueError("target was created after planning")
            mode = 0o600
        else:
            if current is None or not _same_content(current[0], original):
                raise ValueError("target changed after planning")
            if intended == original:
                return
            mode = stat.S_IMODE(current[1].st_mode)


        _verify_parent(target, parent_descriptor)
        rechecked = _read_target(parent_descriptor, target.name)
        if original is None:
            if rechecked is not None:
                raise ValueError("target was created before write")
            temporary_name, temporary_descriptor = _temporary_file(
                parent_descriptor,
                mode,
            )
            try:
                _write_all(temporary_descriptor, intended)
            except OSError as exc:
                raise ValueError(
                    "intended content could not be written safely"
                ) from exc
            finally:
                os.close(temporary_descriptor)
            try:
                os.link(
                    temporary_name,
                    target.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise ValueError("target was created before write") from exc
            os.unlink(temporary_name, dir_fd=parent_descriptor)
            temporary_name = None
        else:
            if (
                rechecked is None
                or _revision(rechecked[1]) != _revision(current[1])
                or not _same_content(rechecked[0], original)
            ):
                raise ValueError("target changed before write")
            try:
                replaced = replace_regular_file_at_if_unchanged(
                    parent_descriptor,
                    target.name,
                    original,
                    intended,
                    mode,
                    expected_info=current[1],
                )
            except UnsafePathError as exc:
                raise ValueError(
                    "target could not be written atomically"
                ) from exc
            if not replaced:
                raise ValueError("target changed before write")
        os.fsync(parent_descriptor)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError("target could not be written atomically") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(parent_descriptor)
