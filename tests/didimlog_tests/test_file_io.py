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
