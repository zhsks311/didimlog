"""Coordinate the complete personal index and the current prepared project index."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import subprocess

from didimlog.file_io import UnsafePathError, read_regular_file_beneath
from didimlog.locking import path_lock
from didimlog.personal import index as personal_index
from didimlog.personal.paths import data_home
from didimlog.project import index as project_index
from didimlog.project.scaffold import plan_scaffold


_GIT_TIMEOUT_SECONDS = 5
PERSONAL_INDEX_CURRENT = "PERSONAL_INDEX_CURRENT"
PROJECT_INDEX_CURRENT = "PROJECT_INDEX_CURRENT"
PROJECT_NOT_CONFIGURED = "PROJECT_NOT_CONFIGURED"
# 원본은 멀쩡한데 잠금만 못 얻은 경우다. 원본 오류와 섞으면
# 고칠 것이 없는 사용자에게 원본을 고치라고 안내하게 된다.
PERSONAL_INDEX_BUSY = "PERSONAL_INDEX_BUSY"
_PROJECT_NOT_CONFIGURED_TEXT = (
    "프로젝트 근거: 설정되지 않음 — didim setup을 실행하세요."
)


@dataclass(frozen=True)
class IndexResult:
    personal: str
    project: str
    personal_token: str
    project_token: str


def _status(label: str, token: str) -> str:
    return "{}: {}".format(label, token)


def _result(personal_token: str, project_token: str) -> IndexResult:
    project = (
        _PROJECT_NOT_CONFIGURED_TEXT
        if project_token == PROJECT_NOT_CONFIGURED
        else _status("프로젝트 근거", project_token)
    )
    return IndexResult(
        personal=_status("개인 지식", personal_token),
        project=project,
        personal_token=personal_token,
        project_token=project_token,
    )


def _discover_git_root(cwd) -> Path | None:
    source = Path.cwd() if cwd is None else Path(cwd)
    try:
        source = source.absolute()
        linked = source.lstat()
    except (OSError, TypeError, ValueError):
        return None
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
        return None
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "-C", os.fspath(source), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return None
    if result.returncode != 0:
        return None
    root_text = result.stdout.strip()
    if not root_text:
        return None
    root = Path(root_text)
    try:
        linked = root.lstat()
    except OSError:
        return None
    if not root.is_absolute() or stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(
        linked.st_mode
    ):
        return None
    return Path(os.path.abspath(root))


def _prepared_project(root: Path) -> bool:
    try:
        with path_lock(root / "knowledge", shared=True):
            plan = plan_scaffold(root)
            for directory in plan.directories:
                linked = directory.lstat()
                if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
                    return False
            updates = {
                path: (original, intended)
                for path, original, intended in plan.updates
            }
            for path, expected in plan.files:
                relative = path.relative_to(root)
                accepted = updates.get(path, (expected,))
                actual = read_regular_file_beneath(
                    root,
                    relative,
                    max(len(value) for value in accepted),
                )
                if actual not in accepted:
                    return False
    except (OSError, UnsafePathError, ValueError):
        return False
    return True


def _personal_check_locked(root: Path) -> str:
    destination = root / "index"
    if not root.exists():
        return "PERSONAL_INDEX_MISSING"
    try:
        outputs = personal_index.build_all(root)
    except (personal_index.KnowledgeIndexError, OSError, UnicodeError):
        return "PERSONAL_INDEX_INVALID_SOURCE"

    try:
        state = personal_index.inspect_index(outputs, destination)
    except (personal_index.KnowledgeIndexError, UnicodeError):
        return "PERSONAL_INDEX_INVALID_SOURCE"
    if state is personal_index.IndexCheckState.MISSING:
        return "PERSONAL_INDEX_MISSING"
    if state is personal_index.IndexCheckState.EXTRA:
        return "PERSONAL_INDEX_EXTRA"
    if state is personal_index.IndexCheckState.STALE:
        return "PERSONAL_INDEX_STALE"
    return PERSONAL_INDEX_CURRENT


def _personal_check(root: Path) -> str:
    if not root.exists():
        return "PERSONAL_INDEX_MISSING"
    try:
        with path_lock(root, shared=True):
            return _personal_check_locked(root)
    except OSError:
        # 다른 실행이 잠금을 쥐고 있을 뿐 원본은 손대지 않았다.
        # 잠시 뒤 다시 확인하면 되고, 고칠 파일은 없다.
        return PERSONAL_INDEX_BUSY


def _project_check(root: Path) -> str:
    output = root / "knowledge" / "index" / "INDEX.md"
    try:
        current = project_index.check_index(root)
    except (OSError, ValueError):
        return "PROJECT_INDEX_INVALID_SOURCE"
    try:
        entries = tuple(output.parent.iterdir())
    except FileNotFoundError:
        return "PROJECT_INDEX_MISSING"
    except OSError:
        return "PROJECT_INDEX_EXTRA"
    names = {entry.name for entry in entries}
    if "INDEX.md" not in names:
        return "PROJECT_INDEX_MISSING"
    if names != {"INDEX.md"}:
        return "PROJECT_INDEX_EXTRA"
    if current != 0:
        return "PROJECT_INDEX_STALE"
    return PROJECT_INDEX_CURRENT


def run_index(*, check: bool, home=None, cwd=None) -> IndexResult:
    """Process every personal source and the current configured project, if any."""
    personal_root = data_home(home)
    project_root = _discover_git_root(cwd)
    configured_project = (
        project_root
        if project_root is not None and _prepared_project(project_root)
        else None
    )

    if check:
        personal_token = _personal_check(personal_root)
        project_token = (
            PROJECT_NOT_CONFIGURED
            if configured_project is None
            else _project_check(configured_project)
        )
        return _result(personal_token, project_token)

    personal_index.write_all(
        data_root=personal_root,
        target=personal_root / "index",
    )
    personal_token = "PERSONAL_INDEX_WRITTEN"
    if configured_project is None:
        project_token = PROJECT_NOT_CONFIGURED
    else:
        project_index.write_index(configured_project)
        project_token = "PROJECT_INDEX_WRITTEN"
    return _result(personal_token, project_token)
