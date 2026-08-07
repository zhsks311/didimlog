import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.claude.setup import apply_setup, plan_setup
from didimlog.errors import DidimError
from didimlog.indexing import run_index


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

    def _plan(self):
        with mock.patch(
            "didimlog.claude.setup._find_launcher",
            return_value=str(self.launcher),
        ):
            return plan_setup(
                home=self.home,
                cwd=self.project,
                config=self.config,
                include_project=True,
                skip_claude=False,
            )

    def _snapshot(self, root):
        if not root.exists():
            return None
        result = {}
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                result[relative] = ("directory",)
            else:
                result[relative] = ("file", path.read_bytes())
        return result

    def test_approval_is_required_before_any_write(self):
        plan = self._plan()
        before = self._snapshot(self.root)

        with self.assertRaises(DidimError) as caught:
            apply_setup(plan, approved=False)

        self.assertEqual(caught.exception.token, "SETUP_APPROVAL_REQUIRED")
        self.assertEqual(self._snapshot(self.root), before)

    def test_fresh_apply_creates_every_surface_and_passes_real_postchecks(self):
        plan = self._plan()

        self.assertIsNone(apply_setup(plan, approved=True))

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

    def test_second_plan_and_apply_are_idempotent(self):
        apply_setup(self._plan(), approved=True)
        before = self._snapshot(self.root)

        second = self._plan()
        self.assertEqual(second.personal_changes, ())
        self.assertEqual(second.project_changes, ())
        self.assertEqual(second.claude_changes, ())
        apply_setup(second, approved=True)

        self.assertEqual(self._snapshot(self.root), before)

    def test_personal_failure_starts_neither_project_nor_claude(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup._apply_personal",
            side_effect=OSError("personal failed"),
        ):
            with self.assertRaises(OSError):
                apply_setup(plan, approved=True)

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
                apply_setup(plan, approved=True)

        self.assertTrue((self.home / "knowledge" / "MY-RULES.md").is_file())
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())

    def test_index_failure_preserves_scaffolds_and_starts_no_claude_write(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup.personal_index.write_all",
            side_effect=OSError("index failed"),
        ):
            with self.assertRaises(OSError):
                apply_setup(plan, approved=True)

        self.assertTrue((self.home / "knowledge" / "MY-RULES.md").is_file())
        self.assertTrue((self.project / "knowledge" / "README.md").is_file())
        self.assertFalse((self.config / "CLAUDE.md").exists())
        self.assertFalse((self.config / "settings.json").exists())

    def test_claude_failure_preserves_material_and_indexes(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup.apply_connect",
            side_effect=ValueError("Claude failed"),
        ):
            with self.assertRaises(ValueError):
                apply_setup(plan, approved=True)

        checked = run_index(check=True, home=self.home, cwd=self.project)
        self.assertTrue(checked.personal.endswith("PERSONAL_INDEX_CURRENT"))
        self.assertTrue(checked.project.endswith("PROJECT_INDEX_CURRENT"))
        self.assertTrue((self.home / "knowledge" / "MY-RULES.md").is_file())
        self.assertTrue((self.project / "knowledge" / "README.md").is_file())

    def test_failed_postcheck_is_not_reported_as_success(self):
        plan = self._plan()
        with mock.patch(
            "didimlog.claude.setup.inspect",
            return_value=(mock.Mock(token="CLAUDE_IMPORT_MISSING"),),
        ):
            with self.assertRaises(DidimError) as caught:
                apply_setup(plan, approved=True)

        self.assertEqual(caught.exception.token, "SETUP_POSTCHECK_FAILED")


if __name__ == "__main__":
    unittest.main()
