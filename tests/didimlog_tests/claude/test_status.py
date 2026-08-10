import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.claude.setup import apply_setup, plan_setup
from didimlog.claude.status import doctor_text, status_text


class StatusDoctorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.config = self.home / ".claude"
        self.project = self.root / "demo-project"
        self.home.mkdir()
        self.config.mkdir()
        self.project.mkdir()
        if shutil.which("git") is None:
            self.skipTest("git is required")
        subprocess.run(
            ["git", "init", "-q"],
            cwd=self.project,
            check=True,
            capture_output=True,
        )
        self.launcher = self.root / "bin" / "didim"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o755)
        with mock.patch(
            "didimlog.claude.setup._find_launcher",
            return_value=str(self.launcher),
        ):
            plan = plan_setup(
                home=self.home,
                cwd=self.project,
                config=self.config,
                include_project=True,
                skip_claude=False,
            )
        apply_setup(plan, approved=True)

    def _status(self, cwd=None):
        return status_text(
            home=self.home,
            cwd=self.project if cwd is None else cwd,
            config=self.config,
        )

    def _doctor(self, cwd=None):
        return doctor_text(
            home=self.home,
            cwd=self.project if cwd is None else cwd,
            config=self.config,
        )

    def _snapshot(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                result[relative] = ("directory",)
            else:
                result[relative] = ("file", path.read_bytes())
        return result

    def test_healthy_status_summarizes_current_surfaces(self):
        text = self._status()

        self.assertEqual(
            text,
            "Didimlog 0.0.1\n"
            "개인 지식: 최신\n"
            "현재 프로젝트: demo-project\n"
            "프로젝트 근거: 최신\n"
            "Claude 연결: 정상\n",
        )
        self.assertNotIn(str(self.home), text)

    def test_status_distinguishes_personal_stale_and_unconfigured_project(self):
        personal_index = self.home / "knowledge" / "index"
        (personal_index / "extra.txt").write_bytes(b"extra\n")
        outside = self.root / "outside"
        outside.mkdir()

        text = self._status(cwd=outside)

        self.assertIn("개인 지식: 알 수 없는 index 파일 있음", text)
        self.assertIn("현재 프로젝트: 없음", text)
        self.assertIn("프로젝트 근거: 설정되지 않음", text)

    def test_status_reports_claude_disconnect(self):
        (self.config / "CLAUDE.md").write_bytes(b"# disconnected\n")

        text = self._status()

        self.assertIn("Claude 연결: 문제 있음", text)

    def test_healthy_doctor_has_stable_token_and_zero_exit(self):
        code, text = self._doctor()

        self.assertEqual(code, 0)
        self.assertEqual(text, "DOCTOR_OK\n문제 없음\n")

    def test_doctor_lists_what_impact_and_one_action_per_problem(self):
        (self.config / "CLAUDE.md").write_bytes(b"# disconnected\n")
        (self.home / "knowledge" / "index" / "extra.txt").write_bytes(b"extra\n")

        code, text = self._doctor()

        self.assertEqual(code, 3)
        self.assertTrue(text.startswith("DOCTOR_PROBLEMS\n"))
        self.assertIn("무엇: CLAUDE_IMPORT_MISSING", text)
        self.assertIn("무엇: PERSONAL_INDEX_EXTRA", text)
        self.assertIn("영향: ", text)
        problem_count = text.count("무엇: ")
        self.assertEqual(text.count("수정: "), problem_count)
        self.assertNotIn(str(self.home), text)


    def test_status_and_doctor_are_read_only(self):
        before = self._snapshot()

        self._status()
        self._doctor()

        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
