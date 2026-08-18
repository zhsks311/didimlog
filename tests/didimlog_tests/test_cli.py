import contextlib
import io
import unittest

from didimlog import cli


class TerminalBuffer(io.StringIO):
    def __init__(self, *, tty: bool):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def invoke(argv, *, tty=False):
    stdout = io.StringIO()
    stderr = TerminalBuffer(tty=tty)
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        return_code = cli.main(argv)
    return return_code, stdout.getvalue(), stderr.getvalue()


class CliShellTests(unittest.TestCase):
    def test_no_arguments_prints_help_to_stdout(self):
        return_code, stdout, stderr = invoke([])

        self.assertEqual(return_code, 0)
        self.assertIn("usage: didim", stdout)
        self.assertEqual(stderr, "")

    def test_help_prints_only_to_stdout(self):
        return_code, stdout, stderr = invoke(["--help"])

        self.assertEqual(return_code, 0)
        self.assertIn("usage: didim", stdout)
        self.assertEqual(stderr, "")

    def test_unknown_command_is_machine_token_only_in_non_tty(self):
        return_code, stdout, stderr = invoke(["unknown"])

        self.assertEqual(return_code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "CLI_USAGE_ERROR\n")

    def test_explain_errors_adds_one_korean_help_line(self):
        return_code, stdout, stderr = invoke(["--explain-errors", "unknown"])

        self.assertEqual(return_code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "CLI_USAGE_ERROR\n"
            "도움말: 명령과 옵션을 확인하고 didim --help로 사용법을 살펴보세요.\n",
        )

    def test_explain_errors_does_not_accept_abbreviations(self):
        return_code, stdout, stderr = invoke(["--explain"])

        self.assertEqual(return_code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "CLI_USAGE_ERROR\n")

    def test_tty_automatically_adds_help_line(self):
        return_code, stdout, stderr = invoke(["unknown"], tty=True)

        self.assertEqual(return_code, 2)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("CLI_USAGE_ERROR\n도움말: "))

    def test_domain_error_preserves_token_exit_code_and_custom_help(self):
        from didimlog.errors import DidimError, emit_error

        stderr = TerminalBuffer(tty=False)
        error = DidimError(
            "PROJECT_POLICY_INVALID",
            exit_code=3,
            help_text="프로젝트 설정을 확인하세요.",
        )
        with contextlib.redirect_stderr(stderr):
            return_code = emit_error(error, explain=True, tty=False)

        self.assertEqual(return_code, 3)
        self.assertEqual(
            stderr.getvalue(),
            "PROJECT_POLICY_INVALID\n도움말: 프로젝트 설정을 확인하세요.\n",
        )

    def test_domain_error_prints_details_only_when_explained(self):
        from didimlog.errors import DidimError, emit_error

        error = DidimError(
            "PERSONAL_INDEX_INVALID_SOURCE",
            exit_code=3,
            details=(
                "무엇: docs/work/invalid.md",
                "이유: missing title or find_when",
            ),
            help_text="표시된 개인 지식 원본을 고친 뒤 didim index를 다시 실행하세요.",
        )
        plain = TerminalBuffer(tty=False)
        with contextlib.redirect_stderr(plain):
            plain_code = emit_error(error, explain=False, tty=False)
        explained = TerminalBuffer(tty=False)
        with contextlib.redirect_stderr(explained):
            explained_code = emit_error(error, explain=True, tty=False)

        self.assertEqual(plain_code, 3)
        self.assertEqual(plain.getvalue(), "PERSONAL_INDEX_INVALID_SOURCE\n")
        self.assertEqual(explained_code, 3)
        self.assertEqual(
            explained.getvalue(),
            "PERSONAL_INDEX_INVALID_SOURCE\n"
            "무엇: docs/work/invalid.md\n"
            "이유: missing title or find_when\n"
            "도움말: 표시된 개인 지식 원본을 고친 뒤 didim index를 다시 실행하세요.\n",
        )

    def test_git_unavailable_exit_code_is_preserved(self):
        from didimlog.errors import DidimError, emit_error

        stderr = TerminalBuffer(tty=False)
        with contextlib.redirect_stderr(stderr):
            return_code = emit_error(
                DidimError("GIT_UNAVAILABLE", exit_code=7),
                explain=False,
                tty=False,
            )

        self.assertEqual(return_code, 7)
        self.assertEqual(stderr.getvalue(), "GIT_UNAVAILABLE\n")


if __name__ == "__main__":
    unittest.main()
