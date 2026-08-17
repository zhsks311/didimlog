import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import multiprocessing
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog import file_io
from didimlog.personal import index as knowledge_index
from didimlog.personal import render as personal_render
from didimlog.personal.lesson_writing import publish_lesson


GENERATED_NOTICE = "<!-- Didimlog Personal Knowledge가 자동 생성한다. 직접 수정하지 마라. -->"
LESSON = """---
topic: partial-update
title: 부분수정에서 null은 해제다
summary: 긴 본문은 index에 들어가면 안 된다
tags: [nullable, partial-update]
date: 2026-08-05
---
## 교훈
상세 본문 표식
"""
DOC = """---
title: 상품고시 이관 절차
find_when: [EWG, migration]
---
상세 문서 본문 표식
"""
BOOK = """---
title: MongoDB 이관 설명
find_when: [mongodb, recovery]
---
장문 해설 본문 표식
"""



def _write_paused_stale_index(root, snapshot_ready, release_snapshot):
    data_root = Path(root)
    original_build = knowledge_index._build_all_with_projects

    def blocked_build(candidate_root=None):
        result = original_build(candidate_root)
        snapshot_ready.set()
        release_snapshot.wait(5)
        return result

    knowledge_index._build_all_with_projects = blocked_build
    knowledge_index.write_all(
        data_root=data_root,
        target=data_root / "index",
    )



class KnowledgeIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.root = self.temporary / "knowledge"
        for name in ("lessons", "docs", "book", "index"):
            (self.root / name).mkdir(parents=True)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def test_collect_returns_title_find_when_and_detail_path_for_every_section(self):
        lesson = LESSON.replace(
            "date: 2026-08-05\n",
            "date: 2026-08-05\nreview_by: 2026-12-31\n",
        )
        self.write("lessons/demo-api/null-clear.md", lesson)
        self.write("docs/demo-api/migrations/notice.md", DOC)
        self.write("book/demo-api/mongodb.md", BOOK)

        collected = knowledge_index.collect(self.root)

        self.assertEqual(
            collected,
            {
                "demo-api": [
                    {
                        "kind": "lesson",
                        "title": "부분수정에서 null은 해제다",
                        "find_when": ["nullable", "partial-update"],
                        "path": "lessons/demo-api/null-clear.md",
                        "review_by": "2026-12-31",
                    },
                    {
                        "kind": "docs",
                        "title": "상품고시 이관 절차",
                        "find_when": ["EWG", "migration"],
                        "path": "docs/demo-api/migrations/notice.md",
                        "review_by": None,
                    },
                    {
                        "kind": "book",
                        "title": "MongoDB 이관 설명",
                        "find_when": ["mongodb", "recovery"],
                        "path": "book/demo-api/mongodb.md",
                        "review_by": None,
                    },
                ]
            },
        )

    def test_render_project_emits_exact_didimlog_notice_and_three_sections(self):
        items = [
            {
                "kind": "book",
                "title": "MongoDB 이관 설명",
                "find_when": ["mongodb", "recovery"],
                "path": "book/demo-api/mongodb.md",
                "review_by": None,
            },
            {
                "kind": "lesson",
                "title": "부분수정에서 null은 해제다",
                "find_when": ["nullable", "partial-update"],
                "path": "lessons/demo-api/null-clear.md",
                "review_by": None,
            },
            {
                "kind": "docs",
                "title": "상품고시 이관 절차",
                "find_when": ["EWG", "migration"],
                "path": "docs/demo-api/migrations/notice.md",
                "review_by": None,
            },
        ]

        output = knowledge_index.render_project("demo-api", items)

        self.assertEqual(knowledge_index.GENERATED_NOTICE, GENERATED_NOTICE)
        self.assertEqual(
            output.encode("utf-8"),
            """<!-- Didimlog Personal Knowledge가 자동 생성한다. 직접 수정하지 마라. -->
# demo-api 지식 목록

## 작업 규칙

- **부분수정에서 null은 해제다**
  - 찾을 때: nullable, partial-update
  - 상세: `lessons/demo-api/null-clear.md`

## 작업 문서

- **상품고시 이관 절차**
  - 찾을 때: EWG, migration
  - 상세: `docs/demo-api/migrations/notice.md`

## 해설 자료

- **MongoDB 이관 설명**
  - 찾을 때: mongodb, recovery
  - 상세: `book/demo-api/mongodb.md`
""".encode("utf-8"),
        )

    def test_build_all_indexes_metadata_but_never_source_bodies_or_summary(self):
        self.write("lessons/demo-api/null-clear.md", LESSON)
        self.write("docs/demo-api/migrations/notice.md", DOC)
        self.write("book/demo-api/mongodb.md", BOOK)

        output = knowledge_index.build_all(self.root)["demo-api"]

        self.assertIn("# demo-api 지식 목록", output)
        self.assertIn("## 작업 규칙", output)
        self.assertIn("## 작업 문서", output)
        self.assertIn("## 해설 자료", output)
        self.assertIn("찾을 때: nullable, partial-update", output)
        self.assertIn("`docs/demo-api/migrations/notice.md`", output)
        self.assertNotIn("상세 본문 표식", output)
        self.assertNotIn("상세 문서 본문 표식", output)
        self.assertNotIn("장문 해설 본문 표식", output)
        self.assertNotIn("긴 본문은 index에 들어가면 안 된다", output)

    def test_global_and_project_sources_are_kept_in_separate_outputs(self):
        self.write(
            "lessons/_global/global.md",
            LESSON.replace("부분수정에서 null은 해제다", "전역 규칙"),
        )
        self.write("lessons/demo-api/project.md", LESSON)
        self.write("docs/_global/global.md", DOC.replace("상품고시 이관 절차", "전역 문서"))
        self.write("book/demo-api/project.md", BOOK)

        outputs = knowledge_index.build_all(self.root)

        self.assertEqual(set(outputs), {"_global", "demo-api"})
        self.assertIn("전역 규칙", outputs["_global"])
        self.assertIn("전역 문서", outputs["_global"])
        self.assertNotIn("부분수정에서 null은 해제다", outputs["_global"])
        self.assertNotIn("MongoDB 이관 설명", outputs["_global"])
        self.assertIn("부분수정에서 null은 해제다", outputs["demo-api"])
        self.assertIn("MongoDB 이관 설명", outputs["demo-api"])
        self.assertNotIn("전역 규칙", outputs["demo-api"])
        self.assertNotIn("전역 문서", outputs["demo-api"])

    def test_unrelated_entries_do_not_change_generated_indexes(self):
        self.write("lessons/demo-api/rule.md", LESSON)
        expected = knowledge_index.build_all(self.root)

        for relative in (
            "lessons/.DS_Store",
            "docs/.DS_Store",
            "book/.DS_Store",
            "lessons/demo-api/notes.txt",
            "docs/demo-api/image.png",
            "book/demo-api/draft.txt",
            "lessons/not_a_project/ignored.md",
        ):
            self.write(relative, "ignored")
        self.write("lessons/demo-api/guides/ignored.md", "not lesson metadata")
        self.write("book/demo-api/drafts/ignored.md", "not book metadata")

        self.assertEqual(knowledge_index.build_all(self.root), expected)

    def test_invalid_utf8_unrelated_entries_do_not_change_generated_indexes(self):
        project = self.write("lessons/demo-api/rule.md", LESSON).parent
        expected = knowledge_index.build_all(self.root)
        original_listdir = os.listdir
        project_identity = (project.stat().st_dev, project.stat().st_ino)

        def injected_listdir(directory):
            names = list(original_listdir(directory))
            if isinstance(directory, int):
                info = os.fstat(directory)
                if (info.st_dev, info.st_ino) == project_identity:
                    names.extend(["notes-\udcff.txt", "drafts-\udcff"])
            return names

        with mock.patch.object(os, "listdir", side_effect=injected_listdir):
            actual = knowledge_index.build_all(self.root)

        self.assertEqual(actual, expected)

    def test_invalid_utf8_selected_markdown_has_structured_logical_error(self):
        project = self.write(
            "docs/demo-api/placeholder.txt",
            "ignored",
        ).parent
        original_listdir = os.listdir
        project_identity = (project.stat().st_dev, project.stat().st_ino)

        def injected_listdir(directory):
            names = list(original_listdir(directory))
            if isinstance(directory, int):
                info = os.fstat(directory)
                if (info.st_dev, info.st_ino) == project_identity:
                    names.append("bad-\udcff.md")
            return names

        with mock.patch.object(
            os,
            "listdir",
            side_effect=injected_listdir,
        ), self.assertRaises(
            knowledge_index.KnowledgeSourceError
        ) as caught:
            knowledge_index.build_all(self.root)

        self.assertEqual(caught.exception.logical_path, "docs/demo-api")
        self.assertEqual(
            caught.exception.reason,
            "source name must be valid UTF-8",
        )
        self.assertNotIn(str(self.temporary), str(caught.exception))

    def test_invalid_utf8_recursive_directory_without_markdown_is_ignored(self):
        project = self.write(
            "docs/demo-api/placeholder.txt",
            "ignored",
        ).parent
        expected = knowledge_index.build_all(self.root)
        original_listdir = os.listdir
        project_identity = (project.stat().st_dev, project.stat().st_ino)

        def injected_listdir(directory):
            names = list(original_listdir(directory))
            if isinstance(directory, int):
                info = os.fstat(directory)
                if (info.st_dev, info.st_ino) == project_identity:
                    names.append("assets-\udcff")
            return names

        with mock.patch.object(os, "listdir", side_effect=injected_listdir):
            actual = knowledge_index.build_all(self.root)

        self.assertEqual(actual, expected)


    def test_noncanonical_lesson_tags_are_rejected(self):
        invalid = LESSON.replace(
            "tags: [nullable, partial-update]",
            "tags: [partial-update, nullable, partial-update]",
        )
        self.write("lessons/demo-api/invalid.md", invalid)

        with self.assertRaises(knowledge_index.KnowledgeIndexError) as caught:
            knowledge_index.build_all(self.root)

        self.assertIn("invalid lesson metadata", str(caught.exception))

    def test_titles_paths_and_projects_use_deterministic_utf8_byte_order_and_lf(self):
        self.write(
            "docs/z-project/z.md",
            DOC.replace("상품고시 이관 절차", "가 제목"),
        )
        self.write(
            "docs/z-project/a.md",
            DOC.replace("상품고시 이관 절차", "alpha title"),
        )
        self.write("docs/a-project/one.md", DOC)

        outputs = knowledge_index.build_all(self.root)

        self.assertEqual(list(outputs), ["a-project", "z-project"])
        z_output = outputs["z-project"]
        self.assertLess(z_output.index("alpha title"), z_output.index("가 제목"))
        self.assertNotIn("\r", z_output)
        self.assertTrue(z_output.endswith("\n"))
        self.assertEqual(z_output.encode("utf-8").decode("utf-8"), z_output)

    def test_markdown_metacharacters_are_escaped_without_changing_detail_path(self):
        document = """---
title: Use *literal* [name] <now> \\ path
find_when: [a*b, x_y, z`z]
---
body
"""
        self.write("docs/demo-api/a`b.md", document)

        output = knowledge_index.build_all(self.root)["demo-api"]

        self.assertIn("- **Use \\*literal\\* \\[name\\] \\<now\\> \\\\ path**", output)
        self.assertIn("찾을 때: a\\*b, x\\_y, z\\`z", output)
        self.assertIn("상세: ``docs/demo-api/a`b.md``", output)

    def test_equal_titles_are_ordered_by_utf8_detail_path(self):
        self.write("docs/demo-api/zeta.md", DOC)
        self.write("docs/demo-api/alpha.md", DOC)

        output = knowledge_index.build_all(self.root)["demo-api"]

        self.assertLess(
            output.index("`docs/demo-api/alpha.md`"),
            output.index("`docs/demo-api/zeta.md`"),
        )

    def test_index_metadata_length_boundaries_are_enforced(self):
        valid = """---
title: {title}
find_when: [{term}]
---
body
""".format(title="가" * 120, term="a" * 32)
        self.write("docs/demo-api/valid.md", valid)
        self.assertIn("demo-api", knowledge_index.build_all(self.root))

        invalid_documents = (
            valid.replace("가" * 120, "가" * 121),
            valid.replace("a" * 32, "a" * 33),
        )
        for text in invalid_documents:
            with self.subTest(text=text):
                shutil.rmtree(self.root / "docs")
                (self.root / "docs" / "demo-api").mkdir(parents=True)
                self.write("docs/demo-api/invalid.md", text)
                with self.assertRaises(knowledge_index.KnowledgeIndexError):
                    knowledge_index.build_all(self.root)

    def test_malformed_metadata_in_any_section_fails_the_entire_build(self):
        cases = (
            ("lessons/demo-api/bad.md", "# no metadata\n"),
            ("docs/demo-api/bad.md", "# no metadata\n"),
            ("docs/demo-api/bad.md", "---\ntitle: X\nfind_when: []\n---\n"),
            ("book/demo-api/bad.md", "# no metadata\n"),
            ("book/demo-api/bad.md", "---\ntitle: X\nfind_when: []\n---\n"),
        )
        for relative, text in cases:
            with self.subTest(relative=relative, text=text):
                for name in ("lessons", "docs", "book"):
                    shutil.rmtree(self.root / name)
                    (self.root / name).mkdir()
                self.write(relative, text)
                with self.assertRaises(knowledge_index.KnowledgeIndexError):
                    knowledge_index.build_all(self.root)

    def test_duplicate_noncanonical_and_crlf_metadata_preserve_existing_index(self):
        target = self.root / "index" / "demo-api.md"
        invalid_documents = (
            DOC.replace("find_when:", "title: 중복\nfind_when:"),
            DOC.replace("[EWG, migration]", "[migration, EWG]"),
            DOC.replace("\n", "\r\n"),
        )

        for text in invalid_documents:
            with self.subTest(text=text):
                target.write_bytes(b"user bytes\r\n")
                shutil.rmtree(self.root / "docs")
                (self.root / "docs" / "demo-api").mkdir(parents=True)
                self.write("docs/demo-api/bad.md", text)
                with self.assertRaises(knowledge_index.KnowledgeIndexError):
                    knowledge_index.write_all(
                        data_root=self.root,
                        target=self.root / "index",
                    )
                self.assertEqual(target.read_bytes(), b"user bytes\r\n")

    def test_lesson_keeps_its_64_kib_source_limit(self):
        frontmatter = LESSON.split("## 교훈", 1)[0]
        oversized = frontmatter + ("x" * (64 * 1024))
        self.write("lessons/demo-api/oversized.md", oversized)

        with self.assertRaises(
            knowledge_index.KnowledgeSourceError
        ) as caught:
            knowledge_index.build_all(self.root)

        self.assertEqual(
            caught.exception.logical_path,
            "lessons/demo-api/oversized.md",
        )
        self.assertEqual(
            caught.exception.reason,
            "invalid lesson metadata",
        )

    def test_crlf_lesson_metadata_is_rejected(self):
        self.write("lessons/demo-api/crlf.md", LESSON.replace("\n", "\r\n"))

        with self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.build_all(self.root)

    def test_nested_source_symlink_is_rejected_before_any_index_write(self):
        outside = self.temporary / "outside"
        outside.mkdir()
        (outside / "notice.md").write_text(DOC, encoding="utf-8")
        project = self.root / "docs" / "demo-api"
        project.mkdir(parents=True)
        (project / "linked").symlink_to(outside, target_is_directory=True)
        target = self.root / "index" / "demo-api.md"
        target.write_text("keep\n", encoding="utf-8")

        with self.assertRaises(knowledge_index.KnowledgeSourceError) as caught:
            knowledge_index.write_all(data_root=self.root, target=self.root / "index")

        self.assertEqual(caught.exception.logical_path, "docs/demo-api/linked")
        self.assertEqual(
            caught.exception.reason,
            "source directory must be a real directory",
        )
        self.assertNotIn(str(outside), str(caught.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_target_markdown_symlink_is_rejected_but_other_file_symlinks_are_ignored(
        self,
    ):
        outside = self.temporary / "outside"
        outside.mkdir()
        target_markdown = outside / "notice.md"
        target_markdown.write_text(DOC, encoding="utf-8")
        target_text = outside / "readme.txt"
        target_text.write_text("ignored", encoding="utf-8")
        project = self.root / "docs" / "demo-api"
        project.mkdir(parents=True)
        linked_markdown = project / "linked.md"
        linked_markdown.symlink_to(target_markdown)

        with self.assertRaises(knowledge_index.KnowledgeSourceError) as caught:
            knowledge_index.build_all(self.root)

        self.assertEqual(caught.exception.logical_path, "docs/demo-api/linked.md")
        self.assertEqual(
            caught.exception.reason,
            "source must be a regular file",
        )
        self.assertNotIn(str(outside), str(caught.exception))

        linked_markdown.unlink()
        (project / "readme.txt").symlink_to(target_text)
        self.write("docs/demo-api/guide.md", DOC)

        output = knowledge_index.build_all(self.root)["demo-api"]

        self.assertIn("`docs/demo-api/guide.md`", output)
        self.assertNotIn("readme.txt", output)

    def test_malformed_markdown_has_stable_logical_error_details(self):
        self.write("docs/demo-api/bad.md", "# no metadata\n")

        with self.assertRaises(knowledge_index.KnowledgeSourceError) as caught:
            knowledge_index.build_all(self.root)

        self.assertEqual(caught.exception.logical_path, "docs/demo-api/bad.md")
        self.assertEqual(caught.exception.reason, "missing title or find_when")
        self.assertNotIn(str(self.temporary), str(caught.exception))

    def test_linked_projects_are_indexed_with_logical_paths(self):
        external = self.temporary / "external"
        lesson_root = external / "lessons"
        docs_root = external / "docs"
        book_root = external / "book"
        lesson_root.mkdir(parents=True)
        docs_root.mkdir()
        book_root.mkdir()
        (lesson_root / "rule.md").write_text(LESSON, encoding="utf-8")
        (docs_root / "nested").mkdir()
        (docs_root / "nested" / "guide.md").write_text(DOC, encoding="utf-8")
        (book_root / "guide.md").write_text(BOOK, encoding="utf-8")
        (self.root / "lessons" / "demo-api").symlink_to(
            lesson_root,
            target_is_directory=True,
        )
        (self.root / "docs" / "demo-api").symlink_to(
            docs_root,
            target_is_directory=True,
        )
        (self.root / "book" / "demo-api").symlink_to(
            book_root,
            target_is_directory=True,
        )

        output = knowledge_index.build_all(self.root)["demo-api"]

        self.assertIn("`lessons/demo-api/rule.md`", output)
        self.assertIn("`docs/demo-api/nested/guide.md`", output)
        self.assertIn("`book/demo-api/guide.md`", output)
        self.assertNotIn(str(external), output)

    def test_linked_scan_pins_target_inode_across_pathname_aba_swap(self):
        external = self.temporary / "external"
        active = external / "active"
        saved = external / "saved"
        replacement = external / "replacement"
        active.mkdir(parents=True)
        replacement.mkdir()
        original_text = (
            "---\n"
            "title: 원래 문서\n"
            "find_when: [원래 자료]\n"
            "---\n"
        )
        replacement_text = (
            "---\n"
            "title: 바뀐 문서\n"
            "find_when: [바뀐 자료]\n"
            "---\n"
        )
        (active / "guide.md").write_text(original_text, encoding="utf-8")
        (replacement / "guide.md").write_text(
            replacement_text,
            encoding="utf-8",
        )
        (self.root / "docs" / "demo-api").symlink_to(
            active,
            target_is_directory=True,
        )
        original_markdown_files = knowledge_index._markdown_files
        original_document_item = knowledge_index._document_item
        swapped = False

        def swap_target_before_scan(*args, **kwargs):
            nonlocal swapped
            active.rename(saved)
            replacement.rename(active)
            swapped = True
            return original_markdown_files(*args, **kwargs)

        def restore_target_after_read(*args, **kwargs):
            nonlocal swapped
            item = original_document_item(*args, **kwargs)
            active.rename(replacement)
            saved.rename(active)
            swapped = False
            return item

        try:
            with mock.patch.object(
                knowledge_index,
                "_markdown_files",
                side_effect=swap_target_before_scan,
            ), mock.patch.object(
                knowledge_index,
                "_document_item",
                side_effect=restore_target_after_read,
            ):
                output = knowledge_index.build_all(self.root)["demo-api"]
        finally:
            if swapped:
                active.rename(replacement)
                saved.rename(active)

        self.assertIn("원래 문서", output)
        self.assertNotIn("바뀐 문서", output)

    def test_collect_rechecks_all_links_after_later_project_scan(self):
        external = self.temporary / "external"
        first = external / "first"
        first_replacement = external / "first-replacement"
        last = external / "last"
        for directory in (first, first_replacement, last):
            directory.mkdir(parents=True)
        (first / "rule.md").write_text(LESSON, encoding="utf-8")
        (last / "rule.md").write_text(LESSON, encoding="utf-8")
        first_link = self.root / "lessons" / "a-project"
        last_link = self.root / "lessons" / "z-project"
        first_link.symlink_to(first, target_is_directory=True)
        last_link.symlink_to(last, target_is_directory=True)
        original_markdown_files = knowledge_index._markdown_files

        def retarget_first_during_last_scan(
            project,
            *,
            recursive,
            root_descriptor,
        ):
            files = original_markdown_files(
                project,
                recursive=recursive,
                root_descriptor=root_descriptor,
            )
            if project.logical.name == "z-project":
                first_link.unlink()
                first_link.symlink_to(
                    first_replacement,
                    target_is_directory=True,
                )
            return files

        with mock.patch.object(
            knowledge_index,
            "_markdown_files",
            side_effect=retarget_first_during_last_scan,
        ), self.assertRaises(knowledge_index.KnowledgeSourceError) as caught:
            knowledge_index.collect(self.root)

        self.assertEqual(caught.exception.logical_path, "lessons/a-project")
        self.assertEqual(
            caught.exception.reason,
            "project link changed during scan",
        )
        self.assertNotIn(str(external), str(caught.exception))

    def test_build_all_rechecks_all_links_after_rendering(self):
        external = self.temporary / "external"
        first = external / "first"
        first_replacement = external / "first-replacement"
        last = external / "last"
        for directory in (first, first_replacement, last):
            directory.mkdir(parents=True)
        (first / "rule.md").write_text(LESSON, encoding="utf-8")
        (last / "rule.md").write_text(LESSON, encoding="utf-8")
        first_link = self.root / "lessons" / "a-project"
        first_link.symlink_to(first, target_is_directory=True)
        (self.root / "lessons" / "z-project").symlink_to(
            last,
            target_is_directory=True,
        )
        original_render = knowledge_index.render_project

        def retarget_first_after_last_render(project, items):
            output = original_render(project, items)
            if project == "z-project":
                first_link.unlink()
                first_link.symlink_to(
                    first_replacement,
                    target_is_directory=True,
                )
            return output

        with mock.patch.object(
            knowledge_index,
            "render_project",
            side_effect=retarget_first_after_last_render,
        ), self.assertRaises(knowledge_index.KnowledgeSourceError) as caught:
            knowledge_index.build_all(self.root)

        self.assertEqual(caught.exception.logical_path, "lessons/a-project")
        self.assertEqual(
            caught.exception.reason,
            "project link changed during scan",
        )
        self.assertNotIn(str(external), str(caught.exception))

    def test_project_link_change_before_publish_preserves_existing_index(self):
        external = self.temporary / "external"
        original = external / "original"
        replacement = external / "replacement"
        original.mkdir(parents=True)
        replacement.mkdir()
        (original / "rule.md").write_text(LESSON, encoding="utf-8")
        logical = self.root / "lessons" / "demo-api"
        logical.symlink_to(original, target_is_directory=True)
        target = self.root / "index" / "demo-api.md"
        target.write_bytes(b"user bytes\r\n")
        original_fsync = knowledge_index.os.fsync
        retargeted = False

        def retarget_after_prepared_file_sync(descriptor):
            nonlocal retargeted
            original_fsync(descriptor)
            if not retargeted:
                retargeted = True
                logical.unlink()
                logical.symlink_to(replacement, target_is_directory=True)

        with mock.patch.object(
            knowledge_index.os,
            "fsync",
            side_effect=retarget_after_prepared_file_sync,
        ), self.assertRaises(knowledge_index.KnowledgeSourceError) as caught:
            knowledge_index.write_all(data_root=self.root, target=self.root / "index")

        self.assertEqual(caught.exception.logical_path, "lessons/demo-api")
        self.assertEqual(
            caught.exception.reason,
            "project link changed during scan",
        )
        self.assertNotIn(str(external), str(caught.exception))
        self.assertEqual(target.read_bytes(), b"user bytes\r\n")

    def test_source_retarget_after_publish_precheck_rolls_back_index(self):
        external = self.temporary / "external"
        original = external / "original"
        replacement = external / "replacement"
        original.mkdir(parents=True)
        replacement.mkdir()
        (original / "rule.md").write_text(LESSON, encoding="utf-8")
        logical = self.root / "lessons" / "demo-api"
        logical.symlink_to(original, target_is_directory=True)
        target = self.root / "index" / "demo-api.md"
        target.write_bytes(b"previous bytes\r\n")
        target.chmod(0o640)
        original_replace = knowledge_index.os.replace
        retargeted = False

        def retarget_before_first_replace(source, destination):
            nonlocal retargeted
            if not retargeted:
                retargeted = True
                logical.unlink()
                logical.symlink_to(replacement, target_is_directory=True)
            return original_replace(source, destination)

        with mock.patch.object(
            knowledge_index.os,
            "replace",
            side_effect=retarget_before_first_replace,
        ), self.assertRaises(knowledge_index.KnowledgeSourceError):
            knowledge_index.write_all(data_root=self.root, target=self.root / "index")

        self.assertEqual(target.read_bytes(), b"previous bytes\r\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(
            {entry.name for entry in (self.root / "index").iterdir()},
            {"demo-api.md"},
        )

    def test_source_retarget_during_multi_index_publish_rolls_back_namespace(self):
        external = self.temporary / "external"
        first = external / "first"
        first_replacement = external / "first-replacement"
        last = external / "last"
        for directory in (first, first_replacement, last):
            directory.mkdir(parents=True)
        (first / "rule.md").write_text(LESSON, encoding="utf-8")
        (last / "rule.md").write_text(LESSON, encoding="utf-8")
        first_link = self.root / "lessons" / "a-project"
        first_link.symlink_to(first, target_is_directory=True)
        (self.root / "lessons" / "z-project").symlink_to(
            last,
            target_is_directory=True,
        )
        target = self.root / "index"
        first_index = target / "a-project.md"
        first_index.write_bytes(b"first previous\r\n")
        first_index.chmod(0o640)
        stale = target / "obsolete.md"
        stale_bytes = (GENERATED_NOTICE + "\n# obsolete\n").encode("utf-8")
        stale.write_bytes(stale_bytes)
        stale.chmod(0o604)
        original_replace = knowledge_index.os.replace
        replacements = 0

        def retarget_after_first_replace(source, destination):
            nonlocal replacements
            result = original_replace(source, destination)
            replacements += 1
            if replacements == 1:
                first_link.unlink()
                first_link.symlink_to(
                    first_replacement,
                    target_is_directory=True,
                )
            return result

        with mock.patch.object(
            knowledge_index.os,
            "replace",
            side_effect=retarget_after_first_replace,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_ROLLBACK_FAILED",
        ) as caught:
            knowledge_index.write_all(data_root=self.root, target=target)

        self.assertNotIn(str(external), str(caught.exception))
        self.assertEqual(first_index.read_bytes(), b"first previous\r\n")
        self.assertEqual(stat.S_IMODE(first_index.stat().st_mode), 0o640)
        self.assertFalse((target / "z-project.md").exists())
        self.assertEqual(stale.read_bytes(), stale_bytes)
        self.assertEqual(stat.S_IMODE(stale.stat().st_mode), 0o604)
        recovery = [
            entry
            for entry in target.iterdir()
            if (
                entry.name.startswith(".index-quarantine-")
                and entry.suffix == ".tmp"
            )
        ]
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0].read_bytes(), b"")
        metadata = recovery[0].with_suffix(".name")
        self.assertEqual(metadata.read_text(encoding="utf-8"), "z-project.md\n")
        retired = [
            entry
            for entry in target.iterdir()
            if (
                entry.name.startswith(".index-retired-")
                and entry.suffix == ".tmp"
            )
        ]
        self.assertEqual(len(retired), 1)
        retired_metadata = retired[0].with_suffix(".name")
        self.assertEqual(retired[0].read_bytes(), stale_bytes)
        self.assertEqual(
            retired_metadata.read_text(encoding="utf-8"),
            "obsolete.md\n",
        )
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {
                "a-project.md",
                "obsolete.md",
                recovery[0].name,
                metadata.name,
                retired[0].name,
                retired_metadata.name,
            },
        )

    def test_rollback_keeps_open_fd_mutation_of_retired_index(self):
        target = self.root / "index"
        stale = target / "obsolete.md"
        stale_bytes = (GENERATED_NOTICE + "\n# obsolete\n").encode("utf-8")
        stale.write_bytes(stale_bytes)
        original_restore = knowledge_index._restore_missing_index

        def mutate_retired_then_restore(*args, **kwargs):
            retired = next(
                entry
                for entry in target.iterdir()
                if entry.name.startswith(".index-retired-")
                and entry.suffix == ".tmp"
            )
            descriptor = os.open(retired, os.O_WRONLY)
            try:
                os.write(descriptor, b"user open-fd bytes\n")
                os.ftruncate(descriptor, len(b"user open-fd bytes\n"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return original_restore(*args, **kwargs)

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError("forced postcheck failure"),
            ],
        ), mock.patch.object(
            knowledge_index,
            "_restore_missing_index",
            side_effect=mutate_retired_then_restore,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={},
                data_root=self.root,
                target=target,
            )

        self.assertEqual(stale.read_bytes(), stale_bytes)
        retired = [
            entry
            for entry in target.iterdir()
            if entry.name.startswith(".index-retired-")
            and entry.suffix == ".tmp"
        ]
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0].read_bytes(), b"user open-fd bytes\n")
        self.assertTrue(retired[0].with_suffix(".name").is_file())
        knowledge_index.write_all(
            outputs={"obsolete": stale_bytes.decode("utf-8")},
            data_root=self.root,
            target=target,
        )
        self.assertEqual(retired[0].read_bytes(), b"user open-fd bytes\n")
        self.assertTrue(retired[0].with_suffix(".name").is_file())


    def test_rollback_keeps_path_replacement_of_retired_index(self):
        target = self.root / "index"
        stale = target / "obsolete.md"
        stale_bytes = (GENERATED_NOTICE + "\n# obsolete\n").encode("utf-8")
        stale.write_bytes(stale_bytes)
        replacement = self.temporary / "retired-replacement"
        replacement.write_bytes(b"user replacement bytes\n")
        original_restore = knowledge_index._restore_missing_index

        def replace_retired_then_restore(*args, **kwargs):
            retired = next(
                entry
                for entry in target.iterdir()
                if entry.name.startswith(".index-retired-")
                and entry.suffix == ".tmp"
            )
            os.replace(replacement, retired)
            return original_restore(*args, **kwargs)

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError("forced postcheck failure"),
            ],
        ), mock.patch.object(
            knowledge_index,
            "_restore_missing_index",
            side_effect=replace_retired_then_restore,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={},
                data_root=self.root,
                target=target,
            )

        self.assertEqual(stale.read_bytes(), stale_bytes)
        retired = [
            entry
            for entry in target.iterdir()
            if entry.name.startswith(".index-retired-")
            and entry.suffix == ".tmp"
        ]
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0].read_bytes(), b"user replacement bytes\n")
        self.assertTrue(retired[0].with_suffix(".name").is_file())


    def test_source_rollback_does_not_overwrite_concurrent_index_change(self):
        external = self.temporary / "external"
        original = external / "original"
        replacement = external / "replacement"
        original.mkdir(parents=True)
        replacement.mkdir()
        (original / "rule.md").write_text(LESSON, encoding="utf-8")
        logical = self.root / "lessons" / "demo-api"
        logical.symlink_to(original, target_is_directory=True)
        target = self.root / "index" / "demo-api.md"
        target.write_bytes(b"previous bytes\r\n")
        original_replace = knowledge_index.os.replace
        changed = False

        def change_index_after_publish(source, destination):
            nonlocal changed
            result = original_replace(source, destination)
            if not changed:
                changed = True
                Path(destination).write_bytes(b"concurrent user bytes\n")
                logical.unlink()
                logical.symlink_to(replacement, target_is_directory=True)
            return result

        with mock.patch.object(
            knowledge_index.os,
            "replace",
            side_effect=change_index_after_publish,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "published entry restore failed",
        ) as caught:
            knowledge_index.write_all(
                data_root=self.root,
                target=self.root / "index",
            )

        self.assertNotIn(str(external), str(caught.exception))
        self.assertEqual(target.read_bytes(), b"concurrent user bytes\n")
        recovery = [
            entry
            for entry in target.parent.iterdir()
            if (
                entry.name.startswith(".index-recovery-")
                and entry.suffix == ".json"
            )
        ]
        self.assertEqual(len(recovery), 1)
        record = json.loads(recovery[0].read_text(encoding="utf-8"))
        self.assertEqual(record["logical_name"], "demo-api.md")
        self.assertEqual(
            base64.b64decode(record["data_base64"]),
            b"previous bytes\r\n",
        )

    def test_absent_index_rollback_leaves_recovery_artifact_without_unlinking(self):
        external = self.temporary / "external"
        original = external / "original"
        replacement = external / "replacement"
        original.mkdir(parents=True)
        replacement.mkdir()
        (original / "rule.md").write_text(LESSON, encoding="utf-8")
        logical = self.root / "lessons" / "demo-api"
        logical.symlink_to(original, target_is_directory=True)
        target = self.root / "index" / "demo-api.md"
        original_replace = knowledge_index.os.replace
        retargeted = False

        def retarget_after_publish(source, destination):
            nonlocal retargeted
            result = original_replace(source, destination)
            if not retargeted:
                retargeted = True
                logical.unlink()
                logical.symlink_to(replacement, target_is_directory=True)
            return result

        with mock.patch.object(
            knowledge_index.os,
            "replace",
            side_effect=retarget_after_publish,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_ROLLBACK_FAILED",
        ) as caught:
            knowledge_index.write_all(data_root=self.root, target=self.root / "index")

        self.assertNotIn(str(external), str(caught.exception))
        self.assertFalse(target.exists())
        recovery = [
            entry
            for entry in target.parent.iterdir()
            if (
                entry.name.startswith(".index-quarantine-")
                and entry.suffix == ".tmp"
            )
        ]
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0].read_bytes(), b"")
        self.assertEqual(
            recovery[0].with_suffix(".name").read_text(encoding="utf-8"),
            "demo-api.md\n",
        )

    def test_quarantine_metadata_failure_returns_path_safe_aggregate(self):
        target = self.root / "index"
        output = GENERATED_NOTICE + "\n# replacement\n"

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced postcheck failure"
                ),
            ],
        ), mock.patch.object(
            knowledge_index,
            "_persist_quarantine_metadata",
            side_effect=knowledge_index.KnowledgeIndexError(
                "synthetic metadata failure"
            ),
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "published entry cleanup failed",
        ) as caught:
            knowledge_index.write_all(
                outputs={"demo-api": output},
                data_root=self.root,
                target=target,
            )

        self.assertNotIn(str(self.temporary), str(caught.exception))
        self.assertFalse((target / "demo-api.md").exists())
        quarantine = [
            entry
            for entry in target.iterdir()
            if (
                entry.name.startswith(".index-quarantine-")
                and entry.suffix == ".tmp"
            )
        ]
        self.assertEqual(len(quarantine), 1)
        self.assertEqual(quarantine[0].read_bytes(), b"")
        self.assertFalse(quarantine[0].with_suffix(".name").exists())

    def test_rollback_cleanup_failure_does_not_skip_later_existing_index(self):
        target = self.root / "index"
        later = target / "z-project.md"
        later.write_bytes(b"later previous bytes\r\n")
        outputs = {
            "a-project": GENERATED_NOTICE + "\n# a\n",
            "z-project": GENERATED_NOTICE + "\n# z\n",
        }

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced publish invalidation"
                ),
            ],
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_ROLLBACK_FAILED",
        ) as caught:
            knowledge_index.write_all(
                outputs=outputs,
                data_root=self.root,
                target=target,
            )

        self.assertNotIn(str(self.temporary), str(caught.exception))
        self.assertFalse((target / "a-project.md").exists())
        self.assertEqual(later.read_bytes(), b"later previous bytes\r\n")
        recovery = [
            entry
            for entry in target.iterdir()
            if (
                entry.name.startswith(".index-quarantine-")
                and entry.suffix == ".tmp"
            )
        ]
        self.assertEqual(len(recovery), 1)
        self.assertEqual(recovery[0].read_bytes(), b"")

    def test_rollback_cleanup_failure_does_not_skip_later_absent_index(self):
        target = self.root / "index"
        outputs = {
            "a-project": GENERATED_NOTICE + "\n# a\n",
            "z-project": GENERATED_NOTICE + "\n# z\n",
        }

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced publish invalidation"
                ),
            ],
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_ROLLBACK_FAILED",
        ) as caught:
            knowledge_index.write_all(
                outputs=outputs,
                data_root=self.root,
                target=target,
            )

        self.assertNotIn(str(self.temporary), str(caught.exception))
        self.assertFalse((target / "a-project.md").exists())
        self.assertFalse((target / "z-project.md").exists())
        recovery = [
            entry
            for entry in target.iterdir()
            if (
                entry.name.startswith(".index-quarantine-")
                and entry.suffix == ".tmp"
            )
        ]
        self.assertEqual(len(recovery), 2)
        self.assertEqual(
            {
                entry.with_suffix(".name")
                .read_text(encoding="utf-8")
                .removesuffix("\n")
                for entry in recovery
            },
            {"a-project.md", "z-project.md"},
        )
        self.assertEqual(
            [entry.read_bytes() for entry in recovery],
            [b"", b""],
        )

    def test_missing_stale_entry_is_not_restored_after_postcheck_failure(self):
        target = self.root / "index"
        current = target / "demo-api.md"
        current.write_bytes(b"previous index bytes\r\n")
        stale = target / "obsolete.md"
        stale.write_text(
            GENERATED_NOTICE + "\n# obsolete\n",
            encoding="utf-8",
        )
        outputs = {
            "demo-api": GENERATED_NOTICE + "\n# replacement\n",
        }
        original_rename = personal_render._rename_entry_no_replace
        deleted = False

        def delete_stale_before_quarantine(
            directory_descriptor,
            source_name,
            destination_name,
        ):
            nonlocal deleted
            if not deleted and source_name == "obsolete.md":
                deleted = True
                stale.unlink()
            return original_rename(
                directory_descriptor,
                source_name,
                destination_name,
            )

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced postcheck failure"
                ),
            ],
        ), mock.patch.object(
            personal_render,
            "_rename_entry_no_replace",
            side_effect=delete_stale_before_quarantine,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "forced postcheck failure",
        ):
            knowledge_index.write_all(
                outputs=outputs,
                data_root=self.root,
                target=target,
            )

        self.assertTrue(deleted)
        self.assertFalse(stale.exists())
        self.assertEqual(current.read_bytes(), b"previous index bytes\r\n")

    def test_failed_existing_restore_retains_durable_backup_artifact(self):
        target = self.root / "index"
        current = target / "demo-api.md"
        previous = b"previous index bytes\r\n"
        current.write_bytes(previous)
        current.chmod(0o640)
        outputs = {
            "demo-api": GENERATED_NOTICE + "\n# replacement\n",
        }
        original_replace = (
            knowledge_index.replace_regular_file_at_if_unchanged_with_info
        )
        replacements = 0

        def fail_rollback_restore(*args, **kwargs):
            nonlocal replacements
            replacements += 1
            if replacements == 1:
                raise OSError("synthetic rollback restore failure")
            return original_replace(*args, **kwargs)

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced postcheck failure"
                ),
            ],
        ), mock.patch.object(
            knowledge_index,
            "replace_regular_file_at_if_unchanged_with_info",
            side_effect=fail_rollback_restore,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "published entry restore failed",
        ) as caught:
            knowledge_index.write_all(
                outputs=outputs,
                data_root=self.root,
                target=target,
            )

        self.assertNotIn(str(self.temporary), str(caught.exception))
        recovery = [
            entry
            for entry in target.iterdir()
            if (
                entry.name.startswith(".index-recovery-")
                and entry.suffix == ".json"
            )
        ]
        self.assertEqual(len(recovery), 1)
        record = json.loads(recovery[0].read_text(encoding="utf-8"))
        self.assertEqual(record["version"], 1)
        self.assertEqual(record["logical_name"], "demo-api.md")
        self.assertEqual(record["mode"], 0o640)
        self.assertEqual(
            base64.b64decode(record["data_base64"]),
            previous,
        )
        self.assertEqual(stat.S_IMODE(recovery[0].stat().st_mode), 0o600)

    def test_two_failed_restores_have_unambiguous_recovery_names(self):
        target = self.root / "index"
        previous = b"identical previous bytes\r\n"
        for name in ("a-project.md", "z-project.md"):
            entry = target / name
            entry.write_bytes(previous)
            entry.chmod(0o640)
        outputs = {
            "a-project": GENERATED_NOTICE + "\n# replacement a\n",
            "z-project": GENERATED_NOTICE + "\n# replacement z\n",
        }

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced postcheck failure"
                ),
            ],
        ), mock.patch.object(
            knowledge_index,
            "replace_regular_file_at_if_unchanged_with_info",
            side_effect=OSError("synthetic rollback restore failure"),
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "published entry restore failed",
        ) as caught:
            knowledge_index.write_all(
                outputs=outputs,
                data_root=self.root,
                target=target,
            )

        self.assertNotIn(str(self.temporary), str(caught.exception))
        recovery = [
            entry
            for entry in target.iterdir()
            if (
                entry.name.startswith(".index-recovery-")
                and entry.suffix == ".json"
            )
        ]
        self.assertEqual(len(recovery), 2)
        records = [
            json.loads(entry.read_text(encoding="utf-8"))
            for entry in recovery
        ]
        self.assertEqual(
            {record["logical_name"] for record in records},
            {"a-project.md", "z-project.md"},
        )
        self.assertEqual(
            [base64.b64decode(record["data_base64"]) for record in records],
            [previous, previous],
        )
        self.assertEqual(
            [record["mode"] for record in records],
            [0o640, 0o640],
        )
        self.assertEqual(
            [stat.S_IMODE(entry.stat().st_mode) for entry in recovery],
            [0o600, 0o600],
        )
        self.assertEqual(knowledge_index.build_all(self.root), {})
    def test_generator_owned_stale_index_is_retired_and_not_resurrected(self):
        target = self.root / "index"
        previous = (GENERATED_NOTICE + "\n# obsolete\n").encode("utf-8")
        stale = target / "obsolete.md"
        stale.write_bytes(previous)
        stale.chmod(0o640)

        knowledge_index.write_all(
            outputs={},
            data_root=self.root,
            target=target,
        )

        self.assertFalse(stale.exists())
        retired = [
            entry
            for entry in target.iterdir()
            if (
                entry.name.startswith(".index-retired-")
                and entry.suffix == ".tmp"
            )
        ]
        self.assertEqual(len(retired), 1)
        self.assertEqual(retired[0].read_bytes(), previous)
        self.assertEqual(
            retired[0].with_suffix(".name").read_text(encoding="utf-8"),
            "obsolete.md\n",
        )

        knowledge_index.write_all(
            outputs={},
            data_root=self.root,
            target=target,
        )
        self.assertFalse(stale.exists())
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {retired[0].name, retired[0].with_suffix(".name").name},
        )

    def test_missing_index_is_restored_from_recovery_record_on_next_write(self):
        target = self.root / "index"
        previous = (GENERATED_NOTICE + "\n# previous\n").encode("utf-8")
        current = target / "demo-api.md"
        current.write_bytes(previous)
        current.chmod(0o640)

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced postcheck failure"
                ),
            ],
        ), mock.patch.object(
            knowledge_index,
            "replace_regular_file_at_if_unchanged_with_info",
            side_effect=OSError("synthetic rollback restore failure"),
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={
                    "demo-api": GENERATED_NOTICE + "\n# replacement\n",
                },
                data_root=self.root,
                target=target,
            )

        current.unlink()
        knowledge_index.write_all(
            outputs={"demo-api": previous.decode("utf-8")},
            data_root=self.root,
            target=target,
        )

        self.assertEqual(current.read_bytes(), previous)
        self.assertEqual(stat.S_IMODE(current.stat().st_mode), 0o640)
        self.assertFalse(
            any(
                entry.name.startswith(".index-recovery-")
                for entry in target.iterdir()
            )
        )
        resolved = [
            entry
            for entry in target.iterdir()
            if entry.name.startswith(".index-resolved-")
        ]
        self.assertEqual(len(resolved), 1)
        record = json.loads(resolved[0].read_text(encoding="utf-8"))
        self.assertEqual(record["logical_name"], "demo-api.md")
        self.assertEqual(
            base64.b64decode(record["data_base64"]),
            previous,
        )

    def test_recovery_record_conflict_reports_only_logical_index_name(self):
        target = self.root / "index"
        previous = (GENERATED_NOTICE + "\n# previous\n").encode("utf-8")
        current = target / "demo-api.md"
        current.write_bytes(previous)

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced postcheck failure"
                ),
            ],
        ), mock.patch.object(
            knowledge_index,
            "replace_regular_file_at_if_unchanged_with_info",
            side_effect=OSError("synthetic rollback restore failure"),
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={
                    "demo-api": GENERATED_NOTICE + "\n# replacement\n",
                },
                data_root=self.root,
                target=target,
            )

        with self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_RECOVERY_CONFLICT demo-api.md",
        ) as caught:
            knowledge_index.write_all(
                outputs={"demo-api": previous.decode("utf-8")},
                data_root=self.root,
                target=target,
            )

        self.assertNotIn(str(self.temporary), str(caught.exception))
        self.assertEqual(
            current.read_bytes(),
            (GENERATED_NOTICE + "\n# replacement\n").encode("utf-8"),
        )

    def test_existing_index_restore_applies_exact_mode_after_writing_bytes(self):
        target = self.root / "index"
        current = target / "demo-api.md"
        previous = (GENERATED_NOTICE + "\n# previous\n").encode("utf-8")
        replacement = (GENERATED_NOTICE + "\n# replacement\n").encode("utf-8")
        current.write_bytes(replacement)
        expected_info = current.stat()
        events = []
        original_write = os.write
        original_fchmod = os.fchmod

        def trace_write(descriptor, data):
            events.append(("write", descriptor, None))
            return original_write(descriptor, data)

        def trace_fchmod(descriptor, mode):
            events.append(("fchmod", descriptor, mode))
            return original_fchmod(descriptor, mode)

        directory_descriptor = file_io.open_directory_path(target)
        try:
            with mock.patch.object(
                file_io.os,
                "write",
                side_effect=trace_write,
            ), mock.patch.object(
                file_io.os,
                "fchmod",
                side_effect=trace_fchmod,
            ):
                restored_info = (
                    file_io.replace_regular_file_at_if_unchanged_with_info(
                        directory_descriptor,
                        current.name,
                        replacement,
                        previous,
                        0o4755,
                        expected_info=expected_info,
                    )
                )
        finally:
            os.close(directory_descriptor)

        self.assertIsNotNone(restored_info)
        replacement_descriptor = next(
            descriptor
            for action, descriptor, _ in events
            if action == "write"
        )
        last_write = max(
            index
            for index, event in enumerate(events)
            if event[:2] == ("write", replacement_descriptor)
        )
        final_mode = max(
            index
            for index, event in enumerate(events)
            if event == ("fchmod", replacement_descriptor, 0o4755)
        )
        self.assertGreater(final_mode, last_write)
        self.assertEqual(current.read_bytes(), previous)
        self.assertEqual(stat.S_IMODE(current.stat().st_mode), 0o4755)

    def test_recovery_record_restores_exact_special_mode_bits(self):
        target = self.root / "index"
        previous = (GENERATED_NOTICE + "\n# previous\n").encode("utf-8")
        current = target / "demo-api.md"
        current.write_bytes(previous)
        current.chmod(0o7640)

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced postcheck failure"
                ),
            ],
        ), mock.patch.object(
            knowledge_index,
            "replace_regular_file_at_if_unchanged_with_info",
            side_effect=OSError("synthetic rollback restore failure"),
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={
                    "demo-api": GENERATED_NOTICE + "\n# replacement\n",
                },
                data_root=self.root,
                target=target,
            )

        current.unlink()
        knowledge_index.write_all(
            outputs={"demo-api": previous.decode("utf-8")},
            data_root=self.root,
            target=target,
        )

        self.assertEqual(current.read_bytes(), previous)
        self.assertEqual(stat.S_IMODE(current.stat().st_mode), 0o7640)


    def test_long_project_second_write_uses_bounded_recovery_names(self):
        project = "a" * 240
        self.write("lessons/{}/rule.md".format(project), LESSON)
        target = self.root / "index"

        knowledge_index.write_all(data_root=self.root, target=target)
        knowledge_index.write_all(data_root=self.root, target=target)

        self.assertTrue((target / (project + ".md")).is_file())
        self.assertEqual(
            [
                entry.name
                for entry in target.iterdir()
                if entry.name.startswith(".index-")
            ],
            [],
        )

    def test_backup_metadata_path_replacement_is_never_unlinked(self):
        target = self.root / "index"
        current = target / "demo-api.md"
        current.write_bytes(b"previous public bytes\r\n")
        user_file = self.temporary / "user-metadata"
        user_file.write_bytes(b"user replacement bytes\n")
        replaced_path = None

        def replace_metadata_path(_projects):
            nonlocal replaced_path
            if replaced_path is not None:
                return
            metadata = next(
                (
                    entry
                    for entry in target.iterdir()
                    if entry.name.startswith(".index-backup-")
                    and entry.suffix == ".name"
                ),
                None,
            )
            if metadata is not None:
                os.replace(user_file, metadata)
                replaced_path = metadata

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=replace_metadata_path,
        ):
            knowledge_index.write_all(
                outputs={
                    "demo-api": GENERATED_NOTICE + "\n# replacement\n",
                },
                data_root=self.root,
                target=target,
            )

        preserved = user_file if user_file.exists() else replaced_path
        self.assertIsNotNone(preserved)
        self.assertTrue(preserved.exists())
        self.assertEqual(preserved.read_bytes(), b"user replacement bytes\n")

    def test_backup_metadata_open_descriptor_write_is_never_unlinked(self):
        target = self.root / "index"
        current = target / "demo-api.md"
        current.write_bytes(b"previous public bytes\r\n")
        mutated_path = None

        def mutate_metadata_inode(_projects):
            nonlocal mutated_path
            if mutated_path is not None:
                return
            metadata = next(
                (
                    entry
                    for entry in target.iterdir()
                    if entry.name.startswith(".index-backup-")
                    and entry.suffix == ".name"
                ),
                None,
            )
            if metadata is None:
                return
            descriptor = os.open(metadata, os.O_WRONLY | os.O_TRUNC)
            try:
                os.write(descriptor, b"user descriptor bytes\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            mutated_path = metadata

        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=mutate_metadata_inode,
        ):
            knowledge_index.write_all(
                outputs={
                    "demo-api": GENERATED_NOTICE + "\n# replacement\n",
                },
                data_root=self.root,
                target=target,
            )

        if mutated_path is not None:
            self.assertTrue(mutated_path.exists())
            self.assertEqual(
                mutated_path.read_bytes(),
                b"user descriptor bytes\n",
            )

    def test_recovery_name_collision_does_not_remove_existing_artifact(self):
        target = self.root / "index"
        collision = target / ".index-recovery-collision.json"
        collision.write_bytes(b"user artifact bytes\n")
        directory_descriptor = knowledge_index.open_directory_path(target)
        try:
            with mock.patch.object(
                knowledge_index,
                "_recovery_name",
                return_value=collision.name,
            ), self.assertRaisesRegex(
                knowledge_index.KnowledgeIndexError,
                "cannot allocate index recovery record",
            ):
                knowledge_index._persist_index_recovery_record(
                    directory_descriptor,
                    "demo-api.md",
                    b"previous public bytes\r\n",
                    0o640,
                )
        finally:
            os.close(directory_descriptor)

        self.assertEqual(collision.read_bytes(), b"user artifact bytes\n")

    def test_normal_write_does_not_materialize_recovery_records(self):
        target = self.root / "index"
        current = target / "demo-api.md"
        current.write_bytes(b"previous public bytes\r\n")
        replacement = GENERATED_NOTICE + "\n# replacement\n"
        original_open = knowledge_index.os.open

        def reject_recovery_record(path, flags, *args, **kwargs):
            if Path(os.fspath(path)).name.startswith(".index-recovery-"):
                raise OSError("unexpected recovery record creation")
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(
            knowledge_index.os,
            "open",
            side_effect=reject_recovery_record,
        ):
            knowledge_index.write_all(
                outputs={"demo-api": replacement},
                data_root=self.root,
                target=target,
            )

        self.assertEqual(current.read_bytes(), replacement.encode("utf-8"))
        self.assertEqual(
            [
                entry.name
                for entry in target.iterdir()
                if entry.name.startswith(".index-")
            ],
            [],
        )

    def test_long_project_recovery_sidecars_map_backup_and_quarantine(self):
        existing_project = "a" * 240
        absent_project = "z" * 240
        target = self.root / "index"
        previous = b"previous long project bytes\r\n"
        existing = target / (existing_project + ".md")
        existing.write_bytes(previous)
        existing.chmod(0o640)
        outputs = {
            existing_project: GENERATED_NOTICE + "\n# replacement a\n",
            absent_project: GENERATED_NOTICE + "\n# replacement z\n",
        }
        original_replace = (
            knowledge_index.replace_regular_file_at_if_unchanged_with_info
        )

        def fail_existing_restore(parent_descriptor, name, *args, **kwargs):
            if name == existing_project + ".md":
                raise OSError("synthetic rollback restore failure")
            return original_replace(
                parent_descriptor,
                name,
                *args,
                **kwargs,
            )


        with mock.patch.object(
            knowledge_index,
            "_require_projects_unchanged",
            side_effect=[
                None,
                knowledge_index.KnowledgeIndexError(
                    "forced postcheck failure"
                ),
            ],
        ), mock.patch.object(
            knowledge_index,
            "replace_regular_file_at_if_unchanged_with_info",
            side_effect=fail_existing_restore,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_ROLLBACK_FAILED",
        ) as caught:
            knowledge_index.write_all(
                outputs=outputs,
                data_root=self.root,
                target=target,
            )

        self.assertNotIn(str(self.temporary), str(caught.exception))
        metadata = [
            entry
            for entry in target.iterdir()
            if entry.suffix == ".name"
        ]
        self.assertEqual(len(metadata), 1)
        quarantine = metadata[0].with_suffix(".tmp")
        self.assertTrue(quarantine.is_file())
        self.assertLessEqual(len(os.fsencode(quarantine.name)), 255)
        self.assertLessEqual(len(os.fsencode(metadata[0].name)), 255)
        self.assertEqual(
            metadata[0].read_text(encoding="utf-8"),
            absent_project + ".md\n",
        )
        self.assertTrue(quarantine.name.startswith(".index-quarantine-"))

        recovery = [
            entry
            for entry in target.iterdir()
            if (
                entry.name.startswith(".index-recovery-")
                and entry.suffix == ".json"
            )
        ]
        self.assertEqual(len(recovery), 1)
        self.assertLessEqual(len(os.fsencode(recovery[0].name)), 255)
        record = json.loads(recovery[0].read_text(encoding="utf-8"))
        self.assertEqual(record["logical_name"], existing_project + ".md")
        self.assertEqual(record["mode"], 0o640)
        self.assertEqual(
            base64.b64decode(record["data_base64"]),
            previous,
        )


    def test_stale_index_removal_preserves_concurrent_writer(self):
        self.write("lessons/demo-api/rule.md", LESSON)
        target = self.root / "index"
        stale = target / "obsolete.md"
        stale.write_text(
            GENERATED_NOTICE + "\n# obsolete\n",
            encoding="utf-8",
        )
        concurrent = self.temporary / "concurrent-stale"
        concurrent.write_bytes(b"concurrent stale bytes\n")
        original_read = knowledge_index.read_regular_file_beneath
        writer_published = False

        def publish_writer_after_quarantine_read(parent, name, maximum_bytes):
            nonlocal writer_published
            data = original_read(parent, name, maximum_bytes)
            if not writer_published and name.startswith(".index-retired-"):
                os.replace(concurrent, stale)
                writer_published = True
            return data

        with mock.patch.object(
            knowledge_index,
            "read_regular_file_beneath",
            side_effect=publish_writer_after_quarantine_read,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_ROLLBACK_FAILED",
        ):
            knowledge_index.write_all(data_root=self.root, target=target)

        self.assertTrue(writer_published)
        self.assertEqual(stale.read_bytes(), b"concurrent stale bytes\n")
        self.assertFalse((target / "demo-api.md").exists())

    def test_stale_quarantine_open_descriptor_write_is_preserved(self):
        self.write("lessons/demo-api/rule.md", LESSON)
        target = self.root / "index"
        stale = target / "obsolete.md"
        stale.write_text(
            GENERATED_NOTICE + "\n# obsolete\n",
            encoding="utf-8",
        )
        original_read = knowledge_index.read_regular_file_beneath
        mutated = False

        def mutate_renamed_inode_after_read(parent, name, maximum_bytes):
            nonlocal mutated
            if not mutated and name.startswith(".index-retired-"):
                descriptor = os.open(parent / name, os.O_WRONLY)
                try:
                    data = original_read(parent, name, maximum_bytes)
                    os.write(descriptor, b"open descriptor bytes\n")
                    os.ftruncate(descriptor, len(b"open descriptor bytes\n"))
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                mutated = True
                return data
            return original_read(parent, name, maximum_bytes)

        with mock.patch.object(
            knowledge_index,
            "read_regular_file_beneath",
            side_effect=mutate_renamed_inode_after_read,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_ROLLBACK_FAILED",
        ):
            knowledge_index.write_all(data_root=self.root, target=target)

        self.assertTrue(mutated)
        self.assertEqual(stale.read_bytes(), b"open descriptor bytes\n")
        self.assertFalse((target / "demo-api.md").exists())

    def test_stale_quarantine_path_replacement_is_preserved(self):
        self.write("lessons/demo-api/rule.md", LESSON)
        target = self.root / "index"
        stale = target / "obsolete.md"
        stale.write_text(
            GENERATED_NOTICE + "\n# obsolete\n",
            encoding="utf-8",
        )
        concurrent = self.temporary / "concurrent-quarantine"
        concurrent.write_bytes(b"quarantine replacement bytes\n")
        original_read = knowledge_index.read_regular_file_beneath
        replaced = False

        def replace_quarantine_after_read(parent, name, maximum_bytes):
            nonlocal replaced
            data = original_read(parent, name, maximum_bytes)
            if not replaced and name.startswith(".index-retired-"):
                os.replace(concurrent, parent / name)
                replaced = True
            return data

        with mock.patch.object(
            knowledge_index,
            "read_regular_file_beneath",
            side_effect=replace_quarantine_after_read,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_ROLLBACK_FAILED",
        ):
            knowledge_index.write_all(data_root=self.root, target=target)

        self.assertTrue(replaced)
        self.assertEqual(stale.read_bytes(), b"quarantine replacement bytes\n")
        self.assertFalse((target / "demo-api.md").exists())

    def test_project_link_change_during_scan_preserves_existing_index(self):
        external = self.temporary / "external"
        external.mkdir()
        (external / "rule.md").write_text(LESSON, encoding="utf-8")
        (self.root / "lessons" / "demo-api").symlink_to(
            external,
            target_is_directory=True,
        )
        target = self.root / "index" / "demo-api.md"
        target.write_bytes(b"user bytes\r\n")

        with mock.patch.object(
            knowledge_index,
            "project_directory_unchanged",
            return_value=False,
        ), self.assertRaises(knowledge_index.KnowledgeSourceError) as caught:
            knowledge_index.write_all(data_root=self.root, target=self.root / "index")

        self.assertEqual(caught.exception.logical_path, "lessons/demo-api")
        self.assertEqual(
            caught.exception.reason,
            "project link changed during scan",
        )
        self.assertNotIn(str(external), str(caught.exception))
        self.assertEqual(target.read_bytes(), b"user bytes\r\n")

    def test_nonrecursive_sources_ignore_nested_markdown(self):
        cases = (
            (
                "lessons/demo-api/rule.md",
                LESSON,
                "lessons/demo-api/guides/item.md",
                LESSON,
            ),
            (
                "book/demo-api/guide.md",
                BOOK,
                "book/demo-api/guides/item.md",
                BOOK,
            ),
        )
        for direct_relative, direct_text, nested_relative, nested_text in cases:
            with self.subTest(nested_relative=nested_relative):
                self.write(direct_relative, direct_text)
                self.write(nested_relative, nested_text)

                output = knowledge_index.build_all(self.root)["demo-api"]

                self.assertIn("`{}`".format(direct_relative), output)
                self.assertNotIn(nested_relative, output)
                self.assertEqual(output.count("  - 상세:"), 1)
                shutil.rmtree((self.root / direct_relative).parent)

    def test_docs_are_recursive_but_book_assets_and_html_are_not_indexed(self):
        self.write("docs/demo-api/guides/nested.md", DOC)
        self.write("book/demo-api/guide.md", BOOK)
        self.write("book/demo-api/assets/readme.md", "not metadata")
        self.write("book/demo-api/html/rendered.md", "not metadata")

        output = knowledge_index.build_all(self.root)["demo-api"]

        self.assertIn("`docs/demo-api/guides/nested.md`", output)
        self.assertIn("`book/demo-api/guide.md`", output)
        self.assertNotIn("assets/readme.md", output)
        self.assertNotIn("html/rendered.md", output)
        self.assertEqual(output.count("  - 상세:"), 2)

    def test_hidden_or_invalid_project_directory_is_ignored(self):
        self.write("lessons/.hidden/ignored.md", "not lesson metadata")
        self.write("docs/not_a_project/ignored.md", "not document metadata")

        self.assertEqual(knowledge_index.build_all(self.root), {})

    def test_valid_project_name_must_be_a_directory_or_link(self):
        self.write("lessons/demo-api", "not a directory")

        with self.assertRaises(knowledge_index.KnowledgeSourceError) as caught:
            knowledge_index.build_all(self.root)

        self.assertEqual(caught.exception.logical_path, "lessons/demo-api")
        self.assertEqual(
            caught.exception.reason,
            "project entry must point to a directory",
        )

    def test_large_source_set_has_no_count_or_32_kib_output_cap(self):
        project = self.root / "lessons" / "large"
        project.mkdir()
        for item_number in range(500):
            title = "규칙 {:03d} {}".format(item_number, "가" * 20)
            (project / "item-{:03d}.md".format(item_number)).write_text(
                LESSON.replace("부분수정에서 null은 해제다", title),
                encoding="utf-8",
            )

        output = knowledge_index.build_all(self.root)["large"]

        self.assertGreater(len(output.encode("utf-8")), 32 * 1024)
        self.assertEqual(output.count("  - 상세:"), 500)
        self.assertIn("규칙 000", output)
        self.assertIn("규칙 499", output)

    def test_write_all_writes_exact_utf8_outputs_to_explicit_temp_target(self):
        target = self.temporary / "generated-index"
        outputs = {
            "demo-api": GENERATED_NOTICE + "\n# 한글\n",
            "_global": GENERATED_NOTICE + "\n# global\n",
        }

        returned = knowledge_index.write_all(
            outputs=outputs,
            data_root=self.root,
            target=target,
        )

        self.assertEqual(returned, target)
        self.assertEqual((target / "demo-api.md").read_bytes(), outputs["demo-api"].encode("utf-8"))
        self.assertEqual((target / "_global.md").read_bytes(), outputs["_global"].encode("utf-8"))


    def test_lesson_publish_cannot_be_overwritten_by_an_older_index_snapshot(self):
        project_lessons = self.root / "lessons" / "demo-api"
        project_lessons.mkdir()
        context = multiprocessing.get_context("spawn")
        snapshot_ready = context.Event()
        release_snapshot = context.Event()
        stale_writer = context.Process(
            target=_write_paused_stale_index,
            args=(self.root, snapshot_ready, release_snapshot),
        )
        stale_writer.start()
        self.addCleanup(
            lambda: stale_writer.kill() if stale_writer.is_alive() else None
        )
        self.assertTrue(snapshot_ready.wait(5))

        with ThreadPoolExecutor(max_workers=1) as executor:
            publisher = executor.submit(
                publish_lesson,
                "new-rule",
                LESSON.replace(
                    "부분수정에서 null은 해제다",
                    "가장 최근 규칙",
                ),
                "demo-api",
                self.root / "lessons",
            )
            try:
                with self.assertRaises(TimeoutError):
                    publisher.result(timeout=0.1)
            finally:
                release_snapshot.set()
            stale_writer.join(5)
            publisher.result(timeout=5)

        self.assertEqual(stale_writer.exitcode, 0)
        index_text = (self.root / "index" / "demo-api.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("가장 최근 규칙", index_text)

    def test_project_index_remains_when_only_one_source_section_is_removed(self):
        self.write("lessons/demo-api/one.md", LESSON)
        self.write("docs/demo-api/one.md", DOC)
        target = self.root / "index"
        knowledge_index.write_all(data_root=self.root, target=target)
        shutil.rmtree(self.root / "docs" / "demo-api")

        knowledge_index.write_all(data_root=self.root, target=target)

        self.assertTrue((target / "demo-api.md").exists())
        output = (target / "demo-api.md").read_text(encoding="utf-8")
        self.assertIn("작업 규칙", output)
        self.assertNotIn("작업 문서", output)

    def test_check_rejects_missing_outdated_extra_and_non_lf_index_bytes(self):
        self.write("lessons/demo-api/one.md", LESSON)
        target = self.root / "index"
        errors = io.StringIO()

        with contextlib.redirect_stderr(errors):
            self.assertEqual(knowledge_index.check(self.root, target), 1)
            knowledge_index.write_all(data_root=self.root, target=target)
            (target / "demo-api.md").write_text("outdated\n", encoding="utf-8")
            self.assertEqual(knowledge_index.check(self.root, target), 1)
            knowledge_index.write_all(data_root=self.root, target=target)
            current = target / "demo-api.md"
            current.write_bytes(current.read_bytes().replace(b"\n", b"\r\n"))
            self.assertEqual(knowledge_index.check(self.root, target), 1)
            knowledge_index.write_all(data_root=self.root, target=target)
            (target / "obsolete.md").write_text(
                GENERATED_NOTICE + "\n",
                encoding="utf-8",
            )
            self.assertEqual(knowledge_index.check(self.root, target), 1)

        self.assertIn("KNOWLEDGE_INDEX_STALE", errors.getvalue())
        self.assertIn("재생성 가능한 파생 파일", errors.getvalue())
        self.assertIn("원본", errors.getvalue())

    def test_unknown_index_entries_are_never_silently_skipped_or_deleted(self):
        self.write("lessons/demo-api/one.md", LESSON)
        target = self.root / "index"
        cases = (
            ("notes.txt", "mine\n"),
            ("personal.md", "# mine\n"),
        )
        for name, contents in cases:
            with self.subTest(name=name):
                entry = target / name
                entry.write_text(contents, encoding="utf-8")

                with self.assertRaises(knowledge_index.KnowledgeIndexError):
                    knowledge_index.write_all(data_root=self.root, target=target)

                self.assertEqual(entry.read_text(encoding="utf-8"), contents)
                entry.unlink()

    def test_unknown_index_entry_makes_check_stale_without_mutating_it(self):
        self.write("lessons/demo-api/one.md", LESSON)
        target = self.root / "index"
        knowledge_index.write_all(data_root=self.root, target=target)
        unknown = target / "personal.md"
        unknown.write_text("# mine\n", encoding="utf-8")
        errors = io.StringIO()

        with contextlib.redirect_stderr(errors):
            result = knowledge_index.check(self.root, target)

        self.assertEqual(result, 1)
        self.assertEqual(unknown.read_text(encoding="utf-8"), "# mine\n")
        self.assertIn("KNOWLEDGE_INDEX_STALE", errors.getvalue())

    def test_generator_temp_name_cannot_hide_unknown_symlink(self):
        self.write("lessons/demo-api/one.md", LESSON)
        outside = self.temporary / "outside"
        outside.write_text("mine", encoding="utf-8")
        unknown = self.root / "index" / ".index-foreign"
        unknown.symlink_to(outside)

        with self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(data_root=self.root, target=self.root / "index")

        self.assertTrue(unknown.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "mine")

    def test_broken_index_symlink_is_rejected_with_domain_error(self):
        self.write("lessons/demo-api/one.md", LESSON)
        target = self.root / "index"
        shutil.rmtree(target)
        target.symlink_to(self.root / "missing-index", target_is_directory=True)

        with self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "index must be a real directory",
        ):
            knowledge_index.write_all(data_root=self.root, target=target)


if __name__ == "__main__":
    unittest.main()
