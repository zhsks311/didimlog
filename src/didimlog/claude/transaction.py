"""설치한 일반 파일을 사용자 변경 없이 조건부로 되돌린다."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
from pathlib import Path

_ABSENT = "ABSENT"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class InstallJournal:
    """설치 전후 파일 상태를 기록하고 안전한 변경만 역순으로 복원한다."""

    def __init__(self, path: str | os.PathLike[str], reset: bool = False) -> None:
        self.path = Path(path)
        self.data: dict[str, object] = {"version": 1, "targets": {}}
        if self.path.exists() and not reset:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        if reset:
            self._save()

    def record_original(
        self,
        name: str,
        path: str | os.PathLike[str],
        original: bytes | None,
        backup: str | os.PathLike[str] | None,
    ) -> None:
        targets = self._targets()
        # Re-recording a name makes this its newest change for rollback ordering.
        targets.pop(name, None)
        targets[name] = {
            "path": str(path),
            "original": _ABSENT if original is None else _digest(original),
            "installed": None,
            "backup": str(backup) if backup is not None else None,
            "phase": "prepared",
        }
        self._save()

    def record_installed(self, name: str, data: bytes) -> None:
        target = self._targets()[name]
        path = Path(target["path"])
        target["installed"] = _digest(data)
        target["installed_parent"] = self._parent_identity(path)
        target["phase"] = "installed"
        self._save()

    def classify(self, name: str) -> str:
        target = self._targets()[name]
        current = self._current_digest(Path(target["path"]))
        if current is None:
            return "absent"
        if current is False:
            return "concurrent"
        if current == target["original"]:
            return "original"
        if current == target["installed"]:
            installed = self._installed_digest(target)
            return "installed" if installed == target["installed"] else "concurrent"
        return "concurrent"

    def rollback(self) -> None:
        for name in reversed(tuple(self._targets())):
            self._rollback_target(name)

    def _rollback_target(self, name: str) -> bool:
        target = self._targets()[name]
        parent_descriptor = self._open_installed_parent(target)
        if parent_descriptor is None:
            return False

        path = Path(target["path"])
        try:
            if self._digest_at(parent_descriptor, path.name) != target["installed"]:
                return False
            if target["original"] == _ABSENT:
                try:
                    os.unlink(path.name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    return False
                return True

            backup = target["backup"]
            if backup is None:
                return False
            original = Path(backup).read_bytes()
            if _digest(original) != target["original"]:
                return False

            descriptor, temporary_name = self._create_temporary(parent_descriptor)
            temporary: str | None = temporary_name
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(original)
                    handle.flush()
                    os.fsync(handle.fileno())
                if self._digest_at(parent_descriptor, path.name) != target["installed"]:
                    return False
                os.replace(
                    temporary_name,
                    path.name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                temporary = None
                return True
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=parent_descriptor)
                    except FileNotFoundError:
                        pass
        finally:
            os.close(parent_descriptor)

    @staticmethod
    def _parent_identity(path: Path) -> dict[str, int]:
        descriptor = InstallJournal._open_parent(path)
        try:
            info = os.fstat(descriptor)
            return {"device": info.st_dev, "inode": info.st_ino}
        finally:
            os.close(descriptor)

    @staticmethod
    def _open_parent(path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(path.parent, flags)

    @classmethod
    def _open_installed_parent(cls, target: dict[str, object]) -> int | None:
        expected = target.get("installed_parent")
        if not isinstance(expected, dict):
            return None
        try:
            descriptor = cls._open_parent(Path(target["path"]))
        except OSError:
            return None
        info = os.fstat(descriptor)
        if (
            info.st_dev != expected.get("device")
            or info.st_ino != expected.get("inode")
        ):
            os.close(descriptor)
            return None
        return descriptor

    @classmethod
    def _installed_digest(cls, target: dict[str, object]) -> str | None | bool:
        descriptor = cls._open_installed_parent(target)
        if descriptor is None:
            return False
        try:
            return cls._digest_at(descriptor, Path(target["path"]).name)
        finally:
            os.close(descriptor)

    @staticmethod
    def _digest_at(parent_descriptor: int, name: str) -> str | None | bool:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            return None
        except OSError:
            return False
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return False
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 64 * 1024):
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(descriptor)

    @staticmethod
    def _create_temporary(parent_descriptor: int) -> tuple[int, str]:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        for _ in range(100):
            name = ".rollback-" + secrets.token_hex(12)
            try:
                return os.open(name, flags, 0o600, dir_fd=parent_descriptor), name
            except FileExistsError:
                continue
        raise FileExistsError("could not allocate rollback temporary file")
    @staticmethod
    def _current_digest(path: Path) -> str | None | bool:
        if path.is_symlink():
            return False
        try:
            if not path.is_file():
                return None if not path.exists() else False
            return _digest(path.read_bytes())
        except (FileNotFoundError, IsADirectoryError):
            return None if not path.is_symlink() else False

    def _targets(self) -> dict[str, dict[str, object]]:
        targets = self.data["targets"]
        if not isinstance(targets, dict):
            raise ValueError("install journal targets must be an object")
        return targets

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".journal-", dir=self.path.parent
        )
        temporary: str | None = temporary_name
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
