#!/usr/bin/env python3
"""Deterministic release preparation checks used by GitHub Actions."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import re
import sys
import tomllib


_VERSION_PATTERN = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
_UNRELEASED_HEADING = "## [Unreleased]"
_VERSION_HEADING_PATTERN = re.compile(r"## \[[^]]+\] - \d{4}-\d{2}-\d{2}")
_UNRELEASED_LINK_PREFIX = "[Unreleased]: "
_REPOSITORY_URL = "https://github.com/zhsks311/didimlog"


class ReleaseError(ValueError):
    pass


def _version(value: str) -> tuple[int, int, int]:
    if _VERSION_PATTERN.fullmatch(value) is None:
        raise ReleaseError(f"invalid release version: {value}")
    return tuple(int(component) for component in value.split("."))


def _project_version(path: Path) -> str:
    with path.open("rb") as stream:
        project = tomllib.load(stream).get("project")
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        raise ReleaseError(f"project version is missing: {path}")
    value = project["version"]
    _version(value)
    return value


def _locked_project_version(path: Path) -> str:
    with path.open("rb") as stream:
        packages = tomllib.load(stream).get("package", [])
    matches = [
        package.get("version")
        for package in packages
        if package.get("name") == "didimlog"
        and package.get("source") == {"editable": "."}
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise ReleaseError("uv.lock must contain one editable didimlog package")
    _version(matches[0])
    return matches[0]


def prepare_changelog(path: Path, version: str, release_date: str) -> None:
    _version(version)
    try:
        date.fromisoformat(release_date)
    except ValueError as error:
        raise ReleaseError(f"invalid release date: {release_date}") from error

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    try:
        unreleased_index = lines.index(_UNRELEASED_HEADING)
    except ValueError as error:
        raise ReleaseError("CHANGELOG is missing the Unreleased heading") from error

    next_version_index = next(
        (
            index
            for index in range(unreleased_index + 1, len(lines))
            if _VERSION_HEADING_PATTERN.fullmatch(lines[index]) is not None
        ),
        None,
    )
    if next_version_index is None:
        raise ReleaseError("CHANGELOG has no published version after Unreleased")

    body = lines[unreleased_index + 1 : next_version_index]
    while body and not body[0]:
        body.pop(0)
    while body and not body[-1]:
        body.pop()
    if not body:
        raise ReleaseError("Unreleased section is empty")
    if any(line.startswith(f"## [{version}]") for line in lines):
        raise ReleaseError(f"CHANGELOG already contains {version}")

    updated = (
        lines[: unreleased_index + 1]
        + ["", f"## [{version}] - {release_date}", ""]
        + body
        + [""]
        + lines[next_version_index:]
    )
    link_index = next(
        (
            index
            for index, line in enumerate(updated)
            if line.startswith(_UNRELEASED_LINK_PREFIX)
        ),
        None,
    )
    if link_index is None:
        raise ReleaseError("CHANGELOG is missing the Unreleased comparison link")
    updated[link_index] = (
        f"[Unreleased]: {_REPOSITORY_URL}/compare/v{version}...HEAD"
    )
    updated.insert(
        link_index + 1,
        f"[{version}]: {_REPOSITORY_URL}/releases/tag/v{version}",
    )
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


def check_release(previous_pyproject: Path, current_pyproject: Path, lock: Path) -> str:
    previous = _project_version(previous_pyproject)
    current = _project_version(current_pyproject)
    if _version(current) <= _version(previous):
        raise ReleaseError(
            f"release version must increase: previous={previous}, current={current}"
        )
    locked = _locked_project_version(lock)
    if locked != current:
        raise ReleaseError(
            f"uv.lock version {locked} does not match project version {current}"
        )
    return current


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    changelog = commands.add_parser("prepare-changelog")
    changelog.add_argument("--path", type=Path, required=True)
    changelog.add_argument("--version", required=True)
    changelog.add_argument("--date", required=True)

    release = commands.add_parser("check-release")
    release.add_argument("--previous-pyproject", type=Path, required=True)
    release.add_argument("--current-pyproject", type=Path, required=True)
    release.add_argument("--lock", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare-changelog":
            prepare_changelog(arguments.path, arguments.version, arguments.date)
            print(arguments.version)
        else:
            print(
                check_release(
                    arguments.previous_pyproject,
                    arguments.current_pyproject,
                    arguments.lock,
                )
            )
    except (OSError, ReleaseError, tomllib.TOMLDecodeError) as error:
        print(f"release.py: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
