"""Plan and apply explicit Claude connect or disconnect transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import importlib.resources
import json
import os
from pathlib import Path
import secrets
import stat
from collections.abc import Mapping

from didimlog.conditional_file import (
    read_optional_regular_file,
    write_regular_file_if_unchanged,
)
from didimlog.file_io import (
    UnsafePathError,
    open_directory_path,
    read_regular_file_at_with_stat,
)
from didimlog.locking import acquire_directory_lock

from . import config as config_module
from . import resources as resource_module
from .config import plan_claude_md, plan_settings
from .paths import config_dir, config_target
from .transaction import InstallJournal


_RESOURCE_NAMES = ("KNOWLEDGE_USAGE.md", "LESSON_WRITING_RULES.md")
_RESOURCE_PACKAGE = "didimlog.resources.personal"
_MANAGED_FILE_MAXIMUM_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class _FileChange:
    name: str
    path: Path
    original: bytes | None
    intended: bytes | None


@dataclass(frozen=True)
class ClaudeChangePlan:
    config_dir: Path
    changes: tuple[str, ...]
    _files: tuple[_FileChange, ...] = field(repr=False, compare=False)


def _packaged_resources() -> tuple[tuple[str, bytes], ...]:
    root = importlib.resources.files(_RESOURCE_PACKAGE)
    return tuple((name, root.joinpath(name).read_bytes()) for name in _RESOURCE_NAMES)


def _validate_launcher(launcher: Path) -> Path:
    candidate = Path(launcher)
    try:
        linked = candidate.lstat()
    except OSError as error:
        raise ValueError("launcher must be an existing regular file") from error
    if (
        not candidate.is_absolute()
        or stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or not os.access(candidate, os.X_OK)
    ):
        raise ValueError("launcher must be an executable regular file")
    if candidate.name != "didim":
        raise ValueError("launcher must be the didim executable")
    return candidate


def _selected_config(explicit, *, environ, home) -> tuple[Path, Path]:
    selected_home = Path.home() if home is None else Path(home)
    selected = config_dir(explicit, environ=environ, home=selected_home)
    return selected, selected_home.resolve(strict=True)


def _target(config: Path, name: str, home: Path) -> Path:
    return config_target(config, name, home=home)

def _resource_directory_exists(config: Path) -> bool:
    try:
        (config / "didimlog").lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ValueError("managed resource directory could not be inspected") from error
    return True


def plan_connect(
    explicit=None,
    *,
    launcher: Path,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ClaudeChangePlan:
    """Return a read-only, exact-byte plan for one selected Claude profile."""
    selected, selected_home = _selected_config(
        explicit,
        environ=environ,
        home=home,
    )
    launcher_path = _validate_launcher(launcher)
    changes: list[str] = []
    file_changes: list[_FileChange] = []

    resource_directory_exists = _resource_directory_exists(selected)
    for name, intended in _packaged_resources():
        path = _target(selected, "didimlog/" + name, selected_home)
        original = (
            read_optional_regular_file(
                path,
                _MANAGED_FILE_MAXIMUM_BYTES,
            )
            if resource_directory_exists
            else None
        )
        if original != intended:
            file_changes.append(
                _FileChange("resource:" + name, path, original, intended)
            )
            changes.append("관리 지침 설치: {}".format(path))

    claude_path = _target(selected, "CLAUDE.md", selected_home)
    claude_original = read_optional_regular_file(
        claude_path,
        _MANAGED_FILE_MAXIMUM_BYTES,
    )
    claude_input = b"" if claude_original is None else claude_original
    claude_intended = plan_claude_md(claude_input, selected)
    if claude_original != claude_intended:
        file_changes.append(
            _FileChange(
                "claude-md",
                claude_path,
                claude_original,
                claude_intended,
            )
        )
        changes.append("Claude 지침 연결: {}".format(claude_path))

    settings_path = _target(selected, "settings.json", selected_home)
    settings_original = read_optional_regular_file(
        settings_path,
        _MANAGED_FILE_MAXIMUM_BYTES,
    )
    settings_input = b"" if settings_original is None else settings_original
    settings_intended = plan_settings(settings_input, launcher_path)
    if settings_original != settings_intended:
        file_changes.append(
            _FileChange(
                "settings",
                settings_path,
                settings_original,
                settings_intended,
            )
        )
        changes.append("SessionStart hook 연결: {}".format(settings_path))

    return ClaudeChangePlan(selected, tuple(changes), tuple(file_changes))


def _backup_original(
    journal: InstallJournal,
    name: str,
    original: bytes | None,
) -> Path | None:
    if original is None:
        return None
    directory = journal.path.parent
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    for _ in range(32):
        path = directory / ".{}.{}.backup".format(safe_name, secrets.token_hex(8))
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            continue
        try:
            remaining = memoryview(original)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return path
    raise ValueError("could not allocate transaction backup")


def _ensure_resource_directory(config: Path) -> None:
    config_descriptor = resource_module._open_directory(config)
    try:
        managed_descriptor = resource_module._open_managed_directory(config_descriptor)
        os.close(managed_descriptor)
    finally:
        os.close(config_descriptor)


def _apply_writes(plan: ClaudeChangePlan, journal: InstallJournal) -> None:
    if any(change.path.parent.name == "didimlog" for change in plan._files):
        _ensure_resource_directory(plan.config_dir)
    ordered = sorted(
        plan._files,
        key=lambda change: (
            0
            if change.name.startswith("resource:")
            else 1
            if change.name == "settings"
            else 2
        ),
    )
    for change in ordered:
        if change.intended is None:
            continue
        backup = _backup_original(journal, change.name, change.original)
        journal.record_original(
            change.name,
            change.path,
            change.original,
            backup,
        )
        write_regular_file_if_unchanged(
            change.path,
            change.original,
            change.intended,
        )
        journal.record_installed(change.name, change.intended)


def apply_connect(plan: ClaudeChangePlan, journal: InstallJournal) -> None:
    """Apply an approved connect plan and rollback only unchanged owned bytes."""
    if not isinstance(plan, ClaudeChangePlan):
        raise ValueError("invalid Claude connect plan")
    try:
        _apply_writes(plan, journal)
    except BaseException:
        journal.rollback()
        raise


def _remove_managed_block(original: bytes) -> bytes:
    start = config_module._START
    end = config_module._END
    starts = original.count(config_module._START_PREFIX)
    ends = original.count(config_module._END_PREFIX)
    if starts == 0 and ends == 0:
        return original
    if starts != 1 or ends != 1 or original.count(start) != 1 or original.count(end) != 1:
        raise ValueError("CLAUDE.md has invalid Didimlog markers")
    begin = original.index(start)
    end_begin = original.index(end)
    if end_begin < begin + len(start):
        raise ValueError("CLAUDE.md has mismatched Didimlog markers")
    finish = end_begin + len(end)
    prefix = original[:begin]
    suffix = original[finish:]
    if not suffix and prefix.endswith(b"\n\n"):
        prefix = prefix[:-1]
    return prefix + suffix


def _remove_managed_hooks(original: bytes) -> bytes:
    value = config_module._load_settings(original)
    hooks = value.get("hooks")
    if hooks is None:
        return original
    if not isinstance(hooks, dict):
        raise ValueError("settings.json hooks must be an object")
    session_start = hooks.get("SessionStart")
    if session_start is None:
        return original
    if not isinstance(session_start, list):
        raise ValueError("settings.json SessionStart must be an array")
    planned = []
    changed = False
    for matcher in session_start:
        if not isinstance(matcher, dict):
            raise ValueError("settings.json SessionStart entries must be objects")
        matcher_hooks = matcher.get("hooks")
        if not isinstance(matcher_hooks, list) or any(
            not isinstance(hook, dict) for hook in matcher_hooks
        ):
            raise ValueError("settings.json SessionStart hooks must be objects")
        remaining = [
            hook
            for hook in matcher_hooks
            if not config_module._is_managed_session_start_hook(hook, "")
        ]
        if len(remaining) == len(matcher_hooks):
            planned.append(matcher)
            continue
        changed = True
        if remaining or set(matcher) != {"hooks"}:
            replacement = dict(matcher)
            replacement["hooks"] = remaining
            planned.append(replacement)
    if not changed:
        return original
    if planned:
        hooks["SessionStart"] = planned
    else:
        hooks.pop("SessionStart", None)
    if not hooks:
        value.pop("hooks", None)
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def plan_disconnect(
    explicit=None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ClaudeChangePlan:
    """Return a read-only plan removing only current Didimlog-owned wiring."""
    selected, selected_home = _selected_config(
        explicit,
        environ=environ,
        home=home,
    )
    changes: list[str] = []
    file_changes: list[_FileChange] = []

    claude_path = _target(selected, "CLAUDE.md", selected_home)
    claude_original = read_optional_regular_file(
        claude_path,
        _MANAGED_FILE_MAXIMUM_BYTES,
    )
    if claude_original is not None:
        intended = _remove_managed_block(claude_original)
        if intended != claude_original:
            file_changes.append(
                _FileChange("disconnect:claude-md", claude_path, claude_original, intended)
            )
            changes.append("Claude 지침 연결 해제: {}".format(claude_path))

    settings_path = _target(selected, "settings.json", selected_home)
    settings_original = read_optional_regular_file(
        settings_path,
        _MANAGED_FILE_MAXIMUM_BYTES,
    )
    if settings_original is not None:
        intended = _remove_managed_hooks(settings_original)
        if intended != settings_original:
            file_changes.append(
                _FileChange("disconnect:settings", settings_path, settings_original, intended)
            )
            changes.append("SessionStart hook 연결 해제: {}".format(settings_path))

    resource_directory_exists = _resource_directory_exists(selected)
    for name, packaged in _packaged_resources():
        path = _target(selected, "didimlog/" + name, selected_home)
        original = (
            read_optional_regular_file(
                path,
                _MANAGED_FILE_MAXIMUM_BYTES,
            )
            if resource_directory_exists
            else None
        )
        if original == packaged:
            file_changes.append(
                _FileChange("disconnect:resource:" + name, path, original, None)
            )
            changes.append("관리 지침 제거: {}".format(path))

    return ClaudeChangePlan(selected, tuple(changes), tuple(file_changes))


def _delete_unchanged(change: _FileChange) -> None:
    if change.original is None:
        raise ValueError("managed resource did not exist during planning")
    try:
        parent_descriptor = open_directory_path(change.path.parent)
    except UnsafePathError as error:
        raise ValueError("managed resource parent is unsafe") from error
    lock_descriptor: int | None = None
    try:
        lock_descriptor = acquire_directory_lock(parent_descriptor)
        current, current_info = read_regular_file_at_with_stat(
            parent_descriptor,
            change.path.name,
            len(change.original),
        )
        if current != change.original:
            raise ValueError("managed resource changed after planning")
        rechecked, rechecked_info = read_regular_file_at_with_stat(
            parent_descriptor,
            change.path.name,
            len(change.original),
        )
        if (
            rechecked != change.original
            or (rechecked_info.st_dev, rechecked_info.st_ino)
            != (current_info.st_dev, current_info.st_ino)
            or rechecked_info.st_mode != current_info.st_mode
            or rechecked_info.st_size != current_info.st_size
            or rechecked_info.st_mtime_ns != current_info.st_mtime_ns
            or rechecked_info.st_ctime_ns != current_info.st_ctime_ns
        ):
            raise ValueError("managed resource changed before removal")
        os.unlink(change.path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except ValueError:
        raise
    except OSError as error:
        raise ValueError("managed resource could not be removed safely") from error
    finally:
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        os.close(parent_descriptor)


def apply_disconnect(plan: ClaudeChangePlan, journal: InstallJournal) -> None:
    """Apply selective disconnect and preserve changed resources and user data."""
    if not isinstance(plan, ClaudeChangePlan):
        raise ValueError("invalid Claude disconnect plan")
    writes = tuple(change for change in plan._files if change.intended is not None)
    removals = tuple(change for change in plan._files if change.intended is None)
    deleted: list[_FileChange] = []
    write_plan = ClaudeChangePlan(plan.config_dir, plan.changes, writes)
    try:
        _apply_writes(write_plan, journal)
        for change in removals:
            _delete_unchanged(change)
            deleted.append(change)
        managed = plan.config_dir / "didimlog"
        try:
            managed.rmdir()
        except OSError:
            pass
    except BaseException:
        try:
            for change in reversed(deleted):
                try:
                    if (
                        read_optional_regular_file(
                            change.path,
                            _MANAGED_FILE_MAXIMUM_BYTES,
                        )
                        is None
                        and change.original is not None
                    ):
                        write_regular_file_if_unchanged(
                            change.path,
                            None,
                            change.original,
                        )
                except (OSError, ValueError):
                    continue
        finally:
            journal.rollback()
        raise
