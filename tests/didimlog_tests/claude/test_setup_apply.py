import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.claude import setup as setup_module
from didimlog.claude.setup import apply_setup, plan_setup
from didimlog.errors import DidimError
from didimlog.indexing import run_index
from didimlog.project.git_exclude import project_knowledge_is_ignored


GIT = shutil.which("git")
START = b"# DIDIMLOG:START project-knowledge"
RULE = b"/knowledge/"
END = b"# DIDIMLOG:END project-knowledge"
LOCAL_BLOCK = START + b"\n" + RULE + b"\n" + END + b"\n"


class SetupApplyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.config = self.home / ".claude"
        self.project = self.root / "project"
        self.home.mkdir()
        self.config.mkdir()
        self.project.mkdir()
        if GIT is None:
            self.skipTest("git is required")
        self.git_environment = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CONFIG_HOME": str(self.root / "xdg"),
        }
        self._git(self.project, "init", "-q")
        self.launcher = self.root / "bin" / "didim"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o755)

    def _git(self, project, *arguments, expected=0):
        result = subprocess.run(
            [GIT, *arguments],
            cwd=project,
            env=self.git_environment,
            check=False,
            capture_output=True,
        )
        self.assertEqual(
            result.returncode,
            expected,
            result.stderr.decode(errors="replace"),
        )
        return result

    def _exclude_path(self, project=None):
        selected = self.project if project is None else project
        result = self._git(
            selected,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/exclude",
        )
        return Path(result.stdout.decode("utf-8").strip())

    def _knowledge_is_ignored(self, project=None):
        selected = self.project if project is None else project
        with mock.patch.dict(os.environ, self.git_environment, clear=True):
            return project_knowledge_is_ignored(selected)

    def _plan(self, **overrides):
        options = {
            "home": self.home,
            "cwd": self.project,
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

    def _apply(self, plan, *, approved=True, environment=None):
        selected_environment = dict(self.git_environment)
        if environment is not None:
            selected_environment.update(environment)
        with mock.patch.dict(os.environ, selected_environment, clear=True):
            return apply_setup(plan, approved=approved)

    def _snapshot(self, root):
        if not root.exists():
            return None
        result = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                status = path.stat()
                result[relative] = (
                    "directory",
                    status.st_ino,
                    status.st_mtime_ns,
                )
            else:
                status = path.stat()
                result[relative] = (
                    "file",
                    path.read_bytes(),
                    status.st_ino,
                    status.st_mtime_ns,
                )
        return result

    def test_approval_is_required_before_any_write(self):
        plan = self._plan()
        before = self._snapshot(self.root)

        with self.assertRaises(DidimError) as caught:
            self._apply(plan, approved=False)

        self.assertEqual(caught.exception.token, "SETUP_APPROVAL_REQUIRED")
        self.assertEqual(self._snapshot(self.root), before)

    def test_fresh_apply_creates_every_surface_and_passes_real_postchecks(self):
        plan = self._plan()

        self.assertEqual(self._apply(plan), ())
        self.assertTrue(self._knowledge_is_ignored())
        self.assertIn(LOCAL_BLOCK, self._exclude_path().read_bytes())

        self.assertTrue((self.home / "knowledge" / "MY-RULES.md").is_file())
        self.assertTrue((self.home / "knowledge" / "index").is_dir())
        self.assertTrue((self.project / "knowledge" / "README.md").is_file())
        self.assertTrue(
            (self.project / "knowledge" / "index" / "INDEX.md").is_file()
        )
        self.assertTrue((self.config / "CLAUDE.md").is_file())
        self.assertTrue((self.config / "settings.json").is_file())
        checked = run_index(check=True, home=self.home, cwd=self.project)
        self.assertTrue(checked.personal.endswith("PERSONAL_INDEX_CURRENT"))
        self.assertTrue(checked.project.endswith("PROJECT_INDEX_CURRENT"))

    def test_postcheck_ignores_repository_environment_pointing_at_stale_other_repo(self):
        other = self.root / "other-project"
        other.mkdir()
        self._git(other, "init", "-q")
        setup_module.apply_scaffold(setup_module.plan_scaffold(other))
        setup_module.write_index(other)
        (other / "knowledge" / "index" / "INDEX.md").write_bytes(b"stale\n")
        repository_environment = {
            "GIT_DIR": str(other / ".git"),
            "GIT_WORK_TREE": str(other),
        }

        plan = self._plan()

        self.assertEqual(
            self._apply(plan, environment=repository_environment),
            (),
        )

    def test_postcheck_rejects_stale_planned_repo_when_environment_points_elsewhere(self):
        other = self.root / "other-project"
        other.mkdir()
        self._git(other, "init", "-q")
        setup_module.apply_scaffold(setup_module.plan_scaffold(other))
        setup_module.write_index(other)
        repository_environment = {
            "GIT_DIR": str(other / ".git"),
            "GIT_WORK_TREE": str(other),
        }
        plan = self._plan()
        real_apply_git_exclude = setup_module.apply_git_exclude

        def apply_exclude_then_stale(exclude_plan):
            real_apply_git_exclude(exclude_plan)
            (self.project / "knowledge" / "index" / "INDEX.md").write_bytes(
                b"stale\n"
            )

        with mock.patch(
            "didimlog.claude.setup.apply_git_exclude",
            side_effect=apply_exclude_then_stale,
        ):
            with self.assertRaises(DidimError) as caught:
                self._apply(plan, environment=repository_environment)

        self.assertEqual(caught.exception.token, "SETUP_POSTCHECK_FAILED")

    def test_postcheck_rejects_concurrent_project_scaffold_change(self):
        plan = self._plan()
        real_apply_git_exclude = setup_module.apply_git_exclude

        def apply_exclude_then_change_scaffold(exclude_plan):
            real_apply_git_exclude(exclude_plan)
            (self.project / "knowledge" / "README.md").write_bytes(
                b"changed concurrently\n"
            )

        with mock.patch(
            "didimlog.claude.setup.apply_git_exclude",
            side_effect=apply_exclude_then_change_scaffold,
        ):
            with self.assertRaises(DidimError) as caught:
                self._apply(plan)

        self.assertEqual(caught.exception.token, "SETUP_POSTCHECK_FAILED")
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())

    def test_apply_runs_each_stage_in_the_approved_order(self):
        plan = self._plan()
        calls = mock.Mock()
        with mock.patch.object(
            setup_module,
            "_apply_personal",
            wraps=setup_module._apply_personal,
        ) as personal, mock.patch.object(
            setup_module.personal_index,
            "write_all",
            wraps=setup_module.personal_index.write_all,
        ) as personal_index, mock.patch.object(
            setup_module,
            "apply_scaffold",
            wraps=setup_module.apply_scaffold,
        ) as scaffold, mock.patch.object(
            setup_module,
            "write_index",
            wraps=setup_module.write_index,
        ) as project_index, mock.patch.object(
            setup_module,
            "apply_git_exclude",
            wraps=setup_module.apply_git_exclude,
        ) as exclude, mock.patch.object(
            setup_module,
            "apply_connect",
            wraps=setup_module.apply_connect,
        ) as claude, mock.patch.object(
            setup_module,
            "_postcheck",
            wraps=setup_module._postcheck,
        ) as postcheck:
            calls.attach_mock(personal, "personal")
            calls.attach_mock(personal_index, "personal_index")
            calls.attach_mock(scaffold, "scaffold")
            calls.attach_mock(project_index, "project_index")
            calls.attach_mock(exclude, "exclude")
            calls.attach_mock(claude, "claude")
            calls.attach_mock(postcheck, "postcheck")

            self.assertEqual(self._apply(plan), ())

        self.assertEqual(
            [call[0] for call in calls.mock_calls],
            [
                "personal",
                "personal_index",
                "scaffold",
                "project_index",
                "exclude",
                "claude",
                "postcheck",
            ],
        )

    def test_shared_transition_removes_the_managed_block_and_stops_ignoring(self):
        self._apply(self._plan())

        shared = self._plan(project_knowledge="shared")
        self.assertIn(
            "knowledge 폴더의 Git 로컬 제외를 제거",
            shared.project_changes,
        )
        self.assertEqual(self._apply(shared), ())

        self.assertNotIn(START, self._exclude_path().read_bytes())
        self.assertFalse(self._knowledge_is_ignored())

    def test_shared_transition_returns_notice_when_gitignore_still_excludes(self):
        self._apply(self._plan())
        (self.project / ".gitignore").write_bytes(b"/knowledge/\n")

        shared = self._plan(project_knowledge="shared")

        self.assertEqual(
            shared.project_notices,
            ("다른 Git 규칙이 knowledge 폴더를 계속 제외하고 있습니다.",),
        )
        self.assertEqual(self._apply(shared), shared.project_notices)
        self.assertNotIn(START, self._exclude_path().read_bytes())
        self.assertTrue(self._knowledge_is_ignored())

    def test_shared_apply_allows_existing_tracked_knowledge(self):
        knowledge = self.project / "knowledge"
        knowledge.mkdir()
        tracked = knowledge / "tracked.txt"
        tracked.write_bytes(b"tracked\n")
        self._git(self.project, "add", "knowledge/tracked.txt")

        shared = self._plan(project_knowledge="shared")

        self.assertEqual(self._apply(shared), ())
        self.assertNotIn(START, self._exclude_path().read_bytes())
        self.assertEqual(
            self._git(
                self.project,
                "ls-files",
                "--",
                "knowledge/tracked.txt",
            ).stdout,
            b"knowledge/tracked.txt\n",
        )

    def test_linked_worktree_apply_writes_the_common_exclude_file(self):
        seed = self.project / "seed"
        seed.write_bytes(b"seed\n")
        self._git(self.project, "add", "seed")
        self._git(
            self.project,
            "-c",
            "user.name=Didimlog Test",
            "-c",
            "user.email=didimlog@example.invalid",
            "commit",
            "-qm",
            "seed",
        )
        linked = self.root / "linked"
        self._git(self.project, "worktree", "add", "-q", str(linked))
        common_exclude = self._exclude_path(self.project)

        plan = self._plan(cwd=linked)
        self.assertEqual(self._apply(plan), ())

        self.assertEqual(plan._project_exclude.path, common_exclude)
        self.assertIn(LOCAL_BLOCK, common_exclude.read_bytes())
        self.assertTrue(self._knowledge_is_ignored(linked))

    def test_second_plan_and_apply_are_idempotent(self):
        self._apply(self._plan())
        before = self._snapshot(self.root)

        second = self._plan()
        self.assertEqual(second.personal_changes, ())
        self.assertEqual(second.project_changes, ())
        self.assertEqual(second.claude_changes, ())
        self.assertEqual(second.project_notices, ())
        self.assertEqual(self._apply(second), ())

        self.assertEqual(self._snapshot(self.root), before)

    def test_personal_failure_starts_neither_project_nor_claude(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup._apply_personal",
            side_effect=OSError("personal failed"),
        ):
            with self.assertRaises(OSError):
                self._apply(plan)

        self.assertFalse((self.project / "knowledge").exists())
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())

    def test_project_failure_preserves_personal_material_but_starts_no_claude_write(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup.apply_scaffold",
            side_effect=OSError("project failed"),
        ):
            with self.assertRaises(OSError):
                self._apply(plan)

        self.assertTrue((self.home / "knowledge" / "MY-RULES.md").is_file())
        self.assertTrue((self.home / "knowledge" / "index" / "_global.md").is_file())
        self.assertNotIn(START, self._exclude_path().read_bytes())
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())

    def test_personal_index_failure_preserves_personal_files_and_starts_no_project(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup.personal_index.write_all",
            side_effect=OSError("index failed"),
        ):
            with self.assertRaises(OSError):
                self._apply(plan)

        self.assertTrue((self.home / "knowledge" / "MY-RULES.md").is_file())
        self.assertFalse((self.project / "knowledge").exists())
        self.assertNotIn(START, self._exclude_path().read_bytes())
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())

    def test_project_index_failure_preserves_both_scaffolds_and_skips_exclude(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup.write_index",
            side_effect=OSError("project index failed"),
        ):
            with self.assertRaises(OSError):
                self._apply(plan)

        self.assertTrue((self.home / "knowledge" / "MY-RULES.md").is_file())
        self.assertTrue((self.project / "knowledge" / "README.md").is_file())
        self.assertNotIn(START, self._exclude_path().read_bytes())
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())

    def test_concurrent_exclude_change_preserves_completed_stages_and_skips_claude(self):
        plan = self._plan()
        exclude = self._exclude_path()
        exclude.write_bytes(b"changed concurrently\n")

        with mock.patch(
            "didimlog.claude.setup.apply_connect",
            side_effect=AssertionError("Claude must not start"),
        ) as apply_connect:
            with self.assertRaises(DidimError) as caught:
                self._apply(plan)

        apply_connect.assert_not_called()

        self.assertEqual(caught.exception.token, "PROJECT_EXCLUDE_CHANGED")
        self.assertEqual(exclude.read_bytes(), b"changed concurrently\n")
        self.assertTrue((self.home / "knowledge" / "index" / "_global.md").is_file())
        self.assertTrue(
            (self.project / "knowledge" / "index" / "INDEX.md").is_file()
        )
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())

    def test_claude_failure_preserves_material_and_indexes(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup.apply_connect",
            side_effect=ValueError("Claude failed"),
        ):
            with self.assertRaises(ValueError):
                self._apply(plan)

        checked = run_index(check=True, home=self.home, cwd=self.project)
        self.assertTrue(checked.personal.endswith("PERSONAL_INDEX_CURRENT"))
        self.assertTrue(checked.project.endswith("PROJECT_INDEX_CURRENT"))
        self.assertTrue((self.home / "knowledge" / "MY-RULES.md").is_file())
        self.assertTrue((self.project / "knowledge" / "README.md").is_file())
        self.assertTrue(self._knowledge_is_ignored())
        self.assertIn(LOCAL_BLOCK, self._exclude_path().read_bytes())

    def test_failed_postcheck_is_not_reported_as_success(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup.inspect",
            return_value=(mock.Mock(token="CLAUDE_IMPORT_MISSING"),),
        ):
            with self.assertRaises(DidimError) as caught:
                self._apply(plan)

        self.assertEqual(caught.exception.token, "SETUP_POSTCHECK_FAILED")
        self.assertTrue(self._knowledge_is_ignored())
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())

    def test_final_tracked_check_rolls_back_only_claude_and_preserves_exclude(self):
        plan = self._plan()
        real_apply_connect = setup_module.apply_connect

        def connect_then_track(claude_plan, journal):
            real_apply_connect(claude_plan, journal)
            self._git(
                self.project,
                "add",
                "-f",
                "knowledge/README.md",
            )

        with mock.patch(
            "didimlog.claude.setup.apply_connect",
            side_effect=connect_then_track,
        ):
            with self.assertRaises(DidimError) as caught:
                self._apply(plan)

        self.assertEqual(caught.exception.token, "SETUP_POSTCHECK_FAILED")
        self.assertIn(LOCAL_BLOCK, self._exclude_path().read_bytes())
        self.assertTrue((self.home / "knowledge" / "MY-RULES.md").is_file())
        self.assertTrue((self.project / "knowledge" / "README.md").is_file())
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())


if __name__ == "__main__":
    unittest.main()
