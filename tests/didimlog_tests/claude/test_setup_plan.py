import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.errors import DidimError
from didimlog.claude.setup import SetupPlan, plan_setup


class SetupPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.config = self.home / ".claude-profile"
        self.cwd = self.root / "outside"
        self.home.mkdir()
        self.config.mkdir()
        self.cwd.mkdir()
        self.launcher = self.root / "bin" / "didim"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o755)

    def _snapshot(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                result[relative] = ("directory", path.stat().st_mode)
            else:
                result[relative] = ("file", path.read_bytes(), path.stat().st_mode)
        return result

    def _plan(self, **overrides):
        options = {
            "home": self.home,
            "cwd": self.cwd,
            "config": self.config,
            "include_project": True,
            "skip_claude": False,
        }
        options.update(overrides)
        with mock.patch(
            "didimlog.claude.setup._find_launcher",
            return_value=str(self.launcher),
        ):
            return plan_setup(**options)

    def _git_project(self):
        if shutil.which("git") is None:
            self.skipTest("git is required")
        project = self.root / "demo-project"
        project.mkdir()
        subprocess.run(
            ["git", "init", "-q"],
            cwd=project,
            check=True,
            capture_output=True,
        )
        return project

    def test_fresh_plan_preflights_every_surface_without_writing(self):
        project = self._git_project()
        before = self._snapshot()

        plan = self._plan(cwd=project)

        self.assertIsInstance(plan, SetupPlan)
        self.assertEqual(plan.version, "0.0.1")
        self.assertIn("개인 지식 디렉터리 생성", "\n".join(plan.personal_changes))
        self.assertIn("MY-RULES.md 생성", "\n".join(plan.personal_changes))
        self.assertIn("프로젝트 근거 저장소 생성", "\n".join(plan.project_changes))
        self.assertIn("Claude 지침 연결", "\n".join(plan.claude_changes))
        self.assertIn("SessionStart hook 연결", "\n".join(plan.claude_changes))
        self.assertEqual(self._snapshot(), before)

    def test_git_outside_is_an_explicit_non_error_project_summary(self):
        plan = self._plan()

        self.assertEqual(
            plan.project_changes,
            ("프로젝트 근거: 설정되지 않음 — didim setup을 Git 프로젝트에서 실행하세요.",),
        )
        self.assertFalse((self.cwd / "knowledge").exists())

    def test_project_and_claude_can_be_explicitly_skipped_without_probe_side_effects(self):
        before = self._snapshot()

        with mock.patch(
            "didimlog.claude.setup._find_launcher",
            side_effect=AssertionError("launcher lookup must be skipped"),
        ):
            plan = plan_setup(
                home=self.home,
                cwd=self.cwd,
                config=self.config,
                include_project=False,
                skip_claude=True,
            )

        self.assertEqual(plan.project_changes, ())
        self.assertEqual(plan.claude_changes, ())
        self.assertEqual(self._snapshot(), before)

    def test_existing_user_rules_are_preserved_and_not_reported_as_a_write(self):
        knowledge = self.home / "knowledge"
        knowledge.mkdir()
        user_rules = knowledge / "MY-RULES.md"
        user_rules.write_bytes(b"user-owned rules\n")

        plan = self._plan(skip_claude=True, include_project=False)

        self.assertNotIn("MY-RULES.md 생성", "\n".join(plan.personal_changes))
        self.assertEqual(user_rules.read_bytes(), b"user-owned rules\n")

    def test_personal_symlink_is_refused_without_mutating_any_surface(self):
        outside = self.root / "outside-data"
        outside.mkdir()
        (self.home / "knowledge").symlink_to(outside, target_is_directory=True)
        before = self._snapshot()

        with self.assertRaises(ValueError):
            self._plan()

        self.assertEqual(self._snapshot(), before)

    def test_invalid_settings_aborts_the_whole_plan_before_personal_or_project_writes(self):
        project = self._git_project()
        settings = self.config / "settings.json"
        settings.write_bytes(b"not json\n")
        before = self._snapshot()

        with self.assertRaises(ValueError):
            self._plan(cwd=project)

        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.home / "knowledge").exists())
        self.assertFalse((project / "knowledge").exists())

    def test_project_scaffold_conflict_aborts_the_whole_plan_without_writing(self):
        project = self._git_project()
        knowledge = project / "knowledge"
        knowledge.write_bytes(b"user file\n")
        before = self._snapshot()

        with self.assertRaises(DidimError):
            self._plan(cwd=project)

        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.home / "knowledge").exists())


if __name__ == "__main__":
    unittest.main()
