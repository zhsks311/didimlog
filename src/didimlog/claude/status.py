"""Human-readable, privacy-safe Didimlog status and diagnosis."""

from __future__ import annotations

import os
import unicodedata
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

from .paths import config_dir
from .probe import Problem, inspect


_PERSONAL_LABELS = {
    "PERSONAL_INDEX_CURRENT": "최신",
    "PERSONAL_INDEX_MISSING": "목록 없음",
    "PERSONAL_INDEX_STALE": "갱신 필요",
    "PERSONAL_INDEX_EXTRA": "알 수 없는 index 파일 있음",
    "PERSONAL_INDEX_INVALID_SOURCE": "원본 오류",
    "PERSONAL_INDEX_BUSY": "사용 중이라 확인 못 함",
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
        "?" if unicodedata.category(character).startswith("C") else character
        for character in value
    )


def _profile_label(home: Path, config) -> str:
    """Name the selected Claude profile without exposing the home path."""

    try:
        selected = config_dir(config, home=home)
    except (OSError, ValueError):
        return "확인 실패"
    try:
        relative = selected.relative_to(home.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return "확인 실패"
    if len(relative.parts) != 1:
        return "확인 실패"
    return _safe_label(relative.parts[0])


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
                blocks_repair=True,
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
    # 어떤 프로필이 왜 막혔는지는 doctor만 안다. status는 그 경로를 알려 준다.
    claude_label = "문제 있음 (didim doctor)" if wiring_problem else "정상"
    return "\n".join(
        (
            "Didimlog {}".format(didimlog_version()),
            "개인 지식: {}".format(personal_label),
            "현재 프로젝트: {}".format(project_name),
            "프로젝트 근거: {}".format(project_label),
            "Claude 프로필: {}".format(_profile_label(selected_home, config)),
            "Claude 연결: {}".format(claude_label),
            "",
        )
    )


def _problem_lines(problem: Problem) -> tuple[str, ...]:
    return (
        "무엇: {}".format(problem.token),
        "영향: {}".format(problem.impact),
        "수정: {}".format(problem.action),
        "",
    )


def _healthy_doctor_text(project_root: Path | None) -> str:
    """Report a healthy run, and name a Git project that stores no evidence yet.

    An unprepared project is a choice, not a fault, so the exit stays ``0``.
    """

    if project_root is None or _prepared_project(project_root):
        return "DOCTOR_OK\n문제 없음\n"
    return "\n".join(
        (
            "DOCTOR_OK",
            "문제 없음",
            "안내: PROJECT_NOT_CONFIGURED",
            "이 Git 프로젝트는 아직 근거를 저장하지 않습니다.",
            "여기에도 근거를 남기려면: didim setup",
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
        return 0, _healthy_doctor_text(project_root)

    blocking = [problem for problem in problems if problem.blocks_repair]
    if not blocking:
        lines = ["DOCTOR_PROBLEMS"]
        for problem in problems:
            lines.extend(_problem_lines(problem))
        return EXIT_POLICY, "\n".join(lines)

    blocking_tokens = {problem.token for problem in blocking}
    symptoms_by_cause = {
        problem.token: [
            candidate
            for candidate in problems
            if candidate.caused_by == problem.token
        ]
        for problem in blocking
    }
    independent = [
        problem
        for problem in problems
        if problem.token not in blocking_tokens
        and problem.caused_by not in blocking_tokens
    ]

    lines = ["DOCTOR_PROBLEMS", "먼저 할 일"]
    if len(blocking) > 1:
        # 원인이 여럿이면 어느 것도 나머지를 대신 고치지 못한다.
        # 순서를 지어내는 대신 전부 필요하다는 사실을 밝힌다.
        lines.append("아래 {}가지를 모두 고쳐야 합니다.".format(len(blocking)))
        lines.append("")
    for problem in blocking:
        lines.extend(_problem_lines(problem))
        symptoms = symptoms_by_cause[problem.token]
        if symptoms:
            lines.append("위를 고치면 함께 해결되는 증상")
            lines.extend(
                "- {}: {}".format(symptom.token, symptom.impact)
                for symptom in symptoms
            )
            lines.append("")
    if independent:
        lines.append("별도로 확인할 문제")
        for problem in independent:
            lines.extend(_problem_lines(problem))
    return EXIT_POLICY, "\n".join(lines)
