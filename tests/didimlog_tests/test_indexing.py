import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from didimlog.indexing import IndexResult, run_index
from didimlog.project.scaffold import apply_scaffold, plan_scaffold


LESSON = """---
topic: indexing
title: index 확인 규칙
summary: index 상태를 구분한다
tags: [indexing]
date: 2026-08-05
---
## 교훈
index는 원본 전체와 일치해야 한다.
"""


class IndexServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.cwd = self.root / "outside"
        self.home.mkdir()
        self.cwd.mkdir()
        (self.home / "knowledge").mkdir()

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
        apply_scaffold(plan_scaffold(project))
        return project

    def _write_lesson(self, text=LESSON):
        lesson = self.home / "knowledge" / "lessons" / "_global" / "index.md"
        lesson.parent.mkdir(parents=True, exist_ok=True)
        lesson.write_text(text, encoding="utf-8", newline="")
        return lesson

    def test_always_writes_all_personal_indexes_and_only_a_prepared_git_project(self):
        project = self._git_project()
        self._write_lesson()

        result = run_index(check=False, home=self.home, cwd=project / "knowledge")

        self.assertEqual(
            result,
            IndexResult(
                personal="개인 지식: PERSONAL_INDEX_WRITTEN",
                project="프로젝트 근거: PROJECT_INDEX_WRITTEN",
            ),
        )
        self.assertTrue((self.home / "knowledge" / "index" / "_global.md").is_file())
        self.assertTrue((project / "knowledge" / "index" / "INDEX.md").is_file())

    def test_outside_git_still_processes_personal_and_explains_project_setup(self):
        self._write_lesson()

        result = run_index(check=False, home=self.home, cwd=self.cwd)

        self.assertEqual(result.personal, "개인 지식: PERSONAL_INDEX_WRITTEN")
        self.assertEqual(
            result.project,
            "프로젝트 근거: 설정되지 않음 — didim setup을 실행하세요.",
        )
        self.assertTrue((self.home / "knowledge" / "index" / "_global.md").is_file())
        self.assertFalse((self.cwd / "knowledge").exists())

    def test_git_project_without_complete_scaffold_is_not_mutated(self):
        if shutil.which("git") is None:
            self.skipTest("git is required")
        project = self.root / "unconfigured"
        project.mkdir()
        subprocess.run(
            ["git", "init", "-q"],
            cwd=project,
            check=True,
            capture_output=True,
        )

        result = run_index(check=False, home=self.home, cwd=project)

        self.assertEqual(
            result.project,
            "프로젝트 근거: 설정되지 않음 — didim setup을 실행하세요.",
        )
        self.assertFalse((project / "knowledge").exists())

    def test_check_distinguishes_missing_without_writing(self):
        project = self._git_project()
        self._write_lesson()
        personal_index = self.home / "knowledge" / "index"
        project_index = project / "knowledge" / "index" / "INDEX.md"

        result = run_index(check=True, home=self.home, cwd=project)

        self.assertEqual(result.personal, "개인 지식: PERSONAL_INDEX_MISSING")
        self.assertEqual(result.project, "프로젝트 근거: PROJECT_INDEX_MISSING")
        self.assertFalse(personal_index.exists())
        self.assertFalse(project_index.exists())

    def test_check_distinguishes_stale_and_never_rewrites_current_bytes(self):
        project = self._git_project()
        self._write_lesson()
        run_index(check=False, home=self.home, cwd=project)
        personal_index = self.home / "knowledge" / "index" / "_global.md"
        project_index = project / "knowledge" / "index" / "INDEX.md"
        personal_index.write_bytes(b"stale personal\n")
        project_index.write_bytes(b"stale project\n")
        personal_before = personal_index.read_bytes()
        project_before = project_index.read_bytes()

        result = run_index(check=True, home=self.home, cwd=project)

        self.assertEqual(result.personal, "개인 지식: PERSONAL_INDEX_STALE")
        self.assertEqual(result.project, "프로젝트 근거: PROJECT_INDEX_STALE")
        self.assertEqual(personal_index.read_bytes(), personal_before)
        self.assertEqual(project_index.read_bytes(), project_before)

    def test_check_distinguishes_extra_personal_index_without_deleting_it(self):
        self._write_lesson()
        run_index(check=False, home=self.home, cwd=self.cwd)
        extra = self.home / "knowledge" / "index" / "extra.txt"
        extra.write_bytes(b"user bytes\n")

        result = run_index(check=True, home=self.home, cwd=self.cwd)

        self.assertEqual(result.personal, "개인 지식: PERSONAL_INDEX_EXTRA")
        self.assertEqual(extra.read_bytes(), b"user bytes\n")

    def test_check_distinguishes_invalid_source_and_does_not_mask_project_result(self):
        project = self._git_project()
        self._write_lesson("not valid lesson metadata\n")

        result = run_index(check=True, home=self.home, cwd=project)

        self.assertEqual(
            result.personal,
            "개인 지식: PERSONAL_INDEX_INVALID_SOURCE",
        )
        self.assertEqual(result.project, "프로젝트 근거: PROJECT_INDEX_MISSING")
        self.assertFalse((self.home / "knowledge" / "index").exists())
        self.assertFalse((project / "knowledge" / "index" / "INDEX.md").exists())

    def test_check_reports_both_current_surfaces(self):
        project = self._git_project()
        self._write_lesson()
        run_index(check=False, home=self.home, cwd=project)

        result = run_index(check=True, home=self.home, cwd=project)

        self.assertEqual(
            result,
            IndexResult(
                personal="개인 지식: PERSONAL_INDEX_CURRENT",
                project="프로젝트 근거: PROJECT_INDEX_CURRENT",
            ),
        )


if __name__ == "__main__":
    unittest.main()
