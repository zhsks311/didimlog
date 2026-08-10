import contextlib
import datetime
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from didimlog import cli
from didimlog.personal.lesson_writing import LessonSecret
from didimlog.project.capture import CaptureRequest


class TerminalInput(io.StringIO):
    def __init__(self, value="", *, tty=False):
        super().__init__(value)
        self.tty = tty

    def isatty(self):
        return self.tty



class TerminalOutput(io.StringIO):
    def __init__(self, *, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


def invoke(argv, *, stdin="", tty=False):
    source = TerminalInput(stdin, tty=tty)
    stdout = TerminalOutput(tty=tty)
    stderr = TerminalOutput(tty=tty)
    with mock.patch.object(sys, "stdin", source), contextlib.redirect_stdout(
        stdout
    ), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class CliCommandSurfaceTests(unittest.TestCase):
    def test_help_registers_the_exact_public_and_internal_command_tree(self):
        code, stdout, stderr = invoke(["--help"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        for command in (
            "setup",
            "connect",
            "disconnect",
            "add",
            "index",
            "status",
            "doctor",
            "hook",
        ):
            self.assertIn(command, stdout)
        parser = cli.build_parser()
        self.assertNotIn("personal", parser.format_help())
        self.assertNotIn("project", parser.format_help())

    def test_group_no_args_and_help_are_successful_but_unknown_values_are_usage_errors(self):
        for argv in (["connect"], ["disconnect"], ["add"], ["hook"]):
            with self.subTest(argv=argv):
                code, stdout, stderr = invoke(argv)
                self.assertEqual(code, 0)
                self.assertIn("usage: didim", stdout)
                self.assertEqual(stderr, "")
        help_paths = (
            ["setup", "--help"],
            ["connect", "--help"],
            ["connect", "claude", "--help"],
            ["disconnect", "--help"],
            ["disconnect", "claude", "--help"],
            ["add", "--help"],
            ["add", "lesson", "--help"],
            ["add", "observation", "--help"],
            ["add", "experiment", "--help"],
            ["add", "evidence", "--help"],
            ["index", "--help"],
            ["status", "--help"],
            ["doctor", "--help"],
            ["hook", "--help"],
            ["hook", "session-start", "--help"],
        )
        for argv in help_paths:
            with self.subTest(argv=argv):
                code, stdout, stderr = invoke(argv)
                self.assertEqual(code, 0)
                self.assertIn("usage: didim", stdout)
                self.assertEqual(stderr, "")
        for argv in (["connect", "unknown"], ["add", "unknown"], ["index", "x"]):
            with self.subTest(argv=argv):
                code, stdout, stderr = invoke(argv)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "CLI_USAGE_ERROR\n")

    def test_setup_dry_run_and_yes_use_the_same_plan_summary(self):
        plan = SimpleNamespace(
            version="0.0.1",
            personal_changes=("personal one",),
            project_changes=("project one",),
            claude_changes=("claude one",),
        )
        with mock.patch("didimlog.cli.plan_setup", return_value=plan) as planned, mock.patch(
            "didimlog.cli.apply_setup"
        ) as applied:
            dry_code, dry_stdout, dry_stderr = invoke(
                ["setup", "--dry-run", "--config-dir", "/safe/config"]
            )
            yes_code, yes_stdout, yes_stderr = invoke(
                ["setup", "--yes", "--config-dir", "/safe/config"]
            )

        self.assertEqual((dry_code, dry_stderr), (0, ""))
        self.assertEqual((yes_code, yes_stderr), (0, ""))
        self.assertIn("personal one", dry_stdout)
        self.assertIn("project one", dry_stdout)
        self.assertIn("claude one", dry_stdout)
        self.assertIn("personal one", yes_stdout)
        self.assertEqual(planned.call_count, 2)
        applied.assert_called_once_with(plan, approved=True)

    def test_noninteractive_setup_without_yes_refuses_before_apply(self):
        plan = SimpleNamespace(
            version="0.0.1",
            personal_changes=(),
            project_changes=(),
            claude_changes=(),
        )
        with mock.patch("didimlog.cli.plan_setup", return_value=plan), mock.patch(
            "didimlog.cli.apply_setup"
        ) as applied:
            code, stdout, stderr = invoke(["setup"])

        self.assertEqual(code, 2)
        self.assertIn("SETUP_APPROVAL_REQUIRED", stderr)
        applied.assert_not_called()

    def test_noninteractive_connect_without_yes_refuses_before_apply(self):
        plan = SimpleNamespace(changes=("connect change",), config_dir=Path("/safe"))
        with mock.patch(
            "didimlog.cli._find_launcher", return_value=Path("/bin/didim")
        ), mock.patch(
            "didimlog.cli.plan_connect", return_value=plan
        ), mock.patch(
            "didimlog.cli.apply_connect"
        ) as applied:
            code, stdout, stderr = invoke(
                ["connect", "claude", "--config-dir", "/safe"]
            )

        self.assertEqual(code, 2)
        self.assertIn("connect change", stdout)
        self.assertEqual(stderr, "CLAUDE_CONNECT_APPROVAL_REQUIRED\n")
        applied.assert_not_called()


    def test_connect_disconnect_status_and_doctor_forward_config_selection(self):
        connect_plan = SimpleNamespace(changes=("connect change",), config_dir=Path("/safe"))
        disconnect_plan = SimpleNamespace(changes=("disconnect change",), config_dir=Path("/safe"))
        with mock.patch("didimlog.cli._find_launcher", return_value=Path("/bin/didim")), mock.patch(
            "didimlog.cli.plan_connect", return_value=connect_plan
        ) as connect, mock.patch("didimlog.cli.apply_connect"), mock.patch(
            "didimlog.cli.plan_disconnect", return_value=disconnect_plan
        ) as disconnect, mock.patch("didimlog.cli.apply_disconnect"), mock.patch(
            "didimlog.cli.status_text", return_value="status\n"
        ) as status, mock.patch(
            "didimlog.cli.doctor_text", return_value=(3, "doctor\n")
        ) as doctor:
            self.assertEqual(
                invoke(
                    ["connect", "claude", "--yes", "--config-dir", "/safe"]
                )[0],
                0,
            )
            self.assertEqual(invoke(["disconnect", "claude", "--config-dir", "/safe"])[0], 0)
            self.assertEqual(invoke(["status", "--config-dir", "/safe"])[0], 0)
            self.assertEqual(invoke(["doctor", "--config-dir", "/safe"])[0], 3)

        self.assertEqual(connect.call_args.args, (Path("/safe"),))
        self.assertEqual(disconnect.call_args.args, (Path("/safe"),))
        self.assertEqual(status.call_args.kwargs["config"], Path("/safe"))
        self.assertEqual(doctor.call_args.kwargs["config"], Path("/safe"))

    def test_index_prints_both_surfaces_and_check_fails_on_any_noncurrent_configured_surface(self):
        stale = SimpleNamespace(
            personal="개인 지식: PERSONAL_INDEX_STALE",
            project="프로젝트 근거: PROJECT_INDEX_CURRENT",
        )
        with mock.patch("didimlog.cli.run_index", return_value=stale) as run:
            code, stdout, stderr = invoke(["index", "--check"])

        self.assertEqual(code, 3)
        self.assertEqual(stderr, "")
        self.assertIn("PERSONAL_INDEX_STALE", stdout)
        self.assertIn("PROJECT_INDEX_CURRENT", stdout)
        run.assert_called_once_with(check=True)

    def test_hook_session_start_delegates_raw_streams_and_stays_zero(self):
        with mock.patch("didimlog.cli.session_start", return_value=0) as hook:
            code, _, _ = invoke(["hook", "session-start"], stdin='{"x":1}')

        self.assertEqual(code, 0)
        self.assertIsInstance(hook.call_args.args[0], TerminalInput)


class AddCommandTests(unittest.TestCase):
    def test_non_tty_requires_explicit_date_and_nonempty_stdin(self):
        code, _, stderr = invoke(
            ["add", "observation", "--title", "T"],
            stdin='{"body":"B"}',
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "ADD_DATE_REQUIRED\n")

        code, _, stderr = invoke(
            [
                "add",
                "observation",
                "--date",
                "2026-08-05",
                "--title",
                "T",
            ],
            stdin="",
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "ADD_STDIN_REQUIRED\n")

    def test_tty_prompts_with_today_as_the_default_date(self):
        today = datetime.date.today().isoformat()
        with mock.patch("builtins.input", return_value="") as prompt, mock.patch(
            "didimlog.cli.capture", return_value=Path("record.md")
        ) as captured:
            code, stdout, stderr = invoke(
                ["add", "observation", "--title", "T"],
                stdin='{"body":"B"}',
                tty=True,
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("record.md", stdout)
        self.assertIn(today, prompt.call_args.args[0])
        self.assertEqual(captured.call_args.args[1].date, today)

    def test_observation_experiment_and_evidence_map_explicit_options_and_json_stdin(self):
        cases = (
            (
                "observation",
                '{"body":"observed"}',
                {"body": "observed"},
            ),
            (
                "experiment",
                '{"hypothesis":"H","method":"M","result":"failure","contradicts":"none","interpretation":"I"}',
                {
                    "hypothesis": "H",
                    "method": "M",
                    "result": "failure",
                    "contradicts": "none",
                    "interpretation": "I",
                },
            ),
            (
                "evidence",
                '{"artifact":"knowledge/raw/a.txt","origin":"local","collection":"test","artifact_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
                {
                    "artifact": "knowledge/raw/a.txt",
                    "origin": "local",
                    "collection": "test",
                    "artifact_sha256": "a" * 64,
                },
            ),
        )
        for record_type, stdin, fields in cases:
            with self.subTest(record_type=record_type), mock.patch(
                "didimlog.cli.capture", return_value=Path(record_type + ".md")
            ) as captured:
                code, _, stderr = invoke(
                    [
                        "add",
                        record_type,
                        "--date",
                        "2026-08-05",
                        "--title",
                        "Title",
                        "--tags",
                        "z,a",
                        "--sources",
                        "OBS-20260805-01,EVD-20260805-02",
                    ],
                    stdin=stdin,
                )
            self.assertEqual((code, stderr), (0, ""))
            request = captured.call_args.args[1]
            self.assertEqual(
                request,
                CaptureRequest(
                    type=record_type,
                    date="2026-08-05",
                    scope="project",
                    title="Title",
                    tags=("a", "z"),
                    sources=("EVD-20260805-02", "OBS-20260805-01"),
                    fields=fields,
                ),
            )

    def test_lesson_global_is_explicit_and_date_must_match_document(self):
        document = """---
topic: cli
title: CLI lesson
summary: contract
tags: [cli]
date: 2026-08-05
---
## 교훈
body
"""
        with mock.patch(
            "didimlog.cli.publish_lesson",
            return_value=Path("lessons/_global/cli.md"),
        ) as publish:
            code, stdout, stderr = invoke(
                [
                    "add",
                    "lesson",
                    "cli",
                    "--date",
                    "2026-08-05",
                    "--global",
                ],
                stdin=document,
            )

        self.assertEqual((code, stderr), (0, ""))
        self.assertIn("lessons/_global/cli.md", stdout)
        self.assertEqual(publish.call_args.kwargs["project"], "_global")

        code, _, stderr = invoke(
            ["add", "lesson", "cli", "--date", "2026-08-06", "--global"],
            stdin=document,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "LESSON_DATE_MISMATCH\n")

    def test_secret_lesson_returns_exit_five_without_echoing_the_value(self):
        secret = ("g" + "hp_") + "abcdefghijklmnopqrstuvwxyz123456"
        document = """---
topic: cli
title: Secret contract
summary: contract
tags: [cli]
date: 2026-08-05
---
## 교훈
{}
""".format(secret)

        with mock.patch(
            "didimlog.cli.publish_lesson",
            side_effect=LessonSecret("detected " + secret),
        ):
            code, stdout, stderr = invoke(
                ["add", "lesson", "secret", "--date", "2026-08-05"],
                stdin=document,
            )
            explained_code, _, explained_stderr = invoke(
                [
                    "--explain-errors",
                    "add",
                    "lesson",
                    "secret",
                    "--date",
                    "2026-08-05",
                ],
                stdin=document,
            )

        self.assertEqual((code, stdout, stderr), (5, "", "LESSON_SECRET\n"))
        self.assertEqual(explained_code, 5)
        self.assertTrue(explained_stderr.startswith("LESSON_SECRET\n도움말: "))
        self.assertNotIn(secret, explained_stderr)


class InstalledConsoleScriptTests(unittest.TestCase):
    def test_installed_console_script_version_and_help(self):
        executable = shutil.which("didim")
        if executable is None:
            self.skipTest("didim console script is not installed")
        version = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        help_result = subprocess.run(
            [executable, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertEqual(version.returncode, 0)
        self.assertEqual(version.stdout, "Didimlog 0.0.1\n")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("didim setup", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
