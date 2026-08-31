"""Read-only, privacy-safe checks for the active Didimlog wiring."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import stat

from didimlog.conditional_file import read_optional_regular_file
from didimlog.indexing import (
    PERSONAL_INDEX_BUSY,
    PERSONAL_INDEX_CURRENT,
    PROJECT_INDEX_CURRENT,
    _discover_git_root,
    _personal_check,
    _prepared_project,
    _project_check,
)
from didimlog.personal.paths import data_home

from . import config as config_module
from .connect import (
    _MANAGED_FILE_MAXIMUM_BYTES,
    _packaged_resources,
    _resource_directory_exists,
)
from .paths import ConfigPathError, config_dir, config_target

_PROJECT_ROOT_UNSET = object()
_SAFE_PROFILE_NAME = re.compile(r"[A-Za-z0-9._-]+\Z")
_PROFILE_MISMATCH_SYMPTOMS = frozenset(
    {
        "CLAUDE_RESOURCE_INVALID",
        "CLAUDE_LAUNCHER_INVALID",
    }
)


@dataclass(frozen=True)
class Problem:
    """One actionable fault, optionally caused by another reported fault."""

    token: str
    impact: str
    action: str
    blocks_repair: bool = False
    caused_by: str | None = None


def _problem(
    token: str,
    impact: str,
    action: str,
    *,
    blocks_repair: bool = False,
    caused_by: str | None = None,
) -> Problem:
    return Problem(
        token=token,
        impact=impact,
        action=action,
        blocks_repair=blocks_repair,
        caused_by=caused_by,
    )


def _profile_setup_action(destination: Path | None, home: Path) -> str:
    """Name the profile that owns a linked file, without exposing the home path."""

    if destination is None:
        return "didim setup"
    try:
        # ``destination`` is already resolved, so the home must be too;
        # otherwise a symlinked home (``/var`` vs ``/private/var``) never matches.
        resolved_home = home.resolve(strict=True)
    except (OSError, RuntimeError):
        return "didim setup"
    try:
        profile = destination.parent.relative_to(resolved_home)
    except ValueError:
        return "didim setup"
    if len(profile.parts) != 1 or _SAFE_PROFILE_NAME.fullmatch(profile.parts[0]) is None:
        return "didim setup"
    return "CLAUDE_CONFIG_DIR=~/{} didim setup".format(profile.parts[0])


def _refused_target_problem(
    error: ConfigPathError,
    *,
    home: Path,
    linked_token: str,
    linked_impact: str,
    unreadable_token: str,
    unreadable_impact: str,
) -> Problem:
    """Turn a refused path into one problem the user can act on.

    A refused path is always repair-blocking: ``setup`` writes through the same
    check, so it cannot fix this or anything downstream until the path changes.
    """

    if error.reason in ("target-symlink", "parent-not-regular") and (
        error.destination is not None
    ):
        return _problem(
            linked_token,
            linked_impact,
            _profile_setup_action(error.destination, home),
            blocks_repair=True,
        )
    if error.reason == "target-symlink":
        return _problem(
            linked_token,
            linked_impact,
            "didim setup",
            blocks_repair=True,
        )
    return _problem(
        unreadable_token,
        unreadable_impact,
        "didim setup",
        blocks_repair=True,
    )


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
            if hook.get("type") != "command":
                continue
            launcher = config_module._launcher_from_session_start_command(
                hook.get("command")
            )
            if launcher is not None:
                launchers.append(launcher)
    return launchers[0] if len(launchers) == 1 else None


def _index_problem(token: str, *, personal: bool) -> Problem | None:
    current = PERSONAL_INDEX_CURRENT if personal else PROJECT_INDEX_CURRENT
    if token == current:
        return None
    if token == PERSONAL_INDEX_BUSY:
        # 목록도 원본도 손댈 것이 없다. didim index를 안내하면
        # 고칠 것이 없는 사용자에게 헛일을 시키게 된다.
        return _problem(
            token,
            "다른 Didimlog 실행이 개인 지식을 사용 중이라 지금은 상태를 확인하지 못했습니다.",
            "잠시 뒤 didim index --check",
        )
    if personal:
        impact = "개인 지식 목록이 전체 원본과 일치하지 않아 조회 결과를 신뢰할 수 없습니다."
    else:
        impact = "현재 프로젝트 근거 목록이 기록 원본과 일치하지 않습니다."
    return _problem(token, impact, "didim index")


def inspect(
    *,
    home=None,
    cwd=None,
    config=None,
    _project_root=_PROJECT_ROOT_UNSET,
) -> tuple[Problem, ...]:
    """Inspect wiring and derived indexes without reading source bodies into output."""
    selected_home = Path.home() if home is None else Path(home)
    selected_home = Path(os.path.abspath(selected_home))
    selected_config = config_dir(config, home=selected_home)
    problems: list[Problem] = []

    try:
        claude_path = config_target(
            selected_config,
            "CLAUDE.md",
            home=selected_home,
        )
    except ConfigPathError as error:
        problems.append(
            _refused_target_problem(
                error,
                home=selected_home,
                linked_token="CLAUDE_IMPORT_LINKED",
                linked_impact=(
                    "이 프로필의 CLAUDE.md가 다른 위치를 가리키는 링크라서 "
                    "Didimlog가 안전하게 수정할 수 없습니다."
                ),
                unreadable_token="CLAUDE_IMPORT_UNREADABLE",
                unreadable_impact=(
                    "이 프로필의 CLAUDE.md가 일반 파일이 아니라서 "
                    "Didimlog가 읽고 쓸 수 없습니다."
                ),
            )
        )
    else:
        claude_raw = read_optional_regular_file(
            claude_path,
            _MANAGED_FILE_MAXIMUM_BYTES,
        )
        expected_block = config_module.render_managed_block(selected_config)
        if claude_raw is None or claude_raw.count(expected_block) != 1:
            problems.append(
                _problem(
                    "CLAUDE_IMPORT_MISSING",
                    "Claude가 필요한 지식의 위치와 조회 절차를 받지 못합니다.",
                    "didim setup",
                )
            )

    resource_refusal: ConfigPathError | None = None
    resource_invalid = not _resource_directory_exists(selected_config)
    if not resource_invalid:
        for name, expected in _packaged_resources():
            try:
                path = config_target(
                    selected_config,
                    "didimlog/" + name,
                    home=selected_home,
                )
            except ConfigPathError as error:
                resource_refusal = error
                resource_invalid = True
                break
            if (
                read_optional_regular_file(path, _MANAGED_FILE_MAXIMUM_BYTES)
                != expected
            ):
                resource_invalid = True
    if resource_refusal is not None:
        problems.append(
            _refused_target_problem(
                resource_refusal,
                home=selected_home,
                linked_token="CLAUDE_RESOURCE_LINKED",
                linked_impact=(
                    "Claude 지식 사용 지침이 다른 위치를 가리키는 링크라서 "
                    "Didimlog가 안전하게 수정할 수 없습니다."
                ),
                unreadable_token="CLAUDE_RESOURCE_INVALID",
                unreadable_impact=(
                    "Claude 지식 사용 지침이 설치본과 일치하지 않습니다."
                ),
            )
        )
    elif resource_invalid:
        problems.append(
            _problem(
                "CLAUDE_RESOURCE_INVALID",
                "Claude 지식 사용 지침이 설치본과 일치하지 않습니다.",
                "didim setup",
            )
        )

    try:
        settings_path = config_target(
            selected_config,
            "settings.json",
            home=selected_home,
        )
    except ConfigPathError as error:
        problems.append(
            _refused_target_problem(
                error,
                home=selected_home,
                linked_token="CLAUDE_SETTINGS_LINKED",
                linked_impact=(
                    "이 프로필의 settings.json이 다른 위치를 가리키는 링크라서 "
                    "SessionStart 확인을 연결할 수 없습니다."
                ),
                unreadable_token="CLAUDE_SETTINGS_UNREADABLE",
                unreadable_impact=(
                    "이 프로필의 settings.json이 일반 파일이 아니라서 "
                    "SessionStart 확인을 연결할 수 없습니다."
                ),
            )
        )
        settings_path = None
    launcher = (
        None
        if settings_path is None
        else _launcher_from_settings(
            read_optional_regular_file(
                settings_path,
                _MANAGED_FILE_MAXIMUM_BYTES,
            )
        )
    )
    if settings_path is not None and (
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

    project_root = (
        _discover_git_root(cwd)
        if _project_root is _PROJECT_ROOT_UNSET
        else _project_root
    )
    if project_root is not None and _prepared_project(project_root):
        project_problem = _index_problem(
            _project_check(project_root),
            personal=False,
        )
        if project_problem is not None:
            problems.append(project_problem)

    blocking_profile_problems = [
        problem
        for problem in problems
        if problem.blocks_repair
        and problem.action.startswith("CLAUDE_CONFIG_DIR=")
    ]
    if len(blocking_profile_problems) == 1:
        cause = blocking_profile_problems[0]
        problems = [
            replace(problem, caused_by=cause.token)
            if problem.token in _PROFILE_MISMATCH_SYMPTOMS
            else problem
            for problem in problems
        ]

    return tuple(problems)
