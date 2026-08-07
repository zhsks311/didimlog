"""Coordinate the complete personal index and the current prepared project index."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import subprocess

from didimlog.personal import index as personal_index
from didimlog.personal.paths import data_home
from didimlog.project import index as project_index
from didimlog.project.scaffold import plan_scaffold


_GIT_TIMEOUT_SECONDS = 5
_PROJECT_NOT_CONFIGURED = (
    "프로젝트 근거: 설정되지 않음 — didim setup을 실행하세요."
)


@dataclass(frozen=True)
class IndexResult:
    personal: str
    project: str


def _status(label: str, token: str) -> str:
    return "{}: {}".format(label, token)


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
        plan = plan_scaffold(root)
        for directory in plan.directories:
            linked = directory.lstat()
            if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
                return False
        for path, expected in plan.files:
            linked = path.lstat()
            if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
                return False
            if path.read_bytes() != expected:
                return False
    except (OSError, ValueError):
        return False
    return True


def _personal_check(root: Path) -> str:
    destination = root / "index"
    if not root.exists():
        return _status("개인 지식", "PERSONAL_INDEX_MISSING")
    try:
        outputs = personal_index.build_all(root)
    except (personal_index.KnowledgeIndexError, OSError, UnicodeError):
        return _status("개인 지식", "PERSONAL_INDEX_INVALID_SOURCE")

    if destination.is_symlink() or (
        destination.exists() and not destination.is_dir()
    ):
        return _status("개인 지식", "PERSONAL_INDEX_EXTRA")
    if not destination.is_dir():
        return _status("개인 지식", "PERSONAL_INDEX_MISSING")
    try:
        entries = tuple(destination.iterdir())
    except OSError:
        return _status("개인 지식", "PERSONAL_INDEX_EXTRA")
    expected_names = {project + ".md" for project in outputs}
    actual_names = {entry.name for entry in entries}
    if expected_names - actual_names:
        return _status("개인 지식", "PERSONAL_INDEX_MISSING")
    if actual_names - expected_names:
        return _status("개인 지식", "PERSONAL_INDEX_EXTRA")
    for project, text in outputs.items():
        path = destination / (project + ".md")
        try:
            linked = path.lstat()
            if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
                return _status("개인 지식", "PERSONAL_INDEX_EXTRA")
            if path.read_bytes() != text.encode("utf-8"):
                return _status("개인 지식", "PERSONAL_INDEX_STALE")
        except OSError:
            return _status("개인 지식", "PERSONAL_INDEX_MISSING")
    return _status("개인 지식", "PERSONAL_INDEX_CURRENT")


def _project_check(root: Path) -> str:
    output = root / "knowledge" / "index" / "INDEX.md"
    try:
        expected = project_index.build_index_bytes(root)
    except (OSError, ValueError):
        return _status("프로젝트 근거", "PROJECT_INDEX_INVALID_SOURCE")
    try:
        entries = tuple(output.parent.iterdir())
    except FileNotFoundError:
        return _status("프로젝트 근거", "PROJECT_INDEX_MISSING")
    except OSError:
        return _status("프로젝트 근거", "PROJECT_INDEX_EXTRA")
    names = {entry.name for entry in entries}
    if "INDEX.md" not in names:
        return _status("프로젝트 근거", "PROJECT_INDEX_MISSING")
    if names != {"INDEX.md"}:
        return _status("프로젝트 근거", "PROJECT_INDEX_EXTRA")
    try:
        linked = output.lstat()
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
            return _status("프로젝트 근거", "PROJECT_INDEX_EXTRA")
        actual = output.read_bytes()
    except OSError:
        return _status("프로젝트 근거", "PROJECT_INDEX_MISSING")
    if actual != expected:
        return _status("프로젝트 근거", "PROJECT_INDEX_STALE")
    return _status("프로젝트 근거", "PROJECT_INDEX_CURRENT")


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
        personal = _personal_check(personal_root)
        project = (
            _PROJECT_NOT_CONFIGURED
            if configured_project is None
            else _project_check(configured_project)
        )
        return IndexResult(personal=personal, project=project)

    personal_index.write_all(
        data_root=personal_root,
        target=personal_root / "index",
    )
    personal = _status("개인 지식", "PERSONAL_INDEX_WRITTEN")
    if configured_project is None:
        project = _PROJECT_NOT_CONFIGURED
    else:
        project_index.write_index(configured_project)
        project = _status("프로젝트 근거", "PROJECT_INDEX_WRITTEN")
    return IndexResult(personal=personal, project=project)
