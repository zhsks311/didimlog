"""Fail-open Claude SessionStart hook for read-only Didimlog checks."""

from __future__ import annotations

import json

from .probe import inspect


def _write_json(stdout, payload: dict[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False) + "\n"
    try:
        stdout.write(text)
    except TypeError:
        stdout.write(text.encode("utf-8"))


def _repair_command(problems) -> str:
    """Pick the single most specific repair, so one command fixes the most."""
    setup_actions = [
        problem.action
        for problem in problems
        if problem.action.endswith("didim setup")
    ]
    if not setup_actions:
        return "didim index"
    profile_actions = []
    for action in setup_actions:
        if action != "didim setup" and action not in profile_actions:
            profile_actions.append(action)
    if not profile_actions:
        return "didim setup"
    # 프로필별 명령이 여럿이면 하나만 보여 주는 것은 거짓이다.
    # 그 하나를 실행해도 나머지는 그대로 남는다.
    if len(profile_actions) > 1:
        return "didim doctor"
    return profile_actions[0]


def _message(problems) -> str:
    lines = ["Didimlog 상태 확인에서 문제가 발견됐습니다."]
    lines.extend(
        "- {}: {}".format(problem.token, problem.impact) for problem in problems
    )
    lines.append("수정: {}".format(_repair_command(problems)))
    return "\n".join(lines)


def session_start(stdin, stdout) -> int:
    """Consume the hook payload, report only state, and never block Claude startup."""
    try:
        raw = stdin.read()
        try:
            payload = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            payload = {}
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        problems = inspect(cwd=cwd)
        result: dict[str, object] = {"continue": True}
        if problems:
            result["systemMessage"] = _message(problems)
    except Exception:
        result = {
            "continue": True,
            "systemMessage": (
                "Didimlog 상태를 확인하지 못했습니다. 수정: didim doctor"
            ),
        }
    _write_json(stdout, result)
    return 0
