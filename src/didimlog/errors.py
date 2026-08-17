"""Stable command-line errors and human-readable explanations."""

import sys


EXIT_USAGE = 2
EXIT_POLICY = 3
EXIT_SECRET = 5
EXIT_GIT = 7

_DEFAULT_HELP = {
    EXIT_USAGE: "명령과 옵션을 확인하고 didim --help로 사용법을 살펴보세요.",
    EXIT_POLICY: "경로와 지식 무결성 정책을 확인한 뒤 다시 시도하세요.",
    EXIT_SECRET: "비밀값을 제거하거나 안전한 라벨로 바꾼 뒤 다시 시도하세요.",
    EXIT_GIT: "Git 설치와 현재 저장소 상태를 확인한 뒤 다시 시도하세요.",
}


class DidimError(Exception):
    """A stable machine token with an exit code and optional explanation."""

    def __init__(
        self,
        token: str,
        *,
        exit_code: int,
        help_text: str | None = None,
        details: tuple[str, ...] = (),
    ) -> None:
        super().__init__(token)
        self.token = token
        self.exit_code = exit_code
        self.help_text = help_text
        self.details = tuple(details)


def emit_error(error: DidimError, *, explain: bool, tty: bool) -> int:
    """Write the stable token first and an explanation only when requested."""
    print(error.token, file=sys.stderr)
    if explain or tty:
        for detail in error.details:
            print(detail, file=sys.stderr)
        help_text = error.help_text or _DEFAULT_HELP.get(
            error.exit_code,
            "첫 번째 오류 줄을 확인한 뒤 입력을 고치세요.",
        )
        print(f"도움말: {help_text}", file=sys.stderr)
    return error.exit_code
