"""Human-readable, privacy-safe Didimlog status and diagnosis."""

from __future__ import annotations

import os
from pathlib import Path

from didimlog import version as didimlog_version
from didimlog.errors import EXIT_POLICY
from didimlog.indexing import (
    _discover_git_root,
    _personal_check,
    _prepared_project,
    _project_check,
)
from didimlog.personal.paths import data_home

from . import config as config_module
from .connect import (
    _LEGACY_END,
    _LEGACY_START,
    _is_legacy_hook,
    _read_optional,
)
from .paths import config_dir, config_target
from .probe import Problem, inspect


_PERSONAL_LABELS = {
    "PERSONAL_INDEX_CURRENT": "최신",
    "PERSONAL_INDEX_MISSING": "목록 없음",
    "PERSONAL_INDEX_STALE": "갱신 필요",
    "PERSONAL_INDEX_EXTRA": "알 수 없는 index 파일 있음",
    "PERSONAL_INDEX_INVALID_SOURCE": "원본 오류",
}
_PROJECT_LABELS = {
    "PROJECT_INDEX_CURRENT": "최신",
    "PROJECT_INDEX_MISSING": "목록 없음",
    "PROJECT_INDEX_STALE": "갱신 필요",
    "PROJECT_INDEX_EXTRA": "알 수 없는 index 파일 있음",
    "PROJECT_INDEX_INVALID_SOURCE": "원본 오류",
}


def _safe_label(value: str) -> str:
    return "".join(
        character if ord(character) >= 32 and ord(character) != 127 else "?"
        for character in value
    )


def _legacy_present(home: Path, selected_config: Path | None) -> bool:
    legacy_root = home / ".local" / "share" / "improver" / "personal-knowledge"
    try:
        if legacy_root.exists() or legacy_root.is_symlink():
            return True
    except OSError:
        return True
    if selected_config is None:
        return False
    try:
        claude = _read_optional(
            config_target(selected_config, "CLAUDE.md", home=home)
        )
        if claude is not None and (
            _LEGACY_START in claude or _LEGACY_END in claude
        ):
            return True
        settings = _read_optional(
            config_target(selected_config, "settings.json", home=home)
        )
        if settings is None:
            return False
        value = config_module._load_settings(settings)
        hooks = value.get("hooks", {})
        session_start = hooks.get("SessionStart", []) if isinstance(hooks, dict) else []
        for matcher in session_start if isinstance(session_start, list) else ():
            if not isinstance(matcher, dict):
                continue
            matcher_hooks = matcher.get("hooks", ())
            if not isinstance(matcher_hooks, list):
                continue
            if any(_is_legacy_hook(hook, legacy_root) for hook in matcher_hooks):
                return True
    except (OSError, ValueError):
        return False
    return False


def _diagnostic_problems(*, home: Path, cwd, config) -> tuple[Problem, ...]:
    try:
        return inspect(home=home, cwd=cwd, config=config)
    except (OSError, ValueError):
        return (
            Problem(
                token="CLAUDE_CONFIG_INVALID",
                impact="Claude 설정을 안전하게 읽을 수 없어 연결 상태를 확인하지 못합니다.",
                action="didim setup",
            ),
        )


def status_text(*, home=None, cwd=None, config=None) -> str:
    """Summarize current state without exposing absolute home paths."""
    selected_home = Path.home() if home is None else Path(home)
    selected_home = Path(os.path.abspath(selected_home))
    personal_token = _personal_check(data_home(selected_home))
    personal_label = _PERSONAL_LABELS.get(personal_token, "확인 필요")

    project_root = _discover_git_root(cwd)
    if project_root is None:
        project_name = "없음"
        project_label = "설정되지 않음"
    else:
        project_name = _safe_label(project_root.name)
        if _prepared_project(project_root):
            project_token = _project_check(project_root)
            project_label = _PROJECT_LABELS.get(project_token, "확인 필요")
        else:
            project_label = "설정되지 않음"

    problems = _diagnostic_problems(
        home=selected_home,
        cwd=cwd,
        config=config,
    )
    wiring_problem = any(
        problem.token.startswith("CLAUDE_")
        or problem.token == "PERSONAL_RULES_INVALID"
        for problem in problems
    )
    claude_label = "문제 있음" if wiring_problem else "정상"
    try:
        selected_config = config_dir(config, home=selected_home)
    except ValueError:
        selected_config = None
    legacy_label = (
        "감지됨"
        if _legacy_present(selected_home, selected_config)
        else "없음"
    )
    return "\n".join(
        (
            "Didimlog {}".format(didimlog_version()),
            "개인 지식: {}".format(personal_label),
            "현재 프로젝트: {}".format(project_name),
            "프로젝트 근거: {}".format(project_label),
            "Claude 연결: {}".format(claude_label),
            "legacy Personal Knowledge: {}".format(legacy_label),
            "",
        )
    )


def doctor_text(*, home=None, cwd=None, config=None) -> tuple[int, str]:
    """Return stable diagnosis text and a nonzero policy exit for any problem."""
    selected_home = Path.home() if home is None else Path(home)
    selected_home = Path(os.path.abspath(selected_home))
    problems = list(
        _diagnostic_problems(home=selected_home, cwd=cwd, config=config)
    )
    try:
        selected_config = config_dir(config, home=selected_home)
    except ValueError:
        selected_config = None
    if _legacy_present(selected_home, selected_config):
        problems.append(
            Problem(
                token="LEGACY_WIRING_PRESENT",
                impact="이전 Personal Knowledge 연결과 Didimlog 연결이 함께 남아 있습니다.",
                action="didim setup",
            )
        )
    if not problems:
        return 0, "DOCTOR_OK\n문제 없음\n"

    lines = ["DOCTOR_PROBLEMS"]
    for problem in problems:
        lines.extend(
            (
                "무엇: {}".format(problem.token),
                "영향: {}".format(problem.impact),
                "수정: {}".format(problem.action),
                "",
            )
        )
    return EXIT_POLICY, "\n".join(lines)
