"""Personal knowledge paths and project-name resolution."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import stat
import subprocess
from pathlib import Path

from didimlog.errors import DidimError, EXIT_GIT, EXIT_POLICY, EXIT_USAGE


_PROJECT_SLUG = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_GLOBAL_PROJECT = "_global"
_GIT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ProjectDirectory:
    logical: Path
    physical: Path
    entry_identity: tuple[int, int, int]
    target_identity: tuple[int, int, int]


class ProjectDirectoryError(ValueError):
    def __init__(self, logical: Path, reason: str) -> None:
        super().__init__(reason)
        self.logical = logical
        self.reason = reason


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


class _ProjectError(DidimError, ValueError):
    """A project-selection error usable by both the API and CLI boundary."""


def _project_error(
    token: str,
    *,
    exit_code: int,
    help_text: str,
) -> _ProjectError:
    return _ProjectError(token, exit_code=exit_code, help_text=help_text)

def _source_path_contains_symlink(source: Path, git_root: Path) -> bool:
    try:
        root_info = git_root.stat()
    except OSError:
        return True

    for repository_candidate in (source, *source.parents):
        try:
            candidate_info = repository_candidate.stat()
        except OSError:
            return True
        if (
            candidate_info.st_dev != root_info.st_dev
            or candidate_info.st_ino != root_info.st_ino
        ):
            continue

        current = source
        while True:
            if current.is_symlink():
                return True
            if current == repository_candidate:
                return False
            current = current.parent
    return True


def data_home(home=None) -> Path:
    """Return the personal knowledge root below the selected home directory."""
    selected_home = Path.home() if home is None else Path(home)
    return selected_home / "knowledge"


def lessons_dir(home=None) -> Path:
    return data_home(home) / "lessons"


def docs_dir(home=None) -> Path:
    return data_home(home) / "docs"


def book_dir(home=None) -> Path:
    return data_home(home) / "book"


def index_dir(home=None) -> Path:
    return data_home(home) / "index"


def validate_project(value: str, *, allow_global: bool = False) -> str:
    """Validate a portable project slug, optionally accepting ``_global``."""
    if value == _GLOBAL_PROJECT:
        if allow_global:
            return value
        raise _project_error(
            "PROJECT_GLOBAL_REQUIRES_EXPLICIT_SELECTION",
            exit_code=EXIT_USAGE,
            help_text="전역 범위는 --global처럼 명시적인 선택으로만 사용할 수 있습니다.",
        )
    if not isinstance(value, str) or _PROJECT_SLUG.fullmatch(value) is None:
        raise _project_error(
            "PROJECT_SLUG_INVALID",
            exit_code=EXIT_USAGE,
            help_text="프로젝트 이름에는 영문자, 숫자와 단일 하이픈만 사용하세요.",
        )
    return value


def resolve_project_directory(base: Path, project: str) -> ProjectDirectory | None:
    """Resolve a validated logical project entry to a real directory."""
    validated_project = validate_project(project, allow_global=True)
    normalized_base = Path(os.path.abspath(base))
    try:
        base_info = normalized_base.lstat()
    except OSError as error:
        raise ProjectDirectoryError(
            normalized_base,
            "source category must be a real directory",
        ) from error
    if not stat.S_ISDIR(base_info.st_mode):
        raise ProjectDirectoryError(
            normalized_base,
            "source category must be a real directory",
        )

    logical = normalized_base / validated_project
    try:
        entry_info = logical.lstat()
    except FileNotFoundError:
        return None

    if stat.S_ISDIR(entry_info.st_mode):
        identity = _directory_identity(entry_info)
        return ProjectDirectory(
            logical=logical,
            physical=logical,
            entry_identity=identity,
            target_identity=identity,
        )

    if not stat.S_ISLNK(entry_info.st_mode):
        raise ProjectDirectoryError(
            logical,
            "project entry must point to a directory",
        )

    try:
        physical = logical.resolve(strict=True)
    except FileNotFoundError as error:
        raise ProjectDirectoryError(
            logical,
            "project link target is missing",
        ) from error
    except (OSError, RuntimeError) as error:
        raise ProjectDirectoryError(
            logical,
            "project link cannot be resolved",
        ) from error

    try:
        target_info = physical.stat()
    except FileNotFoundError as error:
        raise ProjectDirectoryError(
            logical,
            "project link target is missing",
        ) from error
    except OSError as error:
        raise ProjectDirectoryError(
            logical,
            "project link cannot be resolved",
        ) from error

    if not stat.S_ISDIR(target_info.st_mode):
        raise ProjectDirectoryError(
            logical,
            "project entry must point to a directory",
        )

    return ProjectDirectory(
        logical=logical,
        physical=physical,
        entry_identity=_directory_identity(entry_info),
        target_identity=_directory_identity(target_info),
    )


def project_directory_unchanged(directory: ProjectDirectory) -> bool:
    """Return whether a project entry still resolves to the same directory."""
    try:
        current = resolve_project_directory(
            directory.logical.parent,
            directory.logical.name,
        )
    except (OSError, ValueError):
        return False
    return current == directory


def resolve_project(
    explicit=None,
    *,
    cwd=None,
    allow_global: bool = False,
) -> str:
    """Resolve an explicit project or the current Git root's basename."""
    if explicit is not None:
        return validate_project(explicit, allow_global=allow_global)

    try:
        source_directory = Path.cwd() if cwd is None else Path(cwd)
    except (OSError, TypeError, ValueError) as error:
        raise _project_error(
            "GIT_UNAVAILABLE",
            exit_code=EXIT_GIT,
            help_text="유효한 Git 작업 디렉터리에서 실행하거나 --project를 지정하세요.",
        ) from error
    source_directory = source_directory.absolute()

    if source_directory.is_symlink():
        raise _project_error(
            "PROJECT_SOURCE_DIRECTORY_SYMLINK",
            exit_code=EXIT_POLICY,
            help_text="심볼릭 링크가 아닌 실제 작업 디렉터리에서 실행하세요.",
        )

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(source_directory),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise _project_error(
            "GIT_UNAVAILABLE",
            exit_code=EXIT_GIT,
            help_text="유효한 Git 저장소에서 실행하거나 --project를 지정하세요.",
        ) from error

    root_text = result.stdout.strip()
    if not root_text:
        raise _project_error(
            "GIT_UNAVAILABLE",
            exit_code=EXIT_GIT,
            help_text="Git 저장소 루트를 확인할 수 없어 --project를 지정해야 합니다.",
        )

    git_root = Path(root_text)
    if not git_root.is_absolute() or git_root.is_symlink() or not git_root.is_dir():
        raise _project_error(
            "PROJECT_SOURCE_DIRECTORY_INVALID",
            exit_code=EXIT_POLICY,
            help_text="Git이 반환한 실제 저장소 디렉터리를 확인하세요.",
        )

    if _source_path_contains_symlink(source_directory, git_root):
        raise _project_error(
            "PROJECT_SOURCE_DIRECTORY_SYMLINK",
            exit_code=EXIT_POLICY,
            help_text="심볼릭 링크가 아닌 실제 작업 디렉터리에서 실행하세요.",
        )

    # Discovery never selects the reserved global scope, even when callers may
    # accept it for an explicit selection.
    return validate_project(git_root.name)
