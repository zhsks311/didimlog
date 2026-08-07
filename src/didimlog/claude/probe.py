"""Read-only, privacy-safe checks for the active Didimlog wiring."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

from didimlog.indexing import (
    _discover_git_root,
    _personal_check,
    _prepared_project,
    _project_check,
)
from didimlog.personal.paths import data_home

from . import config as config_module
from .connect import _packaged_resources, _read_optional
from .paths import config_dir, config_target


@dataclass(frozen=True)
class Problem:
    token: str
    impact: str
    action: str


def _problem(token: str, impact: str, action: str) -> Problem:
    return Problem(token=token, impact=impact, action=action)


def _valid_regular(path: Path, *, nonempty: bool) -> bool:
    try:
        linked = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
        return False
    return not nonempty or linked.st_size > 0


def _launcher_from_settings(raw: bytes | None) -> Path | None:
    if raw is None:
        return None
    try:
        value = config_module._load_settings(raw)
    except ValueError:
        return None
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        return None
    session_start = hooks.get("SessionStart")
    if not isinstance(session_start, list):
        return None
    launchers = []
    for matcher in session_start:
        if not isinstance(matcher, dict):
            return None
        matcher_hooks = matcher.get("hooks")
        if not isinstance(matcher_hooks, list):
            return None
        for hook in matcher_hooks:
            if not isinstance(hook, dict):
                return None
            if not config_module._is_managed_session_start_hook(hook, ""):
                continue
            command = hook["command"]
            launchers.append(
                Path(command[: -len(config_module._SESSION_START_SUFFIX)])
            )
    return launchers[0] if len(launchers) == 1 else None


def _index_problem(status: str, *, personal: bool) -> Problem | None:
    token = status.partition(": ")[2]
    current = "PERSONAL_INDEX_CURRENT" if personal else "PROJECT_INDEX_CURRENT"
    if token == current:
        return None
    if personal:
        impact = "개인 지식 목록이 전체 원본과 일치하지 않아 조회 결과를 신뢰할 수 없습니다."
    else:
        impact = "현재 프로젝트 근거 목록이 기록 원본과 일치하지 않습니다."
    return _problem(token, impact, "didim index")


def inspect(*, home=None, cwd=None, config=None) -> tuple[Problem, ...]:
    """Inspect wiring and derived indexes without reading source bodies into output."""
    selected_home = Path.home() if home is None else Path(home)
    selected_home = Path(os.path.abspath(selected_home))
    selected_config = config_dir(config, home=selected_home)
    problems: list[Problem] = []

    claude_path = config_target(
        selected_config,
        "CLAUDE.md",
        home=selected_home,
    )
    claude_raw = _read_optional(claude_path)
    expected_block = config_module.render_managed_block(selected_config)
    if claude_raw is None or claude_raw.count(expected_block) != 1:
        problems.append(
            _problem(
                "CLAUDE_IMPORT_MISSING",
                "Claude가 필요한 지식의 위치와 조회 절차를 받지 못합니다.",
                "didim setup",
            )
        )

    resource_invalid = False
    for name, expected in _packaged_resources():
        path = config_target(
            selected_config,
            "didimlog/" + name,
            home=selected_home,
        )
        if _read_optional(path) != expected:
            resource_invalid = True
    if resource_invalid:
        problems.append(
            _problem(
                "CLAUDE_RESOURCE_INVALID",
                "Claude 지식 사용 지침이 설치본과 일치하지 않습니다.",
                "didim setup",
            )
        )

    settings_path = config_target(
        selected_config,
        "settings.json",
        home=selected_home,
    )
    launcher = _launcher_from_settings(_read_optional(settings_path))
    if (
        launcher is None
        or not launcher.is_absolute()
        or not _valid_regular(launcher, nonempty=True)
        or not os.access(launcher, os.X_OK)
    ):
        problems.append(
            _problem(
                "CLAUDE_LAUNCHER_INVALID",
                "새 Claude 세션에서 Didimlog 상태 확인을 실행할 수 없습니다.",
                "didim setup",
            )
        )

    rules = data_home(selected_home) / "MY-RULES.md"
    if not _valid_regular(rules, nonempty=True):
        problems.append(
            _problem(
                "PERSONAL_RULES_INVALID",
                "모든 프로젝트에서 사용할 개인 규칙을 Claude가 읽을 수 없습니다.",
                "didim setup",
            )
        )

    personal_problem = _index_problem(
        _personal_check(data_home(selected_home)),
        personal=True,
    )
    if personal_problem is not None:
        problems.append(personal_problem)

    project_root = _discover_git_root(cwd)
    if project_root is not None and _prepared_project(project_root):
        project_problem = _index_problem(
            _project_check(project_root),
            personal=False,
        )
        if project_problem is not None:
            problems.append(project_problem)

    return tuple(problems)
