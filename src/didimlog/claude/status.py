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
    return "\n".join(
        (
            "Didimlog {}".format(didimlog_version()),
            "개인 지식: {}".format(personal_label),
            "현재 프로젝트: {}".format(project_name),
            "프로젝트 근거: {}".format(project_label),
            "Claude 연결: {}".format(claude_label),
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
