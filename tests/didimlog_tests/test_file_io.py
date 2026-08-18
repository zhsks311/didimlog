import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from didimlog import file_io

from didimlog.file_io import (
    UnsafePathError,
    open_child_directory,
    open_directory_path,
    read_regular_file_at,
    replace_regular_file_at_if_unchanged,
    write_all_and_sync,
)


def stale_revision(info):
    return mock.Mock(
        st_dev=info.st_dev,
        st_ino=info.st_ino,
        st_mode=info.st_mode,
        st_uid=info.st_uid,
        st_gid=info.st_gid,
        st_nlink=info.st_nlink,
        st_size=info.st_size,
        st_mtime_ns=info.st_mtime_ns,
        st_ctime_ns=info.st_ctime_ns - 1,
    )


class FileIoContractTests(unittest.TestCase):
    def test_write_all_and_sync_retries_short_positive_writes(self):
        real_write = os.write

        def short_write(descriptor, data):
            return real_write(descriptor, data[:2])

        with tempfile.TemporaryFile() as target, mock.patch.object(
            file_io.os,
            "write",
            side_effect=short_write,
        ) as write:
            write_all_and_sync(target.fileno(), b"abcdef")
            target.seek(0)
            self.assertEqual(target.read(), b"abcdef")

        self.assertEqual(write.call_count, 3)

    def test_write_all_and_sync_rejects_zero_write(self):
        with tempfile.TemporaryFile() as target, mock.patch.object(
            file_io.os,
            "write",
            return_value=0,
        ), mock.patch.object(file_io.os, "fsync") as sync:
            with self.assertRaises(OSError):
                write_all_and_sync(target.fileno(), b"data")

        sync.assert_not_called()

    def test_write_all_and_sync_writes_final_bytes_then_syncs(self):
        with tempfile.TemporaryFile() as target, mock.patch.object(
            file_io.os,
            "fsync",
            wraps=os.fsync,
        ) as sync:
            write_all_and_sync(target.fileno(), b"complete")
            target.seek(0)
            self.assertEqual(target.read(), b"complete")
            sync.assert_called_once_with(target.fileno())

    def test_read_is_bounded_to_maximum_plus_one_byte(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "record.md").write_bytes(b"abcdef")
            root_descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                self.assertEqual(
                    read_regular_file_at(root_descriptor, "record.md", 4),
                    b"abcde",
                )
            finally:
                os.close(root_descriptor)

    def test_file_inode_replacement_after_lstat_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            record = root / "record.md"
            record.write_bytes(b"expected")
            replacement = root / "replacement.md"
            replacement.write_bytes(b"external sentinel")
            linked = stale_revision(replacement.stat())
            root_descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            real_open = os.open

            def replace_before_open(path, flags, *args, **kwargs):
                if path == "record.md" and kwargs.get("dir_fd") == root_descriptor:
                    replacement.replace(record)
                return real_open(path, flags, *args, **kwargs)

            try:
                with (
                    mock.patch("didimlog.file_io.os.stat", return_value=linked),
                    mock.patch(
                        "didimlog.file_io.os.open",
                        side_effect=replace_before_open,
                    ),
                    self.assertRaises(UnsafePathError),
                ):
                    read_regular_file_at(root_descriptor, "record.md", 64)
            finally:
                os.close(root_descriptor)

    def test_directory_reused_inode_with_changed_revision_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            child = root / "child"
            replacement = root / "replacement"
            child.mkdir()
            replacement.mkdir()
            linked = stale_revision(replacement.stat())
            root_descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            real_open = os.open

            def replace_before_open(path, flags, *args, **kwargs):
                if path == "child" and kwargs.get("dir_fd") == root_descriptor:
                    child.rmdir()
                    replacement.replace(child)
                return real_open(path, flags, *args, **kwargs)

            try:
                with (
                    mock.patch("didimlog.file_io.os.stat", return_value=linked),
                    mock.patch(
                        "didimlog.file_io.os.open",
                        side_effect=replace_before_open,
                    ),
                    self.assertRaises(UnsafePathError),
                ):
                    open_child_directory(root_descriptor, "child")
            finally:
                os.close(root_descriptor)

    def test_directory_path_reused_inode_with_changed_revision_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            replacement = root / "replacement"
            target.mkdir()
            replacement.mkdir()
            linked = stale_revision(replacement.stat())
            real_open = os.open

            def replace_before_open(path, flags, *args, **kwargs):
                if path == str(target):
                    target.rmdir()
                    replacement.replace(target)
                return real_open(path, flags, *args, **kwargs)

            with (
                mock.patch("didimlog.file_io.os.lstat", return_value=linked),
                mock.patch(
                    "didimlog.file_io.os.open",
                    side_effect=replace_before_open,
                ),
                self.assertRaises(UnsafePathError),
            ):
                open_directory_path(target)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO is unavailable")
    def test_fifo_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            os.mkfifo(root / "pipe")
            root_descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with self.assertRaises(UnsafePathError):
                    read_regular_file_at(root_descriptor, "pipe", 64)
            finally:
                os.close(root_descriptor)

    def test_directory_inode_replacement_with_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            child = root / "child"
            outside = root / "outside"
            child.mkdir()
            outside.mkdir()
            (outside / "sentinel").write_bytes(b"external sentinel")
            root_descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            real_open = os.open

            def replace_before_open(path, flags, *args, **kwargs):
                if path == "child" and kwargs.get("dir_fd") == root_descriptor:
                    child.rmdir()
                    child.symlink_to(outside.name)
                return real_open(path, flags, *args, **kwargs)

            try:
                with mock.patch("didimlog.file_io.os.open", side_effect=replace_before_open):
                    with self.assertRaises(UnsafePathError):
                        open_child_directory(root_descriptor, "child")
            finally:
                os.close(root_descriptor)

    def test_first_directory_sync_failure_restores_original_and_syncs_rollback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.write_bytes(b"original")
            target.chmod(0o640)
            expected_info = target.stat()
            root_descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            real_fsync = os.fsync
            directory_sync_calls = 0
            rollback_sync_observed = False

            def fail_first_directory_sync(descriptor):
                nonlocal directory_sync_calls, rollback_sync_observed
                if descriptor == root_descriptor:
                    directory_sync_calls += 1
                    if directory_sync_calls == 1:
                        raise OSError("publication directory sync failed")
                    if directory_sync_calls == 2:
                        backups = [
                            entry
                            for entry in root.iterdir()
                            if entry.name.startswith(".didim-backup-")
                        ]
                        rollback_sync_observed = (
                            target.read_bytes() == b"original"
                            and len(backups) == 1
                            and backups[0].read_bytes() == b"original"
                        )
                return real_fsync(descriptor)

            try:
                with (
                    mock.patch.object(
                        file_io.os,
                        "fsync",
                        side_effect=fail_first_directory_sync,
                    ),
                    self.assertRaises(UnsafePathError),
                ):
                    replace_regular_file_at_if_unchanged(
                        root_descriptor,
                        target.name,
                        b"original",
                        b"managed",
                        0o640,
                        expected_info=expected_info,
                    )
            finally:
                os.close(root_descriptor)

            self.assertEqual(directory_sync_calls, 3)
            self.assertTrue(rollback_sync_observed)
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertEqual([entry.name for entry in root.iterdir()], ["target"])

    def test_first_directory_sync_failure_preserves_same_inode_concurrent_write(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.write_bytes(b"original")
            expected_info = target.stat()
            root_descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            real_fsync = os.fsync
            directory_sync_calls = 0
            write_inodes = []

            def write_target_then_fail_first_directory_sync(descriptor):
                nonlocal directory_sync_calls
                if descriptor == root_descriptor:
                    directory_sync_calls += 1
                    if directory_sync_calls == 1:
                        write_inodes.append(target.stat().st_ino)
                        target.write_bytes(b"latest")
                        write_inodes.append(target.stat().st_ino)
                        raise OSError("publication directory sync failed")
                return real_fsync(descriptor)

            try:
                with (
                    mock.patch.object(
                        file_io.os,
                        "fsync",
                        side_effect=write_target_then_fail_first_directory_sync,
                    ),
                    self.assertRaises(UnsafePathError),
                ):
                    replace_regular_file_at_if_unchanged(
                        root_descriptor,
                        target.name,
                        b"original",
                        b"managed",
                        0o600,
                        expected_info=expected_info,
                    )
            finally:
                os.close(root_descriptor)

            self.assertEqual(write_inodes[0], write_inodes[1])
            self.assertEqual(target.read_bytes(), b"latest")
            self.assertEqual([entry.name for entry in root.iterdir()], ["target"])

    def test_double_directory_sync_failure_retains_original_backup_for_indeterminate_recovery(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.write_bytes(b"original")
            expected_info = target.stat()
            root_descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            real_fsync = os.fsync
            directory_sync_calls = 0

            def fail_publication_and_rollback_sync(descriptor):
                nonlocal directory_sync_calls
                if descriptor == root_descriptor:
                    directory_sync_calls += 1
                    if directory_sync_calls <= 2:
                        raise OSError("directory sync failed")
                return real_fsync(descriptor)

            try:
                with (
                    mock.patch.object(
                        file_io.os,
                        "fsync",
                        side_effect=fail_publication_and_rollback_sync,
                    ),
                    self.assertRaises(UnsafePathError),
                ):
                    replace_regular_file_at_if_unchanged(
                        root_descriptor,
                        target.name,
                        b"original",
                        b"managed",
                        0o600,
                        expected_info=expected_info,
                    )
            finally:
                os.close(root_descriptor)

            backups = [
                entry
                for entry in root.iterdir()
                if entry.name.startswith(".didim-backup-")
            ]
            replacements = [
                entry
                for entry in root.iterdir()
                if entry.name.startswith(".didim-replacement-")
            ]
            original_is_preserved = (
                target.exists() and target.read_bytes() == b"original"
            ) or any(backup.read_bytes() == b"original" for backup in backups)

            self.assertEqual(directory_sync_calls, 2)
            self.assertTrue(original_is_preserved)
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"original")
            self.assertEqual(len(replacements), 1)
            self.assertEqual(replacements[0].read_bytes(), b"managed")

    def test_first_publication_sync_observes_original_backup(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.write_bytes(b"original")
            expected_info = target.stat()
            root_descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            real_fsync = os.fsync
            directory_sync_calls = 0
            backup_observed = False

            def observe_first_directory_sync(descriptor):
                nonlocal directory_sync_calls, backup_observed
                if descriptor == root_descriptor:
                    directory_sync_calls += 1
                    if directory_sync_calls == 1:
                        backups = [
                            entry
                            for entry in root.iterdir()
                            if entry.name.startswith(".didim-backup-")
                        ]
                        backup_observed = (
                            len(backups) == 1
                            and backups[0].read_bytes() == b"original"
                        )
                return real_fsync(descriptor)

            try:
                with mock.patch.object(
                    file_io.os,
                    "fsync",
                    side_effect=observe_first_directory_sync,
                ):
                    replaced = replace_regular_file_at_if_unchanged(
                        root_descriptor,
                        target.name,
                        b"original",
                        b"managed",
                        0o600,
                        expected_info=expected_info,
                    )
            finally:
                os.close(root_descriptor)

            self.assertTrue(replaced)
            self.assertTrue(backup_observed)
            self.assertEqual(target.read_bytes(), b"managed")
            self.assertEqual([entry.name for entry in root.iterdir()], ["target"])

    def test_cleanup_directory_sync_failure_keeps_committed_success(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            target.write_bytes(b"original")
            expected_info = target.stat()
            root_descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            real_fsync = os.fsync
            directory_sync_calls = 0

            def fail_cleanup_directory_sync(descriptor):
                nonlocal directory_sync_calls
                if descriptor == root_descriptor:
                    directory_sync_calls += 1
                    if directory_sync_calls == 2:
                        raise OSError("cleanup directory sync failed")
                return real_fsync(descriptor)

            try:
                with mock.patch.object(
                    file_io.os,
                    "fsync",
                    side_effect=fail_cleanup_directory_sync,
                ):
                    replaced = replace_regular_file_at_if_unchanged(
                        root_descriptor,
                        target.name,
                        b"original",
                        b"managed",
                        0o600,
                        expected_info=expected_info,
                    )
            finally:
                os.close(root_descriptor)

            self.assertTrue(replaced)
            self.assertEqual(directory_sync_calls, 2)
            self.assertEqual(target.read_bytes(), b"managed")
            self.assertEqual([entry.name for entry in root.iterdir()], ["target"])

    def test_concurrent_atomic_replace_before_publication_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            concurrent = root / "concurrent"
            target.write_bytes(b"original")
            concurrent.write_bytes(b"latest")
            expected_info = target.stat()
            concurrent_info = concurrent.stat()
            root_descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            real_link = os.link
            interleaving_occurred = False

            def replace_target_before_replacement_link(
                source_name,
                destination_name,
                *args,
                **kwargs,
            ):
                nonlocal interleaving_occurred
                if (
                    not interleaving_occurred
                    and source_name.startswith(".didim-replacement-")
                ):
                    concurrent.replace(target)
                    interleaving_occurred = True
                return real_link(source_name, destination_name, *args, **kwargs)

            try:
                with mock.patch.object(
                    file_io.os,
                    "link",
                    side_effect=replace_target_before_replacement_link,
                ):
                    replaced = replace_regular_file_at_if_unchanged(
                        root_descriptor,
                        target.name,
                        b"original",
                        b"managed",
                        0o600,
                        expected_info=expected_info,
                    )
            finally:
                os.close(root_descriptor)

            self.assertFalse(replaced)
            self.assertEqual(target.read_bytes(), b"latest")
            target_info = target.stat()
            self.assertEqual(
                (target_info.st_dev, target_info.st_ino),
                (concurrent_info.st_dev, concurrent_info.st_ino),
            )
            self.assertEqual([entry.name for entry in root.iterdir()], ["target"])

    def test_rollback_preserves_a_concurrent_replace_after_publish(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "target"
            concurrent = root / "concurrent"
            target.write_bytes(b"original")
            concurrent.write_bytes(b"latest")
            expected_info = target.stat()
            root_descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            real_read = file_io.read_regular_file_at_with_stat
            real_rename = os.rename
            read_calls = 0
            rename_calls = 0

            def force_rollback(parent_descriptor, name, maximum_bytes):
                nonlocal read_calls
                data, info = real_read(parent_descriptor, name, maximum_bytes)
                read_calls += 1
                if read_calls == 2:
                    return b"changed", info
                return data, info

            def install_latest_before_rollback(*args, **kwargs):
                nonlocal rename_calls
                rename_calls += 1
                if rename_calls == 2:
                    os.replace(concurrent, target)
                return real_rename(*args, **kwargs)

            try:
                with (
                    mock.patch.object(
                        file_io,
                        "read_regular_file_at_with_stat",
                        side_effect=force_rollback,
                    ),
                    mock.patch.object(
                        file_io.os,
                        "rename",
                        side_effect=install_latest_before_rollback,
                    ),
                ):
                    replaced = replace_regular_file_at_if_unchanged(
                        root_descriptor,
                        target.name,
                        b"original",
                        b"managed",
                        0o600,
                        expected_info=expected_info,
                    )
            finally:
                os.close(root_descriptor)

            self.assertFalse(replaced)
            self.assertEqual(target.read_bytes(), b"latest")
            self.assertEqual([entry.name for entry in root.iterdir()], ["target"])


if __name__ == "__main__":
    unittest.main()
