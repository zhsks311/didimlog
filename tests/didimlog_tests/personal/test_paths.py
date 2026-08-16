import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.personal.paths import (
    ProjectDirectoryError,
    book_dir,
    data_home,
    docs_dir,
    index_dir,
    lessons_dir,
    project_directory_unchanged,
    resolve_project,
    resolve_project_directory,
    validate_project,
)


GIT = shutil.which("git")


def isolated_git_environment(home, ceiling):
    return {
        "GIT_CEILING_DIRECTORIES": str(ceiling),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home),
        "LC_ALL": "C",
        "PATH": str(Path(GIT).parent) + os.pathsep + os.defpath,
        "XDG_CONFIG_HOME": str(home / ".config"),
    }


class PersonalPathTests(unittest.TestCase):
    def create_symlink(self, link, target, *, target_is_directory=True):
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (NotImplementedError, OSError) as error:
            self.skipTest("symlinks unavailable: {}".format(error))

    def test_data_home_uses_injected_home(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"

            self.assertEqual(data_home(home=home), home / "knowledge")

    def test_personal_directories_are_below_injected_data_home(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            knowledge = home / "knowledge"

            self.assertEqual(lessons_dir(home=home), knowledge / "lessons")
            self.assertEqual(docs_dir(home=home), knowledge / "docs")
            self.assertEqual(book_dir(home=home), knowledge / "book")
            self.assertEqual(index_dir(home=home), knowledge / "index")

    def test_validate_project_accepts_a_slug(self):
        self.assertEqual(validate_project("Demo-api9"), "Demo-api9")

    def test_validate_project_rejects_global_without_permission(self):
        with self.assertRaises(ValueError):
            validate_project("_global")

    def test_validate_project_allows_global_when_permitted(self):
        self.assertEqual(
            validate_project("_global", allow_global=True),
            "_global",
        )

    def test_validate_project_rejects_invalid_slugs(self):
        invalid_values = (
            "",
            "-demo",
            "demo-",
            "demo--api",
            "demo_api",
            "demo/api",
            ".",
        )

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_project(value)

    def test_explicit_project_takes_precedence_over_git_discovery(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_cwd = Path(temporary_directory) / "does-not-exist"

            self.assertEqual(
                resolve_project("manual-project", cwd=missing_cwd),
                "manual-project",
            )

    def test_explicit_global_requires_permission(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_cwd = Path(temporary_directory) / "does-not-exist"

            with self.assertRaises(ValueError):
                resolve_project("_global", cwd=missing_cwd)
            self.assertEqual(
                resolve_project("_global", cwd=missing_cwd, allow_global=True),
                "_global",
            )

    def test_project_directory_resolves_real_and_linked_directories(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            base = root / "knowledge" / "lessons"
            real = base / "real"
            external = root / "external"
            real.mkdir(parents=True)
            external.mkdir()
            self.create_symlink(base / "linked", external)

            resolved_real = resolve_project_directory(base, "real")
            resolved_link = resolve_project_directory(base, "linked")

            self.assertEqual(resolved_real.logical, real)
            self.assertEqual(resolved_real.physical, real)
            self.assertEqual(resolved_link.logical, base / "linked")
            self.assertEqual(resolved_link.physical, external.resolve(strict=True))
            self.assertTrue(project_directory_unchanged(resolved_real))
            self.assertTrue(project_directory_unchanged(resolved_link))

    def test_project_directory_returns_none_when_project_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory) / "knowledge" / "lessons"
            base.mkdir(parents=True)

            self.assertIsNone(resolve_project_directory(base, "missing"))

    def test_project_directory_rejects_base_replaced_during_lookup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            base = root / "knowledge" / "lessons"
            base.mkdir(parents=True)
            logical = base / "missing"
            original_lstat = Path.lstat

            def replace_base_before_child_lstat(path):
                if path == logical:
                    base.rename(root / "original-lessons")
                    base.mkdir()
                return original_lstat(path)

            with mock.patch.object(
                Path,
                "lstat",
                replace_base_before_child_lstat,
            ):
                with self.assertRaises(ProjectDirectoryError) as caught:
                    resolve_project_directory(base, "missing")

            self.assertEqual(caught.exception.logical, base)
            self.assertEqual(
                caught.exception.reason,
                "source category must be a real directory",
            )

    def test_project_directory_rejects_symlink_base(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            real_base = root / "real-lessons"
            real_base.mkdir()
            base = root / "knowledge" / "lessons"
            base.parent.mkdir()
            self.create_symlink(base, real_base)

            with self.assertRaises(ProjectDirectoryError) as caught:
                resolve_project_directory(base, "demo")

            self.assertEqual(caught.exception.logical, base)
            self.assertEqual(
                caught.exception.reason,
                "source category must be a real directory",
            )

    def test_project_directory_rejects_dangling_link(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory) / "knowledge" / "lessons"
            base.mkdir(parents=True)
            linked = base / "linked"
            self.create_symlink(linked, base / "missing")

            with self.assertRaises(ProjectDirectoryError) as caught:
                resolve_project_directory(base, "linked")

            self.assertEqual(caught.exception.logical, linked)
            self.assertEqual(caught.exception.reason, "project link target is missing")

    def test_project_directory_rejects_link_cycle(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory) / "knowledge" / "lessons"
            base.mkdir(parents=True)
            linked = base / "linked"
            self.create_symlink(linked, linked)

            with self.assertRaises(ProjectDirectoryError) as caught:
                resolve_project_directory(base, "linked")

            self.assertEqual(caught.exception.logical, linked)
            self.assertEqual(caught.exception.reason, "project link cannot be resolved")

    def test_project_directory_rejects_link_to_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            base = root / "knowledge" / "lessons"
            base.mkdir(parents=True)
            target = root / "target.txt"
            target.write_text("not a directory", encoding="utf-8")
            linked = base / "linked-file"
            self.create_symlink(linked, target, target_is_directory=False)

            with self.assertRaises(ProjectDirectoryError) as caught:
                resolve_project_directory(base, "linked-file")

            self.assertEqual(caught.exception.logical, linked)
            self.assertEqual(
                caught.exception.reason,
                "project entry must point to a directory",
            )

    def test_project_directory_detects_retargeted_link(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            base = root / "knowledge" / "lessons"
            base.mkdir(parents=True)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            linked = base / "linked"
            self.create_symlink(linked, first)
            resolved = resolve_project_directory(base, "linked")

            linked.unlink()
            self.create_symlink(linked, second)

            self.assertFalse(project_directory_unchanged(resolved))



@unittest.skipUnless(GIT, "git is required for project discovery tests")
class GitProjectResolutionTests(unittest.TestCase):
    def make_environment(self, root):
        home = root / "home"
        home.mkdir()
        return isolated_git_environment(home, root)

    def init_repository(self, repository, environment):
        repository.mkdir()
        subprocess.run(
            [GIT, "-c", "init.defaultBranch=main", "init", "-q", str(repository)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )

    def test_resolve_project_uses_git_root_basename(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = self.make_environment(root)
            repository = root / "demo-repository"
            self.init_repository(repository, environment)
            nested = repository / "nested"
            nested.mkdir()

            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(
                    resolve_project(cwd=nested),
                    "demo-repository",
                )

    def test_git_discovery_cannot_select_global_implicitly(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = self.make_environment(root)
            repository = root / "_global"
            self.init_repository(repository, environment)

            with mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ValueError):
                    resolve_project(cwd=repository, allow_global=True)

    def test_resolve_project_fails_outside_git(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = self.make_environment(root)
            outside_repository = root / "not-a-repository"
            outside_repository.mkdir()

            with mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ValueError):
                    resolve_project(cwd=outside_repository)

    def test_resolve_project_rejects_symlink_source_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = self.make_environment(root)
            repository = root / "real-repository"
            self.init_repository(repository, environment)
            linked_repository = root / "linked-repository"
            try:
                linked_repository.symlink_to(repository, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest("directory symlinks unavailable: {}".format(error))

            with mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ValueError):
                    resolve_project(cwd=linked_repository)

    def test_resolve_project_rejects_child_below_symlinked_repository(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = self.make_environment(root)
            repository = root / "real-repository"
            self.init_repository(repository, environment)
            nested = repository / "nested"
            nested.mkdir()
            linked_repository = root / "linked-repository"
            try:
                linked_repository.symlink_to(repository, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest("directory symlinks unavailable: {}".format(error))

            with mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(ValueError):
                    resolve_project(cwd=linked_repository / nested.name)


if __name__ == "__main__":
    unittest.main()
