import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.claude.transaction import InstallJournal
from didimlog.connections import apply_connections, plan_connections
from didimlog.startup import _INPUT_LIMIT, startup_check


class StartupCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.knowledge = self.home / "knowledge"
        (self.knowledge / "index").mkdir(parents=True)
        (self.knowledge / "index/_global.md").write_text("# Global\n", encoding="utf-8")
        self.launcher = self.root / "bin/didim"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o755)
        self.omp = self.home / ".omp/agent"
        self.omp.mkdir(parents=True)
        plan = plan_connections(
            (("omp", self.omp),),
            launcher=self.launcher,
            home=self.home,
            environ={},
            connect=True,
            require_storage=True,
        )
        apply_connections(
            plan,
            InstallJournal(self.root / "journal.json", reset=True),
        )

    def _snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): (
                "directory" if path.is_dir() else path.read_bytes()
            )
            for path in self.root.rglob("*")
        }

    def test_healthy_omp_check_is_quiet_and_does_not_write(self):
        before = self._snapshot()
        output = io.StringIO()
        code = startup_check(
            client="omp",
            root=self.omp,
            revision="1",
            cwd=str(self.root),
            stdin=io.StringIO(""),
            stdout=output,
            home=self.home,
        )
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self._snapshot(), before)

    def test_missing_selected_asset_is_one_fixed_redacted_warning(self):
        (self.omp / "extensions/didimlog.js").unlink()
        output = io.StringIO()
        startup_check(
            client="omp",
            root=self.omp,
            revision="1",
            cwd="/private/project\x1b]52;c;secret\x07",
            stdin=io.StringIO(""),
            stdout=output,
            home=self.home,
        )
        self.assertEqual(output.getvalue(), "DIDIMLOG_STARTUP_WARNING\n")
        self.assertNotIn("private", output.getvalue())
        self.assertLessEqual(len(output.getvalue().encode("utf-8")), 1024)

    def test_codex_malformed_and_oversized_input_warn_without_context_fields(self):
        for raw in ("{", "x" * (_INPUT_LIMIT + 1)):
            with self.subTest(size=len(raw)):
                output = io.StringIO()
                startup_check(
                    client="codex",
                    root=self.omp,
                    revision="1",
                    cwd=None,
                    stdin=io.StringIO(raw),
                    stdout=output,
                    home=self.home,
                )
                payload = json.loads(output.getvalue())
                self.assertEqual(set(payload), {"systemMessage"})
                self.assertNotIn("additionalContext", payload)
                self.assertNotIn("continue", payload)
                self.assertLessEqual(len(output.getvalue().encode("utf-8")), 1024)

    def test_replaced_root_ancestor_warns_without_following_new_tree(self):
        moved = self.home / ".omp-original"
        (self.home / ".omp").rename(moved)
        foreign = self.root / "foreign-omp"
        (foreign / "agent").mkdir(parents=True)
        (self.home / ".omp").symlink_to(foreign, target_is_directory=True)
        output = io.StringIO()
        startup_check(
            client="omp",
            root=self.omp,
            revision="1",
            cwd=str(self.root),
            stdin=io.StringIO(),
            stdout=output,
            home=self.home,
        )
        self.assertEqual(output.getvalue(), "DIDIMLOG_STARTUP_WARNING\n")

    def test_non_eof_deadline_becomes_warning_instead_of_success(self):
        output = io.StringIO()
        with mock.patch(
            "didimlog.startup._bounded_input",
            side_effect=TimeoutError("stdin never ended"),
        ):
            startup_check(
                client="codex",
                root=self.omp,
                revision="1",
                cwd=None,
                stdin=io.StringIO(),
                stdout=output,
                home=self.home,
            )
        self.assertEqual(set(json.loads(output.getvalue())), {"systemMessage"})



if __name__ == "__main__":
    unittest.main()
