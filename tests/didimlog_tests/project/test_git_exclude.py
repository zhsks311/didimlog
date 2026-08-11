import dataclasses
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from didimlog import conditional_file
from didimlog.errors import DidimError
from didimlog.project.git_exclude import (
    GitExcludePlan,
    apply_git_exclude,
    discover_project_for_setup,
    plan_git_exclude,
    project_knowledge_is_ignored,
)


GIT = shutil.which("git")
START = b"# DIDIMLOG:START project-knowledge"
RULE = b"/knowledge/"
END = b"# DIDIMLOG:END project-knowledge"
LF_BLOCK = START + b"\n" + RULE + b"\n" + END + b"\n"
CRLF_BLOCK = START + b"\r\n" + RULE + b"\r\n" + END + b"\r\n"


class ErrorContractMixin:
    def assert_token(self, token, operation):
        with self.assertRaises(DidimError) as captured:
            operation()
        error = captured.exception
        self.assertEqual(error.token, token)
        self.assertIsInstance(error.help_text, str)
        self.assertTrue(error.help_text)
        self.assertNotIn(str(getattr(self, "root", "")), str(error))
        self.assertNotIn(str(getattr(self, "root", "")), error.help_text)
        return error


@unittest.skipUnless(GIT, "git is required for Git exclude contract tests")
class GitExcludeContractTests(ErrorContractMixin, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="didimlog-git-exclude-")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CONFIG_HOME": str(self.root / "xdg"),
        }
        self.project = self.make_repository("project")

    def tearDown(self):
        self.temporary.cleanup()

    @contextmanager
    def isolated_environment(self):
        with mock.patch.dict(os.environ, self.environment, clear=True):
            yield

    def git(self, project, *arguments, expected=0):
        result = subprocess.run(
            [GIT, *arguments],
            cwd=project,
            env=self.environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, expected, result.stderr.decode(errors="replace"))
        return result

    def make_repository(self, name):
        project = self.root / name
        project.mkdir()
        self.git(project, "-c", "init.defaultBranch=main", "init", "-q")
        return project

    def exclude_path(self, project=None):
        selected = self.project if project is None else project
        result = self.git(
            selected,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/exclude",
        )
        return Path(result.stdout.decode("utf-8").strip())

    def call(self, operation, *arguments):
        with self.isolated_environment():
            return operation(*arguments)

    def test_discovery_finds_root_from_nested_directory_and_returns_none_outside_git(self):
        nested = self.project / "one" / "two"
        nested.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()

        self.assertEqual(self.call(discover_project_for_setup, nested), self.project)
        self.assertIsNone(self.call(discover_project_for_setup, outside))

    def test_discovery_returns_linked_worktree_root(self):
        seed = self.project / "seed"
        seed.write_text("seed\n", encoding="utf-8")
        self.git(self.project, "add", "seed")
        self.git(
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
        self.git(self.project, "worktree", "add", "-q", str(linked))

        self.assertEqual(self.call(discover_project_for_setup, linked), linked)
        self.assertEqual(
            self.call(plan_git_exclude, linked, "shared").path,
            self.exclude_path(self.project),
        )

    def test_discovery_distinguishes_missing_git_from_a_broken_marked_repository(self):
        outside = self.root / "plain"
        outside.mkdir()
        timeout = subprocess.TimeoutExpired(["git"], 5)

        with self.isolated_environment(), mock.patch(
            "didimlog.project.git_exclude.subprocess.run", side_effect=FileNotFoundError
        ):
            self.assertIsNone(discover_project_for_setup(outside))
            self.assert_token(
                "PROJECT_EXCLUDE_GIT_UNAVAILABLE",
                lambda: discover_project_for_setup(self.project),
            )

        with self.isolated_environment(), mock.patch(
            "didimlog.project.git_exclude.subprocess.run", side_effect=timeout
        ) as run:
            self.assert_token(
                "PROJECT_EXCLUDE_GIT_UNAVAILABLE",
                lambda: discover_project_for_setup(self.project),
            )
            self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_discovery_rejects_empty_multiline_non_utf8_and_relative_git_output(self):
        outputs = (b"", b"/one\n/two\n", b"\xff\n", b"relative\n")
        for output in outputs:
            with self.subTest(output=output), self.isolated_environment(), mock.patch(
                "didimlog.project.git_exclude.subprocess.run",
                return_value=subprocess.CompletedProcess(["git"], 0, output, b""),
            ):
                self.assert_token(
                    "PROJECT_EXCLUDE_GIT_UNAVAILABLE",
                    lambda: discover_project_for_setup(self.project),
                )

    def test_local_and_shared_round_trip_exact_user_bytes(self):
        cases = (
            (b"user\n", b"user\n" + LF_BLOCK),
            (b"user\r\n", b"user\r\n" + CRLF_BLOCK),
            (b"no-final-newline", LF_BLOCK + b"no-final-newline"),
            (b"", LF_BLOCK),
        )
        path = self.exclude_path()
        for original, intended in cases:
            with self.subTest(original=original):
                path.write_bytes(original)
                local = self.call(plan_git_exclude, self.project, "local")
                self.assertEqual(local.original, original)
                self.assertEqual(local.intended, intended)
                self.call(apply_git_exclude, local)
                self.assertEqual(path.read_bytes(), intended)

                shared = self.call(plan_git_exclude, self.project, "shared")
                self.assertEqual(shared.intended, original)
                self.call(apply_git_exclude, shared)
                self.assertEqual(path.read_bytes(), original)

    def test_shared_recognizes_one_exact_block_at_front_middle_or_end(self):
        path = self.exclude_path()
        cases = (
            (LF_BLOCK + b"user", b"user"),
            (b"before\n" + LF_BLOCK + b"after\n", b"before\nafter\n"),
            (b"user\r\n" + CRLF_BLOCK, b"user\r\n"),
            (b"user\n" + LF_BLOCK[:-1], b"user\n"),
        )
        for original, intended in cases:
            with self.subTest(original=original):
                path.write_bytes(original)
                plan = self.call(plan_git_exclude, self.project, "shared")
                self.assertEqual(plan.intended, intended)

    def test_duplicate_incomplete_residual_and_internally_changed_markers_are_invalid(self):
        path = self.exclude_path()
        invalid_contents = (
            LF_BLOCK + LF_BLOCK,
            START + b"\n" + RULE + b"\n",
            END + b"\n",
            START + b"\n/other/\n" + END + b"\n",
            LF_BLOCK + START + b" trailing",
        )
        for content in invalid_contents:
            with self.subTest(content=content):
                path.write_bytes(content)
                self.assert_token(
                    "PROJECT_EXCLUDE_MARKERS_INVALID",
                    lambda: self.call(plan_git_exclude, self.project, "local"),
                )

    def test_shared_missing_exclude_is_a_noop_and_does_not_create_a_file(self):
        path = self.exclude_path()
        path.unlink()

        plan = self.call(plan_git_exclude, self.project, "shared")
        self.assertIsNone(plan.original)
        self.assertIsNone(plan.intended)
        self.assertEqual(plan.changes, ())
        self.call(apply_git_exclude, plan)

        self.assertFalse(path.exists())

    def test_existing_noop_preserves_inode_and_mtime(self):
        path = self.exclude_path()
        path.write_bytes(LF_BLOCK)
        before = path.stat()

        plan = self.call(plan_git_exclude, self.project, "local")
        self.assertEqual(plan.original, plan.intended)
        self.call(apply_git_exclude, plan)
        after = path.stat()

        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_local_refuses_tracked_knowledge_during_plan_and_apply(self):
        knowledge = self.project / "knowledge"
        knowledge.mkdir()
        (knowledge / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        self.git(self.project, "add", "knowledge/tracked.txt")
        self.assert_token(
            "PROJECT_KNOWLEDGE_TRACKED",
            lambda: self.call(plan_git_exclude, self.project, "local"),
        )

        self.git(
            self.project,
            "rm",
            "--cached",
            "-q",
            "--",
            "knowledge/tracked.txt",
        )
        plan = self.call(plan_git_exclude, self.project, "local")
        self.git(self.project, "add", "knowledge/tracked.txt")
        self.assert_token(
            "PROJECT_KNOWLEDGE_TRACKED",
            lambda: self.call(apply_git_exclude, plan),
        )
        self.assertNotIn(START, self.exclude_path().read_bytes())

    def test_local_rechecks_tracked_state_after_the_write(self):
        knowledge = self.project / "knowledge"
        knowledge.mkdir()
        tracked = knowledge / "tracked.txt"
        tracked.write_text("tracked\n", encoding="utf-8")
        plan = self.call(plan_git_exclude, self.project, "local")
        real_writer = conditional_file.write_regular_file_if_unchanged

        def write_then_track(path, original, intended):
            real_writer(path, original, intended)
            self.git(self.project, "add", "-f", "knowledge/tracked.txt")

        with self.isolated_environment(), mock.patch(
            "didimlog.project.git_exclude.write_regular_file_if_unchanged",
            side_effect=write_then_track,
        ):
            self.assert_token(
                "PROJECT_KNOWLEDGE_TRACKED",
                lambda: apply_git_exclude(plan),
            )

    def test_local_planned_state_refuses_a_later_negate_rule_before_writing(self):
        path = self.exclude_path()
        path.write_bytes(b"/knowledge/\n!/knowledge/")
        before = path.read_bytes()

        self.assert_token(
            "PROJECT_EXCLUDE_CONFLICT",
            lambda: self.call(plan_git_exclude, self.project, "local"),
        )
        self.assertEqual(path.read_bytes(), before)

    def test_local_planned_state_refuses_a_gitignore_negate_rule(self):
        gitignore = self.project / ".gitignore"
        gitignore.write_bytes(b"/knowledge/\n!/knowledge/\n")
        before_exclude = self.exclude_path().read_bytes()

        self.assert_token(
            "PROJECT_EXCLUDE_CONFLICT",
            lambda: self.call(plan_git_exclude, self.project, "local"),
        )
        self.assertEqual(self.exclude_path().read_bytes(), before_exclude)
        self.assertEqual(gitignore.read_bytes(), b"/knowledge/\n!/knowledge/\n")

    def test_shared_notices_when_gitignore_keeps_knowledge_ignored(self):
        gitignore = self.project / ".gitignore"
        gitignore.write_bytes(b"/knowledge/\n")
        path = self.exclude_path()
        path.write_bytes(LF_BLOCK)

        plan = self.call(plan_git_exclude, self.project, "shared")

        self.assertTrue(plan.notices)
        self.assertEqual(gitignore.read_bytes(), b"/knowledge/\n")

    def test_shared_notices_when_user_exclude_or_global_exclude_keeps_rule(self):
        path = self.exclude_path()
        global_exclude = self.root / "global-excludes"
        cases = (
            (b"/knowledge/\n" + LF_BLOCK, None),
            (LF_BLOCK, global_exclude),
        )
        for content, configured_global in cases:
            with self.subTest(configured_global=configured_global):
                path.write_bytes(content)
                if configured_global is not None:
                    global_exclude.write_bytes(b"/knowledge/\n")
                    self.git(
                        self.project,
                        "config",
                        "core.excludesFile",
                        str(global_exclude),
                    )
                plan = self.call(plan_git_exclude, self.project, "shared")
                self.assertTrue(plan.notices)
        self.git(self.project, "config", "--unset-all", "core.excludesFile")

    def test_dry_run_copies_ignore_case_for_case_variant_gitignore_rule(self):
        (self.project / ".gitignore").write_bytes(b"/KNOWLEDGE/\n")
        self.git(self.project, "config", "core.ignoreCase", "true")
        self.exclude_path().write_bytes(LF_BLOCK)

        plan = self.call(plan_git_exclude, self.project, "shared")

        self.assertTrue(plan.notices)

    def test_apply_sets_and_removes_effective_ignore_without_changing_gitignore_or_index(self):
        gitignore = self.project / ".gitignore"
        gitignore.write_bytes(b"user rule\n")
        before_gitignore = gitignore.read_bytes()
        before_index = self.git(self.project, "ls-files", "-z").stdout

        local = self.call(plan_git_exclude, self.project, "local")
        self.call(apply_git_exclude, local)
        self.assertTrue(self.call(project_knowledge_is_ignored, self.project))
        shared = self.call(plan_git_exclude, self.project, "shared")
        self.call(apply_git_exclude, shared)
        self.assertFalse(self.call(project_knowledge_is_ignored, self.project))

        self.assertEqual(gitignore.read_bytes(), before_gitignore)
        self.assertEqual(self.git(self.project, "ls-files", "-z").stdout, before_index)

    def test_concurrent_create_change_and_delete_are_rejected_without_overwrite(self):
        path = self.exclude_path()

        path.unlink()
        created_plan = self.call(plan_git_exclude, self.project, "local")
        path.write_bytes(b"created concurrently\n")
        self.assert_token(
            "PROJECT_EXCLUDE_CHANGED",
            lambda: self.call(apply_git_exclude, created_plan),
        )
        self.assertEqual(path.read_bytes(), b"created concurrently\n")

        path.write_bytes(b"planned\n")
        changed_plan = self.call(plan_git_exclude, self.project, "local")
        path.write_bytes(b"changed concurrently\n")
        self.assert_token(
            "PROJECT_EXCLUDE_CHANGED",
            lambda: self.call(apply_git_exclude, changed_plan),
        )
        self.assertEqual(path.read_bytes(), b"changed concurrently\n")

        path.write_bytes(b"planned\n")
        deleted_plan = self.call(plan_git_exclude, self.project, "local")
        path.unlink()
        self.assert_token(
            "PROJECT_EXCLUDE_CHANGED",
            lambda: self.call(apply_git_exclude, deleted_plan),
        )
        self.assertFalse(path.exists())

    def test_forged_plan_fields_and_changed_exclude_path_are_rejected(self):
        plan = self.call(plan_git_exclude, self.project, "local")
        for forged in (
            dataclasses.replace(plan, path=self.root / "forged"),
            dataclasses.replace(plan, intended=b"forged"),
            dataclasses.replace(plan, original=b"forged"),
            dataclasses.replace(plan, changes=("forged",)),
            dataclasses.replace(plan, notices=("forged",)),
            dataclasses.replace(plan, mode="shared"),
        ):
            with self.subTest(forged=forged):
                self.assert_token(
                    "PROJECT_EXCLUDE_CHANGED",
                    lambda forged=forged: self.call(apply_git_exclude, forged),
                )

        changed_path = self.root / "changed" / "info" / "exclude"
        changed_path.parent.mkdir(parents=True)
        with self.isolated_environment(), mock.patch(
            "didimlog.project.git_exclude._git_exclude_path",
            return_value=changed_path,
        ):
            self.assert_token(
                "PROJECT_EXCLUDE_CHANGED",
                lambda: apply_git_exclude(plan),
            )

    def test_symlink_final_and_direct_parent_are_unsafe(self):
        path = self.exclude_path()
        outside = self.root / "outside-exclude"
        outside.write_bytes(b"outside\n")
        path.unlink()
        path.symlink_to(outside)
        self.assert_token(
            "PROJECT_EXCLUDE_UNSAFE",
            lambda: self.call(plan_git_exclude, self.project, "local"),
        )
        self.assertEqual(outside.read_bytes(), b"outside\n")

        second = self.make_repository("parent-symlink")
        second_path = self.exclude_path(second)
        real_info = self.root / "real-info"
        second_path.parent.rename(real_info)
        second_path.parent.symlink_to(real_info, target_is_directory=True)
        self.assert_token(
            "PROJECT_EXCLUDE_UNSAFE",
            lambda: self.call(plan_git_exclude, second, "local"),
        )

    def test_missing_or_non_directory_direct_parent_is_unsafe(self):
        missing_parent_repo = self.make_repository("missing-parent")
        missing_path = self.exclude_path(missing_parent_repo)
        shutil.rmtree(missing_path.parent)
        self.assert_token(
            "PROJECT_EXCLUDE_UNSAFE",
            lambda: self.call(plan_git_exclude, missing_parent_repo, "shared"),
        )

        file_parent_repo = self.make_repository("file-parent")
        file_parent_path = self.exclude_path(file_parent_repo)
        shutil.rmtree(file_parent_path.parent)
        file_parent_path.parent.write_bytes(b"not a directory")
        self.assert_token(
            "PROJECT_EXCLUDE_UNSAFE",
            lambda: self.call(plan_git_exclude, file_parent_repo, "shared"),
        )

    def test_public_plan_is_frozen_and_mode_is_validated(self):
        plan = self.call(plan_git_exclude, self.project, "shared")
        self.assertIsInstance(plan, GitExcludePlan)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.mode = "local"
        with self.assertRaises(ValueError):
            self.call(plan_git_exclude, self.project, "invalid")


if __name__ == "__main__":
    unittest.main()
