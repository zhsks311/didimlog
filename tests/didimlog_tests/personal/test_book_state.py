import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog import file_io
from didimlog.personal import book_state


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
