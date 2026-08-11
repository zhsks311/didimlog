import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog import conditional_file
from didimlog.conditional_file import (
    read_optional_regular_file,
    write_regular_file_if_unchanged,
)


class ConditionalReadTests(unittest.TestCase):
    def test_missing_final_file_returns_none(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "missing"

            self.assertIsNone(read_optional_regular_file(target, 64))

    def test_regular_file_returns_exact_bytes_at_size_limit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            target.write_bytes(b"exact")

            self.assertEqual(read_optional_regular_file(target, 5), b"exact")

    def test_file_larger_than_size_limit_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            target.write_bytes(b"too large")

            with self.assertRaises(ValueError):
                read_optional_regular_file(target, 3)

    def test_missing_direct_parent_is_an_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "missing" / "target"

            with self.assertRaises(ValueError):
                read_optional_regular_file(target, 64)

    def test_symlink_parent_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            actual_parent = root / "actual"
            actual_parent.mkdir()
            (actual_parent / "target").write_bytes(b"user bytes")
            linked_parent = root / "linked"
            linked_parent.symlink_to(actual_parent, target_is_directory=True)

            with self.assertRaises(ValueError):
                read_optional_regular_file(linked_parent / "target", 64)

    def test_non_directory_parent_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory) / "not-a-directory"
            parent.write_bytes(b"user bytes")

            with self.assertRaises(ValueError):
                read_optional_regular_file(parent / "target", 64)

    def test_symlink_final_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destination = root / "destination"
            destination.write_bytes(b"user bytes")
            target = root / "target"
            target.symlink_to(destination)

            with self.assertRaises(ValueError):
                read_optional_regular_file(target, 64)

    def test_non_regular_final_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            target.mkdir()

            with self.assertRaises(ValueError):
                read_optional_regular_file(target, 64)

    def test_relative_and_parent_escaping_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            invalid_paths = (Path("target"), root / "child" / ".." / "target")

            for target in invalid_paths:
                with self.subTest(target=target), self.assertRaises(ValueError):
                    read_optional_regular_file(target, 64)


