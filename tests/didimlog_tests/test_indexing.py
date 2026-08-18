import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import didimlog.indexing as indexing_module
from didimlog.indexing import IndexResult, run_index
from didimlog.personal import index as personal_index
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


def _legacy_readme(current):
    replacements = (
        (
            (
                "- ID 형식은 `PREFIX-YYYYMMDD-NN` (예: `OBS-20260714-01`). "
                "`didim add`는\n"
                "  `--date YYYY-MM-DD`와 기존 record를 기준으로 ID의 날짜와 두 자리 "
                "순번을 자동 할당한다."
            ),
            (
                "- ID 형식은 `PREFIX-YYYYMMDD-NN` (예: `OBS-20260714-01`). "
                "날짜와 2자리 순번을\n"
                "  사람이 직접 지정하며 자동 추측하지 않는다."
            ),
        ),
        (
            (
                "`didim add experiment`는 JSON stdin의 `contradicts` 필드로 모순 ID를 "
                "입력한다. 값은\n"
                '모순이 없으면 `"none"`, 있으면 `"<ID>, <ID>, ..."`인 문자열이다. '
                "이 필드는 필수이며\n"
                "기본값도 추론도 없다."
            ),
            (
                "`didim add experiment`는 `--contradicts`가 필수이며 기본값도 "
                "추론도 없다."
            ),
        ),
    )
    text = current.decode("utf-8")
    for current_paragraph, legacy_paragraph in replacements:
        assert text.count(current_paragraph) == 1
        text = text.replace(current_paragraph, legacy_paragraph)
    legacy = text.encode("utf-8")
    assert len(legacy) == 16_336
    assert (
        hashlib.sha256(legacy).hexdigest()
        == "6347d06afaab04f94c9f409717e0539add7252d000e5b0d51ea68d00036b0961"
    )
    return legacy


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
                personal_token="PERSONAL_INDEX_WRITTEN",
                project_token="PROJECT_INDEX_WRITTEN",
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
        self.assertEqual(result.personal_token, "PERSONAL_INDEX_WRITTEN")
        self.assertEqual(result.project_token, "PROJECT_NOT_CONFIGURED")
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

    def test_exact_legacy_readme_remains_prepared_for_index_check_and_write(self):
        project = self._git_project()
        readme = project / "knowledge" / "README.md"
        legacy = _legacy_readme(readme.read_bytes())
        readme.write_bytes(legacy)
        project_index = project / "knowledge" / "index" / "INDEX.md"

        check_result = run_index(check=True, home=self.home, cwd=project)

        self.assertEqual(check_result.project_token, "PROJECT_INDEX_MISSING")
        self.assertEqual(
            check_result.project,
            "프로젝트 근거: PROJECT_INDEX_MISSING",
        )
        self.assertFalse(project_index.exists())
        self.assertEqual(readme.read_bytes(), legacy)

        write_result = run_index(check=False, home=self.home, cwd=project)

        self.assertEqual(write_result.project_token, "PROJECT_INDEX_WRITTEN")
        self.assertEqual(
            write_result.project,
            "프로젝트 근거: PROJECT_INDEX_WRITTEN",
        )
        self.assertTrue(project_index.is_file())
        self.assertEqual(readme.read_bytes(), legacy)

    def test_index_accepts_current_readme_when_legacy_plan_turns_stale(self):
        import os
        import pathlib

        project = self._git_project()
        readme = project / "knowledge" / "README.md"
        readme.write_bytes(_legacy_readme(readme.read_bytes()))
        resolved_project = pathlib.Path(os.path.realpath(project))
        stale_plan = plan_scaffold(resolved_project)
        apply_scaffold(stale_plan)

        with mock.patch.object(
            indexing_module,
            "plan_scaffold",
            return_value=stale_plan,
        ):
            result = run_index(check=False, home=self.home, cwd=project)

        self.assertEqual(result.project_token, "PROJECT_INDEX_WRITTEN")
        self.assertTrue(
            (project / "knowledge/index/INDEX.md").is_file()
        )

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

    def test_check_reads_valid_retired_personal_index_history_without_mutation(self):
        self._write_lesson()
        run_index(check=False, home=self.home, cwd=self.cwd)
        index_directory = self.home / "knowledge" / "index"
        logical_name = "_global.md"
        generated_bytes = (index_directory / logical_name).read_bytes()
        logical_name_digest = hashlib.sha256(
            logical_name.encode("utf-8")
        ).hexdigest()[:16]
        retired_base = (
            f".index-retired-{logical_name_digest}-"
            "0123456789abcdef01234567"
        )
        retired_files = {
            index_directory / f"{retired_base}.tmp": generated_bytes,
            index_directory / f"{retired_base}.name": (
                logical_name + "\n"
            ).encode("utf-8"),
        }
        for path, data in retired_files.items():
            path.write_bytes(data)

        result = run_index(check=True, home=self.home, cwd=self.cwd)

        self.assertEqual(result.personal_token, "PERSONAL_INDEX_CURRENT")
        self.assertEqual(
            {path: path.read_bytes() for path in retired_files},
            retired_files,
        )

    def test_check_rejects_pathname_swap_after_namespace_validation(self):
        self._write_lesson()
        run_index(check=False, home=self.home, cwd=self.cwd)
        destination = self.home / "knowledge" / "index"
        saved = self.root / "index-saved"
        replacement = self.root / "index-replacement"
        replacement.mkdir()
        (replacement / "_global.md").write_bytes(
            (destination / "_global.md").read_bytes()
        )
        outside = self.root / "outside-entry"
        outside.write_bytes(b"user bytes\n")
        (replacement / "unknown-link").symlink_to(outside)
        original_validate = personal_index._validate_index_directory
        swapped = False

        def swap_after_validation(directory_descriptor, expected):
            nonlocal swapped
            public_names = original_validate(directory_descriptor, expected)
            destination.rename(saved)
            replacement.rename(destination)
            swapped = True
            return public_names

        try:
            with mock.patch.object(
                personal_index,
                "_validate_index_directory",
                side_effect=swap_after_validation,
            ):
                result = run_index(check=True, home=self.home, cwd=self.cwd)
        finally:
            if swapped:
                destination.rename(replacement)
                saved.rename(destination)

        self.assertEqual(result.personal_token, "PERSONAL_INDEX_EXTRA")
        self.assertEqual(
            (replacement / "_global.md").read_bytes(),
            (destination / "_global.md").read_bytes(),
        )
        self.assertTrue((replacement / "unknown-link").is_symlink())

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
                personal_token="PERSONAL_INDEX_CURRENT",
                project_token="PROJECT_INDEX_CURRENT",
            ),
        )


if __name__ == "__main__":
    unittest.main()
