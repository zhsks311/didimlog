import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from didimlog import cli
from didimlog.claude.connect import plan_connect
from didimlog.connections import plan_connections
from didimlog.errors import DidimError


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

def invoke(arguments, *, tty=False, stdin=""):
    output = io.StringIO()
    error = io.StringIO()
    with mock.patch.object(cli.sys, "stdin", TerminalInput(stdin, tty=tty)), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
        code = cli.main(arguments)
    return code, output.getvalue(), error.getvalue()


def invoke_real(arguments, *, tty=False, stdin=""):
    output = TerminalOutput(tty=tty)
    error = TerminalOutput(tty=tty)
    with mock.patch.object(cli.sys, "argv", ["didim", *arguments]), mock.patch.object(
        cli.sys,
        "stdin",
        TerminalInput(stdin, tty=tty),
    ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
        code = cli.main()
    return code, output.getvalue(), error.getvalue()


class MultiClientCliTests(unittest.TestCase):

    def test_successful_setup_prints_only_new_runtime_notices(self):
        plan = SimpleNamespace(
            version="test",
            personal_changes=(),
            project_changes=(),
            project_notices=("already shown",),
            claude_changes=(),
            connection_changes=(),
            connection_notices=(),
        )
        with mock.patch("didimlog.cli.plan_setup", return_value=plan), mock.patch(
            "didimlog.cli.apply_setup",
            return_value=("already shown", "runtime notice"),
        ):
            code, output, error = invoke(["setup", "--yes"])
        self.assertEqual((code, error), (0, ""))
        self.assertIn("runtime notice", output)
        self.assertEqual(output.count("already shown"), 1)

    def test_setup_rejects_claude_skip_conflict_before_planning(self):
        with mock.patch("didimlog.cli.plan_setup") as planned:
            code, output, error = invoke(
                ["setup", "--dry-run", "--client", "claude", "--skip-claude"]
            )
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(error, "CLI_USAGE_ERROR\n")
        planned.assert_not_called()

    def test_setup_rejects_claude_config_in_non_claude_explicit_mode(self):
        with mock.patch("didimlog.cli.plan_setup") as planned:
            code, output, error = invoke(
                [
                    "setup",
                    "--dry-run",
                    "--client",
                    "omp",
                    "--config-dir",
                    "/claude",
                ]
            )
        self.assertEqual((code, output), (2, ""))
        self.assertEqual(error, "CLI_USAGE_ERROR\n")
        planned.assert_not_called()

    def test_new_connect_dry_run_and_noninteractive_denial_never_apply(self):
        plan = SimpleNamespace(changes=("planned",), notices=())
        with mock.patch("didimlog.cli._find_launcher", return_value=Path("/bin/didim")), mock.patch(
            "didimlog.cli.plan_connections", return_value=plan
        ), mock.patch("didimlog.cli.apply_connections") as applied:
            dry = invoke(["connect", "omp", "--dry-run"])
            denied = invoke(["connect", "omp"])
        self.assertEqual(dry[0], 0)
        self.assertEqual(denied[0], 2)
        self.assertEqual(denied[2], "OMP_CONNECT_APPROVAL_REQUIRED\n")
        applied.assert_not_called()

    def test_claude_connect_rolls_back_when_selection_state_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            config = home / ".claude"
            knowledge = home / "knowledge"
            launcher = root / "bin/didim"
            config.mkdir(parents=True)
            knowledge.mkdir()
            launcher.parent.mkdir()
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o755)
            claude_plan = plan_connect(
                config,
                launcher=launcher,
                home=home,
            )
            connection_plan = plan_connections(
                (("claude", claude_plan.config_dir),),
                launcher=launcher,
                home=home,
                environ={},
                connect=True,
                require_storage=True,
            )
            state_path = knowledge / ".didimlog/connections.json"
            state_path.parent.mkdir()
            concurrent = b'{"version":1,"clients":{}}\n'
            state_path.write_bytes(concurrent)

            with self.assertRaises(DidimError) as caught:
                cli._apply_claude_with_selection(
                    claude_plan,
                    connection_plan,
                    connect=True,
                )

            self.assertEqual(caught.exception.token, "CONNECTION_PLAN_CHANGED")
            self.assertEqual(state_path.read_bytes(), concurrent)
            self.assertFalse((config / "CLAUDE.md").exists())
            self.assertFalse((config / "settings.json").exists())

    def test_claude_connect_reports_incomplete_recovery_for_preserved_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            config = home / ".claude"
            launcher = root / "bin/didim"
            config.mkdir(parents=True)
            launcher.parent.mkdir()
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o755)
            settings = config / "settings.json"
            concurrent = b'{"independent":"user bytes"}\n'
            real_fsync = os.fsync
            injected = False

            def edit_during_settings_parent_sync(descriptor):
                nonlocal injected
                if (
                    not injected
                    and settings.exists()
                    and stat.S_ISDIR(os.fstat(descriptor).st_mode)
                ):
                    injected = True
                    settings.write_bytes(concurrent)
                    raise OSError("synthetic parent sync failure")
                return real_fsync(descriptor)

            with mock.patch.object(
                Path,
                "home",
                return_value=home,
            ), mock.patch(
                "didimlog.cli._find_launcher",
                return_value=launcher,
            ), mock.patch(
                "didimlog.conditional_file.os.fsync",
                side_effect=edit_during_settings_parent_sync,
            ):
                code, _output, error = invoke_real(
                    [
                        "connect",
                        "claude",
                        "--yes",
                        "--config-dir",
                        str(config),
                    ],
                    tty=True,
                )

            self.assertTrue(injected)
            self.assertEqual(code, 3)
            self.assertEqual(error.splitlines()[0], "CONNECTION_ROLLBACK_INCOMPLETE")
            self.assertIn("didim doctor", error)
            self.assertEqual(settings.read_bytes(), concurrent)

    def test_real_dry_run_and_rejection_skip_automatic_update(self):
        plan = SimpleNamespace(changes=("planned",), notices=())
        cases = (
            ("dry-run", ["connect", "omp", "--dry-run"], ""),
            ("rejected", ["connect", "omp"], "n\n"),
        )
        for name, arguments, stdin in cases:
            with self.subTest(name=name), mock.patch(
                "didimlog.cli._find_launcher",
                return_value=Path("/bin/didim"),
            ), mock.patch(
                "didimlog.cli.plan_connections",
                return_value=plan,
            ), mock.patch(
                "didimlog.cli.apply_connections"
            ) as applied, mock.patch(
                "didimlog.cli.automatic_update_notice"
            ) as update:
                code, _output, error = invoke_real(
                    arguments,
                    tty=True,
                    stdin=stdin,
                )

            self.assertEqual((code, error), (0, ""))
            applied.assert_not_called()
            update.assert_not_called()



if __name__ == "__main__":
    unittest.main()
