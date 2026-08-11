"""Human-readable, privacy-safe Didimlog status and diagnosis."""

from __future__ import annotations

import os
from pathlib import Path

from didimlog import version as didimlog_version
from didimlog.errors import DidimError, EXIT_POLICY
from didimlog.indexing import (
    _personal_check,
    _prepared_project,
    _project_check,
)
from didimlog.project.git_exclude import discover_project_for_setup
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


def _project_discovery_problem(error: DidimError) -> Problem | None:
    if error.token != "PROJECT_EXCLUDE_GIT_UNAVAILABLE":
        return None
    return Problem(
        token=error.token,
        impact="Git 저장소를 확인하지 못해 현재 프로젝트 근거 상태를 진단할 수 없습니다.",
        action="Git 설치와 현재 저장소 상태를 확인한 뒤 다시 시도하세요.",
    )


def _discover_project(cwd) -> tuple[Path | None, Problem | None]:
    try:
        return discover_project_for_setup(cwd), None
    except DidimError as error:
        problem = _project_discovery_problem(error)
        if problem is None:
            raise
        return None, problem


def _diagnostic_problems(
    *,
    home: Path,
    cwd,
    config,
    project_root: Path | None,
) -> tuple[Problem, ...]:
    try:
        return inspect(
            home=home,
            cwd=cwd,
            config=config,
            _project_root=project_root,
        )
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

    project_root, project_problem = _discover_project(cwd)
    if project_problem is not None:
        project_name = "확인 실패"
        project_label = "확인 실패"
    elif project_root is None:
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
        project_root=project_root,
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
    project_root, project_problem = _discover_project(cwd)
    problems = list(
        _diagnostic_problems(
            home=selected_home,
            cwd=cwd,
            config=config,
            project_root=project_root,
        )
    )
    if project_problem is not None:
        problems.append(project_problem)
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
