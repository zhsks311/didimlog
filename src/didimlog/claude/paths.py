"""Claude 설정 경로를 홈 디렉터리 안의 안전한 대상으로 제한한다."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path


_MANAGED_TARGET = re.compile(r"didimlog/[^/\\]+\.md\Z")
_TOP_LEVEL_TARGETS = frozenset({"CLAUDE.md", "settings.json"})


def _home_directories(
    home: str | os.PathLike[str] | None,
) -> tuple[Path, Path]:
    candidate = Path.home() if home is None else Path(home)
    try:
        lexical = candidate.expanduser().absolute()
        resolved = lexical.resolve(strict=True)
        mode = resolved.stat().st_mode
    except (OSError, RuntimeError) as exc:
        raise ValueError("home must be an existing directory") from exc
    if not stat.S_ISDIR(mode):
        raise ValueError("home must be an existing directory")
    return lexical, resolved


def _configured_directory(
    candidate: str | os.PathLike[str],
    *,
    home: Path,
    lexical_home: Path,
) -> Path:
    try:
        path = Path(candidate).expanduser()
    except (OSError, RuntimeError, TypeError) as exc:
        raise ValueError("Claude config path is invalid") from exc

    if ".." in path.parts:
        raise ValueError("Claude config path must not escape the user home")
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.absolute()

    for base in (lexical_home, home):
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        break
    else:
        raise ValueError("Claude config directory must stay inside the user home")

    current = base
    try:
        for component in relative.parts:
            current = current / component
            entry = current.lstat()
            if stat.S_ISLNK(entry.st_mode):
                raise ValueError("Claude config path must not contain symlinks")
            if not stat.S_ISDIR(entry.st_mode):
                raise ValueError("Claude config path must be a regular directory")
        resolved = path.resolve(strict=True)
    except ValueError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ValueError("Claude config path must be an existing directory") from exc

    if resolved != home and not resolved.is_relative_to(home):
        raise ValueError("Claude config directory must stay inside the user home")
    return resolved


def config_dir(
    explicit: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the selected existing, non-symlinked Claude config directory.

    An explicit path wins over ``CLAUDE_CONFIG_DIR``; an unset or empty
    environment value falls back to ``~/.claude``.
    """

    lexical_home, resolved_home = _home_directories(home)
    environment = os.environ if environ is None else environ
    if explicit is not None:
        selected: str | os.PathLike[str] = explicit
    else:
        selected = environment.get("CLAUDE_CONFIG_DIR") or resolved_home / ".claude"
    return _configured_directory(
        selected,
        home=resolved_home,
        lexical_home=lexical_home,
    )


def config_target(
    config: Path,
    name: str,
    *,
    home: Path,
) -> Path:
    """Return an allowlisted Claude target after rejecting unsafe path entries."""

    lexical_home, resolved_home = _home_directories(home)
    _configured_directory(
        config,
        home=resolved_home,
        lexical_home=lexical_home,
    )

    if not isinstance(name, str) or (
        name not in _TOP_LEVEL_TARGETS and _MANAGED_TARGET.fullmatch(name) is None
    ):
        raise ValueError("Claude config target is not managed by Didimlog")

    target = config / name
    if name.startswith("didimlog/"):
        managed_directory = config / "didimlog"
        try:
            parent_entry = managed_directory.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError("Claude config target parent is unsafe") from exc
        else:
            if stat.S_ISLNK(parent_entry.st_mode) or not stat.S_ISDIR(parent_entry.st_mode):
                raise ValueError("Claude config target parent must be a regular directory")

    try:
        target_entry = target.lstat()
    except FileNotFoundError:
        return target
    except OSError as exc:
        raise ValueError("Claude config target is unsafe") from exc
    if stat.S_ISLNK(target_entry.st_mode) or not stat.S_ISREG(target_entry.st_mode):
        raise ValueError("Claude config target must be a regular file")
    return target
