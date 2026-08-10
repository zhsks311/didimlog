import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from didimlog.locking import acquire_directory_lock, path_lock


def _hold_directory_lock(path, acquired, release):
    directory_descriptor = os.open(path, os.O_RDONLY)
    try:
        lock_descriptor = acquire_directory_lock(directory_descriptor)
        try:
            acquired.set()
            release.wait(5)
        finally:
            os.close(lock_descriptor)
    finally:
        os.close(directory_descriptor)


def _hold_path_lock(path, acquired, release):
    with path_lock(path):
        acquired.set()
        release.wait(5)

class DirectoryLockTests(unittest.TestCase):
    def test_acquire_locks_the_directory_inode_without_creating_an_artifact(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            parent_descriptor = os.open(directory, os.O_RDONLY)
            try:
                lock_descriptor = acquire_directory_lock(parent_descriptor)
                try:
                    parent = os.fstat(parent_descriptor)
                    locked = os.fstat(lock_descriptor)
                    self.assertEqual(
                        (locked.st_dev, locked.st_ino),
                        (parent.st_dev, parent.st_ino),
                    )
                    self.assertEqual(list(directory.iterdir()), [])
                finally:
                    os.close(lock_descriptor)
            finally:
                os.close(parent_descriptor)

    def test_same_directory_serializes_and_another_directory_does_not_wait(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_directory = root / "first"
            second_directory = root / "second"
            first_directory.mkdir()
            second_directory.mkdir()

            first_acquired = context.Event()
            release_first = context.Event()
            first = context.Process(
                target=_hold_directory_lock,
                args=(first_directory, first_acquired, release_first),
            )
            first.start()
            self.assertTrue(first_acquired.wait(5))

            same_acquired = context.Event()
            release_same = context.Event()
            same = context.Process(
                target=_hold_directory_lock,
                args=(first_directory, same_acquired, release_same),
            )
            same.start()
            self.assertFalse(same_acquired.wait(0.2))

            other_acquired = context.Event()
            release_other = context.Event()
            other = context.Process(
                target=_hold_directory_lock,
                args=(second_directory, other_acquired, release_other),
            )
            other.start()
            self.assertTrue(other_acquired.wait(5))

            release_other.set()
            other.join(5)
            self.assertEqual(other.exitcode, 0)

            release_first.set()
            first.join(5)
            self.assertEqual(first.exitcode, 0)
            self.assertTrue(same_acquired.wait(5))
            release_same.set()
            same.join(5)
            self.assertEqual(same.exitcode, 0)

    def test_path_lock_serializes_a_replacement_inode_in_the_same_namespace(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            target = parent / "knowledge"
            displaced = parent / "knowledge.old"
            target.mkdir()

            first_acquired = context.Event()
            release_first = context.Event()
            first = context.Process(
                target=_hold_path_lock,
                args=(target, first_acquired, release_first),
            )
            first.start()
            self.assertTrue(first_acquired.wait(5))

            target.rename(displaced)
            target.mkdir()

            replacement_acquired = context.Event()
            release_replacement = context.Event()
            replacement = context.Process(
                target=_hold_path_lock,
                args=(target, replacement_acquired, release_replacement),
            )
            replacement.start()
            self.assertFalse(replacement_acquired.wait(0.2))

            release_first.set()
            first.join(5)
            self.assertEqual(first.exitcode, 0)
            self.assertTrue(replacement_acquired.wait(5))
            release_replacement.set()
            replacement.join(5)
            self.assertEqual(replacement.exitcode, 0)


    def test_process_termination_releases_the_directory_lock(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temporary_directory:
            acquired = context.Event()
            release = context.Event()
            holder = context.Process(
                target=_hold_directory_lock,
                args=(Path(temporary_directory), acquired, release),
            )
            holder.start()
            self.assertTrue(acquired.wait(5))
            holder.terminate()
            holder.join(5)
            self.assertFalse(holder.is_alive())

            next_acquired = context.Event()
            release_next = context.Event()
            follower = context.Process(
                target=_hold_directory_lock,
                args=(Path(temporary_directory), next_acquired, release_next),
            )
            follower.start()
            self.assertTrue(next_acquired.wait(5))
            release_next.set()
            follower.join(5)
            self.assertEqual(follower.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
