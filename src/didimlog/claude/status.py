"""Human-readable, privacy-safe Didimlog status and diagnosis."""

from __future__ import annotations

from dataclasses import dataclass

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
from didimlog.connections import (
    ClientStatus,
    inspect_connections,
    load_state,
)

from .connect import managed_connection_present
from .probe import Problem, _index_problem, inspect


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


@dataclass(frozen=True)
class StatusSnapshot:
    version: str
    personal_token: str
    project_name: str | None
    project_token: str
    claude_token: str
    problems: tuple[Problem, ...]
    client_statuses: tuple[ClientStatus, ...] = ()


def _safe_label(value: str) -> str:
    return "".join(
        "?" if unicodedata.category(character).startswith("C") else character
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
    personal_token: str,
    project_token: str,
) -> tuple[Problem, ...]:
    try:
        return inspect(
            home=home,
            cwd=cwd,
            config=config,
            _project_root=project_root,
            _personal_token=personal_token,
            _project_token=project_token,
        )
    except (OSError, ValueError):
        problems = [
            Problem(
                token="CLAUDE_CONFIG_INVALID",
                impact="Claude 설정을 안전하게 읽을 수 없어 연결 상태를 확인하지 못합니다.",
                action="didim setup",
            )
        ]
        for token, personal in (
            (personal_token, True),
            (project_token, False),
        ):
            if not token.startswith(
                "PERSONAL_INDEX_" if personal else "PROJECT_INDEX_"
            ):
                continue
            problem = _index_problem(token, personal=personal)
            if problem is not None:
                problems.append(problem)
        return tuple(problems)


def status_snapshot(
    *,
    home=None,
    cwd=None,
    config=None,
    _personal_token: str | None = None,
) -> StatusSnapshot:
    """Return typed, privacy-safe read-only health state for other surfaces."""
    selected_home = Path.home() if home is None else Path(home)
    selected_home = Path(os.path.abspath(selected_home))
    personal_token = (
        _personal_check(data_home(selected_home))
        if _personal_token is None
        else _personal_token
    )
    project_root, project_problem = _discover_project(cwd)

    if project_problem is not None:
        project_name = None
        project_token = "PROJECT_STATUS_UNKNOWN"
    elif project_root is None:
        project_name = None
        project_token = "PROJECT_NOT_CONFIGURED"
    else:
        project_name = _safe_label(project_root.name)
        project_token = (
            _project_check(project_root)
            if _prepared_project(project_root)
            else "PROJECT_NOT_CONFIGURED"
        )

    state_error = False
    try:
        state, _state_raw = load_state(selected_home)
    except (OSError, ValueError):
        state = None
        state_error = True

    client_statuses: tuple[ClientStatus, ...] = ()
    if state is None and not state_error:
        problems = list(
            _diagnostic_problems(
                home=selected_home,
                cwd=cwd,
                config=config,
                project_root=project_root,
                personal_token=personal_token,
                project_token=project_token,
            )
        )
        wiring_problem = any(
            problem.token.startswith("CLAUDE_")
            or problem.token == "PERSONAL_RULES_INVALID"
            for problem in problems
        )
        claude_token = "CLAUDE_PROBLEMS" if wiring_problem else "CLAUDE_OK"
    else:
        problems = []
        for token, personal in ((personal_token, True), (project_token, False)):
            if not token.startswith(
                "PERSONAL_INDEX_" if personal else "PROJECT_INDEX_"
            ):
                continue
            problem = _index_problem(token, personal=personal)
            if problem is not None:
                problems.append(problem)
        if state_error:
            problems.append(
                Problem(
                    token="CONNECTION_STATE_INVALID",
                    impact="선택한 도구 연결 상태를 안전하게 읽을 수 없습니다.",
                    action="didim doctor",
                )
            )
            claude_token = "CLAUDE_STATUS_UNKNOWN"
        else:
            inspected, connection_problems = inspect_connections(
                state,
                home=selected_home,
            )
            client_statuses = tuple(
                status for status in inspected if status.client != "claude"
            )
            problems.extend(
                Problem(token=token, impact=impact, action=action)
                for token, impact, action in connection_problems
            )
            claude_roots = state.clients.get("claude", {})
            connected_claude = tuple(
                Path(root)
                for root, selection in claude_roots.items()
                if selection.intent == "connected"
            )
            selected_claude = (
                (config,)
                if config is not None
                else connected_claude
            )
            if selected_claude:
                claude_problems = []
                for selected_config in selected_claude:
                    claude_problems.extend(
                        _diagnostic_problems(
                            home=selected_home,
                            cwd=cwd,
                            config=selected_config,
                            project_root=project_root,
                            personal_token=personal_token,
                            project_token=project_token,
                        )
                    )
                problems.extend(
                    problem
                    for problem in claude_problems
                    if problem.token.startswith("CLAUDE_")
                    or problem.token == "PERSONAL_RULES_INVALID"
                )
                claude_token = (
                    "CLAUDE_PROBLEMS"
                    if any(
                        problem.token.startswith("CLAUDE_")
                        or problem.token == "PERSONAL_RULES_INVALID"
                        for problem in claude_problems
                    )
                    else "CLAUDE_OK"
                )
            elif claude_roots:
                claude_token = "CLAUDE_DISABLED"
            elif managed_connection_present(None, home=selected_home) is None:
                problems.append(
                    Problem(
                        token="CLAUDE_CONFIG_INVALID",
                        impact="Claude 설정을 안전하게 읽을 수 없어 연결 상태를 확인하지 못합니다.",
                        action="didim setup",
                    )
                )
                claude_token = "CLAUDE_STATUS_UNKNOWN"
            else:
                claude_token = "CLAUDE_UNSELECTED"

    if project_problem is not None:
        problems.append(project_problem)
    unique_problems = {
        (problem.token, problem.impact, problem.action): problem
        for problem in problems
    }
    return StatusSnapshot(
        version=didimlog_version(),
        personal_token=personal_token,
        project_name=project_name,
        project_token=project_token,
        claude_token=claude_token,
        problems=tuple(unique_problems.values()),
        client_statuses=client_statuses,
    )


