import concurrent.futures
import contextlib
import errno
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from didimlog.personal import lesson_writing
from didimlog.personal.lesson import parse_lesson


GIT = shutil.which("git")
MAX_INPUT_BYTES = 64 * 1024


def valid_lesson(title="검증된 교훈", body="격리 테스트로 검증했다."):
    return """---
topic: lesson-writing
title: {title}
summary: 검증된 내용만 안전하게 저장한다
tags: [lesson, testing]
date: 2026-08-07
---
## 상황
일반 작업에서 재사용할 교훈을 확인했다.
## 교훈
완성된 문서만 create-only로 저장한다.
## 근거
{body}
""".format(title=title, body=body)


def lesson_with_encoded_size(size):
    text = valid_lesson()
    padding = size - len(text.encode("utf-8"))
    if padding < 0:
        raise ValueError("requested lesson size is too small")
    return text + ("a" * padding)


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


class LessonWritingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.data_root = self.temporary / "knowledge"
        for name in ("lessons", "docs", "book", "index"):
            (self.data_root / name).mkdir(parents=True)
        self.lessons = self.data_root / "lessons"

    def publish(self, slug, text, *, project="demo-api", root=None, cwd=None):
        return lesson_writing.publish_lesson(
            slug,
            text,
            project=project,
            root=self.lessons if root is None else root,
            cwd=cwd,
        )
    def test_symlinked_lessons_parent_cannot_redirect_publish(self):
        outside = self.temporary / "outside"
        outside_lessons = outside / "lessons"
        outside_lessons.mkdir(parents=True)
        linked_parent = self.temporary / "linked-knowledge"
        try:
            linked_parent.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest("directory symlinks unavailable: {}".format(error))

        with self.assertRaises(lesson_writing.LessonInvalid):
            self.publish(
                "escaped",
                valid_lesson(),
                root=linked_parent / "lessons",
            )

        self.assertFalse((outside_lessons / "demo-api" / "escaped.md").exists())


    def test_safe_lesson_is_created_with_exact_bytes_mode_and_relative_path(self):
        text = valid_lesson()

        relative = self.publish("safe", text)

        path = self.lessons / "demo-api" / "safe.md"
        self.assertEqual(relative, Path("lessons/demo-api/safe.md"))
        self.assertEqual(path.read_bytes(), text.encode("utf-8"))
        self.assertIsNotNone(parse_lesson(path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_successful_publish_refreshes_the_personal_index(self):
        self.publish("indexed", valid_lesson("저장 직후 색인한다"))

        index = self.data_root / "index" / "demo-api.md"
        self.assertTrue(index.is_file())
        output = index.read_text(encoding="utf-8")
        self.assertIn("저장 직후 색인한다", output)
        self.assertIn("`lessons/demo-api/indexed.md`", output)


    def test_publish_lesson_writes_through_linked_project_and_returns_logical_path(self):
        external = self.temporary / "external-lessons"
        external.mkdir()
        linked = self.lessons / "demo-api"
        linked.symlink_to(external, target_is_directory=True)

        result = self.publish("linked-rule", valid_lesson())

        self.assertEqual(result, Path("lessons/demo-api/linked-rule.md"))
        self.assertTrue((external / "linked-rule.md").is_file())
        index = self.data_root / "index" / "demo-api.md"
        output = index.read_text(encoding="utf-8")
        self.assertIn("lessons/demo-api/linked-rule.md", output)
        self.assertNotIn(str(external), output)

    def test_link_change_before_publish_leaves_no_lesson(self):
        external = self.temporary / "external-lessons"
        external.mkdir()
        replacement = self.temporary / "replacement-lessons"
        replacement.mkdir()
        linked = self.lessons / "demo-api"
        linked.symlink_to(external, target_is_directory=True)
        index = self.data_root / "index" / "demo-api.md"
        original_index = b"user-owned index bytes\r\n"
        index.write_bytes(original_index)
        original_write_all = lesson_writing._write_all

        def write_and_retarget(descriptor, data):
            original_write_all(descriptor, data)
            linked.unlink()
            linked.symlink_to(replacement, target_is_directory=True)

        with mock.patch.object(
            lesson_writing,
            "_write_all",
            side_effect=write_and_retarget,
        ):
            with self.assertRaisesRegex(
                lesson_writing.LessonInvalid,
                "^project lessons link changed during write$",
            ):
                self.publish("retargeted", valid_lesson())

        self.assertFalse((external / "retargeted.md").exists())
        self.assertFalse((replacement / "retargeted.md").exists())
        self.assertEqual(list(external.glob(".lesson-*.tmp")), [])
        self.assertEqual(list(replacement.glob(".lesson-*.tmp")), [])
        self.assertEqual(index.read_bytes(), original_index)

    def test_link_change_after_publish_rolls_back_unchanged_lesson(self):
        external = self.temporary / "external-lessons"
        external.mkdir()
        replacement = self.temporary / "replacement-lessons"
        replacement.mkdir()
        linked = self.lessons / "demo-api"
        linked.symlink_to(external, target_is_directory=True)
        index = self.data_root / "index" / "demo-api.md"
        original_index = b"user-owned index bytes\r\n"
        index.write_bytes(original_index)
        text = valid_lesson("공개 직후 연결 교체")
        original_link = os.link

        def link_and_retarget(source, destination, **kwargs):
            source_info = os.stat(
                source,
                dir_fd=kwargs["src_dir_fd"],
                follow_symlinks=False,
            )
            original_link(source, destination, **kwargs)
            if destination == "after-link.md":
                published_info = os.stat(
                    destination,
                    dir_fd=kwargs["dst_dir_fd"],
                    follow_symlinks=False,
                )
                self.assertEqual(
                    (published_info.st_dev, published_info.st_ino),
                    (source_info.st_dev, source_info.st_ino),
                )
                self.assertEqual(published_info.st_mode & 0o777, 0o600)
                self.assertEqual(
                    (external / destination).read_bytes(),
                    text.encode("utf-8"),
                )
                linked.unlink()
                linked.symlink_to(replacement, target_is_directory=True)

        with mock.patch.object(
            lesson_writing.os,
            "link",
            side_effect=link_and_retarget,
        ):
            with self.assertRaisesRegex(
                lesson_writing.LessonInvalid,
                "^project lessons link changed during write$",
            ):
                self.publish("after-link", text)

        self.assertFalse((external / "after-link.md").exists())
        self.assertFalse((replacement / "after-link.md").exists())
        self.assertEqual(list(external.glob(".lesson-*.tmp")), [])
        self.assertEqual(list(replacement.glob(".lesson-*.tmp")), [])
        self.assertEqual(index.read_bytes(), original_index)

    def test_recovery_reservation_failure_happens_before_publish(self):
        external = self.temporary / "external-lessons"
        external.mkdir()
        replacement = self.temporary / "replacement-lessons"
        replacement.mkdir()
        linked = self.lessons / "demo-api"
        linked.symlink_to(external, target_is_directory=True)
        index = self.data_root / "index" / "demo-api.md"
        original_index = b"user-owned index bytes\r\n"
        index.write_bytes(original_index)
        original_temporary_file = lesson_writing._temporary_file
        original_link = os.link
        temporary_calls = 0

        def temporary_then_exhausted(directory_descriptor):
            nonlocal temporary_calls
            temporary_calls += 1
            if temporary_calls == 2:
                raise lesson_writing.LessonError(
                    "unable to create lesson recovery file"
                ) from OSError(errno.ENOSPC, "no space left")
            return original_temporary_file(directory_descriptor)

        def link_and_retarget(source, destination, **kwargs):
            original_link(source, destination, **kwargs)
            if destination == "no-recovery.md":
                linked.unlink()
                linked.symlink_to(replacement, target_is_directory=True)

        with mock.patch.object(
            lesson_writing,
            "_temporary_file",
            side_effect=temporary_then_exhausted,
        ), mock.patch.object(
            lesson_writing.os,
            "link",
            side_effect=link_and_retarget,
        ), self.assertRaises(lesson_writing.LessonError):
            self.publish("no-recovery", valid_lesson())

        self.assertFalse((external / "no-recovery.md").exists())
        self.assertFalse((replacement / "no-recovery.md").exists())
        self.assertEqual(list(external.glob(".lesson-*.tmp")), [])
        self.assertEqual(list(replacement.glob(".lesson-*.tmp")), [])
        self.assertEqual(index.read_bytes(), original_index)

    def test_recovery_cleanup_error_retries_and_keeps_publish_successful(self):
        external = self.temporary / "external-lessons"
        external.mkdir()
        linked = self.lessons / "demo-api"
        linked.symlink_to(external, target_is_directory=True)
        original_unlink = os.unlink
        cleanup_failed_once = False

        def fail_first_recovery_cleanup(path, *args, **kwargs):
            nonlocal cleanup_failed_once
            if (
                not cleanup_failed_once
                and str(path).startswith(".lesson-")
                and str(path).endswith(".tmp")
            ):
                cleanup_failed_once = True
                raise OSError(errno.EIO, "temporary cleanup failed")
            return original_unlink(path, *args, **kwargs)

        errors = io.StringIO()
        with mock.patch.object(
            lesson_writing.os,
            "unlink",
            side_effect=fail_first_recovery_cleanup,
        ), contextlib.redirect_stderr(errors):
            result = self.publish("cleanup-retry", valid_lesson())

        self.assertTrue(cleanup_failed_once)
        self.assertEqual(
            result,
            Path("lessons/demo-api/cleanup-retry.md"),
        )
        published = external / "cleanup-retry.md"
        self.assertEqual(published.read_bytes(), valid_lesson().encode("utf-8"))
        index = self.data_root / "index" / "demo-api.md"
        self.assertIn(
            "lessons/demo-api/cleanup-retry.md",
            index.read_text(encoding="utf-8"),
        )
        self.assertEqual(list(external.glob(".lesson-*.tmp")), [])
        self.assertEqual(errors.getvalue(), "")

    def test_concurrent_in_place_change_after_publish_is_preserved(self):
        external = self.temporary / "external-lessons"
        external.mkdir()
        replacement = self.temporary / "replacement-lessons"
        replacement.mkdir()
        linked = self.lessons / "demo-api"
        linked.symlink_to(external, target_is_directory=True)
        index = self.data_root / "index" / "demo-api.md"
        original_index = b"user-owned index bytes\r\n"
        index.write_bytes(original_index)
        user_bytes = b"concurrent in-place user bytes\r\n"
        original_link = os.link
        published_once = False

        def link_change_and_retarget(source, destination, **kwargs):
            nonlocal published_once
            original_link(source, destination, **kwargs)
            if destination == "in-place.md" and not published_once:
                published_once = True
                published = external / destination
                published.write_bytes(user_bytes)
                published.chmod(0o640)
                linked.unlink()
                linked.symlink_to(replacement, target_is_directory=True)

        with mock.patch.object(
            lesson_writing.os,
            "link",
            side_effect=link_change_and_retarget,
        ):
            with self.assertRaisesRegex(
                lesson_writing.LessonInvalid,
                "^project lessons link changed during write$",
            ):
                self.publish("in-place", valid_lesson())

        published = external / "in-place.md"
        self.assertEqual(published.read_bytes(), user_bytes)
        self.assertEqual(published.stat().st_mode & 0o777, 0o640)
        self.assertFalse((replacement / "in-place.md").exists())
        self.assertEqual(list(external.glob(".lesson-*.tmp")), [])
        self.assertEqual(list(replacement.glob(".lesson-*.tmp")), [])
        self.assertEqual(index.read_bytes(), original_index)

    def test_concurrent_replacement_after_publish_is_preserved(self):
        external = self.temporary / "external-lessons"
        external.mkdir()
        replacement = self.temporary / "replacement-lessons"
        replacement.mkdir()
        linked = self.lessons / "demo-api"
        linked.symlink_to(external, target_is_directory=True)
        index = self.data_root / "index" / "demo-api.md"
        original_index = b"user-owned index bytes\r\n"
        index.write_bytes(original_index)
        user_bytes = b"concurrent replacement user bytes\r\n"
        original_link = os.link
        published_once = False

        def link_replace_and_retarget(source, destination, **kwargs):
            nonlocal published_once
            original_link(source, destination, **kwargs)
            if destination == "replaced.md" and not published_once:
                published_once = True
                published = external / destination
                published.unlink()
                published.write_bytes(user_bytes)
                published.chmod(0o640)
                linked.unlink()
                linked.symlink_to(replacement, target_is_directory=True)

        with mock.patch.object(
            lesson_writing.os,
            "link",
            side_effect=link_replace_and_retarget,
        ):
            with self.assertRaisesRegex(
                lesson_writing.LessonInvalid,
                "^project lessons link changed during write$",
            ):
                self.publish("replaced", valid_lesson())

        published = external / "replaced.md"
        self.assertEqual(published.read_bytes(), user_bytes)
        self.assertEqual(published.stat().st_mode & 0o777, 0o640)
        self.assertFalse((replacement / "replaced.md").exists())
        self.assertEqual(list(external.glob(".lesson-*.tmp")), [])
        self.assertEqual(list(replacement.glob(".lesson-*.tmp")), [])
        self.assertEqual(index.read_bytes(), original_index)

    def test_link_limit_does_not_hide_concurrent_replacement(self):
        external = self.temporary / "external-lessons"
        external.mkdir()
        replacement = self.temporary / "replacement-lessons"
        replacement.mkdir()
        linked = self.lessons / "demo-api"
        linked.symlink_to(external, target_is_directory=True)
        index = self.data_root / "index" / "demo-api.md"
        original_index = b"user-owned index bytes\r\n"
        index.write_bytes(original_index)
        user_bytes = b"replacement preserved without another hard link\r\n"
        original_link = os.link
        published_once = False

        def publish_then_exhaust_links(source, destination, **kwargs):
            nonlocal published_once
            if published_once:
                raise OSError(errno.EMLINK, "too many links")
            original_link(source, destination, **kwargs)
            published_once = True
            published = external / destination
            published.unlink()
            published.write_bytes(user_bytes)
            published.chmod(0o640)
            linked.unlink()
            linked.symlink_to(replacement, target_is_directory=True)

        with mock.patch.object(
            lesson_writing.os,
            "link",
            side_effect=publish_then_exhaust_links,
        ):
            with self.assertRaisesRegex(
                lesson_writing.LessonInvalid,
                "^project lessons link changed during write$",
            ):
                self.publish("link-limit", valid_lesson())

        published = external / "link-limit.md"
        self.assertEqual(published.read_bytes(), user_bytes)
        self.assertEqual(published.stat().st_mode & 0o777, 0o640)
        self.assertFalse((replacement / "link-limit.md").exists())
        self.assertEqual(list(external.glob(".lesson-*.tmp")), [])
        self.assertEqual(list(replacement.glob(".lesson-*.tmp")), [])
        self.assertEqual(index.read_bytes(), original_index)

    def test_explicit_global_project_is_supported_but_invalid_project_is_rejected(self):
        relative = self.publish(
            "global-rule",
            valid_lesson("전역 규칙"),
            project="_global",
        )

        self.assertEqual(relative, Path("lessons/_global/global-rule.md"))
        self.assertTrue((self.lessons / "_global" / "global-rule.md").is_file())

        with self.assertRaises(lesson_writing.LessonInvalid):
            self.publish("escape", valid_lesson(), project="../outside")
        self.assertFalse((self.data_root / "outside").exists())

    def test_omitted_root_uses_the_personal_lessons_directory(self):
        with mock.patch.object(
            lesson_writing,
            "lessons_dir",
            return_value=self.lessons,
        ):
            relative = lesson_writing.publish_lesson(
                "default-root",
                valid_lesson(),
                project="demo-api",
            )

        self.assertEqual(relative, Path("lessons/demo-api/default-root.md"))
        self.assertTrue(
            (self.lessons / "demo-api" / "default-root.md").is_file()
        )

    @unittest.skipUnless(GIT, "git is required for project discovery tests")
    def test_omitted_project_uses_the_real_git_root_basename_from_cwd(self):
        repository = self.temporary / "discovered-project"
        repository.mkdir()
        home = self.temporary / "home"
        home.mkdir()
        environment = isolated_git_environment(home, self.temporary)
        subprocess.run(
            [GIT, "-c", "init.defaultBranch=main", "init", "-q", str(repository)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
        nested = repository / "nested"
        nested.mkdir()

        with mock.patch.dict(os.environ, environment, clear=True):
            relative = self.publish(
                "from-cwd",
                valid_lesson(),
                project=None,
                cwd=nested,
            )

        self.assertEqual(
            relative,
            Path("lessons/discovered-project/from-cwd.md"),
        )
        self.assertTrue(
            (self.lessons / "discovered-project" / "from-cwd.md").is_file()
        )

    @unittest.skipUnless(GIT, "git is required for project discovery tests")
    def test_symlinked_source_directory_is_rejected_before_writing(self):
        repository = self.temporary / "real-project"
        repository.mkdir()
        home = self.temporary / "home"
        home.mkdir()
        environment = isolated_git_environment(home, self.temporary)
        subprocess.run(
            [GIT, "-c", "init.defaultBranch=main", "init", "-q", str(repository)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
        linked_repository = self.temporary / "linked-project"
        try:
            linked_repository.symlink_to(repository, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest("directory symlinks unavailable: {}".format(error))

        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(lesson_writing.LessonInvalid):
                self.publish(
                    "linked-source",
                    valid_lesson(),
                    project=None,
                    cwd=linked_repository,
                )

        self.assertEqual(list(self.lessons.rglob("*.md")), [])

    def test_slug_and_lesson_frontmatter_are_validated_before_any_write(self):
        invalid_slugs = (
            "",
            "-leading",
            "trailing-",
            "double--hyphen",
            "has_underscore",
            "../escape",
            "한글",
        )
        for slug in invalid_slugs:
            with self.subTest(slug=slug):
                with self.assertRaises(lesson_writing.LessonInvalid):
                    self.publish(slug, valid_lesson())

        with self.assertRaises(lesson_writing.LessonInvalid):
            self.publish("bad-text", "not frontmatter")

        self.assertEqual(list(self.lessons.rglob("*.md")), [])

    def test_input_limit_is_64_kib_in_encoded_utf8_bytes_and_inclusive(self):
        exact = lesson_with_encoded_size(MAX_INPUT_BYTES)

        self.publish("exact-limit", exact)

        exact_path = self.lessons / "demo-api" / "exact-limit.md"
        self.assertEqual(len(exact_path.read_bytes()), MAX_INPUT_BYTES)

        oversized = lesson_with_encoded_size(MAX_INPUT_BYTES + 1)
        with self.assertRaises(lesson_writing.LessonInvalid):
            self.publish("over-limit", oversized)
        self.assertFalse((self.lessons / "demo-api" / "over-limit.md").exists())

    def test_non_utf8_python_text_and_secret_content_leave_no_lesson(self):
        with self.assertRaises(lesson_writing.LessonInvalid):
            self.publish("invalid-utf8", valid_lesson(body="bad\ud800text"))

        secret = ("g" + "hp_") + "abcdefghijklmnopqrstuvwxyz123456"
        with self.assertRaises(lesson_writing.LessonSecret):
            self.publish("secret", valid_lesson(body="token=" + secret))

        self.assertEqual(list(self.lessons.rglob("*.md")), [])

    def test_crlf_input_is_canonicalized_to_lf_before_validation_and_storage(self):
        canonical = valid_lesson()
        crlf = canonical.replace("\n", "\r\n")

        self.publish("canonical-newlines", crlf)

        stored = self.lessons / "demo-api" / "canonical-newlines.md"
        self.assertEqual(stored.read_bytes(), canonical.encode("utf-8"))
        self.assertNotIn(b"\r", stored.read_bytes())

    def test_lessons_root_symlink_is_rejected(self):
        outside_root = self.temporary / "outside-root"
        outside_root.mkdir()
        linked_root = self.temporary / "linked-lessons"
        try:
            linked_root.symlink_to(outside_root, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            self.skipTest("directory symlinks unavailable: {}".format(error))

        with self.assertRaises(lesson_writing.LessonInvalid):
            self.publish("root-link", valid_lesson(), root=linked_root)
        self.assertEqual(list(outside_root.iterdir()), [])

    def test_existing_lesson_collision_preserves_every_existing_byte(self):
        project = self.lessons / "demo-api"
        project.mkdir()
        existing = project / "same.md"
        original = b"\xffexisting user bytes\r\n"
        existing.write_bytes(original)

        with self.assertRaises(lesson_writing.LessonExists):
            self.publish("same", valid_lesson("새 값"))

        self.assertEqual(existing.read_bytes(), original)
        self.assertEqual(list(project.glob(".lesson-*")), [])

    def test_concurrent_same_id_has_one_atomic_winner_and_no_temp_files(self):
        candidates = {
            "첫째": valid_lesson("첫째"),
            "둘째": valid_lesson("둘째"),
        }

        def attempt(item):
            title, text = item
            try:
                self.publish("shared", text)
                return "created", title
            except lesson_writing.LessonExists:
                return "exists", title

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, candidates.items()))

        self.assertCountEqual([status for status, _ in results], ["created", "exists"])
        final_path = self.lessons / "demo-api" / "shared.md"
        self.assertIn(
            final_path.read_bytes(),
            [text.encode("utf-8") for text in candidates.values()],
        )
        self.assertEqual(list(final_path.parent.glob(".lesson-*")), [])

    def test_index_failure_preserves_lesson_and_reports_stable_recovery_line(self):
        foreign = self.data_root / "index" / "foreign.txt"
        foreign.write_bytes(b"user-owned index bytes\r\n")
        text = valid_lesson("색인이 실패해도 남는 원문")
        errors = io.StringIO()

        with contextlib.redirect_stderr(errors):
            relative = self.publish("saved", text)

        lesson = self.lessons / "demo-api" / "saved.md"
        self.assertEqual(relative, Path("lessons/demo-api/saved.md"))
        self.assertEqual(lesson.read_bytes(), text.encode("utf-8"))
        self.assertEqual(foreign.read_bytes(), b"user-owned index bytes\r\n")
        self.assertEqual(
            errors.getvalue(),
            "KNOWLEDGE_INDEX_STALE: run didim index\n",
        )


if __name__ == "__main__":
    unittest.main()
