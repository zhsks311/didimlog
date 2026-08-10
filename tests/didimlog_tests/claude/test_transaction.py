import tempfile
import unittest
from pathlib import Path

from didimlog.claude.transaction import InstallJournal


class InstallJournalTests(unittest.TestCase):
    def test_classifies_absent_original_installed_and_concurrent_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = InstallJournal(root / "install-journal.json")

            absent = root / "didimlog" / "KNOWLEDGE_USAGE.md"
            journal.record_original("resource", absent, None, None)
            self.assertEqual(journal.classify("resource"), "absent")

            target = root / "CLAUDE.md"
            backup = root / "CLAUDE.md.backup"
            target.write_bytes(b"user rules\n")
            backup.write_bytes(b"user rules\n")
            journal.record_original("claude-md", target, b"user rules\n", backup)
            self.assertEqual(journal.classify("claude-md"), "original")

            target.write_bytes(b"user rules\n# didimlog\n")
            journal.record_installed("claude-md", b"user rules\n# didimlog\n")
            self.assertEqual(journal.classify("claude-md"), "installed")

            target.write_bytes(b"user changed this concurrently\n")
            self.assertEqual(journal.classify("claude-md"), "concurrent")

    def test_rollback_restores_original_when_installed_bytes_still_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "settings.json"
            backup = root / "settings.json.backup"
            target.write_bytes(b'{"user": true}\n')
            backup.write_bytes(b'{"user": true}\n')
            journal = InstallJournal(root / "install-journal.json")
            journal.record_original(
                "settings", target, b'{"user": true}\n', backup
            )

            installed = b'{"user": true, "didimlog": true}\n'
            target.write_bytes(installed)
            journal.record_installed("settings", installed)
            target.unlink()
            target.write_bytes(installed)

            journal.rollback()

            self.assertEqual(target.read_bytes(), b'{"user": true}\n')

    def test_rollback_preserves_concurrent_user_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "CLAUDE.md"
            backup = root / "CLAUDE.md.backup"
            target.write_bytes(b"before\n")
            backup.write_bytes(b"before\n")
            journal = InstallJournal(root / "install-journal.json")
            journal.record_original("claude-md", target, b"before\n", backup)
            target.write_bytes(b"installed\n")
            journal.record_installed("claude-md", b"installed\n")

            target.write_bytes(b"user edit after install\n")
            journal.rollback()

            self.assertEqual(target.read_bytes(), b"user edit after install\n")

    def test_rollback_removes_absent_target_only_when_installed_bytes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = root / "didimlog"
            resources.mkdir()
            unchanged = resources / "KNOWLEDGE_USAGE.md"
            edited = resources / "LESSON_WRITING_RULES.md"
            journal = InstallJournal(root / "install-journal.json")

            journal.record_original("usage-resource", unchanged, None, None)
            unchanged.write_bytes(b"managed usage\n")
            journal.record_installed("usage-resource", b"managed usage\n")

            journal.record_original("rules-resource", edited, None, None)
            edited.write_bytes(b"managed rules\n")
            journal.record_installed("rules-resource", b"managed rules\n")
            edited.write_bytes(b"user customized rules\n")

            journal.rollback()

            self.assertFalse(unchanged.exists())
            self.assertEqual(edited.read_bytes(), b"user customized rules\n")

    def test_rollback_treats_replaced_symlink_parent_as_concurrent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed_parent = root / "config" / "didimlog"
            managed_parent.mkdir(parents=True)
            target = managed_parent / "KNOWLEDGE_USAGE.md"
            installed = b"managed usage\n"
            journal = InstallJournal(root / "install-journal.json")
            journal.record_original("usage-resource", target, None, None)
            target.write_bytes(installed)
            journal.record_installed("usage-resource", installed)

            target.unlink()
            managed_parent.rmdir()
            outside = root / "outside"
            outside.mkdir()
            outside_target = outside / target.name
            outside_target.write_bytes(installed)
            managed_parent.symlink_to(outside, target_is_directory=True)

            self.assertEqual(journal.classify("usage-resource"), "concurrent")
            journal.rollback()

            self.assertEqual(outside_target.read_bytes(), installed)

    def test_rollback_replays_changes_in_reverse_recording_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "CLAUDE.md"
            first_backup = root / "first.backup"
            second_backup = root / "second.backup"
            target.write_bytes(b"original\n")
            first_backup.write_bytes(b"original\n")
            journal = InstallJournal(root / "install-journal.json")

            journal.record_original(
                "first-change", target, b"original\n", first_backup
            )
            target.write_bytes(b"after first\n")
            journal.record_installed("first-change", b"after first\n")

            second_backup.write_bytes(b"after first\n")
            journal.record_original(
                "second-change", target, b"after first\n", second_backup
            )
            target.write_bytes(b"after second\n")
            journal.record_installed("second-change", b"after second\n")

            journal.rollback()

            self.assertEqual(target.read_bytes(), b"original\n")

    def test_rollback_continues_after_one_target_backup_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = InstallJournal(root / "install-journal.json")

            good_target = root / "CLAUDE.md"
            good_backup = root / "CLAUDE.md.backup"
            good_target.write_bytes(b"good original\n")
            good_backup.write_bytes(b"good original\n")
            journal.record_original(
                "good", good_target, b"good original\n", good_backup
            )
            good_target.write_bytes(b"good installed\n")
            journal.record_installed("good", b"good installed\n")

            bad_target = root / "settings.json"
            bad_backup = root / "settings.json.backup"
            bad_target.write_bytes(b"bad original\n")
            bad_backup.write_bytes(b"bad original\n")
            journal.record_original(
                "bad", bad_target, b"bad original\n", bad_backup
            )
            bad_target.write_bytes(b"bad installed\n")
            journal.record_installed("bad", b"bad installed\n")
            bad_backup.unlink()

            journal.rollback()

            self.assertEqual(bad_target.read_bytes(), b"bad installed\n")
            self.assertEqual(good_target.read_bytes(), b"good original\n")


if __name__ == "__main__":
    unittest.main()
