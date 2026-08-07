import os
import tempfile
import unittest
from pathlib import Path

from didimlog.locking import acquire_directory_lock


class DirectoryLockTests(unittest.TestCase):
    def test_acquire_creates_a_private_regular_lock_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            parent_descriptor = os.open(directory, os.O_RDONLY)
            try:
                lock_descriptor = acquire_directory_lock(parent_descriptor)
                try:
                    lock = directory / ".didimlog.lock"
                    self.assertTrue(lock.is_file())
                    self.assertFalse(lock.is_symlink())
                    self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
                finally:
                    os.close(lock_descriptor)
            finally:
                os.close(parent_descriptor)

    def test_acquire_rejects_a_symlink_lock_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "user-data"
            target.write_bytes(b"preserve me")
            lock = directory / ".didimlog.lock"
            try:
                lock.symlink_to(target.name)
            except (NotImplementedError, OSError) as error:
                self.skipTest("symlinks unavailable: {}".format(error))
            parent_descriptor = os.open(directory, os.O_RDONLY)
            try:
                with self.assertRaises(OSError):
                    acquire_directory_lock(parent_descriptor)
            finally:
                os.close(parent_descriptor)

            self.assertEqual(target.read_bytes(), b"preserve me")


if __name__ == "__main__":
    unittest.main()
