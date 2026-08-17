import errno
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog import file_io
from didimlog.personal import book_state
from didimlog.personal.paths import ProjectDirectoryError


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


def lesson_bytes(
    *,
    topic="kafka",
    title="검증된 교훈",
    summary="검증된 내용만 저장한다",
    date="2026-08-07",
    booked=None,
    body=b"## \xea\xb5\x90\xed\x9b\x88\n\xeb\xb3\xb8\xeb\xac\xb8\n",
):
    fields = [
        "topic: {}".format(topic),
        "title: {}".format(title),
        "summary: {}".format(summary),
        "tags: [{}]".format(topic),
        "date: {}".format(date),
    ]
    if booked is not None:
        fields.append("booked: {}".format(booked))
    return "---\n{}\n---\n".format("\n".join(fields)).encode("utf-8") + body


class BookStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.temporary_root = Path(self.temporary_directory.name)
        self.lessons_root = self.temporary_root / "lessons"
        (self.lessons_root / "app").mkdir(parents=True)
        (self.lessons_root / "other").mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_lesson(self, project, slug, data):
        path = self.lessons_root / project / "{}.md".format(slug)
        path.write_bytes(data)
        return path

    def test_candidates_use_the_explicit_project_and_exclude_booked_lessons(self):
        self.write_lesson(
            "app",
            "new",
            lesson_bytes(title="새 교훈"),
        )
        self.write_lesson(
            "app",
            "done",
            lesson_bytes(title="반영됨", booked="[kafka]"),
        )
        self.write_lesson(
            "app",
            "different-topic",
            lesson_bytes(topic="jpa", title="다른 주제"),
        )
        self.write_lesson(
            "other",
            "foreign",
            lesson_bytes(title="다른 프로젝트"),
        )
        missing_cwd = self.temporary_root / "does-not-exist"

        rows = book_state.candidates(
            project="app",
            root=self.lessons_root,
            cwd=missing_cwd,
        )

        self.assertEqual(
            [(row["id"], row["topic"]) for row in rows],
            [("different-topic", "jpa"), ("new", "kafka")],
        )
        self.assertTrue(all(Path(row["path"]).parent == self.lessons_root / "app" for row in rows))

    def test_candidates_read_linked_project_and_return_logical_paths(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        (external / "one.md").write_bytes(lesson_bytes())
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)

        rows = book_state.candidates(project="app", root=self.lessons_root)

        self.assertEqual([row["id"] for row in rows], ["one"])
        self.assertEqual(rows[0]["path"], str(logical / "one.md"))
        self.assertNotIn(str(external), rows[0]["path"])

    def test_candidates_stop_when_link_changes_after_snapshot(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        (external / "one.md").write_bytes(lesson_bytes())
        replacement = self.temporary_root / "replacement-lessons"
        replacement.mkdir()
        (replacement / "other.md").write_bytes(lesson_bytes(topic="jpa"))
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)
        real_parse = book_state._parse_lesson

        def parse_and_retarget(path, data):
            parsed = real_parse(path, data)
            logical.unlink()
            logical.symlink_to(replacement, target_is_directory=True)
            return parsed

        with mock.patch.object(
            book_state,
            "_parse_lesson",
            side_effect=parse_and_retarget,
        ), self.assertRaises(ProjectDirectoryError) as caught:
            book_state.candidates(project="app", root=self.lessons_root)

        self.assertEqual(caught.exception.logical, logical)
        self.assertEqual(
            caught.exception.reason,
            "project link changed during operation",
        )

    def test_candidates_pin_link_target_during_pathname_swap(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        original = lesson_bytes(title="원래 대상")
        (external / "one.md").write_bytes(original)
        replacement = self.temporary_root / "replacement-lessons"
        replacement.mkdir()
        replacement_bytes = lesson_bytes(topic="jpa", title="교체 대상")
        (replacement / "other.md").write_bytes(replacement_bytes)
        displaced = self.temporary_root / "displaced-lessons"
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)
        real_scan = book_state._lessons

        def scan_while_path_swapped(source):
            external.rename(displaced)
            replacement.rename(external)
            try:
                yield from real_scan(source)
            finally:
                external.rename(replacement)
                displaced.rename(external)

        with mock.patch.object(
            book_state,
            "_lessons",
            side_effect=scan_while_path_swapped,
        ):
            rows = book_state.candidates(project="app", root=self.lessons_root)

        self.assertEqual(
            [(row["id"], row["topic"], row["title"]) for row in rows],
            [("one", "kafka", "원래 대상")],
        )
        self.assertEqual(rows[0]["path"], str(logical / "one.md"))
        self.assertEqual((external / "one.md").read_bytes(), original)
        self.assertEqual(
            (replacement / "other.md").read_bytes(),
            replacement_bytes,
        )

    def test_candidates_open_and_close_one_pinned_descriptor(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        (external / "one.md").write_bytes(lesson_bytes())
        (external / "two.md").write_bytes(lesson_bytes(topic="jpa"))
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)
        real_open = file_io.open_directory_path
        opened = []

        def track_open(path):
            descriptor = real_open(path)
            opened.append(descriptor)
            return descriptor

        with mock.patch.object(
            file_io,
            "open_directory_path",
            side_effect=track_open,
        ):
            rows = book_state.candidates(project="app", root=self.lessons_root)

        self.assertEqual([row["id"] for row in rows], ["one", "two"])
        self.assertEqual(len(opened), 1)
        with self.assertRaises(OSError) as caught:
            os.fstat(opened[0])
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_mark_booked_updates_linked_project_with_logical_result(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        original = lesson_bytes()
        external_path = external / "one.md"
        external_path.write_bytes(original)
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)

        result = book_state.mark_booked(
            ["one"],
            project="app",
            root=self.lessons_root,
        )

        self.assertEqual(result["marked"], [str(logical / "one.md")])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(
            external_path.read_bytes(),
            original.replace(b"\n---\n", b"\nbooked: [kafka]\n---\n", 1),
        )

    def test_mark_booked_reuses_one_project_descriptor_for_multiple_lessons(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        first_original = lesson_bytes(title="첫 번째")
        second_original = lesson_bytes(title="두 번째")
        first_path = external / "one.md"
        second_path = external / "two.md"
        first_path.write_bytes(first_original)
        second_path.write_bytes(second_original)
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)
        real_open = file_io.open_directory_path
        open_calls = 0

        def fail_on_second_open(path):
            nonlocal open_calls
            open_calls += 1
            if open_calls > 1:
                raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
            return real_open(path)

        with mock.patch.object(
            file_io,
            "open_directory_path",
            side_effect=fail_on_second_open,
        ):
            result = book_state.mark_booked(
                ["one", "two"],
                project="app",
                root=self.lessons_root,
            )

        self.assertEqual(open_calls, 1)
        self.assertEqual(
            result,
            {
                "marked": [
                    str(logical / "one.md"),
                    str(logical / "two.md"),
                ],
                "skipped": [],
            },
        )
        self.assertIn(b"booked: [kafka]", first_path.read_bytes())
        self.assertIn(b"booked: [kafka]", second_path.read_bytes())

    def test_mark_booked_skips_target_replaced_before_descriptor_open(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        original = lesson_bytes(title="검증된 원본")
        (external / "one.md").write_bytes(original)
        displaced = self.temporary_root / "displaced-lessons"
        replacement = lesson_bytes(title="교체 대상")
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)
        real_open = file_io.open_directory_path

        def replace_target_before_open(path):
            external.rename(displaced)
            external.mkdir()
            (external / "one.md").write_bytes(replacement)
            return real_open(path)

        with mock.patch.object(
            file_io,
            "open_directory_path",
            side_effect=replace_target_before_open,
        ):
            result = book_state.mark_booked(
                ["one"],
                project="app",
                root=self.lessons_root,
            )

        self.assertEqual(result, {"marked": [], "skipped": ["one"]})
        self.assertEqual((displaced / "one.md").read_bytes(), original)
        self.assertEqual((external / "one.md").read_bytes(), replacement)

    def test_mark_booked_skips_link_retargeted_after_descriptor_open(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        original = lesson_bytes(title="검증된 원본")
        external_path = external / "one.md"
        external_path.write_bytes(original)
        replacement = self.temporary_root / "replacement-lessons"
        replacement.mkdir()
        replacement_bytes = lesson_bytes(title="교체 대상")
        replacement_path = replacement / "one.md"
        replacement_path.write_bytes(replacement_bytes)
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)
        real_open = file_io.open_directory_path

        def open_and_retarget(path):
            descriptor = real_open(path)
            logical.unlink()
            logical.symlink_to(replacement, target_is_directory=True)
            return descriptor

        with mock.patch.object(
            file_io,
            "open_directory_path",
            side_effect=open_and_retarget,
        ):
            result = book_state.mark_booked(
                ["one"],
                project="app",
                root=self.lessons_root,
            )

        self.assertEqual(result, {"marked": [], "skipped": ["one"]})
        self.assertEqual(external_path.read_bytes(), original)
        self.assertEqual(replacement_path.read_bytes(), replacement_bytes)

    def test_mark_booked_rolls_back_when_link_retargets_at_publish(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        original = lesson_bytes(title="검증된 원본")
        external_path = external / "one.md"
        external_path.write_bytes(original)
        replacement = self.temporary_root / "replacement-lessons"
        external_path.chmod(0o640)
        replacement.mkdir()
        replacement_bytes = lesson_bytes(topic="jpa", title="교체 대상")
        replacement_path = replacement / "one.md"
        replacement_path.write_bytes(replacement_bytes)
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)
        real_publish = file_io.replace_regular_file_at_if_unchanged_with_ownership
        publish_calls = 0

        def publish_and_retarget(*args, **kwargs):
            nonlocal publish_calls
            publication = real_publish(*args, **kwargs)
            publish_calls += 1
            if publish_calls == 1 and publication is not None:
                logical.unlink()
                logical.symlink_to(replacement, target_is_directory=True)
            return publication

        with mock.patch.object(
            file_io,
            "replace_regular_file_at_if_unchanged_with_ownership",
            side_effect=publish_and_retarget,
        ):
            result = book_state.mark_booked(
                ["one"],
                project="app",
                root=self.lessons_root,
            )

        self.assertEqual(result, {"marked": [], "skipped": ["one"]})
        self.assertEqual(external_path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(external_path.stat().st_mode), 0o640)
        self.assertEqual(replacement_path.read_bytes(), replacement_bytes)

    def test_mark_booked_preserves_concurrent_change_after_publish_retarget(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        original = lesson_bytes(title="검증된 원본")
        external_path = external / "one.md"
        external_path.write_bytes(original)
        replacement = self.temporary_root / "replacement-lessons"
        replacement.mkdir()
        replacement_bytes = lesson_bytes(topic="jpa", title="교체 대상")
        replacement_path = replacement / "one.md"
        replacement_path.write_bytes(replacement_bytes)
        concurrent = lesson_bytes(title="사용자 최신 변경")
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)
        real_publish = file_io.replace_regular_file_at_if_unchanged_with_ownership
        publish_calls = 0

        def publish_retarget_and_save(*args, **kwargs):
            nonlocal publish_calls
            publication = real_publish(*args, **kwargs)
            publish_calls += 1
            if publish_calls == 1 and publication is not None:
                logical.unlink()
                logical.symlink_to(replacement, target_is_directory=True)
                external_path.write_bytes(concurrent)
            return publication

        with mock.patch.object(
            file_io,
            "replace_regular_file_at_if_unchanged_with_ownership",
            side_effect=publish_retarget_and_save,
        ):
            result = book_state.mark_booked(
                ["one"],
                project="app",
                root=self.lessons_root,
            )

        self.assertEqual(result, {"marked": [], "skipped": ["one"]})
        self.assertEqual(external_path.read_bytes(), concurrent)
        self.assertEqual(replacement_path.read_bytes(), replacement_bytes)

    def test_mark_booked_skips_when_retarget_rollback_fails(self):
        external = self.temporary_root / "external-lessons"
        external.mkdir()
        original = lesson_bytes(title="검증된 원본")
        external_path = external / "one.md"
        external_path.write_bytes(original)
        replacement = self.temporary_root / "replacement-lessons"
        replacement.mkdir()
        replacement_bytes = lesson_bytes(topic="jpa", title="교체 대상")
        replacement_path = replacement / "one.md"
        replacement_path.write_bytes(replacement_bytes)
        logical = self.lessons_root / "app"
        logical.rmdir()
        logical.symlink_to(external, target_is_directory=True)
        real_publish = file_io.replace_regular_file_at_if_unchanged_with_ownership
        publish_calls = 0

        def publish_then_fail_rollback(*args, **kwargs):
            nonlocal publish_calls
            publish_calls += 1
            if publish_calls == 2:
                return None
            publication = real_publish(*args, **kwargs)
            if publication is not None:
                logical.unlink()
                logical.symlink_to(replacement, target_is_directory=True)
            return publication

        with mock.patch.object(
            file_io,
            "replace_regular_file_at_if_unchanged_with_ownership",
            side_effect=publish_then_fail_rollback,
        ):
            result = book_state.mark_booked(
                ["one"],
                project="app",
                root=self.lessons_root,
            )

        self.assertEqual(result, {"marked": [], "skipped": ["one"]})
        self.assertIn(b"booked: [kafka]", external_path.read_bytes())
        self.assertEqual(replacement_path.read_bytes(), replacement_bytes)

    def test_mark_booked_inserts_one_field_and_is_idempotent(self):
        body = "## 교훈\n첫 줄  \n마지막 줄".encode("utf-8")
        original = lesson_bytes(body=body)
        path = self.write_lesson("app", "new", original)
        expected = original.replace(b"\n---\n", b"\nbooked: [kafka]\n---\n", 1)

        first = book_state.mark_booked(
            ["new"],
            project="app",
            root=self.lessons_root,
        )
        after_first = path.read_bytes()
        second = book_state.mark_booked(
            ["new"],
            project="app",
            root=self.lessons_root,
        )

        self.assertEqual(first["skipped"], [])
        self.assertEqual(second["skipped"], [])
        self.assertEqual(after_first, expected)
        self.assertEqual(path.read_bytes(), expected)
        self.assertEqual(path.read_bytes().count(b"booked:"), 1)
        self.assertEqual(
            book_state.candidates(project="app", root=self.lessons_root),
            [],
        )

    def test_mark_booked_canonicalizes_only_the_booked_value_and_preserves_body_bytes(self):
        body = "## 상황\n\n본문의 공백은 유지한다.  \n끝".encode("utf-8")
        original = lesson_bytes(booked="[zeta, alpha]", body=body)
        path = self.write_lesson("app", "new", original)
        expected = original.replace(
            b"booked: [zeta, alpha]",
            b"booked: [alpha, kafka, zeta]",
            1,
        )

        book_state.mark_booked(
            ["new"],
            project="app",
            root=self.lessons_root,
        )

        self.assertEqual(path.read_bytes(), expected)
        self.assertTrue(path.read_bytes().endswith(body))

    def test_missing_selected_project_slug_does_not_fall_through_to_another_project(self):
        original = lesson_bytes()
        foreign = self.write_lesson("other", "foreign", original)

        result = book_state.mark_booked(
            ["foreign"],
            project="app",
            root=self.lessons_root,
        )

        self.assertEqual(result["marked"], [])
        self.assertEqual(result["skipped"], ["foreign"])
        self.assertEqual(foreign.read_bytes(), original)

    def test_invalid_topic_is_rejected_without_rewriting_the_lesson(self):
        original = lesson_bytes(topic="has space")
        path = self.write_lesson("app", "invalid-topic", original)

        with self.assertRaises(ValueError):
            book_state.candidates(project="app", root=self.lessons_root)
        with self.assertRaises(ValueError):
            book_state.mark_booked(
                ["invalid-topic"],
                project="app",
                root=self.lessons_root,
            )

        self.assertEqual(path.read_bytes(), original)

    def test_invalid_booked_value_is_rejected_without_rewriting_the_lesson(self):
        original = lesson_bytes(booked="[kafka, kafka]")
        path = self.write_lesson("app", "invalid-booked", original)

        with self.assertRaises(ValueError):
            book_state.candidates(project="app", root=self.lessons_root)
        with self.assertRaises(ValueError):
            book_state.mark_booked(
                ["invalid-booked"],
                project="app",
                root=self.lessons_root,
            )

        self.assertEqual(path.read_bytes(), original)

    def test_change_after_final_recheck_is_preserved(self):
        original = lesson_bytes()
        concurrent = lesson_bytes(title="사용자 최신 변경")
        path = self.write_lesson("app", "new", original)
        real_read = file_io.read_regular_file_at_with_stat
        calls = 0

        def save_while_original_is_moved(parent_descriptor, name, maximum_bytes):
            nonlocal calls
            result = real_read(parent_descriptor, name, maximum_bytes)
            calls += 1
            if calls == 1:
                path.write_bytes(concurrent)
            return result

        with mock.patch.object(
            file_io,
            "read_regular_file_at_with_stat",
            side_effect=save_while_original_is_moved,
        ):
            result = book_state.mark_booked(
                ["new"],
                project="app",
                root=self.lessons_root,
            )

        self.assertEqual(result["marked"], [])
        self.assertEqual(result["skipped"], ["new"])
        self.assertEqual(path.read_bytes(), concurrent)


@unittest.skipUnless(GIT, "git is required for current-project discovery tests")
class CurrentProjectBookStateTests(unittest.TestCase):
    def test_candidates_discover_the_current_project_from_git(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            home = temporary_root / "home"
            home.mkdir()
            environment = isolated_git_environment(home, temporary_root)
            repository = temporary_root / "current-app"
            repository.mkdir()
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
            lessons_root = temporary_root / "lessons"
            current_project = lessons_root / "current-app"
            current_project.mkdir(parents=True)
            (current_project / "current.md").write_bytes(lesson_bytes())
            other_project = lessons_root / "other-app"
            other_project.mkdir()
            (other_project / "foreign.md").write_bytes(lesson_bytes())

            with mock.patch.dict(os.environ, environment, clear=True):
                rows = book_state.candidates(root=lessons_root, cwd=nested)

            self.assertEqual([row["id"] for row in rows], ["current"])


if __name__ == "__main__":
    unittest.main()
