"""Build one write-free setup plan across personal, project, and Claude surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import stat
import tempfile

from didimlog import version as didimlog_version
from didimlog.errors import DidimError, EXIT_POLICY, EXIT_USAGE
from didimlog.indexing import (
    _discover_git_root,
    _personal_check,
    _prepared_project,
    _project_check,
    run_index,
)
from didimlog.personal import index as personal_index
from didimlog.personal.paths import data_home
from didimlog.personal.rules_document import create_user_rules
from didimlog.project.index import write_index
from didimlog.project.scaffold import (
    ScaffoldPlan,
    apply_scaffold,
    plan_scaffold,
)

from .connect import ClaudeChangePlan, apply_connect, plan_connect
from .probe import inspect
from .transaction import InstallJournal


@dataclass(frozen=True)
class _PersonalSetupPlan:
    home: Path
    root: Path
    directories: tuple[Path, ...]
    create_rules: bool


@dataclass(frozen=True)
class SetupPlan:
    version: str
    personal_changes: tuple[str, ...]
    project_changes: tuple[str, ...]
    claude_changes: tuple[str, ...]
    _personal: _PersonalSetupPlan = field(repr=False, compare=False)
    _project: ScaffoldPlan | None = field(repr=False, compare=False)
    _project_root: Path | None = field(repr=False, compare=False)
    _claude: ClaudeChangePlan | None = field(repr=False, compare=False)


def _find_launcher() -> str | None:
    return shutil.which("didim")


def _require_directory(path: Path, *, allow_missing: bool) -> bool:
    try:
        linked = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        raise ValueError("setup directory is missing: {}".format(path)) from None
    except OSError as error:
        raise ValueError("setup directory is unsafe: {}".format(path)) from error
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
        raise ValueError("setup directory must be a real directory: {}".format(path))
    return True


def _plan_personal(home) -> tuple[_PersonalSetupPlan, tuple[str, ...]]:
    selected_home = Path.home() if home is None else Path(home)
    selected_home = Path(os.path.abspath(selected_home))
    _require_directory(selected_home, allow_missing=False)
    root = data_home(selected_home)
    root_exists = _require_directory(root, allow_missing=True)
    directories = (
        root,
        root / "lessons",
        root / "lessons" / "_global",
        root / "docs",
        root / "docs" / "_global",
        root / "book",
        root / "book" / "_global",
        root / "index",
    )
    changes: list[str] = []
    for directory in directories:
        exists = root_exists if directory == root else _require_directory(
            directory,
            allow_missing=True,
        )
        if not exists:
            changes.append("개인 지식 디렉터리 생성: {}".format(directory))

    rules = root / "MY-RULES.md"
    try:
        linked = rules.lstat()
    except FileNotFoundError:
        create_rules = True
        changes.append("MY-RULES.md 생성: {}".format(rules))
    except OSError as error:
        raise ValueError("MY-RULES.md destination is unsafe") from error
    else:
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
            raise ValueError("MY-RULES.md must be a regular file")
        create_rules = False

    index_current = (
        not any("디렉터리 생성" in change for change in changes)
        and _personal_check(root).endswith("PERSONAL_INDEX_CURRENT")
    )
    if not index_current:
        changes.append("개인 지식 index 생성 또는 갱신")
    return (
        _PersonalSetupPlan(
            home=selected_home,
            root=root,
            directories=directories,
            create_rules=create_rules,
        ),
        tuple(changes),
    )


def _project_changes(
    root: Path,
    plan: ScaffoldPlan,
) -> tuple[str, ...]:
    changes = []
    for directory in plan.directories:
        if not directory.exists():
            changes.append("프로젝트 근거 저장소 생성: {}".format(directory))
    for path, _ in plan.files:
        if not path.exists():
            changes.append("프로젝트 근거 파일 생성: {}".format(path))
    if not _prepared_project(root) or not _project_check(root).endswith(
        "PROJECT_INDEX_CURRENT"
    ):
        changes.append("프로젝트 index 생성 또는 갱신")
    return tuple(changes)


def plan_setup(
    *,
    home=None,
    cwd=None,
    config=None,
    include_project: bool,
    skip_claude: bool,
) -> SetupPlan:
    """Preflight every requested surface and return one deterministic summary."""
    personal_plan, personal_changes = _plan_personal(home)

    project_plan: ScaffoldPlan | None = None
    project_root: Path | None = None
    if include_project:
        project_root = _discover_git_root(cwd)
        if project_root is None:
            project_changes = (
                "프로젝트 근거: 설정되지 않음 — didim setup을 Git 프로젝트에서 실행하세요.",
            )
        else:
            project_plan = plan_scaffold(project_root)
            project_changes = _project_changes(project_root, project_plan)
    else:
        project_changes = ()

    claude_plan: ClaudeChangePlan | None = None
    if skip_claude:
        claude_changes = ()
    else:
        launcher = _find_launcher()
        if launcher is None:
            raise ValueError("didim launcher is unavailable")
        claude_plan = plan_connect(
            config,
            launcher=Path(launcher),
            home=personal_plan.home,
        )
        claude_changes = claude_plan.changes

    return SetupPlan(
        version=didimlog_version(),
        personal_changes=personal_changes,
        project_changes=project_changes,
        claude_changes=claude_changes,
        _personal=personal_plan,
        _project=project_plan,
        _project_root=project_root,
        _claude=claude_plan,
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_child_directory(parent: int, path: Path) -> int:
    try:
        linked = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(path.name, _directory_flags(), dir_fd=parent)
    except OSError as error:
        raise ValueError("personal setup path is unsafe: {}".format(path)) from error
    opened = os.fstat(descriptor)
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or linked.st_dev != opened.st_dev
        or linked.st_ino != opened.st_ino
    ):
        os.close(descriptor)
        raise ValueError("personal setup path is unsafe: {}".format(path))
    return descriptor


def _apply_personal(plan: _PersonalSetupPlan) -> None:
    if plan.root != data_home(plan.home):
        raise DidimError("SETUP_PLAN_INVALID", exit_code=EXIT_POLICY)
    expected = (
        plan.root,
        plan.root / "lessons",
        plan.root / "lessons" / "_global",
        plan.root / "docs",
        plan.root / "docs" / "_global",
        plan.root / "book",
        plan.root / "book" / "_global",
        plan.root / "index",
    )
    if plan.directories != expected:
        raise DidimError("SETUP_PLAN_INVALID", exit_code=EXIT_POLICY)

    home_descriptor = os.open(plan.home, _directory_flags())
    descriptors: dict[Path, int] = {plan.home: home_descriptor}
    try:
        for path in plan.directories:
            parent = descriptors[path.parent]
            try:
                os.mkdir(path.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            except OSError as error:
                raise ValueError(
                    "personal setup directory could not be created: {}".format(path)
                ) from error
            descriptors[path] = _open_child_directory(parent, path)
        create_user_rules(plan.home)
    finally:
        for descriptor in reversed(tuple(descriptors.values())):
            os.close(descriptor)


def _postcheck(plan: SetupPlan) -> None:
    checked = run_index(
        check=True,
        home=plan._personal.home,
        cwd=plan._project_root,
    )
    if not checked.personal.endswith("PERSONAL_INDEX_CURRENT"):
        raise DidimError("SETUP_POSTCHECK_FAILED", exit_code=EXIT_POLICY)
    if plan._project_root is not None and not checked.project.endswith(
        "PROJECT_INDEX_CURRENT"
    ):
        raise DidimError("SETUP_POSTCHECK_FAILED", exit_code=EXIT_POLICY)
    if plan._claude is not None and inspect(
        home=plan._personal.home,
        cwd=plan._project_root,
        config=plan._claude.config_dir,
    ):
        raise DidimError("SETUP_POSTCHECK_FAILED", exit_code=EXIT_POLICY)


def apply_setup(plan: SetupPlan, *, approved: bool) -> None:
    """Apply an approved preflight in dependency order and verify every surface."""
    if not approved:
        raise DidimError(
            "SETUP_APPROVAL_REQUIRED",
            exit_code=EXIT_USAGE,
            help_text="변경 요약을 확인한 뒤 --yes를 사용하거나 대화형 승인에 응답하세요.",
        )
    if not isinstance(plan, SetupPlan) or plan.version != didimlog_version():
        raise DidimError("SETUP_PLAN_INVALID", exit_code=EXIT_POLICY)

    _apply_personal(plan._personal)
    if plan._project is not None:
        apply_scaffold(plan._project)

    personal_index.write_all(
        data_root=plan._personal.root,
        target=plan._personal.root / "index",
    )
    if plan._project_root is not None:
        write_index(plan._project_root)

    journal: InstallJournal | None = None
    if plan._claude is not None:
        with tempfile.TemporaryDirectory(
            prefix=".didimlog-setup-",
            dir=plan._personal.root,
        ) as transaction_directory:
            journal = InstallJournal(
                Path(transaction_directory) / "journal.json",
                reset=True,
            )
            apply_connect(plan._claude, journal)
            try:
                _postcheck(plan)
            except BaseException:
                journal.rollback()
                raise
        return
    _postcheck(plan)
