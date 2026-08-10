"""Didimlog command-line interface."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unicodedata

from didimlog import version as didimlog_version
from didimlog.claude.connect import (
    apply_connect,
    apply_disconnect,
    plan_connect,
    plan_disconnect,
)
from didimlog.claude.hook import session_start
from didimlog.claude.setup import apply_setup, plan_setup
from didimlog.claude.status import doctor_text, status_text
from didimlog.claude.transaction import InstallJournal
from didimlog.errors import (
    DidimError,
    EXIT_POLICY,
    EXIT_SECRET,
    EXIT_USAGE,
    emit_error,
)
from didimlog.indexing import run_index
from didimlog.personal.lesson import parse_lesson_text
from didimlog.personal.lesson_writing import (
    LessonError,
    LessonExists,
    LessonInvalid,
    LessonSecret,
    publish_lesson,
)
from didimlog.project.capture import CaptureRequest, capture


_EXPLAIN_ERRORS = "--explain-errors"
_STDIN_MAX_BYTES = 64 * 1024
_COMMANDS = """\
didim setup
didim connect claude
didim disconnect claude
didim add lesson
didim add observation
didim add experiment
didim add evidence
didim index
didim status
didim doctor
"""


class DidimArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DidimError("CLI_USAGE_ERROR", exit_code=EXIT_USAGE)


def _explain_requested(argv: list[str]) -> bool:
    for argument in argv:
        if argument == "--":
            return False
        if argument == _EXPLAIN_ERRORS:
            return True
    return False


def _stderr_is_tty() -> bool:
    try:
        return bool(sys.stderr.isatty())
    except Exception:
        return False


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config-dir",
        type=Path,
        help="Claude 설정 디렉터리를 직접 선택합니다.",
    )


def _add_record_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", help="생성 날짜 YYYY-MM-DD")
    parser.add_argument("--title", required=True, help="record 제목")
    parser.add_argument("--scope", default="project", help="project 또는 task:<name>")
    parser.add_argument("--tags", default="", help="쉼표로 구분한 태그")
    parser.add_argument("--sources", default="", help="쉼표로 구분한 record ID")


def build_parser() -> argparse.ArgumentParser:
    parser = DidimArgumentParser(
        prog="didim",
        allow_abbrev=False,
        description=(
            "확인한 사실과 재사용할 교훈을 안전하게 남깁니다.\n\n" + _COMMANDS
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version="Didimlog {}".format(didimlog_version()),
    )
    parser.add_argument(_EXPLAIN_ERRORS, action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(_help_parser=parser)
    commands = parser.add_subparsers(dest="command", metavar="command")

    setup = commands.add_parser("setup", help="저장 공간과 Claude 연결 준비")
    setup.add_argument("--dry-run", action="store_true", help="변경 계획만 표시")
    setup.add_argument("--yes", action="store_true", help="변경 계획 승인")
    setup.add_argument("--skip-claude", action="store_true", help="Claude 연결 건너뛰기")
    _add_config(setup)
    setup.set_defaults(_handler=_setup, _help_parser=setup)

    connect = commands.add_parser("connect", help="도구 연결")
    connect.set_defaults(_help_parser=connect)
    connect_tools = connect.add_subparsers(dest="connect_tool", metavar="tool")
    connect_claude = connect_tools.add_parser("claude", help="Claude Code 연결")
    _add_config(connect_claude)
    connect_claude.add_argument("--yes", action="store_true", help="변경 계획 승인")
    connect_claude.set_defaults(_handler=_connect_claude, _help_parser=connect_claude)

    disconnect = commands.add_parser("disconnect", help="도구 연결 해제")
    disconnect.set_defaults(_help_parser=disconnect)
    disconnect_tools = disconnect.add_subparsers(
        dest="disconnect_tool",
        metavar="tool",
    )
    disconnect_claude = disconnect_tools.add_parser(
        "claude",
        help="Claude Code 연결 해제",
    )
    _add_config(disconnect_claude)
    disconnect_claude.set_defaults(
        _handler=_disconnect_claude,
        _help_parser=disconnect_claude,
    )

    add = commands.add_parser("add", help="새 자료 저장")
    add.set_defaults(_help_parser=add)
    add_types = add.add_subparsers(dest="add_type", metavar="type")
    lesson = add_types.add_parser("lesson", help="재사용할 교훈 저장")
    lesson.add_argument("slug", help="영문·숫자·하이픈 파일명")
    lesson.add_argument("--date", help="문서와 일치하는 생성 날짜 YYYY-MM-DD")
    destination = lesson.add_mutually_exclusive_group()
    destination.add_argument("--project", help="대상 프로젝트 이름")
    destination.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="모든 프로젝트에 적용",
    )
    lesson.set_defaults(_handler=_add_lesson, _help_parser=lesson)
    for record_type in ("observation", "experiment", "evidence"):
        record = add_types.add_parser(
            record_type,
            help="{} record 저장".format(record_type),
        )
        _add_record_options(record)
        record.set_defaults(_handler=_add_record, _help_parser=record)

    index = commands.add_parser("index", help="검색 목록 생성 또는 확인")
    index.add_argument("--check", action="store_true", help="파일을 바꾸지 않고 확인")
    index.set_defaults(_handler=_index, _help_parser=index)

    status = commands.add_parser("status", help="현재 상태 요약")
    _add_config(status)
    status.set_defaults(_handler=_status, _help_parser=status)

    doctor = commands.add_parser("doctor", help="문제와 수정 방법 진단")
    _add_config(doctor)
    doctor.set_defaults(_handler=_doctor, _help_parser=doctor)

    hook = commands.add_parser("hook", help="Claude가 호출하는 내부 상태 확인")
    hook.set_defaults(_help_parser=hook)
    hook_types = hook.add_subparsers(dest="hook_type", metavar="hook")
    session = hook_types.add_parser("session-start", help="세션 시작 상태 확인")
    session.set_defaults(_handler=_session_start, _help_parser=session)
    return parser


def _summary(plan) -> str:
    groups = (
        ("개인 교훈", plan.personal_changes),
        ("프로젝트 근거", plan.project_changes),
        ("Claude 연결", plan.claude_changes),
    )
    lines = ["Didimlog {} 변경 계획".format(plan.version)]
    for label, changes in groups:
        lines.extend(("", label))
        lines.extend(
            ("- {}".format(change) for change in changes)
            if changes
            else ("- 변경 없음",)
        )
    return "\n".join(lines) + "\n"


def _setup(args) -> int:
    if args.dry_run and args.yes:
        raise DidimError("CLI_USAGE_ERROR", exit_code=EXIT_USAGE)
    plan = plan_setup(
        home=None,
        cwd=None,
        config=args.config_dir,
        include_project=True,
        skip_claude=args.skip_claude,
    )
    print(_summary(plan), end="")
    if args.dry_run:
        return 0
    if args.yes:
        approved = True
    elif not sys.stdin.isatty():
        raise DidimError(
            "SETUP_APPROVAL_REQUIRED",
            exit_code=EXIT_USAGE,
            help_text="변경 요약을 확인한 뒤 --yes를 사용하세요.",
        )
    else:
        approved = input("이 변경을 적용할까요? [y/N]: ").strip().lower() in {
            "y",
            "yes",
        }
        if not approved:
            print("변경하지 않았습니다.")
            return 0
    apply_setup(plan, approved=approved)
    print("Didimlog 준비를 마쳤습니다.")
    return 0


def _find_launcher() -> Path:
    executable = shutil.which("didim")
    if executable is None:
        raise DidimError(
            "DIDIM_LAUNCHER_MISSING",
            exit_code=EXIT_POLICY,
            help_text="설치된 didim 명령을 확인한 뒤 다시 실행하세요.",
        )
    return Path(executable)


def _apply_claude(plan, apply) -> None:
    with tempfile.TemporaryDirectory(prefix="didimlog-cli-") as directory:
        journal = InstallJournal(Path(directory) / "journal.json", reset=True)
        apply(plan, journal)


def _print_changes(title: str, changes: tuple[str, ...]) -> None:
    print(title)
    if changes:
        for change in changes:
            print("- {}".format(change))
    else:
        print("- 변경 없음")


def _connect_claude(args) -> int:
    plan = plan_connect(args.config_dir, launcher=_find_launcher())
    _print_changes("Claude 연결", plan.changes)
    if args.yes:
        approved = True
    elif not sys.stdin.isatty():
        raise DidimError(
            "CLAUDE_CONNECT_APPROVAL_REQUIRED",
            exit_code=EXIT_USAGE,
            help_text="변경 요약을 확인한 뒤 --yes를 사용하세요.",
        )
    else:
        approved = input("이 변경을 적용할까요? [y/N]: ").strip().lower() in {
            "y",
            "yes",
        }
        if not approved:
            print("변경하지 않았습니다.")
            return 0
    _apply_claude(plan, apply_connect)
    return 0


def _disconnect_claude(args) -> int:
    plan = plan_disconnect(args.config_dir)
    _print_changes("Claude 연결 해제", plan.changes)
    _apply_claude(plan, apply_disconnect)
    return 0


def _date(args) -> str:
    if args.date:
        return args.date
    if not sys.stdin.isatty():
        raise DidimError("ADD_DATE_REQUIRED", exit_code=EXIT_USAGE)
    default = datetime.date.today().isoformat()
    entered = input("날짜 [{}]: ".format(default)).strip()
    return entered or default


def _stdin_text() -> str:
    text = sys.stdin.read(_STDIN_MAX_BYTES + 1)
    try:
        encoded = text.encode("utf-8")
    except (AttributeError, UnicodeError) as error:
        raise DidimError("ADD_STDIN_INVALID", exit_code=EXIT_USAGE) from error
    if not encoded:
        raise DidimError("ADD_STDIN_REQUIRED", exit_code=EXIT_USAGE)
    if len(encoded) > _STDIN_MAX_BYTES:
        raise DidimError("ADD_STDIN_TOO_LARGE", exit_code=EXIT_USAGE)
    return text


def _comma_values(raw: str, *, tags: bool) -> tuple[str, ...]:
    if not raw:
        return ()
    values = [value.strip() for value in raw.split(",")]
    if tags:
        values = [
            "".join(
                character.lower() if "A" <= character <= "Z" else character
                for character in unicodedata.normalize("NFKC", value)
            )
            for value in values
        ]
    return tuple(sorted(values, key=lambda value: value.encode("utf-8")))


def _add_lesson(args) -> int:
    created = _date(args)
    text = _stdin_text()
    parsed = parse_lesson_text(args.slug + ".md", text)
    if parsed is None:
        raise DidimError("LESSON_INVALID", exit_code=EXIT_USAGE)
    fields, _, _ = parsed
    if fields["date"] != created:
        raise DidimError("LESSON_DATE_MISMATCH", exit_code=EXIT_USAGE)
    project = "_global" if args.global_scope else args.project
    path = publish_lesson(args.slug, text, project=project)
    print(path.as_posix())
    return 0


def _add_record(args) -> int:
    created = _date(args)
    raw = _stdin_text()
    try:
        fields = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        raise DidimError("ADD_STDIN_INVALID", exit_code=EXIT_USAGE) from error
    if not isinstance(fields, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in fields.items()
    ):
        raise DidimError("ADD_STDIN_INVALID", exit_code=EXIT_USAGE)
    request = CaptureRequest(
        type=args.add_type,
        date=created,
        scope=args.scope,
        title=args.title,
        tags=_comma_values(args.tags, tags=True),
        sources=_comma_values(args.sources, tags=False),
        fields=fields,
    )
    path = capture(Path.cwd(), request)
    print(path.as_posix())
    return 0


def _index(args) -> int:
    result = run_index(check=args.check)
    print(result.personal)
    print(result.project)
    if not args.check:
        return 0
    personal_current = result.personal.endswith("PERSONAL_INDEX_CURRENT")
    project_current = result.project.endswith("PROJECT_INDEX_CURRENT")
    project_unconfigured = "설정되지 않음" in result.project
    return 0 if personal_current and (project_current or project_unconfigured) else EXIT_POLICY


def _status(args) -> int:
    print(status_text(config=args.config_dir), end="")
    return 0


def _doctor(args) -> int:
    code, text = doctor_text(config=args.config_dir)
    print(text, end="")
    return code


def _session_start(args) -> int:
    return session_start(sys.stdin, sys.stdout)


def _as_didim_error(error: Exception) -> DidimError:
    if isinstance(error, DidimError):
        return error
    if isinstance(error, LessonSecret):
        return DidimError("LESSON_SECRET", exit_code=EXIT_SECRET)
    if isinstance(error, LessonExists):
        return DidimError("LESSON_EXISTS", exit_code=EXIT_POLICY)
    if isinstance(error, LessonInvalid):
        return DidimError("LESSON_INVALID", exit_code=EXIT_USAGE)
    if isinstance(error, LessonError):
        return DidimError("LESSON_ERROR", exit_code=EXIT_POLICY)
    return DidimError(
        "COMMAND_FAILED",
        exit_code=EXIT_POLICY,
        help_text="didim doctor로 현재 상태를 확인하세요.",
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    explain = _explain_requested(arguments)
    try:
        parsed = parser.parse_args(arguments)
    except DidimError as error:
        return emit_error(error, explain=explain, tty=_stderr_is_tty())
    except SystemExit as exit_signal:
        return int(exit_signal.code or 0)

    handler = getattr(parsed, "_handler", None)
    if handler is None:
        parsed._help_parser.print_help()
        return 0
    try:
        return handler(parsed)
    except (DidimError, LessonError, OSError, UnicodeError, ValueError) as error:
        return emit_error(
            _as_didim_error(error),
            explain=explain,
            tty=_stderr_is_tty(),
        )


if __name__ == "__main__":
    raise SystemExit(main())
