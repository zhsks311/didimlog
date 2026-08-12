import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.claude.probe import _launcher_from_settings
from didimlog.claude.setup import apply_setup, plan_setup
from didimlog.claude.status import _safe_label, doctor_text, status_text


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
        self.launcher = self.root / "bin with spaces" / "didim"
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

    def test_safe_label_replaces_terminal_controls_and_bidi_overrides(self):
        self.assertEqual(_safe_label("safe\x1b\u202eevil"), "safe??evil")

    def test_non_command_hook_with_managed_command_text_is_not_a_launcher(self):
        settings_path = self.config / "settings.json"
        value = json.loads(settings_path.read_text(encoding="utf-8"))
        managed_command = value["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        value["hooks"]["SessionStart"].insert(
            0,
            {
                "hooks": [
                    {
                        "type": "prompt",
                        "command": managed_command,
                    }
                ]
            },
        )
        settings_path.write_text(json.dumps(value), encoding="utf-8")

        self.assertEqual(
            _launcher_from_settings(settings_path.read_bytes()),
            self.launcher,
        )


    def test_git_unavailable_in_prepared_project_is_reported_privately(self):
        failures = (
            FileNotFoundError("git"),
            subprocess.TimeoutExpired(["git", "rev-parse"], 5),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__, command="status"):
                with mock.patch(
                    "didimlog.project.git_exclude.subprocess.run",
                    side_effect=failure,
                ) as run_git:
                    status = self._status()

                self.assertEqual(run_git.call_count, 1)
                self.assertIn("현재 프로젝트: 확인 실패", status)
                self.assertIn("프로젝트 근거: 확인 실패", status)
                self.assertIn("Claude 연결: 정상", status)
                self.assertNotIn("현재 프로젝트: 없음", status)
                self.assertNotIn(str(self.root), status)

            with self.subTest(failure=type(failure).__name__, command="doctor"):
                with mock.patch(
                    "didimlog.project.git_exclude.subprocess.run",
                    side_effect=failure,
                ) as run_git:
                    code, doctor = self._doctor()

                self.assertEqual(run_git.call_count, 1)
                self.assertEqual(code, 3)
                self.assertIn(
                    "무엇: PROJECT_EXCLUDE_GIT_UNAVAILABLE",
                    doctor,
                )
                self.assertIn(
                    "영향: Git 저장소를 확인하지 못해 현재 프로젝트 근거 상태를 진단할 수 없습니다.",
                    doctor,
                )
                self.assertIn(
                    "수정: Git 설치와 현재 저장소 상태를 확인한 뒤 다시 시도하세요.",
                    doctor,
                )
                self.assertNotIn(str(self.root), doctor)

    def test_git_unavailable_without_marker_remains_non_project(self):
        outside = self.root / "outside"
        outside.mkdir()
        failures = (
            FileNotFoundError("git"),
            subprocess.TimeoutExpired(["git", "rev-parse"], 5),
        )

        for failure in failures:
            with self.subTest(failure=type(failure).__name__, command="status"):
                with mock.patch(
                    "didimlog.project.git_exclude.subprocess.run",
                    side_effect=failure,
                ) as run_git:
                    status = self._status(cwd=outside)

                self.assertEqual(run_git.call_count, 1)
                self.assertIn("현재 프로젝트: 없음", status)
                self.assertIn("프로젝트 근거: 설정되지 않음", status)
                self.assertIn("Claude 연결: 정상", status)

            with self.subTest(failure=type(failure).__name__, command="doctor"):
                with mock.patch(
                    "didimlog.project.git_exclude.subprocess.run",
                    side_effect=failure,
                ) as run_git:
                    code, doctor = self._doctor(cwd=outside)

                self.assertEqual(run_git.call_count, 1)
                self.assertEqual(code, 0)
                self.assertEqual(doctor, "DOCTOR_OK\n문제 없음\n")

    def test_healthy_status_summarizes_current_surfaces(self):
        text = self._status()

        self.assertEqual(
            text,
            "Didimlog 0.0.2\n"
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
