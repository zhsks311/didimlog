import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.claude import setup as setup_module
from didimlog.claude.setup import SetupPlan, plan_setup
from didimlog.errors import DidimError


GIT = shutil.which("git")
START = b"# DIDIMLOG:START project-knowledge"
RULE = b"/knowledge/"
END = b"# DIDIMLOG:END project-knowledge"
LOCAL_BLOCK = START + b"\n" + RULE + b"\n" + END + b"\n"


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
        self.git_environment = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CONFIG_HOME": str(self.root / "xdg"),
        }

    def _snapshot(self):
        result = {}
        for path in sorted(self.root.rglob("*")):
            relative = path.relative_to(self.root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                status = path.stat()
                result[relative] = (
                    "directory",
                    status.st_mode,
                    status.st_ino,
                    status.st_mtime_ns,
                )
            else:
                status = path.stat()
                result[relative] = (
                    "file",
                    path.read_bytes(),
                    status.st_mode,
                    status.st_ino,
                    status.st_mtime_ns,
                )
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
        with mock.patch.dict(
            os.environ,
            self.git_environment,
            clear=True,
        ), mock.patch(
            "didimlog.claude.setup._find_launcher",
            return_value=str(self.launcher),
        ):
            return plan_setup(**options)

    def _git(self, project, *arguments, expected=0):
        result = subprocess.run(
            [GIT, *arguments],
            cwd=project,
            env=self.git_environment,
            check=False,
            capture_output=True,
        )
        self.assertEqual(result.returncode, expected, result.stderr.decode(errors="replace"))
        return result

    def _git_project(self, name="demo-project"):
        if GIT is None:
            self.skipTest("git is required")
        project = self.root / name
        project.mkdir()
        self._git(project, "init", "-q")
        return project

    def _exclude_path(self, project):
        result = self._git(
            project,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/exclude",
        )
        return Path(result.stdout.decode("utf-8").strip())

    def test_fresh_plan_preflights_every_surface_without_writing(self):
        project = self._git_project()
        before = self._snapshot()

        plan = self._plan(cwd=project)

        self.assertIsInstance(plan, SetupPlan)
        self.assertEqual(plan.version, "0.0.2")
        self.assertIn("개인 지식 디렉터리 생성", "\n".join(plan.personal_changes))
        self.assertIn("MY-RULES.md 생성", "\n".join(plan.personal_changes))
        self.assertIn("프로젝트 근거 저장소 생성", "\n".join(plan.project_changes))
        self.assertIn("Claude 지침 연결", "\n".join(plan.claude_changes))
        self.assertIn("SessionStart hook 연결", "\n".join(plan.claude_changes))
        self.assertEqual(plan._project_exclude.mode, "local")
        self.assertEqual(plan._project_exclude.path, self._exclude_path(project))
        self.assertIn(
            "프로젝트 지식을 이 컴퓨터에서만 사용: {}".format(
                plan._project_exclude.path
            ),
            plan.project_changes,
        )
        self.assertEqual(plan.project_notices, ())
        self.assertEqual(self._snapshot(), before)

    def test_preflight_runs_personal_project_scaffold_exclude_and_claude_in_order(self):
        project = self._git_project()
        calls = mock.Mock()
        with mock.patch.object(
            setup_module,
            "_plan_personal",
            wraps=setup_module._plan_personal,
        ) as personal, mock.patch.object(
            setup_module,
            "discover_project_for_setup",
            wraps=setup_module.discover_project_for_setup,
        ) as discovery, mock.patch.object(
            setup_module,
            "plan_scaffold",
            wraps=setup_module.plan_scaffold,
        ) as scaffold, mock.patch.object(
            setup_module,
            "plan_git_exclude",
            wraps=setup_module.plan_git_exclude,
        ) as exclude, mock.patch.object(
            setup_module,
            "plan_connect",
            wraps=setup_module.plan_connect,
        ) as claude:
            calls.attach_mock(personal, "personal")
            calls.attach_mock(discovery, "discovery")
            calls.attach_mock(scaffold, "scaffold")
            calls.attach_mock(exclude, "exclude")
            calls.attach_mock(claude, "claude")

            self._plan(cwd=project)

        self.assertEqual(
            [call[0] for call in calls.mock_calls],
            ["personal", "discovery", "scaffold", "exclude", "claude"],
        )

    def test_shared_plan_removes_the_managed_block_and_forwards_other_rule_notice(self):
        project = self._git_project()
        exclude = self._exclude_path(project)
        exclude.write_bytes(LOCAL_BLOCK)
        (project / ".gitignore").write_bytes(b"/knowledge/\n")
        before = self._snapshot()

        plan = self._plan(
            cwd=project,
            skip_claude=True,
            project_knowledge="shared",
        )

        self.assertEqual(plan._project_exclude.mode, "shared")
        self.assertIn(
            "knowledge 폴더의 Git 로컬 제외를 제거",
            plan.project_changes,
        )
        self.assertEqual(
            plan.project_notices,
            ("다른 Git 규칙이 knowledge 폴더를 계속 제외하고 있습니다.",),
        )
        self.assertEqual(self._snapshot(), before)

    def test_existing_local_exclude_is_a_noop_in_the_project_summary(self):
        project = self._git_project()
        self._exclude_path(project).write_bytes(LOCAL_BLOCK)

        plan = self._plan(cwd=project, skip_claude=True)

        self.assertEqual(plan._project_exclude.changes, ())
        self.assertNotIn(
            "프로젝트 지식을 이 컴퓨터에서만 사용",
            "\n".join(plan.project_changes),
        )
    def test_discovered_launcher_symlink_uses_the_resolved_executable(self):
        launcher_link = self.root / "user-bin" / "didim"
        launcher_link.parent.mkdir()
        launcher_link.symlink_to(self.launcher)

        with mock.patch(
            "didimlog.claude.setup.shutil.which",
            return_value=str(launcher_link),
        ):
            plan = plan_setup(
                home=self.home,
                cwd=self.cwd,
                config=self.config,
                include_project=False,
                skip_claude=False,
            )

        settings = next(
            change for change in plan._claude._files if change.name == "settings"
        )
        self.assertIn(str(self.launcher.resolve()).encode(), settings.intended)
        self.assertNotIn(str(launcher_link).encode(), settings.intended)

    def test_git_outside_is_an_explicit_non_error_project_summary(self):
        plan = self._plan()

        self.assertEqual(
            plan.project_changes,
            ("프로젝트 근거: 설정되지 않음 — didim setup을 Git 프로젝트에서 실행하세요.",),
        )
        self.assertIsNone(plan._project_exclude)
        self.assertEqual(plan.project_notices, ())
        self.assertFalse((self.cwd / "knowledge").exists())

    def test_project_and_claude_can_be_explicitly_skipped_without_git_probes(self):
        before = self._snapshot()

        with mock.patch(
            "didimlog.claude.setup._find_launcher",
            side_effect=AssertionError("launcher lookup must be skipped"),
        ), mock.patch(
            "didimlog.claude.setup.discover_project_for_setup",
            side_effect=AssertionError("Git discovery must be skipped"),
        ), mock.patch(
            "didimlog.claude.setup.plan_git_exclude",
            side_effect=AssertionError("Git exclude planning must be skipped"),
        ):
            plan = plan_setup(
                home=self.home,
                cwd=self.cwd,
                config=self.config,
                include_project=False,
                skip_claude=True,
                project_knowledge="invalid",
            )

        self.assertEqual(plan.project_changes, ())
        self.assertEqual(plan.project_notices, ())
        self.assertIsNone(plan._project_exclude)
        self.assertEqual(plan.claude_changes, ())
        self.assertEqual(self._snapshot(), before)


    def test_invalid_mode_is_rejected_before_discovery_when_project_is_included(self):
        before = self._snapshot()
        with mock.patch(
            "didimlog.claude.setup.discover_project_for_setup",
            side_effect=AssertionError("discovery must not start"),
        ):
            with self.assertRaises(ValueError):
                self._plan(
                    skip_claude=True,
                    project_knowledge="invalid",
                )

        self.assertEqual(self._snapshot(), before)

    def test_markerless_directory_remains_non_project_when_git_is_missing(self):
        before = self._snapshot()
        with mock.patch(
            "didimlog.project.git_exclude.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            plan = self._plan(skip_claude=True)

        self.assertEqual(
            plan.project_changes,
            ("프로젝트 근거: 설정되지 않음 — didim setup을 Git 프로젝트에서 실행하세요.",),
        )
        self.assertEqual(self._snapshot(), before)

    def test_marked_repository_git_failures_abort_the_whole_plan_without_writing(self):
        project = self._git_project()
        before = self._snapshot()
        failures = (
            FileNotFoundError(),
            subprocess.TimeoutExpired(["git"], 5),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), mock.patch(
                "didimlog.project.git_exclude.subprocess.run",
                side_effect=failure,
            ):
                with self.assertRaises(DidimError) as caught:
                    self._plan(cwd=project, skip_claude=True)
                self.assertEqual(
                    caught.exception.token,
                    "PROJECT_EXCLUDE_GIT_UNAVAILABLE",
                )
                self.assertEqual(self._snapshot(), before)

        failed = subprocess.CompletedProcess(["git"], 2, b"", b"failure")
        with mock.patch(
            "didimlog.project.git_exclude.subprocess.run",
            return_value=failed,
        ):
            with self.assertRaises(DidimError) as caught:
                self._plan(cwd=project, skip_claude=True)
        self.assertEqual(caught.exception.token, "PROJECT_EXCLUDE_GIT_UNAVAILABLE")
        self.assertEqual(self._snapshot(), before)

    def test_local_refuses_tracked_knowledge_but_shared_allows_it_without_writing(self):
        project = self._git_project()
        knowledge = project / "knowledge"
        knowledge.mkdir()
        tracked = knowledge / "tracked.txt"
        tracked.write_bytes(b"tracked\n")
        self._git(project, "add", "knowledge/tracked.txt")
        before = self._snapshot()

        with self.assertRaises(DidimError) as caught:
            self._plan(cwd=project, skip_claude=True)
        self.assertEqual(caught.exception.token, "PROJECT_KNOWLEDGE_TRACKED")
        self.assertEqual(self._snapshot(), before)

        shared = self._plan(
            cwd=project,
            skip_claude=True,
            project_knowledge="shared",
        )
        self.assertEqual(shared._project_exclude.mode, "shared")
        self.assertEqual(self._snapshot(), before)

    def test_local_negate_conflict_aborts_the_whole_plan_without_writing(self):
        project = self._git_project()
        gitignore = project / ".gitignore"
        gitignore.write_bytes(b"/knowledge/\n!/knowledge/\n")
        before = self._snapshot()

        with self.assertRaises(DidimError) as caught:
            self._plan(cwd=project, skip_claude=True)

        self.assertEqual(caught.exception.token, "PROJECT_EXCLUDE_CONFLICT")
        self.assertEqual(self._snapshot(), before)

    def test_linked_worktree_uses_the_common_git_exclude_path(self):
        project = self._git_project()
        seed = project / "seed"
        seed.write_bytes(b"seed\n")
        self._git(project, "add", "seed")
        self._git(
            project,
            "-c",
            "user.name=Didimlog Test",
            "-c",
            "user.email=didimlog@example.invalid",
            "commit",
            "-qm",
            "seed",
        )
        linked = self.root / "linked"
        self._git(project, "worktree", "add", "-q", str(linked))
        common_exclude = self._exclude_path(project)

        plan = self._plan(cwd=linked, skip_claude=True)

        self.assertEqual(plan._project_root, linked)
        self.assertEqual(plan._project_exclude.path, common_exclude)

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