def status_text(*, home=None, cwd=None, config=None) -> str:
    """Summarize current state without exposing absolute home paths."""
    snapshot = status_snapshot(home=home, cwd=cwd, config=config)
    personal_label = _PERSONAL_LABELS.get(
        snapshot.personal_token,
        "확인 필요",
    )
    project_name = (
        "확인 실패"
        if snapshot.project_token == "PROJECT_STATUS_UNKNOWN"
        else snapshot.project_name or "없음"
    )
    if snapshot.project_token == "PROJECT_NOT_CONFIGURED":
        project_label = "설정되지 않음"
    elif snapshot.project_token == "PROJECT_STATUS_UNKNOWN":
        project_label = "확인 실패"
    else:
        project_label = _PROJECT_LABELS.get(
            snapshot.project_token,
            "확인 필요",
        )
    claude_label = {
        "CLAUDE_OK": "정상",
        "CLAUDE_PROBLEMS": "문제 있음",
        "CLAUDE_DISABLED": "해제됨",
        "CLAUDE_UNSELECTED": "선택하지 않음",
        "CLAUDE_STATUS_UNKNOWN": "연결 상태 확인 실패",
    }.get(snapshot.claude_token, "확인 필요")
    lines = [
        "Didimlog {}".format(snapshot.version),
        "개인 지식: {}".format(personal_label),
        "현재 프로젝트: {}".format(project_name),
        "프로젝트 근거: {}".format(project_label),
        "Claude 연결: {}".format(claude_label),
    ]
    for client in ("omp", "codex"):
        statuses = tuple(
            status for status in snapshot.client_statuses if status.client == client
        )
        if not statuses:
            continue
        tokens = {status.token for status in statuses}
        if any(token.endswith("_CONNECTION_BROKEN") for token in tokens):
            label = "문제 있음"
        elif any(token.endswith("_RESIDUAL_DISCOVERY") for token in tokens):
            label = "해제했지만 자동 발견 가능"
        elif all(token.endswith("_DISABLED") for token in tokens):
            label = "해제됨"
        else:
            label = "설치됨, 실행 미확인"
        lines.append("{} 연결: {}".format(client.upper(), label))
    lines.append("")
    return "\n".join(lines)


def doctor_text(*, home=None, cwd=None, config=None) -> tuple[int, str]:
    """Return stable diagnosis text and a nonzero policy exit for any problem."""
    problems = status_snapshot(home=home, cwd=cwd, config=config).problems
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
