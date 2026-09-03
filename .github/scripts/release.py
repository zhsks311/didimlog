#!/usr/bin/env python3
"""Deterministic release preparation checks used by GitHub Actions."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
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
_RELEASE_PATHS = ("pyproject.toml", "uv.lock", "CHANGELOG.md")
_ACTION_MESSAGES = {
    "selection_conflict": (
        "Choose at most one of release:patch, release:minor, or "
        "release:major, and do not combine release:none with a bump."
    ),
    "base_ref_invalid": "Target main for release reconciliation.",
    "branch_selection_invalid": (
        "Use patch, minor, or major on develop, and patch only on hotfix/*."
    ),
    "head_missing_current_main": "Merge the latest main into the PR branch.",
    "invalid_release_evidence": (
        "Repair the release preparation history, then rerun reconciliation."
    ),
    "preparation_missing": (
        "Rerun release reconciliation to prepare the selected bump."
    ),
    "unexpected_preparation": (
        "Rerun release reconciliation to cancel the release preparation."
    ),
    "none_version_invalid": (
        "Restore valid matching project and lock versions."
    ),
    "none_version_mismatch": (
        "Make the project and lock versions identical before merging."
    ),
    "none_version_changed": (
        "Revert manual version changes or choose a release bump label."
    ),
    "none_changelog_invalid": "Restore a readable changelog before merging.",
    "none_public_changelog_added": (
        "Revert the public changelog section or choose a release bump label."
    ),
    "none_valid": "No release preparation is required.",
    "preparation_base_not_current": (
        "Rerun release reconciliation against the latest main."
    ),
    "preparation_selection_mismatch": (
        "Rerun release reconciliation for the selected bump."
    ),
    "preparation_not_current_head": (
        "Rerun release reconciliation for the current PR head."
    ),
    "preparation_valid": (
        "The release preparation matches the current PR head and main."
    ),
    "already_none": "No release commit change is required.",
    "cancel_preparation": "Cancel the active release preparation.",
    "prepare_selection": "Prepare the selected release bump.",
    "preparation_current": (
        "Keep the current release preparation and restore release:ready."
    ),
    "selection_changed": (
        "Cancel the active preparation and prepare the newly selected bump."
    ),
    "stale_preparation": (
        "Cancel the stale preparation and prepare the current PR head."
    ),
}


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
    changed_paths: tuple[str, ...]
    tree_valid: bool
    release_kind: str | None
    head_is_preparation: bool


def _git(
    repo: Path,
    *arguments: str,
    strip: bool = True,
    environment: dict[str, str] | None = None,
) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=(
                {**os.environ, **environment}
                if environment is not None
                else None
            ),
        )
    except subprocess.TimeoutExpired as error:
        raise ReleaseError(f"git_timeout:{arguments[0]}") from error
    if result.returncode != 0:
        raise ReleaseError(f"git_failed:{arguments[0]}")
    return result.stdout.strip() if strip else result.stdout


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


def _commit_parents(repo: Path, commit_sha: str) -> tuple[str, ...]:
    fields = _git(
        repo,
        "rev-list",
        "--parents",
        "-n",
        "1",
        commit_sha,
    ).split()
    return tuple(fields[1:])


def _changed_paths(
    repo: Path,
    parent_sha: str,
    commit_sha: str,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            _git(
                repo,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                parent_sha,
                commit_sha,
            ).splitlines()
        )
    )


def _revision_file(repo: Path, revision: str, path: str) -> str:
    return _git(repo, "show", f"{revision}:{path}", strip=False)


def _project_version_text(content: str) -> str:
    project = tomllib.loads(content).get("project")
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        raise ReleaseError("project_version_missing")
    value = project["version"]
    _version(value)
    return value


def _locked_project_version_text(content: str) -> str:
    packages = tomllib.loads(content).get("package", [])
    matches = [
        package.get("version")
        for package in packages
        if package.get("name") == "didimlog"
        and package.get("source") == {"editable": "."}
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise ReleaseError("lock_version_missing")
    _version(matches[0])
    return matches[0]


def _bumped_version(version: str, bump: str) -> str:
    major, minor, patch = _version(version)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _changelog_matches_preparation(
    base_changelog: str,
    prepared_changelog: str,
    version: str,
) -> bool:
    heading_pattern = re.compile(
        rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$",
        re.MULTILINE,
    )
    release_dates = heading_pattern.findall(prepared_changelog)
    if len(release_dates) != 1:
        return False
    try:
        expected = _prepared_changelog_text(
            base_changelog,
            version,
            release_dates[0],
        )
    except ReleaseError:
        return False
    return expected == prepared_changelog


def _validate_preparation_tree(
    repo: Path,
    preparation: PreparationMarker,
) -> tuple[tuple[str, ...], bool, str | None]:
    parents = _commit_parents(repo, preparation.commit_sha)
    if len(parents) != 1:
        return (), False, "preparation_parent_count"

    changed_paths = _changed_paths(
        repo,
        parents[0],
        preparation.commit_sha,
    )
    if set(changed_paths) != set(_RELEASE_PATHS):
        return changed_paths, False, "preparation_changed_paths"

    try:
        base_version = _project_version_text(
            _revision_file(
                repo,
                preparation.base_sha,
                "pyproject.toml",
            )
        )
    except (ReleaseError, tomllib.TOMLDecodeError):
        return changed_paths, False, "preparation_base_version_invalid"
    if _bumped_version(base_version, preparation.bump) != preparation.version:
        return changed_paths, False, "preparation_version_bump_mismatch"

    try:
        project_version = _project_version_text(
            _revision_file(
                repo,
                preparation.commit_sha,
                "pyproject.toml",
            )
        )
    except (ReleaseError, tomllib.TOMLDecodeError):
        return changed_paths, False, "preparation_project_version_invalid"
    if project_version != preparation.version:
        return changed_paths, False, "preparation_project_version_mismatch"

    try:
        lock_version = _locked_project_version_text(
            _revision_file(
                repo,
                preparation.commit_sha,
                "uv.lock",
            )
        )
    except (ReleaseError, tomllib.TOMLDecodeError):
        return changed_paths, False, "preparation_lock_version_invalid"
    if lock_version != preparation.version:
        return changed_paths, False, "preparation_lock_version_mismatch"

    try:
        parent_changelog = _revision_file(
            repo,
            parents[0],
            "CHANGELOG.md",
        )
        prepared_changelog = _revision_file(
            repo,
            preparation.commit_sha,
            "CHANGELOG.md",
        )
    except ReleaseError:
        return changed_paths, False, "preparation_changelog_invalid"
    if not _changelog_matches_preparation(
        parent_changelog,
        prepared_changelog,
        preparation.version,
    ):
        return changed_paths, False, "preparation_changelog_mismatch"
    return changed_paths, True, None


def _expected_release_kind(head_ref: str) -> str | None:
    if head_ref == "develop":
        return "develop"
    if head_ref.startswith("hotfix/") and head_ref != "hotfix/":
        return "hotfix"
    return None


def _three_way_merge_tree(
    repo: Path,
    base_sha: str,
    ours_sha: str,
    theirs_sha: str,
) -> str:
    object_directory = Path(
        _git(repo, "rev-parse", "--git-path", "objects")
    )
    if not object_directory.is_absolute():
        object_directory = repo / object_directory
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_objects = Path(temporary_directory) / "objects"
        temporary_objects.mkdir()
        environment = {
            "GIT_OBJECT_DIRECTORY": str(temporary_objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
                object_directory.resolve()
            ),
            "GIT_AUTHOR_NAME": "Didimlog",
            "GIT_AUTHOR_EMAIL": "didimlog@example.invalid",
            "GIT_AUTHOR_DATE": "1970-01-01T00:00:00 +0000",
            "GIT_COMMITTER_NAME": "Didimlog",
            "GIT_COMMITTER_EMAIL": "didimlog@example.invalid",
            "GIT_COMMITTER_DATE": "1970-01-01T00:00:00 +0000",
        }
        synthetic_theirs = _git(
            repo,
            "commit-tree",
            f"{theirs_sha}^{{tree}}",
            "-p",
            base_sha,
            "-m",
            "Didimlog temporary cancellation merge",
            environment=environment,
        )
        return _git(
            repo,
            "merge-tree",
            "--write-tree",
            ours_sha,
            synthetic_theirs,
            environment=environment,
        )


def _validate_cancel_tree(
    repo: Path,
    cancel: CancelMarker,
    preparation: PreparationMarker,
) -> str | None:
    cancel_parents = _commit_parents(repo, cancel.commit_sha)
    if len(cancel_parents) != 1:
        return "cancel_parent_count"
    changed_paths = _changed_paths(
        repo,
        cancel_parents[0],
        cancel.commit_sha,
    )
    if set(changed_paths) != set(_RELEASE_PATHS):
        return "cancel_changed_paths"

    preparation_parent = _commit_parents(
        repo,
        preparation.commit_sha,
    )[0]
    try:
        expected_tree = _three_way_merge_tree(
            repo,
            preparation.commit_sha,
            cancel_parents[0],
            preparation_parent,
        )
        cancel_tree = _git(
            repo,
            "rev-parse",
            f"{cancel.commit_sha}^{{tree}}",
        )
    except ReleaseError:
        return "cancel_tree_mismatch"
    if not _SHA_PATTERN.fullmatch(expected_tree) or cancel_tree != expected_tree:
        return "cancel_tree_mismatch"
    return None


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
        *,
        preparation: PreparationMarker | None = None,
        changed_paths: tuple[str, ...] = (),
        tree_valid: bool = False,
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
            changed_paths=changed_paths,
            tree_valid=tree_valid,
            release_kind=(
                preparation.release_kind
                if preparation is not None
                else None
            ),
            head_is_preparation=(
                preparation is not None
                and preparation.commit_sha == head_sha
            ),
        )

    preparations: dict[str, PreparationMarker] = {}
    cancellations: list[CancelMarker] = []
    validations: dict[str, tuple[tuple[str, ...], bool]] = {}
    try:
        for commit_sha, message in messages:
            preparation = _parse_preparation_message(message, commit_sha)
            if preparation is not None:
                preparations[commit_sha] = preparation
            cancel = _parse_cancel_message(message, commit_sha)
            if cancel is not None:
                cancellations.append(cancel)

        for preparation in preparations.values():
            if preparation.pr_number != pr_number:
                continue
            changed_paths, tree_valid, reason = _validate_preparation_tree(
                repo,
                preparation,
            )
            validations[preparation.commit_sha] = (
                changed_paths,
                tree_valid,
            )
            if reason is not None:
                return evidence(
                    "invalid",
                    None,
                    reason,
                    preparation=preparation,
                    changed_paths=changed_paths,
                    tree_valid=tree_valid,
                )
            if _expected_release_kind(head_ref) != preparation.release_kind:
                return evidence(
                    "invalid",
                    None,
                    "preparation_release_kind_mismatch",
                    preparation=preparation,
                    changed_paths=changed_paths,
                    tree_valid=True,
                )

        cancelled: set[str] = set()
        for cancel in cancellations:
            preparation = preparations.get(cancel.preparation_sha)
            if preparation is None:
                return evidence("invalid", None, "cancel_target_missing")
            if preparation.pr_number != pr_number:
                return evidence("invalid", None, "cancel_target_other_pr")
            changed_paths, tree_valid = validations[preparation.commit_sha]
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
                    preparation=preparation,
                    changed_paths=changed_paths,
                    tree_valid=tree_valid,
                )
            if cancel.preparation_sha in cancelled:
                return evidence(
                    "invalid",
                    None,
                    "cancel_target_duplicate",
                    preparation=preparation,
                    changed_paths=changed_paths,
                    tree_valid=tree_valid,
                )
            cancel_reason = _validate_cancel_tree(
                repo,
                cancel,
                preparation,
            )
            if cancel_reason is not None:
                return evidence(
                    "invalid",
                    None,
                    cancel_reason,
                    preparation=preparation,
                    changed_paths=changed_paths,
                    tree_valid=tree_valid,
                )
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

    preparation = active[0]
    changed_paths, tree_valid = validations[preparation.commit_sha]
    if preparation.commit_sha != head_sha:
        return evidence(
            "stale",
            preparation.commit_sha,
            "preparation_not_head",
            preparation=preparation,
            changed_paths=changed_paths,
            tree_valid=tree_valid,
        )
    return evidence(
        "prepared",
        preparation.commit_sha,
        "active_preparation",
        preparation=preparation,
        changed_paths=changed_paths,
        tree_valid=tree_valid,
    )


def _normalize_selection(labels: list[str]) -> str | None:
    selections = {
        label.removeprefix("release:")
        for label in labels
        if label in {
            "release:none",
            "release:patch",
            "release:minor",
            "release:major",
        }
    }
    bumps = selections & {"patch", "minor", "major"}
    if len(bumps) > 1 or ("none" in selections and bumps):
        return None
    if bumps:
        return next(iter(bumps))
    return "none"


def _branch_allows_selection(head_ref: str, selection: str) -> bool:
    if selection == "none":
        return True
    if head_ref == "develop":
        return True
    return (
        head_ref.startswith("hotfix/")
        and head_ref != "hotfix/"
        and selection == "patch"
    )


def _is_ancestor(repo: Path, ancestor_sha: str, descendant_sha: str) -> bool:
    try:
        _git(
            repo,
            "merge-base",
            "--is-ancestor",
            ancestor_sha,
            descendant_sha,
        )
    except ReleaseError:
        return False
    return True


def _active_preparation(
    repo: Path,
    evidence: ReleaseEvidence,
) -> PreparationMarker | None:
    if evidence.active_preparation is None:
        return None
    message = _git(
        repo,
        "show",
        "-s",
        "--format=%B",
        evidence.active_preparation,
    )
    preparation = _parse_preparation_message(
        message,
        evidence.active_preparation,
    )
    if preparation is None:
        raise ReleaseError("active_preparation_marker_missing")
    return preparation


def _new_public_changelog_section(
    base_changelog: str,
    head_changelog: str,
) -> bool:
    base_headings = _VERSION_HEADING_PATTERN.findall(base_changelog)
    head_headings = _VERSION_HEADING_PATTERN.findall(head_changelog)
    return any(
        head_headings.count(heading) > base_headings.count(heading)
        for heading in set(head_headings)
    )


def _none_state_reason(
    repo: Path,
    base_sha: str,
    head_sha: str,
) -> str | None:
    try:
        base_project = _project_version_text(
            _revision_file(repo, base_sha, "pyproject.toml")
        )
        base_lock = _locked_project_version_text(
            _revision_file(repo, base_sha, "uv.lock")
        )
        head_project = _project_version_text(
            _revision_file(repo, head_sha, "pyproject.toml")
        )
        head_lock = _locked_project_version_text(
            _revision_file(repo, head_sha, "uv.lock")
        )
    except (ReleaseError, tomllib.TOMLDecodeError):
        return "none_version_invalid"
    if base_project != base_lock or head_project != head_lock:
        return "none_version_mismatch"
    if head_project != base_project or head_lock != base_lock:
        return "none_version_changed"

    try:
        base_changelog = _revision_file(
            repo,
            base_sha,
            "CHANGELOG.md",
        )
        head_changelog = _revision_file(
            repo,
            head_sha,
            "CHANGELOG.md",
        )
    except ReleaseError:
        return "none_changelog_invalid"
    if _new_public_changelog_section(base_changelog, head_changelog):
        return "none_public_changelog_added"
    return None


def _merge_result(
    *,
    verdict: str,
    version: str | None,
    kind: str | None,
    merge_sha: str,
    base_sha: str | None,
    head_sha: str | None,
    reason: str,
) -> dict[str, object]:
    return {
        "verdict": verdict,
        "version": version,
        "kind": kind,
        "merge_sha": merge_sha,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "reason": reason,
    }


def _merge_preparation_history(
    repo: Path,
    base_sha: str,
    head_sha: str,
    *,
    target_pr: int | None,
) -> tuple[list[PreparationMarker], str | None]:
    preparations: dict[str, PreparationMarker] = {}
    cancellations: list[CancelMarker] = []
    try:
        for commit_sha, message in _commit_messages(repo, base_sha, head_sha):
            preparation = _parse_preparation_message(message, commit_sha)
            if preparation is not None:
                preparations[commit_sha] = preparation
            cancellation = _parse_cancel_message(message, commit_sha)
            if cancellation is not None:
                cancellations.append(cancellation)
    except ReleaseError as error:
        return [], str(error)

    relevant_preparations = {
        commit_sha: preparation
        for commit_sha, preparation in preparations.items()
        if target_pr is None or preparation.pr_number == target_pr
    }
    for preparation in relevant_preparations.values():
        _, _, reason = _validate_preparation_tree(repo, preparation)
        if reason is not None:
            return [], reason

    cancelled: set[str] = set()
    for cancellation in cancellations:
        preparation = preparations.get(cancellation.preparation_sha)
        if preparation is None:
            return [], "cancel_target_missing"
        if (
            target_pr is not None
            and preparation.pr_number != target_pr
        ):
            continue
        if cancellation.preparation_sha in cancelled:
            return [], "cancel_target_duplicate"
        if not _is_ancestor(
            repo,
            cancellation.preparation_sha,
            cancellation.commit_sha,
        ):
            return [], "cancel_target_not_ancestor"
        reason = _validate_cancel_tree(repo, cancellation, preparation)
        if reason is not None:
            return [], reason
        cancelled.add(cancellation.preparation_sha)

    return [
        preparation
        for preparation in relevant_preparations.values()
        if preparation.commit_sha not in cancelled
    ], None


def _release_file_blobs_match(
    repo: Path,
    left_sha: str,
    right_sha: str,
) -> bool:
    try:
        return all(
            _git(repo, "rev-parse", f"{left_sha}:{path}")
            == _git(repo, "rev-parse", f"{right_sha}:{path}")
            for path in _RELEASE_PATHS
        )
    except ReleaseError:
        return False


def classify_merge(repo: Path, merge_sha: str) -> dict[str, object]:
    base_sha: str | None = None
    head_sha: str | None = None
    version: str | None = None

    def result(
        verdict: str,
        reason: str,
        *,
        kind: str | None = None,
    ) -> dict[str, object]:
        return _merge_result(
            verdict=verdict,
            version=version,
            kind=kind,
            merge_sha=merge_sha,
            base_sha=base_sha,
            head_sha=head_sha,
            reason=reason,
        )

    try:
        merge_sha = _git(
            repo,
            "rev-parse",
            "--verify",
            f"{merge_sha}^{{commit}}",
        )
        parents = _commit_parents(repo, merge_sha)
    except ReleaseError:
        return result("ERROR", "merge_commit_invalid")

    if parents:
        base_sha = parents[0]
    if len(parents) == 2:
        head_sha = parents[1]
    if len(parents) != 2:
        try:
            version = _project_version_text(
                _revision_file(repo, merge_sha, "pyproject.toml")
            )
        except (ReleaseError, tomllib.TOMLDecodeError):
            pass
        return result("ERROR", "merge_parent_count")

    try:
        merge_base = _git(repo, "merge-base", base_sha, head_sha)
    except ReleaseError:
        return result("ERROR", "merge_ancestry_invalid")
    if merge_base != base_sha:
        return result("ERROR", "second_parent_not_based_on_first")

    try:
        base_project = _project_version_text(
            _revision_file(repo, base_sha, "pyproject.toml")
        )
        base_lock = _locked_project_version_text(
            _revision_file(repo, base_sha, "uv.lock")
        )
    except (ReleaseError, tomllib.TOMLDecodeError):
        return result("ERROR", "base_version_invalid")
    if base_project != base_lock:
        return result("ERROR", "base_version_mismatch")

    try:
        merge_project = _project_version_text(
            _revision_file(repo, merge_sha, "pyproject.toml")
        )
        merge_lock = _locked_project_version_text(
            _revision_file(repo, merge_sha, "uv.lock")
        )
    except (ReleaseError, tomllib.TOMLDecodeError):
        return result("ERROR", "merge_version_invalid")
    version = merge_project
    if merge_project != merge_lock:
        return result("ERROR", "merge_version_mismatch")

    if _version(merge_project) < _version(base_project):
        return result("ERROR", "version_regressed")

    if merge_project == base_project:
        active_preparations, history_reason = _merge_preparation_history(
            repo,
            base_sha,
            head_sha,
            target_pr=None,
        )
        if history_reason is not None:
            return result("ERROR", history_reason)

    if merge_project == base_project:
        if active_preparations:
            return result(
                "ERROR",
                "active_preparation_without_version_increase",
            )
        try:
            base_project_blob = _git(
                repo,
                "rev-parse",
                f"{base_sha}:pyproject.toml",
            )
            merge_project_blob = _git(
                repo,
                "rev-parse",
                f"{merge_sha}:pyproject.toml",
            )
            base_lock_blob = _git(repo, "rev-parse", f"{base_sha}:uv.lock")
            merge_lock_blob = _git(
                repo,
                "rev-parse",
                f"{merge_sha}:uv.lock",
            )
        except ReleaseError:
            return result("ERROR", "no_release_files_invalid")
        if merge_project_blob != base_project_blob:
            return result(
                "ERROR",
                "project_file_changed_without_release",
            )
        if merge_lock_blob != base_lock_blob:
            return result("ERROR", "lock_file_changed_without_release")
        try:
            base_changelog = _revision_file(
                repo,
                base_sha,
                "CHANGELOG.md",
            )
            merge_changelog = _revision_file(
                repo,
                merge_sha,
                "CHANGELOG.md",
            )
        except ReleaseError:
            return result("ERROR", "no_release_changelog_invalid")
        if _new_public_changelog_section(base_changelog, merge_changelog):
            return result("ERROR", "public_changelog_without_release")
        return result("NO_RELEASE", "no_release_changes")

    try:
        head_message = _git(
            repo,
            "show",
            "-s",
            "--format=%B",
            head_sha,
        )
        head_preparation = _parse_preparation_message(
            head_message,
            head_sha,
        )
    except ReleaseError as error:
        return result("ERROR", str(error))
    active_preparations, history_reason = _merge_preparation_history(
        repo,
        base_sha,
        head_sha,
        target_pr=(
            head_preparation.pr_number
            if head_preparation is not None
            else None
        ),
    )
    if history_reason is not None:
        return result("ERROR", history_reason)
    if head_preparation is None:
        return result("ERROR", "preparation_marker_missing")

    if not active_preparations:
        return result("ERROR", "preparation_marker_missing")
    if len(active_preparations) != 1:
        return result("ERROR", "multiple_active_preparations")
    preparation = active_preparations[0]
    if preparation.commit_sha != head_sha:
        return result("ERROR", "preparation_not_merge_head")
    if preparation.base_sha != base_sha:
        return result("ERROR", "preparation_base_mismatch")
    if preparation.version != merge_project:
        return result("ERROR", "preparation_version_mismatch")
    if not _release_file_blobs_match(repo, merge_sha, head_sha):
        return result("ERROR", "merge_release_files_mismatch")
    return result(
        "PUBLISH",
        "validated_preparation",
        kind=preparation.release_kind,
    )


def _policy_result(
    *,
    verdict: str | None,
    action: str | None,
    selection: str | None,
    reason: str,
    message_reason: str | None = None,
    desired_ready: bool,
    evidence: ReleaseEvidence | None,
    cancel_preparation: str | None = None,
    prepare_selection: str | None = None,
) -> dict[str, object]:
    return {
        "verdict": verdict,
        "action": action,
        "selection": selection,
        "reason": reason,
        "action_message": _ACTION_MESSAGES[
            message_reason if message_reason is not None else reason
        ],
        "desired_ready": desired_ready,
        "cancel_preparation": cancel_preparation,
        "prepare_selection": prepare_selection,
        "evidence": asdict(evidence) if evidence is not None else None,
    }


def _check_pr(
    repo: Path,
    base_sha: str,
    head_sha: str,
    pr_number: int,
    base_ref: str,
    head_ref: str,
    labels: list[str],
) -> dict[str, object]:
    selection = _normalize_selection(labels)
    if selection is None:
        return _policy_result(
            verdict="FAIL",
            action=None,
            selection=None,
            reason="selection_conflict",
            desired_ready=False,
            evidence=None,
        )
    if base_ref != "main":
        return _policy_result(
            verdict="FAIL",
            action=None,
            selection=selection,
            reason="base_ref_invalid",
            desired_ready=False,
            evidence=None,
        )
    if not _branch_allows_selection(head_ref, selection):
        return _policy_result(
            verdict="FAIL",
            action=None,
            selection=selection,
            reason="branch_selection_invalid",
            desired_ready=False,
            evidence=None,
        )

    evidence = _inspect_pr(
        repo,
        base_sha,
        head_sha,
        pr_number,
        base_ref,
        head_ref,
        selection,
    )
    if evidence.state == "invalid":
        return _policy_result(
            verdict="FAIL",
            action=None,
            selection=selection,
            reason=evidence.reason,
            message_reason="invalid_release_evidence",
            desired_ready=False,
            evidence=evidence,
        )
    if not _is_ancestor(repo, base_sha, head_sha):
        return _policy_result(
            verdict="FAIL",
            action=None,
            selection=selection,
            reason="head_missing_current_main",
            desired_ready=False,
            evidence=evidence,
        )

    if selection == "none":
        if evidence.state != "none":
            return _policy_result(
                verdict="FAIL",
                action=None,
                selection=selection,
                reason="unexpected_preparation",
                desired_ready=False,
                evidence=evidence,
            )
        reason = _none_state_reason(repo, base_sha, head_sha)
        if reason is not None:
            return _policy_result(
                verdict="FAIL",
                action=None,
                selection=selection,
                reason=reason,
                desired_ready=False,
                evidence=evidence,
            )
        return _policy_result(
            verdict="PASS",
            action=None,
            selection=selection,
            reason="none_valid",
            desired_ready=False,
            evidence=evidence,
        )

    if evidence.state == "none":
        return _policy_result(
            verdict="FAIL",
            action=None,
            selection=selection,
            reason="preparation_missing",
            desired_ready=False,
            evidence=evidence,
        )
    if evidence.state == "stale":
        return _policy_result(
            verdict="FAIL",
            action=None,
            selection=selection,
            reason="preparation_not_current_head",
            desired_ready=False,
            evidence=evidence,
        )

    preparation = _active_preparation(repo, evidence)
    if preparation is None:
        raise ReleaseError("active_preparation_marker_missing")
    if preparation.base_sha != base_sha.lower():
        return _policy_result(
            verdict="FAIL",
            action=None,
            selection=selection,
            reason="preparation_base_not_current",
            desired_ready=False,
            evidence=evidence,
        )
    if preparation.bump != selection:
        return _policy_result(
            verdict="FAIL",
            action=None,
            selection=selection,
            reason="preparation_selection_mismatch",
            desired_ready=False,
            evidence=evidence,
        )
    return _policy_result(
        verdict="PASS",
        action=None,
        selection=selection,
        reason="preparation_valid",
        desired_ready=True,
        evidence=evidence,
    )


def _plan_reconcile(
    repo: Path,
    base_sha: str,
    head_sha: str,
    pr_number: int,
    base_ref: str,
    head_ref: str,
    labels: list[str],
) -> dict[str, object]:
    selection = _normalize_selection(labels)
    if selection is None:
        return _policy_result(
            verdict=None,
            action="ERROR",
            selection=None,
            reason="selection_conflict",
            desired_ready=False,
            evidence=None,
        )
    if base_ref != "main":
        return _policy_result(
            verdict=None,
            action="ERROR",
            selection=selection,
            reason="base_ref_invalid",
            desired_ready=False,
            evidence=None,
        )
    if not _branch_allows_selection(head_ref, selection):
        return _policy_result(
            verdict=None,
            action="ERROR",
            selection=selection,
            reason="branch_selection_invalid",
            desired_ready=False,
            evidence=None,
        )

    evidence = _inspect_pr(
        repo,
        base_sha,
        head_sha,
        pr_number,
        base_ref,
        head_ref,
        selection,
    )
    if evidence.state == "invalid":
        return _policy_result(
            verdict=None,
            action="ERROR",
            selection=selection,
            reason=evidence.reason,
            message_reason="invalid_release_evidence",
            desired_ready=False,
            evidence=evidence,
        )

    preparation = _active_preparation(repo, evidence)
    contains_main = _is_ancestor(repo, base_sha, head_sha)
    stale = evidence.state == "stale" or (
        evidence.state == "prepared"
        and (
            preparation is None
            or preparation.base_sha != base_sha.lower()
            or not contains_main
        )
    )

    if selection == "none":
        if evidence.state in {"prepared", "stale"}:
            return _policy_result(
                verdict=None,
                action="CANCEL",
                selection=selection,
                reason="cancel_preparation",
                desired_ready=False,
                evidence=evidence,
                cancel_preparation=evidence.active_preparation,
            )
        if not contains_main:
            return _policy_result(
                verdict=None,
                action="WAIT_FOR_MAIN",
                selection=selection,
                reason="head_missing_current_main",
                desired_ready=False,
                evidence=evidence,
            )
        return _policy_result(
            verdict=None,
            action="NOOP",
            selection=selection,
            reason="already_none",
            desired_ready=False,
            evidence=evidence,
        )

    if not contains_main:
        return _policy_result(
            verdict=None,
            action="WAIT_FOR_MAIN",
            selection=selection,
            reason="head_missing_current_main",
            desired_ready=False,
            evidence=evidence,
            cancel_preparation=(
                evidence.active_preparation if stale else None
            ),
        )
    if evidence.state == "none":
        return _policy_result(
            verdict=None,
            action="PREPARE",
            selection=selection,
            reason="prepare_selection",
            desired_ready=True,
            evidence=evidence,
            prepare_selection=selection,
        )
    if stale:
        return _policy_result(
            verdict=None,
            action="CANCEL_AND_PREPARE",
            selection=selection,
            reason="stale_preparation",
            desired_ready=True,
            evidence=evidence,
            cancel_preparation=evidence.active_preparation,
            prepare_selection=selection,
        )
    if preparation is None:
        raise ReleaseError("active_preparation_marker_missing")
    if preparation.bump == selection:
        return _policy_result(
            verdict=None,
            action="NOOP",
            selection=selection,
            reason="preparation_current",
            desired_ready=True,
            evidence=evidence,
        )
    return _policy_result(
        verdict=None,
        action="CANCEL_AND_PREPARE",
        selection=selection,
        reason="selection_changed",
        desired_ready=True,
        evidence=evidence,
        cancel_preparation=evidence.active_preparation,
        prepare_selection=selection,
    )


def _version(value: str) -> tuple[int, int, int]:
    if _VERSION_PATTERN.fullmatch(value) is None:
        raise ReleaseError(f"invalid release version: {value}")
    return tuple(int(component) for component in value.split("."))


def _prepared_changelog_text(
    original: str,
    version: str,
    release_date: str,
) -> str:
    _version(version)
    try:
        date.fromisoformat(release_date)
    except ValueError as error:
        raise ReleaseError(f"invalid release date: {release_date}") from error

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
    return "\n".join(updated) + "\n"


def prepare_changelog(path: Path, version: str, release_date: str) -> None:
    original = path.read_text(encoding="utf-8")
    path.write_text(
        _prepared_changelog_text(original, version, release_date),
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    changelog = commands.add_parser("prepare-changelog")
    changelog.add_argument("--path", type=Path, required=True)
    changelog.add_argument("--version", required=True)
    changelog.add_argument("--date", required=True)

    classify = commands.add_parser("classify-merge")
    classify.add_argument("--repo", type=Path, required=True)
    classify.add_argument("--merge-sha", required=True)

    inspect_pr = commands.add_parser("inspect-pr")
    inspect_pr.add_argument("--repo", type=Path, required=True)
    inspect_pr.add_argument("--base-sha", required=True)
    inspect_pr.add_argument("--head-sha", required=True)
    inspect_pr.add_argument("--pr-number", type=int, required=True)
    inspect_pr.add_argument("--base-ref", required=True)
    inspect_pr.add_argument("--head-ref", required=True)
    inspect_pr.add_argument("--selection", required=True)

    for command in ("check-pr", "plan-reconcile"):
        policy = commands.add_parser(command)
        policy.add_argument("--repo", type=Path, required=True)
        policy.add_argument("--base-sha", required=True)
        policy.add_argument("--head-sha", required=True)
        policy.add_argument("--pr-number", type=int, required=True)
        policy.add_argument("--base-ref", required=True)
        policy.add_argument("--head-ref", required=True)
        policy.add_argument("--label", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare-changelog":
            prepare_changelog(arguments.path, arguments.version, arguments.date)
            print(arguments.version)
        elif arguments.command == "classify-merge":
            print(
                json.dumps(
                    classify_merge(
                        arguments.repo,
                        arguments.merge_sha,
                    ),
                    sort_keys=True,
                )
            )
        elif arguments.command == "inspect-pr":
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
        elif arguments.command == "check-pr":
            result = _check_pr(
                arguments.repo,
                arguments.base_sha,
                arguments.head_sha,
                arguments.pr_number,
                arguments.base_ref,
                arguments.head_ref,
                arguments.label,
            )
            print(json.dumps(result, sort_keys=True))
        else:
            result = _plan_reconcile(
                arguments.repo,
                arguments.base_sha,
                arguments.head_sha,
                arguments.pr_number,
                arguments.base_ref,
                arguments.head_ref,
                arguments.label,
            )
            print(json.dumps(result, sort_keys=True))
    except (OSError, ReleaseError, tomllib.TOMLDecodeError) as error:
        print(f"release.py: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
