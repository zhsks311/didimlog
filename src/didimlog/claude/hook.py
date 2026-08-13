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
    for action in setup_actions:
        if action != "didim setup":
            return action
    return "didim setup"


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
