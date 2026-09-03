"""Best-effort notification for newer stable Didimlog releases."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import queue
from pathlib import Path
import re
import stat
import threading
import time
from typing import Callable, Mapping, TextIO
from urllib.request import urlopen

from didimlog.conditional_file import (
    read_optional_regular_file,
    write_regular_file_if_unchanged,
)
from didimlog.file_io import (
    UnsafePathError,
    open_child_directory,
    open_directory_path,
)


PYPI_URL = "https://pypi.org/pypi/didimlog/json"
REQUEST_TIMEOUT = 1.0
RESPONSE_MAX_BYTES = 1024 * 1024
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_CACHE_MAX_BYTES = 512
_DISABLE_ENVIRONMENT = "DIDIM_NO_UPDATE_CHECK"
_STABLE_VERSION = re.compile(
    r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$"
)
_FETCH_GUARD = threading.Lock()


@dataclass(frozen=True)
class _Cache:
    checked_at: int
    latest: str


def _parse_stable_version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    matched = _STABLE_VERSION.fullmatch(value)
    if matched is None:
        return None
    return tuple(int(part) for part in matched.groups())


def is_newer_stable(installed: str, latest: str) -> bool:
    """Return whether two strict stable versions show an available update."""
    installed_parts = _parse_stable_version(installed)
    latest_parts = _parse_stable_version(latest)
    return (
        installed_parts is not None
        and latest_parts is not None
        and latest_parts > installed_parts
    )


def _cache_file(
    environ: Mapping[str, str],
    home: Path,
) -> Path:
    configured = environ.get("XDG_CACHE_HOME")
    if configured:
        root = Path(configured)
        if not root.is_absolute() or ".." in root.parts:
            root = home / ".cache"
    else:
        root = home / ".cache"
    path = root / "didimlog" / "update.json"
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("update cache path is unsafe")
    return path


def _open_or_create_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts or not path.anchor:
        raise ValueError("update cache directory is unsafe")
    descriptor = open_directory_path(Path(path.anchor))
    try:
        for component in path.parts[1:]:
            try:
                child = open_child_directory(descriptor, component)
            except UnsafePathError:
                try:
                    os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    os.fsync(descriptor)
                else:
                    raise ValueError("update cache directory is unsafe") from None
                child = open_child_directory(descriptor, component)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _prepare_cache_parent(path: Path) -> None:
    descriptor = _open_or_create_directory(path.parent)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ValueError("update cache parent is unsafe")
    finally:
        os.close(descriptor)


def _cache_target_is_private(path: Path) -> bool:
    try:
        linked = os.lstat(path)
    except FileNotFoundError:
        return True
    return stat.S_ISREG(linked.st_mode) and not stat.S_IMODE(linked.st_mode) & 0o077


def _decode_cache(data: bytes | None) -> _Cache | None:
    if data is None:
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"checked_at", "latest"}:
        return None
    checked_at = value.get("checked_at")
    latest = value.get("latest")
    if (
        not isinstance(checked_at, int)
        or isinstance(checked_at, bool)
        or checked_at < 0
        or checked_at > 2**63 - 1
        or _parse_stable_version(latest) is None
    ):
        return None
    return _Cache(checked_at=checked_at, latest=latest)


def _fetch_latest(
    opener: Callable[..., object],
) -> str:
    if not _FETCH_GUARD.acquire(blocking=False):
        raise TimeoutError("PyPI request is already in progress")

    results: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def fetch() -> None:
        try:
            with opener(PYPI_URL, timeout=REQUEST_TIMEOUT) as response:
                data = response.read(RESPONSE_MAX_BYTES + 1)
            results.put((True, data))
        except Exception as error:
            results.put((False, error))
        finally:
            _FETCH_GUARD.release()

    worker = threading.Thread(
        target=fetch,
        name="didimlog-update-check",
        daemon=True,
    )
    try:
        worker.start()
    except BaseException:
        _FETCH_GUARD.release()
        raise
    try:
        succeeded, result = results.get(timeout=REQUEST_TIMEOUT)
    except queue.Empty as error:
        raise TimeoutError("PyPI request deadline exceeded") from error
    if not succeeded:
        if isinstance(result, BaseException):
            raise result
        raise RuntimeError("PyPI request failed without an exception")
    data = result
    if not isinstance(data, bytes) or len(data) > RESPONSE_MAX_BYTES:
        raise ValueError("PyPI response is invalid")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("PyPI response is invalid")
    info = value.get("info")
    if not isinstance(info, dict):
        raise ValueError("PyPI response is invalid")
    latest = info.get("version")
    if _parse_stable_version(latest) is None:
        raise ValueError("PyPI stable version is invalid")
    return latest


def _cache_bytes(checked_at: int, latest: str) -> bytes:
    return (
        json.dumps(
            {"checked_at": checked_at, "latest": latest},
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def automatic_update_notice(
    installed_version: str,
    *,
    stderr: TextIO,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    now: float | int | None = None,
    opener: Callable[..., object] | None = None,
) -> None:
    """Emit one best-effort notice without affecting the original command."""
    try:
        selected_environment = os.environ if environ is None else environ
        if selected_environment.get(_DISABLE_ENVIRONMENT) == "1":
            return

        timestamp = time.time() if now is None else now
        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(timestamp)
            or timestamp < 0
        ):
            return

        selected_home = Path.home() if home is None else Path(home)
        cache_file = _cache_file(selected_environment, selected_home)
        _prepare_cache_parent(cache_file)
        if not _cache_target_is_private(cache_file):
            return

        original = read_optional_regular_file(cache_file, _CACHE_MAX_BYTES)
        cached = _decode_cache(original)
        if cached is not None:
            elapsed = timestamp - cached.checked_at
            if 0 <= elapsed < CHECK_INTERVAL_SECONDS:
                return

        latest = _fetch_latest(urlopen if opener is None else opener)
        checked_at = int(timestamp)
        intended = _cache_bytes(checked_at, latest)
        if len(intended) > _CACHE_MAX_BYTES:
            return
        write_regular_file_if_unchanged(cache_file, original, intended)

        if is_newer_stable(installed_version, latest):
            stderr.write(
                "Didimlog {} 업데이트 가능 — uv tool upgrade didimlog\n".format(
                    latest
                )
            )
    except Exception:
        return
