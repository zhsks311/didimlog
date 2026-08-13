#!/usr/bin/env python3
"""Deterministic release preparation checks used by GitHub Actions."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib


_VERSION_PATTERN = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
_UNRELEASED_HEADING = "## [Unreleased]"
_VERSION_HEADING_PATTERN = re.compile(r"## \[[^]]+\] - \d{4}-\d{2}-\d{2}")
_UNRELEASED_LINK_PREFIX = "[Unreleased]: "
_REPOSITORY_URL = "https://github.com/zhsks311/didimlog"
_GIT_TIMEOUT_SECONDS = 10
_PREPARATION_FIELDS = (
    "Didimlog-Release-Prep",
    "Didimlog-Release-Base",
    "Didimlog-Release-Bump",
    "Didimlog-Release-PR",
    "Didimlog-Release-Kind",
)
_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40,64}")


class ReleaseError(ValueError):
    pass


@dataclass(frozen=True)
class PreparationMarker:
    version: str
    base_sha: str
    bump: str
    pr_number: int
    release_kind: str
    commit_sha: str


@dataclass(frozen=True)
class CancelMarker:
    preparation_sha: str
    commit_sha: str


@dataclass(frozen=True)
class ReleaseEvidence:
    state: str
    active_preparation: str | None
    base_sha: str
    reason: str
    head_sha: str
    pr_number: int
    base_ref: str
    head_ref: str
    selection: str


def _git(repo: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise ReleaseError(f"git_timeout:{arguments[0]}") from error
    if result.returncode != 0:
        raise ReleaseError(f"git_failed:{arguments[0]}")
    return result.stdout.strip()


def _commit_messages(
    repo: Path,
    base_sha: str,
    head_sha: str,
) -> list[tuple[str, str]]:
    merge_base = _git(repo, "merge-base", base_sha, head_sha)
    revisions = _git(
        repo,
        "rev-list",
        "--reverse",
        "--topo-order",
        f"{merge_base}..{head_sha}",
    ).splitlines()
    return [
        (
            commit_sha,
            _git(repo, "show", "-s", "--format=%B", commit_sha),
        )
        for commit_sha in revisions
    ]


def _marker_values(message: str, field: str) -> list[str]:
    prefix = f"{field}:"
    return [
        line[len(prefix) :].strip()
        for line in message.splitlines()
        if line.startswith(prefix)
    ]


def _parse_preparation_message(
    message: str,
    commit_sha: str,
) -> PreparationMarker | None:
    values = {
        field: _marker_values(message, field)
        for field in _PREPARATION_FIELDS
    }
    if not any(values.values()):
        return None
    if any(len(field_values) != 1 for field_values in values.values()):
        raise ReleaseError("invalid_preparation_marker")

    version_marker = values["Didimlog-Release-Prep"][0]
    version = version_marker.removeprefix("v")
    base_sha = values["Didimlog-Release-Base"][0]
    bump = values["Didimlog-Release-Bump"][0]
    pr_value = values["Didimlog-Release-PR"][0]
    release_kind = values["Didimlog-Release-Kind"][0]
    if (
        not version_marker.startswith("v")
        or _VERSION_PATTERN.fullmatch(version) is None
        or _SHA_PATTERN.fullmatch(base_sha) is None
        or bump not in {"patch", "minor", "major"}
        or not pr_value.isdecimal()
        or int(pr_value) < 1
        or release_kind not in {"develop", "hotfix"}
    ):
        raise ReleaseError("invalid_preparation_marker")
    return PreparationMarker(
        version=version,
        base_sha=base_sha.lower(),
        bump=bump,
        pr_number=int(pr_value),
        release_kind=release_kind,
        commit_sha=commit_sha,
    )


def _parse_cancel_message(
    message: str,
    commit_sha: str,
) -> CancelMarker | None:
    values = _marker_values(message, "Didimlog-Release-Cancel")
    if not values:
        return None
    if len(values) != 1 or _SHA_PATTERN.fullmatch(values[0]) is None:
        raise ReleaseError("invalid_cancel_marker")
    return CancelMarker(
        preparation_sha=values[0].lower(),
        commit_sha=commit_sha,
    )


def _inspect_pr(
    repo: Path,
    base_sha: str,
    head_sha: str,
    pr_number: int,
    base_ref: str,
    head_ref: str,
    selection: str,
) -> ReleaseEvidence:
    messages = _commit_messages(repo, base_sha, head_sha)

    def evidence(
        state: str,
        active_preparation: str | None,
        reason: str,
    ) -> ReleaseEvidence:
        return ReleaseEvidence(
            state=state,
            active_preparation=active_preparation,
            base_sha=base_sha,
            reason=reason,
            head_sha=head_sha,
            pr_number=pr_number,
            base_ref=base_ref,
            head_ref=head_ref,
            selection=selection,
        )

    preparations: dict[str, PreparationMarker] = {}
    try:
        for commit_sha, message in messages:
            preparation = _parse_preparation_message(message, commit_sha)
            if preparation is not None:
                preparations[commit_sha] = preparation

        cancelled: set[str] = set()
        for commit_sha, message in messages:
            cancel = _parse_cancel_message(message, commit_sha)
            if cancel is None:
                continue
            preparation = preparations.get(cancel.preparation_sha)
            if preparation is None:
                return evidence("invalid", None, "cancel_target_missing")
            if preparation.pr_number != pr_number:
                return evidence("invalid", None, "cancel_target_other_pr")
            try:
                _git(
                    repo,
                    "merge-base",
                    "--is-ancestor",
                    cancel.preparation_sha,
                    cancel.commit_sha,
                )
            except ReleaseError:
                return evidence(
                    "invalid",
                    None,
                    "cancel_target_not_ancestor",
                )
            if cancel.preparation_sha in cancelled:
                return evidence("invalid", None, "cancel_target_duplicate")
            cancelled.add(cancel.preparation_sha)
    except ReleaseError as error:
        return evidence("invalid", None, str(error))

    active = [
        preparation
        for preparation in preparations.values()
        if preparation.pr_number == pr_number
        and preparation.commit_sha not in cancelled
    ]
    if len(active) > 1:
        return evidence("invalid", None, "multiple_active_preparations")
    if not active:
        return evidence("none", None, "no_active_preparation")
    return evidence("prepared", active[0].commit_sha, "active_preparation")


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

    inspect_pr = commands.add_parser("inspect-pr")
    inspect_pr.add_argument("--repo", type=Path, required=True)
    inspect_pr.add_argument("--base-sha", required=True)
    inspect_pr.add_argument("--head-sha", required=True)
    inspect_pr.add_argument("--pr-number", type=int, required=True)
    inspect_pr.add_argument("--base-ref", required=True)
    inspect_pr.add_argument("--head-ref", required=True)
    inspect_pr.add_argument("--selection", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare-changelog":
            prepare_changelog(arguments.path, arguments.version, arguments.date)
            print(arguments.version)
        elif arguments.command == "check-release":
            print(
                check_release(
                    arguments.previous_pyproject,
                    arguments.current_pyproject,
                    arguments.lock,
                )
            )
        else:
            evidence = _inspect_pr(
                arguments.repo,
                arguments.base_sha,
                arguments.head_sha,
                arguments.pr_number,
                arguments.base_ref,
                arguments.head_ref,
                arguments.selection,
            )
            print(json.dumps(asdict(evidence), sort_keys=True))
    except (OSError, ReleaseError, tomllib.TOMLDecodeError) as error:
        print(f"release.py: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