class ConditionalWriteTests(unittest.TestCase):
    def test_absent_target_is_created_only_when_still_absent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"

            write_regular_file_if_unchanged(target, None, b"managed\n")

            self.assertEqual(target.read_bytes(), b"managed\n")

    def test_absent_no_op_checks_absence_without_creating_a_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"

            write_regular_file_if_unchanged(target, None, None)

            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_existing_target_replacement_preserves_mode_and_uses_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            original = b"user bytes\n"
            target.write_bytes(original)
            target.chmod(0o640)

            write_regular_file_if_unchanged(target, original, b"managed bytes\n")

            self.assertEqual(target.read_bytes(), b"managed bytes\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_existing_no_op_preserves_bytes_inode_and_mtime(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            original = b"unchanged\n"
            target.write_bytes(original)
            before = target.stat()

            write_regular_file_if_unchanged(target, original, original)

            after = target.stat()
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(
                (after.st_dev, after.st_ino),
                (before.st_dev, before.st_ino),
            )
            self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_file_created_after_absent_plan_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            concurrent = b"created by user\n"
            target.write_bytes(concurrent)

            with self.assertRaises(ValueError):
                write_regular_file_if_unchanged(target, None, b"managed\n")

            self.assertEqual(target.read_bytes(), concurrent)

    def test_file_changed_after_existing_plan_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            concurrent = b"changed by user\n"
            target.write_bytes(concurrent)

            with self.assertRaises(ValueError):
                write_regular_file_if_unchanged(
                    target,
                    b"bytes seen during planning\n",
                    b"managed\n",
                )

            self.assertEqual(target.read_bytes(), concurrent)

    def test_file_deleted_after_existing_plan_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"

            with self.assertRaises(ValueError):
                write_regular_file_if_unchanged(target, b"deleted\n", b"managed\n")

            self.assertFalse(target.exists())

    def test_change_after_final_recheck_is_preserved_without_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            original = b"planned\n"
            concurrent = b"saved during publish\n"
            target.write_bytes(original)
            real_replace = conditional_file.replace_regular_file_at_if_unchanged

            def change_before_replace(*args, **kwargs):
                target.write_bytes(concurrent)
                return real_replace(*args, **kwargs)

            with (
                mock.patch.object(
                    conditional_file,
                    "replace_regular_file_at_if_unchanged",
                    side_effect=change_before_replace,
                ),
                self.assertRaises(ValueError),
            ):
                write_regular_file_if_unchanged(target, original, b"managed\n")

            self.assertEqual(target.read_bytes(), concurrent)
            self.assertEqual(
                [entry.name for entry in target.parent.iterdir()],
                ["target"],
            )

    def test_creation_race_is_preserved_without_temporary_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            concurrent = b"created during publish\n"
            real_link = os.link

            def create_before_link(*args, **kwargs):
                target.write_bytes(concurrent)
                return real_link(*args, **kwargs)

            with (
                mock.patch.object(
                    conditional_file.os,
                    "link",
                    side_effect=create_before_link,
                ),
                self.assertRaises(ValueError),
            ):
                write_regular_file_if_unchanged(target, None, b"managed\n")

            self.assertEqual(target.read_bytes(), concurrent)
            self.assertEqual(
                [entry.name for entry in target.parent.iterdir()],
                ["target"],
            )

    def test_temporary_unlink_failure_preserves_created_target_and_closes_fd(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            real_temporary_file = conditional_file._temporary_file
            real_unlink = os.unlink
            temporary_descriptor = None
            failed = False

            def capture_temporary_descriptor(*args, **kwargs):
                nonlocal temporary_descriptor
                result = real_temporary_file(*args, **kwargs)
                if temporary_descriptor is None:
                    temporary_descriptor = result[1]
                return result

            def fail_first_temporary_unlink(path, *args, **kwargs):
                nonlocal failed
                if (
                    not failed
                    and isinstance(path, str)
                    and path.startswith(".didimlog-")
                    and path.endswith(".tmp")
                ):
                    failed = True
                    raise OSError("unlink failed at /private/user/path")
                return real_unlink(path, *args, **kwargs)

            with (
                mock.patch.object(
                    conditional_file,
                    "_temporary_file",
                    side_effect=capture_temporary_descriptor,
                ),
                mock.patch.object(
                    conditional_file.os,
                    "unlink",
                    side_effect=fail_first_temporary_unlink,
                ),
                self.assertRaises(ValueError) as raised,
            ):
                write_regular_file_if_unchanged(target, None, b"managed\n")

            self.assertIsNone(raised.exception.__cause__)
            self.assertEqual(
                str(raised.exception),
                "target could not be written atomically",
            )
            self.assertEqual(target.read_bytes(), b"managed\n")
            self.assertEqual(
                [entry.name for entry in target.parent.iterdir()],
                ["target"],
            )
            self.assertIsNotNone(temporary_descriptor)
            with self.assertRaises(OSError):
                os.fstat(temporary_descriptor)

    def test_parent_fsync_failure_preserves_created_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            real_fsync = os.fsync
            failed = False

            def fail_first_directory_fsync(descriptor):
                nonlocal failed
                if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    failed = True
                    raise OSError("fsync failed at /private/user/path")
                return real_fsync(descriptor)

            with (
                mock.patch.object(
                    conditional_file.os,
                    "fsync",
                    side_effect=fail_first_directory_fsync,
                ),
                self.assertRaises(ValueError) as raised,
            ):
                write_regular_file_if_unchanged(target, None, b"managed\n")

            self.assertEqual(
                str(raised.exception),
                "target could not be written atomically",
            )
            self.assertEqual(target.read_bytes(), b"managed\n")
            self.assertEqual(
                [entry.name for entry in target.parent.iterdir()],
                ["target"],
            )

    def test_parent_fsync_failure_preserves_same_inode_concurrent_write(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            concurrent = b"concurrent user bytes\n"
            real_fsync = os.fsync
            failed = False
            write_inodes = []

            def write_target_before_directory_fsync(descriptor):
                nonlocal failed
                if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    failed = True
                    write_inodes.append(target.stat().st_ino)
                    target.write_bytes(concurrent)
                    write_inodes.append(target.stat().st_ino)
                    raise OSError("fsync failed at /private/user/path")
                return real_fsync(descriptor)

            with (
                mock.patch.object(
                    conditional_file.os,
                    "fsync",
                    side_effect=write_target_before_directory_fsync,
                ),
                self.assertRaises(ValueError) as raised,
            ):
                write_regular_file_if_unchanged(target, None, b"managed\n")

            self.assertEqual(
                str(raised.exception),
                "target could not be written atomically",
            )
            self.assertEqual(target.read_bytes(), concurrent)
            self.assertEqual(write_inodes[0], write_inodes[1])
            self.assertEqual(
                [entry.name for entry in root.iterdir()],
                ["target"],
            )

    def test_intended_none_is_not_a_delete_operation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "target"
            original = b"user bytes\n"
            target.write_bytes(original)

            with self.assertRaises(ValueError):
                write_regular_file_if_unchanged(target, original, None)

            self.assertEqual(target.read_bytes(), original)

    def test_symlink_target_is_refused_without_changing_destination(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            destination = root / "destination"
            destination.write_bytes(b"user bytes\n")
            target = root / "target"
            target.symlink_to(destination)

            with self.assertRaises(ValueError):
                write_regular_file_if_unchanged(
                    target,
                    destination.read_bytes(),
                    b"managed\n",
                )

            self.assertTrue(target.is_symlink())
            self.assertEqual(destination.read_bytes(), b"user bytes\n")


if __name__ == "__main__":
    unittest.main()
