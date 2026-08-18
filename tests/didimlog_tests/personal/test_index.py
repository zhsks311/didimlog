import base64
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import hashlib
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

def _legacy_artifact_name(kind, logical_name, suffix, token):
    digest = hashlib.sha256(logical_name.encode("utf-8")).hexdigest()[:16]
    return ".index-{}-{}-{}{}".format(
        kind,
        digest,
        token,
        suffix,
    )


def _legacy_recovery_record_bytes(logical_name, data, mode):
    return (
        json.dumps(
            {
                "data_base64": base64.b64encode(data).decode("ascii"),
                "logical_name": logical_name,
                "mode": mode,
                "version": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )



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

    def test_recursive_docs_read_from_the_directory_inode_that_was_scanned(self):
        project = self.root / "docs" / "demo-api"
        active = project / "nested"
        saved = self.temporary / "nested-saved"
        replacement = self.temporary / "nested-replacement"
        active.mkdir(parents=True)
        replacement.mkdir()
        original_text = (
            "---\n"
            "title: 원래 중첩 문서\n"
            "find_when: [원래 중첩 자료]\n"
            "---\n"
        )
        replacement_text = (
            "---\n"
            "title: 바뀐 중첩 문서\n"
            "find_when: [바뀐 중첩 자료]\n"
            "---\n"
        )
        (active / "guide.md").write_text(original_text, encoding="utf-8")
        (replacement / "guide.md").write_text(
            replacement_text,
            encoding="utf-8",
        )
        original_markdown_files = knowledge_index._markdown_files
        swapped = False

        def swap_nested_after_scan(*args, **kwargs):
            nonlocal swapped
            files = original_markdown_files(*args, **kwargs)
            active.rename(saved)
            replacement.rename(active)
            swapped = True
            return files

        try:
            with mock.patch.object(
                knowledge_index,
                "_markdown_files",
                side_effect=swap_nested_after_scan,
            ):
                output = knowledge_index.build_all(self.root)["demo-api"]
        finally:
            if swapped:
                active.rename(replacement)
                saved.rename(active)

        self.assertIn("원래 중첩 문서", output)
        self.assertNotIn("바뀐 중첩 문서", output)

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
            item_reader=None,
        ):
            files = original_markdown_files(
                project,
                recursive=recursive,
                root_descriptor=root_descriptor,
                item_reader=item_reader,
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

    def test_index_directory_swap_never_writes_the_replacement_namespace(self):
        outputs = {
            "demo-api": GENERATED_NOTICE + "\n# replacement\n",
        }
        boundaries = (
            "open_directory_path",
            "_validate_index_directory",
            "_prepare_index_temporary",
            "_require_projects_unchanged",
            "_rename_entry_no_replace",
        )

        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                target = self.root / ("index-" + boundary.replace(".", "-"))
                target.mkdir()
                current = target / "demo-api.md"
                previous = (GENERATED_NOTICE + "\n# previous\n").encode("utf-8")
                current.write_bytes(previous)
                current.chmod(0o640)
                saved = self.temporary / (target.name + "-saved")
                swapped = False

                def swap_destination():
                    nonlocal swapped
                    if swapped:
                        return
                    target.rename(saved)
                    target.mkdir()
                    swapped = True

                if boundary == "_rename_entry_no_replace":
                    original = personal_render._rename_entry_no_replace

                    def swap_at_boundary(*args, **kwargs):
                        result = original(*args, **kwargs)
                        swap_destination()
                        return result

                    patcher = mock.patch.object(
                        personal_render,
                        "_rename_entry_no_replace",
                        side_effect=swap_at_boundary,
                    )
                else:
                    original = getattr(knowledge_index, boundary)

                    def swap_at_boundary(*args, _original=original, **kwargs):
                        swap_destination()
                        return _original(*args, **kwargs)

                    patcher = mock.patch.object(
                        knowledge_index,
                        boundary,
                        side_effect=swap_at_boundary,
                    )

                with patcher, self.assertRaises(
                    knowledge_index.KnowledgeIndexError
                ) as caught:
                    knowledge_index.write_all(
                        outputs=outputs,
                        data_root=self.root,
                        target=target,
                    )

                self.assertTrue(swapped)
                self.assertNotIn(str(self.temporary), str(caught.exception))
                self.assertEqual(list(target.iterdir()), [])
                saved_bytes = (saved / "demo-api.md").read_bytes()
                self.assertIn(
                    saved_bytes,
                    (previous, outputs["demo-api"].encode("utf-8")),
                )
                self.assertIn(
                    stat.S_IMODE((saved / "demo-api.md").stat().st_mode),
                    (0o600, 0o640),
                )


    def test_absent_index_target_race_never_writes_competing_directory(self):
        parent = self.root / "generated"
        parent.mkdir()
        target = parent / "index"
        output = GENERATED_NOTICE + "\n# generated\n"
        original_rename = personal_render._rename_entry_no_replace
        installed_competitor = False

        def install_competitor_before_target(parent_descriptor, source, destination):
            nonlocal installed_competitor
            if (
                destination == target.name
                and source.startswith(".index-directory-")
                and not installed_competitor
            ):
                target.mkdir()
                installed_competitor = True
            return original_rename(parent_descriptor, source, destination)

        with mock.patch.object(
            personal_render,
            "_rename_entry_no_replace",
            side_effect=install_competitor_before_target,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError) as caught:
            knowledge_index.write_all(
                outputs={"demo-api": output},
                data_root=self.root,
                target=target,
            )

        self.assertTrue(installed_competitor)
        self.assertNotIn(str(self.temporary), str(caught.exception))
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual({entry.name for entry in parent.iterdir()}, {target.name})

    def test_prepared_temp_path_replacement_is_preserved_on_precheck_failure(self):
        user_bytes = b"user replacement bytes\n"

        for case_name in ("regular", "fifo"):
            with self.subTest(case=case_name):
                target = self.root / ("index-temp-" + case_name)
                target.mkdir()
                original_prepare = knowledge_index._prepare_index_temporary
                prepared_name = None

                def replace_prepared_path(parent_descriptor, project, data):
                    nonlocal prepared_name
                    prepared = original_prepare(
                        parent_descriptor,
                        project,
                        data,
                    )
                    prepared_name = prepared[0]
                    prepared_path = target / prepared_name
                    if case_name == "regular":
                        replacement = self.temporary / (
                            "prepared-user-" + case_name
                        )
                        replacement.write_bytes(user_bytes)
                        os.replace(replacement, prepared_path)
                    else:
                        prepared_path.unlink()
                        os.mkfifo(prepared_path)
                    return prepared

                def fail_source_precheck(projects):
                    raise knowledge_index.KnowledgeSourceError(
                        "lessons/demo-api",
                        "project link changed during scan",
                    )

                with mock.patch.object(
                    knowledge_index,
                    "_prepare_index_temporary",
                    side_effect=replace_prepared_path,
                ), mock.patch.object(
                    knowledge_index,
                    "_require_projects_unchanged",
                    side_effect=fail_source_precheck,
                ), self.assertRaises(
                    knowledge_index.KnowledgeIndexError
                ) as caught:
                    knowledge_index.write_all(
                        outputs={
                            "demo-api": GENERATED_NOTICE + "\n# generated\n"
                        },
                        data_root=self.root,
                        target=target,
                    )

                self.assertIsNotNone(prepared_name)
                self.assertNotIn(str(self.temporary), str(caught.exception))
                preserved = target / prepared_name
                if case_name == "regular":
                    self.assertEqual(preserved.read_bytes(), user_bytes)
                else:
                    self.assertTrue(stat.S_ISFIFO(preserved.lstat().st_mode))

    def test_prepared_temp_open_descriptor_mutation_is_preserved(self):
        target = self.root / "index-temp-open-descriptor"
        target.mkdir()
        user_bytes = b"user open-descriptor bytes\n"
        original_prepare = knowledge_index._prepare_index_temporary
        prepared_name = None
        descriptor = None
        mutated = False

        def retain_prepared_descriptor(parent_descriptor, project, data):
            nonlocal descriptor, prepared_name
            prepared = original_prepare(parent_descriptor, project, data)
            prepared_name = prepared[0]
            descriptor = os.open(
                prepared_name,
                os.O_WRONLY,
                dir_fd=parent_descriptor,
            )
            return prepared

        def mutate_before_source_precheck(projects):
            nonlocal mutated
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, user_bytes)
            os.ftruncate(descriptor, len(user_bytes))
            os.fsync(descriptor)
            mutated = True
            raise knowledge_index.KnowledgeSourceError(
                "lessons/demo-api",
                "project link changed during scan",
            )

        try:
            with mock.patch.object(
                knowledge_index,
                "_prepare_index_temporary",
                side_effect=retain_prepared_descriptor,
            ), mock.patch.object(
                knowledge_index,
                "_require_projects_unchanged",
                side_effect=mutate_before_source_precheck,
            ), self.assertRaises(knowledge_index.KnowledgeIndexError) as caught:
                knowledge_index.write_all(
                    outputs={
                        "demo-api": GENERATED_NOTICE + "\n# generated\n"
                    },
                    data_root=self.root,
                    target=target,
                )
        finally:
            if descriptor is not None:
                os.close(descriptor)

        self.assertTrue(mutated)
        self.assertIsNotNone(prepared_name)
        self.assertNotIn(str(self.temporary), str(caught.exception))
        self.assertEqual((target / prepared_name).read_bytes(), user_bytes)

    def test_prepared_temp_replacement_before_publish_is_preserved(self):
        target = self.root / "index-temp-before-publish"
        target.mkdir()
        existing = target / "demo-api.md"
        existing_bytes = GENERATED_NOTICE.encode("utf-8") + b"\n# previous\n"
        existing.write_bytes(existing_bytes)
        replacement_bytes = b"user prepared replacement bytes\n"
        replacement = self.temporary / "prepared-replacement"
        replacement.write_bytes(replacement_bytes)
        original_publish = knowledge_index._publish_index_temporary
        replaced_name = None

        def replace_then_publish(
            parent_descriptor,
            project,
            temporary_name,
            temporary_snapshot,
            output_name,
        ):
            nonlocal replaced_name
            os.replace(replacement, target / temporary_name)
            replaced_name = temporary_name
            return original_publish(
                parent_descriptor,
                project,
                temporary_name,
                temporary_snapshot,
                output_name,
            )

        with mock.patch.object(
            knowledge_index,
            "_publish_index_temporary",
            side_effect=replace_then_publish,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={
                    "demo-api": GENERATED_NOTICE + "\n# replacement\n"
                },
                data_root=self.root,
                target=target,
            )

        self.assertIsNotNone(replaced_name)
        self.assertEqual(existing.read_bytes(), existing_bytes)
        self.assertEqual(
            (target / replaced_name).read_bytes(),
            replacement_bytes,
        )

    def test_prepare_short_write_removes_only_its_partial_temp_and_retry_converges(self):
        target = self.root / "index-short-write"
        target.mkdir()
        output = GENERATED_NOTICE + "\n# generated\n"

        def write_prefix_then_fail(descriptor, data):
            os.write(descriptor, data[: max(1, len(data) // 2)])
            raise OSError("forced index write failure")

        with mock.patch.object(
            knowledge_index,
            "write_all_and_sync",
            side_effect=write_prefix_then_fail,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={"demo-api": output},
                data_root=self.root,
                target=target,
            )

        self.assertEqual(list(target.iterdir()), [])
        knowledge_index.write_all(
            outputs={"demo-api": output},
            data_root=self.root,
            target=target,
        )
        self.assertEqual(
            (target / "demo-api.md").read_bytes(),
            output.encode("utf-8"),
        )

    def test_prepare_failure_preserves_concurrent_open_descriptor_bytes(self):
        target = self.root / "index-prepare-open-descriptor"
        target.mkdir()
        output = GENERATED_NOTICE + "\n# generated\n"
        user_bytes = b"user concurrent bytes\n"

        def mutate_then_fail(descriptor, data):
            os.write(descriptor, data[: max(1, len(data) // 2)])
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, user_bytes)
            os.ftruncate(descriptor, len(user_bytes))
            os.fsync(descriptor)
            raise OSError("forced index write failure")

        with mock.patch.object(
            knowledge_index,
            "write_all_and_sync",
            side_effect=mutate_then_fail,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={"demo-api": output},
                data_root=self.root,
                target=target,
            )

        entries = list(target.iterdir())
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].name.startswith(".index-"))
        self.assertEqual(entries[0].read_bytes(), user_bytes)

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

    def test_source_retarget_after_publish_never_writes_replacement_target(self):
        external = self.temporary / "external"
        original = external / "original"
        replacement = external / "replacement"
        original.mkdir(parents=True)
        replacement.mkdir()
        (original / "rule.md").write_text(LESSON, encoding="utf-8")
        replacement_marker = replacement / "keep.txt"
        replacement_marker.write_bytes(b"replacement target bytes\n")
        logical = self.root / "lessons" / "demo-api"
        logical.symlink_to(original, target_is_directory=True)
        target = self.root / "index"
        expected = knowledge_index.build_all(self.root)["demo-api"].encode(
            "utf-8"
        )
        original_rename = personal_render._rename_entry_no_replace
        retargeted = False

        def retarget_after_first_publish(parent, source, destination):
            nonlocal retargeted
            result = original_rename(parent, source, destination)
            if not retargeted:
                retargeted = True
                logical.unlink()
                logical.symlink_to(replacement, target_is_directory=True)
            return result

        with mock.patch.object(
            personal_render,
            "_rename_entry_no_replace",
            side_effect=retarget_after_first_publish,
        ), self.assertRaises(knowledge_index.KnowledgeSourceError) as caught:
            knowledge_index.write_all(data_root=self.root, target=target)

        self.assertTrue(retargeted)
        self.assertEqual(caught.exception.logical_path, "lessons/demo-api")
        self.assertEqual(
            caught.exception.reason,
            "project link changed during scan",
        )
        self.assertNotIn(str(external), str(caught.exception))
        self.assertEqual((target / "demo-api.md").read_bytes(), expected)
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {"demo-api.md"},
        )
        self.assertEqual(
            replacement_marker.read_bytes(),
            b"replacement target bytes\n",
        )
        self.assertEqual(
            {entry.name for entry in replacement.iterdir()},
            {"keep.txt"},
        )

    def test_partial_publication_is_repaired_by_next_write(self):
        target = self.root / "index"
        outputs = {
            "a-project": GENERATED_NOTICE + "\n# a\n",
            "z-project": GENERATED_NOTICE + "\n# z\n",
        }
        stale = target / "obsolete.md"
        stale_bytes = (GENERATED_NOTICE + "\n# obsolete\n").encode("utf-8")
        stale.write_bytes(stale_bytes)
        original_rename = personal_render._rename_entry_no_replace

        def fail_second_publish(parent, source, destination):
            if destination == "z-project.md":
                raise OSError("injected second publication failure")
            return original_rename(parent, source, destination)

        with mock.patch.object(
            personal_render,
            "_rename_entry_no_replace",
            side_effect=fail_second_publish,
        ), self.assertRaisesRegex(
            knowledge_index.KnowledgeIndexError,
            "KNOWLEDGE_INDEX_INVALID z-project",
        ):
            knowledge_index.write_all(
                outputs=outputs,
                data_root=self.root,
                target=target,
            )

        self.assertEqual(
            (target / "a-project.md").read_bytes(),
            outputs["a-project"].encode("utf-8"),
        )
        self.assertFalse((target / "z-project.md").exists())
        self.assertEqual(stale.read_bytes(), stale_bytes)
        self.assertEqual(
            {
                entry.name
                for entry in target.iterdir()
                if entry.name.startswith(".index-")
            },
            set(),
        )

        knowledge_index.write_all(
            outputs=outputs,
            data_root=self.root,
            target=target,
        )

        self.assertEqual(
            {
                entry.name: entry.read_bytes()
                for entry in target.iterdir()
            },
            {
                "a-project.md": outputs["a-project"].encode("utf-8"),
                "z-project.md": outputs["z-project"].encode("utf-8"),
            },
        )

    def test_owned_legacy_index_artifacts_are_removed_and_write_converges(self):
        target = self.root / "index"
        current_text = GENERATED_NOTICE + "\n# demo-api\n"
        current_bytes = current_text.encode("utf-8")

        recovery_name = _legacy_artifact_name(
            "recovery",
            "demo-api.md",
            ".json",
            "1" * 24,
        )
        (target / recovery_name).write_bytes(
            _legacy_recovery_record_bytes(
                "demo-api.md",
                current_bytes,
                0o640,
            )
        )
        resolved_name = _legacy_artifact_name(
            "resolved",
            "resolved.md",
            ".json",
            "2" * 24,
        )
        (target / resolved_name).write_bytes(
            _legacy_recovery_record_bytes(
                "resolved.md",
                (GENERATED_NOTICE + "\n# resolved\n").encode("utf-8"),
                0o600,
            )
        )

        paired_artifacts = (
            ("retired", "retired.md", current_bytes),
            ("quarantine", "quarantined.md", current_bytes),
            ("quarantine", "empty.md", b""),
        )
        for artifact_number, (kind, logical_name, data) in enumerate(
            paired_artifacts,
            start=3,
        ):
            artifact_name = _legacy_artifact_name(
                kind,
                logical_name,
                ".tmp",
                str(artifact_number) * 24,
            )
            (target / artifact_name).write_bytes(data)
            (target / artifact_name).with_suffix(".name").write_text(
                logical_name + "\n",
                encoding="utf-8",
            )

        temporary = target / (".index-" + "a" * 24 + ".tmp")
        temporary.write_bytes(current_bytes)

        knowledge_index.write_all(
            outputs={"demo-api": current_text},
            data_root=self.root,
            target=target,
        )

        expected_names = {"demo-api.md"}
        self.assertEqual({entry.name for entry in target.iterdir()}, expected_names)
        self.assertEqual((target / "demo-api.md").read_bytes(), current_bytes)
        first_write = {
            entry.name: entry.read_bytes()
            for entry in target.iterdir()
        }

        knowledge_index.write_all(
            outputs={"demo-api": current_text},
            data_root=self.root,
            target=target,
        )

        self.assertEqual(
            {
                entry.name: entry.read_bytes()
                for entry in target.iterdir()
            },
            first_write,
        )

    def test_legacy_cleanup_preserves_open_descriptor_mutation(self):
        target = self.root / "index"
        artifact = target / (".index-" + "e" * 24 + ".tmp")
        artifact.write_bytes(
            (GENERATED_NOTICE + "\n# generated\n").encode("utf-8")
        )
        user_bytes = b"user open-descriptor bytes\n"
        descriptor = os.open(artifact, os.O_WRONLY)
        original_rename = personal_render._rename_entry_no_replace
        mutated = False

        def mutate_before_cleanup(parent, source, destination):
            nonlocal mutated
            if source == artifact.name and not mutated:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, user_bytes)
                os.ftruncate(descriptor, len(user_bytes))
                os.fsync(descriptor)
                mutated = True
            return original_rename(parent, source, destination)

        try:
            with mock.patch.object(
                personal_render,
                "_rename_entry_no_replace",
                side_effect=mutate_before_cleanup,
            ), self.assertRaises(knowledge_index.KnowledgeIndexError):
                knowledge_index.write_all(
                    outputs={},
                    data_root=self.root,
                    target=target,
                )
        finally:
            os.close(descriptor)

        self.assertTrue(mutated)
        self.assertEqual(artifact.read_bytes(), user_bytes)

    def test_legacy_cleanup_preserves_path_replacement(self):
        target = self.root / "index"
        artifact = target / (".index-" + "f" * 24 + ".tmp")
        artifact.write_bytes(
            (GENERATED_NOTICE + "\n# generated\n").encode("utf-8")
        )
        user_bytes = b"user replacement bytes\n"
        replacement = self.temporary / "legacy-replacement"
        replacement.write_bytes(user_bytes)
        original_rename = personal_render._rename_entry_no_replace
        replaced = False

        def replace_before_cleanup(parent, source, destination):
            nonlocal replaced
            if source == artifact.name and not replaced:
                os.replace(replacement, source, dst_dir_fd=parent)
                replaced = True
            return original_rename(parent, source, destination)

        with mock.patch.object(
            personal_render,
            "_rename_entry_no_replace",
            side_effect=replace_before_cleanup,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={},
                data_root=self.root,
                target=target,
            )

        self.assertTrue(replaced)
        self.assertEqual(artifact.read_bytes(), user_bytes)

    def test_ambiguous_legacy_index_artifact_is_preserved_and_fails_closed(self):
        generated_bytes = (GENERATED_NOTICE + "\n# generated\n").encode("utf-8")
        mismatched_resolved = _legacy_artifact_name(
            "resolved",
            "demo-api.md",
            ".json",
            "6" * 24,
        )
        mismatched_retired = _legacy_artifact_name(
            "retired",
            "demo-api.md",
            ".tmp",
            "7" * 24,
        )
        unsafe_quarantine = _legacy_artifact_name(
            "quarantine",
            "bad project.md",
            ".tmp",
            "8" * 24,
        )
        arbitrary_quarantine = _legacy_artifact_name(
            "quarantine",
            "demo-api.md",
            ".tmp",
            "9" * 24,
        )
        cases = (
            (
                "similar temporary name",
                {".index-" + "a" * 23 + ".tmp": generated_bytes},
            ),
            (
                "temporary with arbitrary bytes",
                {".index-" + "b" * 24 + ".tmp": b"user bytes\x00\n"},
            ),
            (
                "resolved record with mismatched digest",
                {
                    mismatched_resolved:
                        _legacy_recovery_record_bytes(
                            "other.md",
                            generated_bytes,
                            0o600,
                        )
                },
            ),
            (
                "retired artifact without label",
                {
                    _legacy_artifact_name(
                        "retired",
                        "unpaired.md",
                        ".tmp",
                        "a" * 24,
                    ): generated_bytes
                },
            ),
            (
                "retired artifact with mismatched label",
                {
                    mismatched_retired: generated_bytes,
                    Path(mismatched_retired).with_suffix(".name").name:
                        b"other.md\n",
                },
            ),
            (
                "unsafe quarantine label",
                {
                    unsafe_quarantine: b"",
                    Path(unsafe_quarantine).with_suffix(".name").name:
                        b"bad project.md\n",
                },
            ),
            (
                "quarantine with arbitrary bytes",
                {
                    arbitrary_quarantine: b"user quarantine bytes\n",
                    Path(arbitrary_quarantine).with_suffix(".name").name:
                        b"demo-api.md\n",
                },
            ),
        )

        for case_name, files in cases:
            with self.subTest(case=case_name):
                target = self.root / "index" / case_name.replace(" ", "-")
                target.mkdir()
                for name, data in files.items():
                    (target / name).write_bytes(data)

                with self.assertRaises(knowledge_index.KnowledgeIndexError):
                    knowledge_index.write_all(
                        outputs={},
                        data_root=self.root,
                        target=target,
                    )

                self.assertEqual(
                    {
                        entry.name: entry.read_bytes()
                        for entry in target.iterdir()
                    },
                    files,
                )

        outside = self.temporary / "outside-index-artifact"
        outside.write_bytes(b"outside bytes\n")
        symlink_target = self.root / "index" / "symlink"
        symlink_target.mkdir()
        symlink = symlink_target / (".index-" + "c" * 24 + ".tmp")
        symlink.symlink_to(outside)

        with self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={},
                data_root=self.root,
                target=symlink_target,
            )

        self.assertTrue(symlink.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside bytes\n")

        directory_target = self.root / "index" / "directory"
        directory_target.mkdir()
        artifact_directory = directory_target / (
            ".index-" + "d" * 24 + ".tmp"
        )
        artifact_directory.mkdir()

        with self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={},
                data_root=self.root,
                target=directory_target,
            )

        self.assertTrue(artifact_directory.is_dir())

    def test_existing_regular_and_symlink_outputs_are_replaced(self):
        target = self.root / "index"
        outputs = {
            "a-project": GENERATED_NOTICE + "\n# current a\n",
            "z-project": GENERATED_NOTICE + "\n# current z\n",
        }
        regular = target / "a-project.md"
        regular.write_text(
            GENERATED_NOTICE + "\n# previous a\n",
            encoding="utf-8",
        )
        linked_target = self.temporary / "linked-index-target.md"
        linked_target.write_bytes(b"user symlink target bytes\n")
        linked = target / "z-project.md"
        linked.symlink_to(linked_target)

        knowledge_index.write_all(
            outputs=outputs,
            data_root=self.root,
            target=target,
        )

        self.assertEqual(
            regular.read_bytes(),
            outputs["a-project"].encode("utf-8"),
        )
        self.assertEqual(
            linked.read_bytes(),
            outputs["z-project"].encode("utf-8"),
        )
        self.assertTrue(stat.S_ISREG(linked.lstat().st_mode))
        self.assertEqual(
            linked_target.read_bytes(),
            b"user symlink target bytes\n",
        )
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {"a-project.md", "z-project.md"},
        )

    def test_directory_and_special_output_paths_are_preserved(self):
        output = GENERATED_NOTICE + "\n# replacement\n"
        cases = ("directory", "fifo")

        for case_name in cases:
            with self.subTest(case=case_name):
                target = self.root / "index" / case_name
                target.mkdir()
                existing = target / "demo-api.md"
                if case_name == "directory":
                    existing.mkdir()
                    marker = existing / "keep.txt"
                    marker.write_bytes(b"directory bytes\n")
                else:
                    os.mkfifo(existing)

                with self.assertRaises(knowledge_index.KnowledgeIndexError):
                    knowledge_index.write_all(
                        outputs={"demo-api": output},
                        data_root=self.root,
                        target=target,
                    )

                if case_name == "directory":
                    self.assertTrue(existing.is_dir())
                    self.assertEqual(marker.read_bytes(), b"directory bytes\n")
                else:
                    self.assertTrue(stat.S_ISFIFO(existing.lstat().st_mode))

    def test_output_path_swap_to_fifo_before_publish_preserves_fifo(self):
        target = self.root / "index" / "output-race"
        target.mkdir()
        existing = target / "demo-api.md"
        existing.write_text(
            GENERATED_NOTICE + "\n# previous\n",
            encoding="utf-8",
        )
        output = GENERATED_NOTICE + "\n# replacement\n"
        original_rename = personal_render._rename_entry_no_replace
        replaced = False

        def replace_before_stage(parent, source, destination):
            nonlocal replaced
            if source == existing.name and not replaced:
                existing.unlink()
                os.mkfifo(existing)
                replaced = True
            return original_rename(parent, source, destination)

        with mock.patch.object(
            personal_render,
            "_rename_entry_no_replace",
            side_effect=replace_before_stage,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError) as caught:
            knowledge_index.write_all(
                outputs={"demo-api": output},
                data_root=self.root,
                target=target,
            )

        self.assertTrue(replaced)
        self.assertNotIn(str(self.temporary), str(caught.exception))
        self.assertTrue(stat.S_ISFIFO(existing.lstat().st_mode))
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {"demo-api.md"},
        )

    def test_output_path_swap_to_regular_before_stage_preserves_replacement(self):
        target = self.root / "index" / "output-regular-race"
        target.mkdir()
        existing = target / "demo-api.md"
        existing.write_text(
            GENERATED_NOTICE + "\n# previous\n",
            encoding="utf-8",
        )
        replacement_bytes = b"user replacement bytes\n"
        replacement = self.temporary / "replacement-output.md"
        replacement.write_bytes(replacement_bytes)
        original_snapshot = personal_render._snapshot_output_entry
        replaced = False

        def snapshot_then_replace(parent, name):
            nonlocal replaced
            snapshot = original_snapshot(parent, name)
            if name == existing.name and not replaced:
                os.replace(replacement, existing)
                replaced = True
            return snapshot

        with mock.patch.object(
            personal_render,
            "_snapshot_output_entry",
            side_effect=snapshot_then_replace,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={
                    "demo-api": GENERATED_NOTICE + "\n# replacement\n"
                },
                data_root=self.root,
                target=target,
            )

        self.assertTrue(replaced)
        self.assertEqual(existing.read_bytes(), replacement_bytes)
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {"demo-api.md"},
        )

    def test_generator_owned_stale_index_is_removed_when_unchanged(self):
        target = self.root / "index"
        stale = target / "obsolete.md"
        stale.write_text(
            GENERATED_NOTICE + "\n# obsolete\n",
            encoding="utf-8",
        )
        output = GENERATED_NOTICE + "\n# current\n"

        knowledge_index.write_all(
            outputs={"demo-api": output},
            data_root=self.root,
            target=target,
        )

        self.assertFalse(stale.exists())
        self.assertEqual(
            {
                entry.name: entry.read_bytes()
                for entry in target.iterdir()
            },
            {"demo-api.md": output.encode("utf-8")},
        )

    def test_stale_index_removal_preserves_open_descriptor_mutation(self):
        target = self.root / "index"
        stale = target / "obsolete.md"
        stale.write_text(
            GENERATED_NOTICE + "\n# obsolete\n",
            encoding="utf-8",
        )
        user_bytes = b"user open-descriptor bytes\n"
        descriptor = os.open(stale, os.O_WRONLY)
        original_rename = personal_render._rename_entry_no_replace
        mutated = False
        output = GENERATED_NOTICE + "\n# current\n"

        def mutate_before_stale_removal(parent, source, destination):
            nonlocal mutated
            if source == stale.name and not mutated:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, user_bytes)
                os.ftruncate(descriptor, len(user_bytes))
                os.fsync(descriptor)
                mutated = True
            return original_rename(parent, source, destination)

        try:
            with mock.patch.object(
                personal_render,
                "_rename_entry_no_replace",
                side_effect=mutate_before_stale_removal,
            ), self.assertRaises(knowledge_index.KnowledgeIndexError):
                knowledge_index.write_all(
                    outputs={"demo-api": output},
                    data_root=self.root,
                    target=target,
                )
        finally:
            os.close(descriptor)

        self.assertTrue(mutated)
        self.assertEqual(stale.read_bytes(), user_bytes)
        self.assertEqual(
            (target / "demo-api.md").read_bytes(),
            output.encode("utf-8"),
        )
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {"demo-api.md", "obsolete.md"},
        )

    def test_stale_index_removal_preserves_path_replacement(self):
        target = self.root / "index"
        stale = target / "obsolete.md"
        stale.write_text(
            GENERATED_NOTICE + "\n# obsolete\n",
            encoding="utf-8",
        )
        user_bytes = b"user replacement bytes\n"
        replacement = self.temporary / "stale-replacement"
        replacement.write_bytes(user_bytes)
        original_rename = personal_render._rename_entry_no_replace
        replaced = False
        output = GENERATED_NOTICE + "\n# current\n"

        def replace_before_stale_removal(parent, source, destination):
            nonlocal replaced
            if source == stale.name and not replaced:
                os.replace(replacement, source, dst_dir_fd=parent)
                replaced = True
            return original_rename(parent, source, destination)

        with mock.patch.object(
            personal_render,
            "_rename_entry_no_replace",
            side_effect=replace_before_stale_removal,
        ), self.assertRaises(knowledge_index.KnowledgeIndexError):
            knowledge_index.write_all(
                outputs={"demo-api": output},
                data_root=self.root,
                target=target,
            )

        self.assertTrue(replaced)
        self.assertEqual(stale.read_bytes(), user_bytes)
        self.assertEqual(
            (target / "demo-api.md").read_bytes(),
            output.encode("utf-8"),
        )
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {"demo-api.md", "obsolete.md"},
        )

    def test_unknown_stale_symlink_and_directory_are_preserved(self):
        output = GENERATED_NOTICE + "\n# current\n"
        outside = self.temporary / "outside-stale.md"
        outside.write_bytes(b"user linked bytes\n")

        for case_name in ("symlink", "directory"):
            with self.subTest(case=case_name):
                target = self.root / "index" / ("stale-" + case_name)
                target.mkdir()
                stale = target / "obsolete.md"
                if case_name == "symlink":
                    stale.symlink_to(outside)
                else:
                    stale.mkdir()
                    (stale / "keep.txt").write_bytes(b"directory bytes\n")

                with self.assertRaises(knowledge_index.KnowledgeIndexError):
                    knowledge_index.write_all(
                        outputs={"demo-api": output},
                        data_root=self.root,
                        target=target,
                    )

                if case_name == "symlink":
                    self.assertTrue(stale.is_symlink())
                    self.assertEqual(outside.read_bytes(), b"user linked bytes\n")
                else:
                    self.assertTrue(stale.is_dir())
                    self.assertEqual(
                        (stale / "keep.txt").read_bytes(),
                        b"directory bytes\n",
                    )

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
        self.assertEqual(
            {entry.name for entry in target.iterdir()},
            {"_global.md", "demo-api.md"},
        )
        self.assertEqual(
            {
                entry.name
                for entry in target.parent.iterdir()
                if entry.name.startswith(".index-directory-")
            },
            set(),
        )


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

    def test_check_classifies_valid_legacy_artifacts_without_removing_them(self):
        self.write("lessons/demo-api/one.md", LESSON)
        target = self.root / "index"
        knowledge_index.write_all(data_root=self.root, target=target)
        current = target / "demo-api.md"
        current_data = current.read_bytes()
        current_mode = stat.S_IMODE(current.stat().st_mode)
        resolved_name = _legacy_artifact_name(
            "resolved",
            current.name,
            ".json",
            "b" * 24,
        )
        (target / resolved_name).write_bytes(
            _legacy_recovery_record_bytes(
                current.name,
                current_data,
                current_mode,
            )
        )
        for artifact_number, kind in enumerate(
            ("retired", "quarantine"),
            start=12,
        ):
            artifact_name = _legacy_artifact_name(
                kind,
                current.name,
                ".tmp",
                "{:x}".format(artifact_number) * 24,
            )
            (target / artifact_name).write_bytes(current_data)
            (target / artifact_name).with_suffix(".name").write_text(
                current.name + "\n",
                encoding="utf-8",
            )
        temporary = target / (".index-" + "e" * 24 + ".tmp")
        temporary.write_bytes(current_data)
        before = {
            entry.name: entry.read_bytes()
            for entry in target.iterdir()
        }

        self.assertEqual(knowledge_index.check(self.root, target), 0)

        self.assertEqual(
            {
                entry.name: entry.read_bytes()
                for entry in target.iterdir()
            },
            before,
        )

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
