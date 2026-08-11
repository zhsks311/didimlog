"""Claude 전역 지침과 SessionStart hook의 Didimlog 소유 부분만 갱신한다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_START = b"<!-- DIDIMLOG:START version=1 -->\n"
_END = b"<!-- DIDIMLOG:END -->\n"
_START_PREFIX = b"<!-- DIDIMLOG:START"
_END_PREFIX = b"<!-- DIDIMLOG:END"
_SESSION_START_SUFFIX = " hook session-start"
_MISSING = object()


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


