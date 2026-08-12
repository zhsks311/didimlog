import hashlib
import importlib.resources
import tempfile
import unittest
from pathlib import Path

from didimlog.claude.resources import materialize_resources


RESOURCE_NAMES = (
    "KNOWLEDGE_USAGE.md",
    "LESSON_WRITING_RULES.md",
)


def packaged_resource_bytes():
    resource_root = importlib.resources.files("didimlog.resources.personal")
    return {
        name: resource_root.joinpath(name).read_bytes()
        for name in RESOURCE_NAMES
    }


class MaterializeResourcesTests(unittest.TestCase):
    def test_creates_exactly_the_two_packaged_markdown_resources(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            expected = packaged_resource_bytes()

            materialized = materialize_resources(config)

            expected_paths = tuple(config / "didimlog" / name for name in RESOURCE_NAMES)
            self.assertEqual(materialized, expected_paths)
            self.assertEqual(
                sorted(
                    path.relative_to(config / "didimlog").as_posix()
                    for path in (config / "didimlog").rglob("*")
                ),
                sorted(RESOURCE_NAMES),
            )
            for path in materialized:
                with self.subTest(path=path.name):
                    self.assertTrue(path.is_file())
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(path.read_bytes(), expected[path.name])

    def test_replaces_stale_managed_resource_bytes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            managed = home / ".claude" / "didimlog"
            managed.mkdir(parents=True)
            for name in RESOURCE_NAMES:
                (managed / name).write_bytes(b"stale managed resource\n")
            expected = packaged_resource_bytes()

            materialized = materialize_resources(home / ".claude")

            self.assertEqual(
                {path.name: path.read_bytes() for path in materialized},
                expected,
            )

    def test_matching_resources_are_left_unchanged_on_repeated_materialization(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            config.mkdir(parents=True)

            first = materialize_resources(config)
            before = {
                path.name: (
                    path.read_bytes(),
                    path.stat().st_ino,
                    path.stat().st_mtime_ns,
                )
                for path in first
            }
            second = materialize_resources(config)
            after = {
                path.name: (
                    path.read_bytes(),
                    path.stat().st_ino,
                    path.stat().st_mtime_ns,
                )
                for path in second
            }

            self.assertEqual(second, first)
            self.assertEqual(after, before)

    def test_materialized_resource_hashes_match_the_packaged_resources(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            expected = packaged_resource_bytes()

            materialized = materialize_resources(config)

            expected_hashes = {
                name: hashlib.sha256(content).hexdigest()
                for name, content in expected.items()
            }
            actual_hashes = {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in materialized
            }
            self.assertEqual(actual_hashes, expected_hashes)

    def test_does_not_copy_or_change_personal_lessons_docs_or_book(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            personal_files = {}
            for directory_name in ("lessons", "docs", "book"):
                personal_file = home / "knowledge" / directory_name / "private.md"
                content = f"private {directory_name} content\n".encode()
                personal_file.parent.mkdir(parents=True, exist_ok=True)
                personal_file.write_bytes(content)
                personal_files[personal_file] = content

            materialized = materialize_resources(config)

            self.assertEqual(
                {path.name for path in materialized},
                set(RESOURCE_NAMES),
            )
            for path, original in personal_files.items():
                with self.subTest(path=path):
                    self.assertEqual(path.read_bytes(), original)
            config_relative_paths = {
                path.relative_to(config).as_posix()
                for path in config.rglob("*")
            }
            for directory_name in ("lessons", "docs", "book"):
                with self.subTest(directory=directory_name):
                    self.assertNotIn(directory_name, config_relative_paths)
                    self.assertFalse(
                        any(
                            relative_path.startswith(f"{directory_name}/")
                            or f"/{directory_name}/" in relative_path
                            for relative_path in config_relative_paths
                        )
                    )


if __name__ == "__main__":
    unittest.main()
