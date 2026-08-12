import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.personal import rules_document


EXPECTED_USER_RULES = """# 내 규칙

여기에 모든 프로젝트에서 항상 지킬 개인 규칙만 적는다.
이 파일은 설치 프로그램이 덮어쓰거나 생성 index를 자동으로 불러오지 않는다.
""".encode("utf-8")


class UserRulesDocumentTests(unittest.TestCase):
    def test_first_create_writes_exact_user_header_without_generated_index_import(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            target = home / "knowledge" / "MY-RULES.md"
            target.parent.mkdir(parents=True)

            created = rules_document.create_user_rules(home=home)

            self.assertEqual(created, target)
            self.assertEqual(target.read_bytes(), EXPECTED_USER_RULES)
            self.assertNotIn(b"@~/knowledge/index", target.read_bytes())

    def test_existing_regular_file_preserves_bytes_and_inode(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            target = home / "knowledge" / "MY-RULES.md"
            target.parent.mkdir(parents=True)
            user_bytes = "# 사용자가 고친 규칙\n\n직접 쓴 문장\n".encode("utf-8")
            target.write_bytes(user_bytes)
            before = target.stat()

            returned = rules_document.create_user_rules(home=home)

            after = target.stat()
            self.assertEqual(returned, target)
            self.assertEqual(target.read_bytes(), user_bytes)
            self.assertEqual(
                (after.st_dev, after.st_ino),
                (before.st_dev, before.st_ino),
            )

    def test_file_appearing_during_creation_is_preserved_and_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            target = home / "knowledge" / "MY-RULES.md"
            target.parent.mkdir(parents=True)
            user_bytes = b"user-created-during-setup\n"
            appeared = {}
            real_link = os.link

            def appear_then_link(source, destination):
                destination_path = Path(destination)
                destination_path.write_bytes(user_bytes)
                info = destination_path.stat()
                appeared["identity"] = (info.st_dev, info.st_ino)
                return real_link(source, destination)

            with mock.patch.object(
                rules_document.os,
                "link",
                side_effect=appear_then_link,
            ):
                with self.assertRaises(
                    rules_document.RulesConcurrentModification
                ):
                    rules_document.create_user_rules(home=home)

            after = target.stat()
            self.assertEqual(target.read_bytes(), user_bytes)
            self.assertEqual(
                (after.st_dev, after.st_ino),
                appeared["identity"],
            )

    def test_final_symlink_is_refused_without_changing_its_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            knowledge = home / "knowledge"
            knowledge.mkdir(parents=True)
            user_file = root / "user-owned.md"
            user_bytes = b"user-owned\n"
            user_file.write_bytes(user_bytes)
            target = knowledge / "MY-RULES.md"
            target.symlink_to(user_file)

            with self.assertRaises(rules_document.RulesDocumentError):
                rules_document.create_user_rules(home=home)

            self.assertTrue(target.is_symlink())
            self.assertEqual(user_file.read_bytes(), user_bytes)

    def test_parent_symlink_is_refused_without_writing_through_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            home.mkdir()
            user_directory = root / "user-knowledge"
            user_directory.mkdir()
            knowledge = home / "knowledge"
            knowledge.symlink_to(user_directory, target_is_directory=True)

            with self.assertRaises(rules_document.RulesDocumentError):
                rules_document.create_user_rules(home=home)

            self.assertTrue(knowledge.is_symlink())
            self.assertFalse((user_directory / "MY-RULES.md").exists())

    def test_creation_links_synced_temporary_inode_then_syncs_parent_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            target = home / "knowledge" / "MY-RULES.md"
            target.parent.mkdir(parents=True)
            events = []
            linked = {}
            real_fsync = os.fsync
            real_link = os.link

            def recording_fsync(descriptor):
                descriptor_mode = os.fstat(descriptor).st_mode
                if stat.S_ISDIR(descriptor_mode):
                    events.append("directory-fsync")
                else:
                    events.append("file-fsync")
                return real_fsync(descriptor)

            def recording_link(source, destination):
                source_path = Path(source)
                info = source_path.stat()
                linked["source"] = source_path
                linked["identity"] = (info.st_dev, info.st_ino)
                events.append("link")
                return real_link(source, destination)

            with mock.patch.object(
                rules_document.os,
                "fsync",
                side_effect=recording_fsync,
            ), mock.patch.object(
                rules_document.os,
                "link",
                side_effect=recording_link,
            ):
                returned = rules_document.create_user_rules(home=home)

            target_info = target.stat()
            self.assertEqual(returned, target)
            self.assertEqual(
                (target_info.st_dev, target_info.st_ino),
                linked["identity"],
            )
            self.assertFalse(linked["source"].exists())
            self.assertLess(events.index("file-fsync"), events.index("link"))
            self.assertLess(events.index("link"), events.index("directory-fsync"))


if __name__ == "__main__":
    unittest.main()
