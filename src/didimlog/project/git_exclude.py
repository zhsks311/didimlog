"""Plan and conditionally apply Didimlog's project-local Git exclusion."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from didimlog.conditional_file import (
    read_optional_regular_file,
    write_regular_file_if_unchanged,
)
from didimlog.errors import DidimError, EXIT_GIT


_GIT_TIMEOUT_SECONDS = 5
_MAXIMUM_EXCLUDE_BYTES = 1024 * 1024
_START = b"# DIDIMLOG:START project-knowledge"
_RULE = b"/knowledge/"
_END = b"# DIDIMLOG:END project-knowledge"
_START_TOKEN = b"DIDIMLOG:START project-knowledge"
_END_TOKEN = b"DIDIMLOG:END project-knowledge"
_VALID_MODES = frozenset(("local", "shared"))
_GIT_REPOSITORY_ENVIRONMENT = frozenset(
    (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    )
)


@dataclass(frozen=True)
class GitExcludePlan:
    project_root: Path
    path: Path
    mode: str
    original: bytes | None
    intended: bytes | None
    changes: tuple[str, ...]
    notices: tuple[str, ...]


def _error(token: str, help_text: str) -> DidimError:
    return DidimError(token, exit_code=EXIT_GIT, help_text=help_text)


def _git_unavailable() -> DidimError:
    return _error(
        "PROJECT_EXCLUDE_GIT_UNAVAILABLE",
        "Git 저장소와 로컬 제외 설정을 확인한 뒤 다시 시도하세요.",
    )


def _unsafe() -> DidimError:
    return _error(
        "PROJECT_EXCLUDE_UNSAFE",
        "Git 로컬 제외 파일과 바로 위 폴더가 일반 파일과 실제 폴더인지 확인하세요.",
    )


def _markers_invalid() -> DidimError:
    return _error(
        "PROJECT_EXCLUDE_MARKERS_INVALID",
        "Didimlog 관리 표시를 직접 고치지 말고 올바른 관리 블록 하나만 남기세요.",
    )


def _changed() -> DidimError:
    return _error(
        "PROJECT_EXCLUDE_CHANGED",
        "계획 뒤 Git 로컬 제외 설정이 바뀌었습니다. 새 계획을 만든 뒤 다시 시도하세요.",
    )


def _conflict() -> DidimError:
    return _error(
        "PROJECT_EXCLUDE_CONFLICT",
        "knowledge 폴더를 다시 포함하는 Git 규칙을 정리한 뒤 다시 시도하세요.",
    )


def _tracked() -> DidimError:
    return _error(
        "PROJECT_KNOWLEDGE_TRACKED",
        "먼저 Git에서 knowledge 폴더의 추적 항목을 직접 정리한 뒤 다시 시도하세요.",
    )


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in _GIT_REPOSITORY_ENVIRONMENT:
        environment.pop(name, None)
    return environment


def _run_git(
    cwd: Path,
    arguments: tuple[str, ...],
    *,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(cwd),
            env=_git_environment(),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise _git_unavailable() from error
    if result.returncode not in allowed_returncodes:
        raise _git_unavailable()
    return result


def _strict_path(output: bytes) -> Path:
    try:
        text = output.decode("utf-8")
    except UnicodeError as error:
        raise _git_unavailable() from error
    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    if not text or "\n" in text or "\r" in text or "\x00" in text:
        raise _git_unavailable()
    path = Path(text)
    if not path.is_absolute():
        raise _git_unavailable()
    return path


def _same_file(left: Path, right: Path) -> bool:
    try:
        left_info = left.stat()
        right_info = right.stat()
    except OSError:
        return False
    return (
        left_info.st_dev == right_info.st_dev
        and left_info.st_ino == right_info.st_ino
    )


def _source_has_git_marker(source: Path) -> bool:
    try:
        source_info = source.lstat()
    except OSError as error:
        raise _git_unavailable() from error
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
        raise _git_unavailable()

    for candidate in (source, *source.parents):
        try:
            marker_info = (candidate / ".git").lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise _git_unavailable() from error
        if stat.S_ISLNK(marker_info.st_mode) or not (
            stat.S_ISDIR(marker_info.st_mode)
            or stat.S_ISREG(marker_info.st_mode)
        ):
            raise _git_unavailable()
        return True
    return False


def _root_candidate(source: Path, root: Path) -> Path | None:
    try:
        root_info = root.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return None
    return next(
        (
            candidate
            for candidate in (source, *source.parents)
            if _same_file(candidate, root)
        ),
        None,
    )


def discover_project_for_setup(cwd) -> Path | None:
    """Return the containing Git work-tree root, or ``None`` outside Git."""
    try:
        source = Path.cwd() if cwd is None else Path(cwd)
        source = source.absolute()
        marker_present = _source_has_git_marker(source)
    except (OSError, TypeError, ValueError) as error:
        raise _git_unavailable() from error

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(source),
            env=_git_environment(),
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        if marker_present:
            raise _git_unavailable() from error
        return None

    if result.returncode != 0:
        if marker_present:
            raise _git_unavailable()
        return None
    try:
        root = _strict_path(result.stdout)
    except DidimError:
        raise
    candidate = _root_candidate(source, root)
    if candidate is None:
        raise _git_unavailable()
    return candidate


def _validated_project_root(project_root: Path) -> Path:
    try:
        selected = Path(project_root)
    except (TypeError, ValueError) as error:
        raise _git_unavailable() from error
    if not selected.is_absolute():
        raise _git_unavailable()
    discovered = discover_project_for_setup(selected)
    if discovered is None or not _same_file(discovered, selected):
        raise _git_unavailable()
    return discovered


def _git_exclude_path(project_root: Path) -> Path:
    result = _run_git(
        project_root,
        (
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/exclude",
        ),
    )
    return _strict_path(result.stdout)


def _preflight(path: Path) -> None:
    if not path.is_absolute():
        raise _unsafe()
    try:
        parent_info = path.parent.lstat()
    except OSError as error:
        raise _unsafe() from error
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise _unsafe()
    try:
        target_info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise _unsafe() from error
    if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISREG(target_info.st_mode):
        raise _unsafe()


def _read_exclude(path: Path) -> bytes | None:
    _preflight(path)
    try:
        return read_optional_regular_file(path, _MAXIMUM_EXCLUDE_BYTES)
    except (OSError, ValueError) as error:
        raise _unsafe() from error


def _block(newline: bytes, *, final_newline: bool = True) -> bytes:
    content = newline.join((_START, _RULE, _END))
    return content + newline if final_newline else content


def _all_occurrences(content: bytes, needle: bytes) -> list[int]:
    offsets: list[int] = []
    offset = 0
    while True:
        found = content.find(needle, offset)
        if found < 0:
            return offsets
        offsets.append(found)
        offset = found + 1


def _find_exact_blocks(content: bytes) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    for newline in (b"\n", b"\r\n"):
        complete = _block(newline)
        for start in _all_occurrences(content, complete):
            if start == 0 or content[start - 1 : start] == b"\n":
                matches.append((start, start + len(complete)))

        unterminated = _block(newline, final_newline=False)
        if content.endswith(unterminated):
            start = len(content) - len(unterminated)
            if (start == 0 or content[start - 1 : start] == b"\n") and not any(
                existing_start == start for existing_start, _ in matches
            ):
                matches.append((start, len(content)))
    return sorted(set(matches))


def _remove_managed_block(content: bytes) -> tuple[bytes, bool]:
    matches = _find_exact_blocks(content)
    if len(matches) > 1:
        raise _markers_invalid()
    if not matches:
        if _START_TOKEN in content or _END_TOKEN in content:
            raise _markers_invalid()
        return content, False

    start, end = matches[0]
    remainder = content[:start] + content[end:]
    if _START_TOKEN in remainder or _END_TOKEN in remainder:
        raise _markers_invalid()
    return remainder, True


def _transform(original: bytes | None, mode: str) -> tuple[bytes | None, tuple[str, ...]]:
    if mode not in _VALID_MODES:
        raise ValueError("mode must be 'local' or 'shared'")
    if original is None:
        if mode == "shared":
            return None, ()
        return _block(b"\n"), ("knowledge 폴더를 Git 로컬 제외에 추가",)

    without_block, has_block = _remove_managed_block(original)
    if mode == "shared":
        if not has_block:
            return original, ()
        return without_block, ("knowledge 폴더의 Git 로컬 제외를 제거",)
    if has_block:
        return original, ()

    if original.endswith(b"\r\n"):
        managed = _block(b"\r\n")
        intended = original + managed
    elif original.endswith(b"\n"):
        managed = _block(b"\n")
        intended = original + managed
    else:
        managed = _block(b"\n")
        intended = managed + original
    return intended, ("knowledge 폴더를 Git 로컬 제외에 추가",)


def _tracked_knowledge_exists(project_root: Path) -> bool:
    result = _run_git(project_root, ("ls-files", "-z", "--", "knowledge/"))
    return bool(result.stdout)


def _read_optional_config_path(project_root: Path) -> str | None:
    result = _run_git(
        project_root,
        ("config", "--path", "-z", "--get", "core.excludesFile"),
        allowed_returncodes=(0, 1),
    )
    if result.returncode == 1:
        return None
    output = result.stdout
    if not output.endswith(b"\x00") or output.count(b"\x00") != 1:
        raise _git_unavailable()
    try:
        value = output[:-1].decode("utf-8")
    except UnicodeError as error:
        raise _git_unavailable() from error
    if not value or "\n" in value or "\r" in value:
        raise _git_unavailable()
    return value


def _read_ignore_case(project_root: Path) -> bool:
    result = _run_git(
        project_root,
        ("config", "--type=bool", "--get", "core.ignoreCase"),
        allowed_returncodes=(0, 1),
    )
    if result.returncode == 1:
        return False
    try:
        value = result.stdout.decode("ascii").strip()
    except UnicodeError as error:
        raise _git_unavailable() from error
    if value == "true":
        return True
    if value == "false":
        return False
    raise _git_unavailable()


def _planned_knowledge_is_ignored(
    project_root: Path,
    intended: bytes | None,
) -> bool:
    excludes_file = _read_optional_config_path(project_root)
    ignore_case = _read_ignore_case(project_root)
    with tempfile.TemporaryDirectory(prefix="didimlog-git-exclude-") as temporary:
        git_directory = Path(temporary) / "git"
        (git_directory / "info").mkdir(parents=True)
        (git_directory / "objects").mkdir()
        (git_directory / "refs" / "heads").mkdir(parents=True)
        (git_directory / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (git_directory / "config").write_bytes(
            b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
        )
        if intended is not None:
            (git_directory / "info" / "exclude").write_bytes(intended)

        arguments = [
            f"--git-dir={git_directory}",
            f"--work-tree={project_root}",
            "-c",
            f"core.ignoreCase={'true' if ignore_case else 'false'}",
        ]
        if excludes_file is not None:
            arguments.extend(("-c", f"core.excludesFile={excludes_file}"))
        arguments.extend(("check-ignore", "--no-index", "-q", "--", "knowledge/"))
        result = _run_git(
            project_root,
            tuple(arguments),
            allowed_returncodes=(0, 1),
        )
        return result.returncode == 0


def _actual_knowledge_is_ignored(project_root: Path) -> bool:
    result = _run_git(
        project_root,
        ("check-ignore", "--no-index", "-q", "--", "knowledge/"),
        allowed_returncodes=(0, 1),
    )
    return result.returncode == 0


def _build_plan(
    project_root: Path,
    path: Path,
    mode: str,
    original: bytes | None,
) -> GitExcludePlan:
    intended, changes = _transform(original, mode)
    if mode == "local" and _tracked_knowledge_exists(project_root):
        raise _tracked()
    planned_ignored = _planned_knowledge_is_ignored(project_root, intended)
    if mode == "local" and not planned_ignored:
        raise _conflict()
    notices: tuple[str, ...] = ()
    if mode == "shared" and planned_ignored:
        notices = (
            "다른 Git 규칙이 knowledge 폴더를 계속 제외하고 있습니다.",
        )
    return GitExcludePlan(
        project_root=project_root,
        path=path,
        mode=mode,
        original=original,
        intended=intended,
        changes=changes,
        notices=notices,
    )


def plan_git_exclude(project_root: Path, mode: str) -> GitExcludePlan:
    """Plan the exact local exclude bytes without changing the repository."""
    if mode not in _VALID_MODES:
        raise ValueError("mode must be 'local' or 'shared'")
    root = _validated_project_root(project_root)
    path = _git_exclude_path(root)
    original = _read_exclude(path)
    return _build_plan(root, path, mode, original)


def _plan_has_valid_shape(plan: object) -> bool:
    return (
        type(plan) is GitExcludePlan
        and isinstance(plan.project_root, Path)
        and plan.project_root.is_absolute()
        and isinstance(plan.path, Path)
        and plan.path.is_absolute()
        and isinstance(plan.mode, str)
        and plan.mode in _VALID_MODES
        and (plan.original is None or isinstance(plan.original, bytes))
        and (plan.intended is None or isinstance(plan.intended, bytes))
        and isinstance(plan.changes, tuple)
        and all(isinstance(change, str) for change in plan.changes)
        and isinstance(plan.notices, tuple)
        and all(isinstance(notice, str) for notice in plan.notices)
    )


def apply_git_exclude(plan: GitExcludePlan) -> None:
    """Apply a still-current canonical plan and verify the resulting policy."""
    if not _plan_has_valid_shape(plan):
        raise _changed()
    try:
        root = _validated_project_root(plan.project_root)
        path = _git_exclude_path(root)
    except DidimError as error:
        raise _changed() from error
    if not _same_file(root, plan.project_root) or path != plan.path:
        raise _changed()

    current = _read_exclude(path)
    if current != plan.original:
        raise _changed()
    try:
        canonical = _build_plan(root, path, plan.mode, current)
    except DidimError as error:
        if error.token == "PROJECT_EXCLUDE_MARKERS_INVALID":
            raise _changed() from error
        raise
    if canonical != plan:
        raise _changed()

    try:
        write_regular_file_if_unchanged(path, plan.original, plan.intended)
    except (OSError, ValueError) as error:
        raise _changed() from error

    if plan.mode == "local" and _tracked_knowledge_exists(root):
        raise _tracked()
    ignored = _actual_knowledge_is_ignored(root)
    if plan.mode == "local" and not ignored:
        raise _conflict()


def project_knowledge_is_ignored(project_root: Path) -> bool:
    """Return whether Git currently ignores ``knowledge/`` in the project."""
    root = _validated_project_root(project_root)
    return _actual_knowledge_is_ignored(root)
