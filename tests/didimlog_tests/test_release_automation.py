import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "release.py"

RELEASE_PATHS = ("pyproject.toml", "uv.lock", "CHANGELOG.md")


class ReleaseAutomationTests(unittest.TestCase):
    def run_script(self, *arguments, cwd=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=cwd or REPO,
            capture_output=True,
            text=True,
        )

    def git(self, repository, *arguments, input_text=None):
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            input=input_text,
            text=True,
        ).stdout.strip()

    def release_files(self, version, *, prepared):
        files = {
            "pyproject.toml": textwrap.dedent(
                f"""\
                [project]
                name = "didimlog"
                version = "{version}"
                """
            ),
            "uv.lock": textwrap.dedent(
                f"""\
                version = 1

                [[package]]
                name = "didimlog"
                version = "{version}"
                source = {{ editable = "." }}
                """
            ),
        }
        if prepared:
            files["CHANGELOG.md"] = textwrap.dedent(
                f"""\
                # 변경 이력

                ## [Unreleased]

                ## [{version}] - 2026-08-13

                - 준비 중인 변경

                ## [0.0.2] - 2026-08-12

                - 이전 변경

                [Unreleased]: https://github.com/zhsks311/didimlog/compare/v{version}...HEAD
                [{version}]: https://github.com/zhsks311/didimlog/releases/tag/v{version}
                [0.0.2]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.2
                """
            )
        else:
            files["CHANGELOG.md"] = textwrap.dedent(
                """\
                # 변경 이력

                ## [Unreleased]

                - 준비 중인 변경

                ## [0.0.2] - 2026-08-12

                - 이전 변경

                [Unreleased]: https://github.com/zhsks311/didimlog/compare/v0.0.2...HEAD
                [0.0.2]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.2
                """
            )
        return files

    def initialize_git_repository(self, repository):
        repository.mkdir()
        self.git(repository, "init")
        self.git(repository, "config", "user.name", "Didimlog Test")
        self.git(repository, "config", "user.email", "didimlog@example.invalid")
        self.git(repository, "branch", "-M", "main")
        files = {"fixture.txt": "base\n", **self.release_files("0.0.2", prepared=False)}
        for path, content in files.items():
            (repository / path).write_text(content, encoding="utf-8")
        self.git(repository, "add", ".")
        self.git(repository, "commit", "-m", "test: base")
        return self.git(repository, "rev-parse", "HEAD")

    def commit(self, repository, message, *, files=None):
        files = files or {}
        for path, content in files.items():
            destination = repository / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        if files:
            self.git(repository, "add", "--", *files)
        self.git(repository, "commit", "--allow-empty", "-m", message)
        return self.git(repository, "rev-parse", "HEAD")

    def create_branch(self, repository, name, start):
        self.git(repository, "branch", name, start)

    def checkout(self, repository, revision):
        self.git(repository, "checkout", revision)

    def merge_commit(
        self,
        repository,
        first_parent,
        second_parent,
        message,
        *,
        tree_revision=None,
    ):
        tree = self.git(
            repository,
            "rev-parse",
            f"{tree_revision or first_parent}^{{tree}}",
        )
        return self.git(
            repository,
            "commit-tree",
            tree,
            "-p",
            first_parent,
            "-p",
            second_parent,
            input_text=f"{message}\n",
        )

    def file_at_revision(self, repository, revision, path):
        return self.git(repository, "show", f"{revision}:{path}")

    def preparation_message(
        self,
        base_sha,
        *,
        version="0.0.3",
        bump="patch",
        pr_number=42,
        release_kind="develop",
    ):
        return "\n".join(
            (
                f"Didimlog-Release-Prep: v{version}",
                f"Didimlog-Release-Base: {base_sha}",
                f"Didimlog-Release-Bump: {bump}",
                f"Didimlog-Release-PR: {pr_number}",
                f"Didimlog-Release-Kind: {release_kind}",
            )
        )

    def preparation_commit(
        self,
        repository,
        base_sha,
        *,
        version="0.0.3",
        bump="patch",
        pr_number=42,
        release_kind="develop",
        files=None,
    ):
        return self.commit(
            repository,
            self.preparation_message(
                base_sha,
                version=version,
                bump=bump,
                pr_number=pr_number,
                release_kind=release_kind,
            ),
            files=files or self.release_files(version, prepared=True),
        )

    def cancel_commit(self, repository, preparation_sha, *, files=None):
        if files is None:
            parent = self.git(repository, "rev-parse", f"{preparation_sha}^")
            self.git(repository, "checkout", parent, "--", *RELEASE_PATHS)
            files = {}
        return self.commit(
            repository,
            f"Didimlog-Release-Cancel: {preparation_sha}",
            files=files,
        )

    def inspect_pr(
        self,
        repository,
        base_sha,
        head_sha,
        *,
        pr_number=42,
        base_ref="main",
        head_ref="develop",
        selection="patch",
    ):
        result = self.run_script(
            "inspect-pr",
            "--repo",
            str(repository),
            "--base-sha",
            base_sha,
            "--head-sha",
            head_sha,
            "--pr-number",
            str(pr_number),
            "--base-ref",
            base_ref,
            "--head-ref",
            head_ref,
            "--selection",
            selection,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def pr_policy(
        self,
        command,
        repository,
        base_sha,
        head_sha,
        *,
        labels=(),
        pr_number=42,
        base_ref="main",
        head_ref="develop",
    ):
        arguments = [
            command,
            "--repo",
            str(repository),
            "--base-sha",
            base_sha,
            "--head-sha",
            head_sha,
            "--pr-number",
            str(pr_number),
            "--base-ref",
            base_ref,
            "--head-ref",
            head_ref,
        ]
        for label in labels:
            arguments.extend(("--label", label))
        result = self.run_script(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def classify_merge(self, repository, merge_sha):
        result = self.run_script(
            "classify-merge",
            "--repo",
            str(repository),
            "--merge-sha",
            merge_sha,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_inspect_pr_tracks_prepare_cancel_and_reprepare(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            first_preparation = self.preparation_commit(repository, base_sha)
            self.cancel_commit(repository, first_preparation)
            active_preparation = self.preparation_commit(repository, base_sha)

            evidence = self.inspect_pr(
                repository,
                base_sha,
                active_preparation,
            )

            self.assertEqual(evidence["state"], "prepared")
            self.assertEqual(
                evidence["active_preparation"],
                active_preparation,
            )
            self.assertEqual(evidence["base_sha"], base_sha)
            self.assertEqual(evidence["reason"], "active_preparation")

    def test_inspect_pr_rejects_dangling_cross_pr_and_duplicate_cancel_markers(self):
        cases = (
            ("dangling", "cancel_target_missing"),
            ("cross-pr", "cancel_target_other_pr"),
            ("duplicate", "invalid_cancel_marker"),
        )
        for case, reason in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                base_sha = self.initialize_git_repository(repository)
                if case == "dangling":
                    head_sha = self.commit(
                        repository,
                        f"Didimlog-Release-Cancel: {'0' * 40}",
                    )
                else:
                    preparation = self.commit(
                        repository,
                        self.preparation_message(
                            base_sha,
                            pr_number=7 if case == "cross-pr" else 42,
                        ),
                    )
                    cancel_marker = f"Didimlog-Release-Cancel: {preparation}"
                    head_sha = self.commit(
                        repository,
                        (
                            cancel_marker
                            if case == "cross-pr"
                            else f"{cancel_marker}\n{cancel_marker}"
                        ),
                    )

                evidence = self.inspect_pr(repository, base_sha, head_sha)

                self.assertEqual(evidence["state"], "invalid")
                self.assertIsNone(evidence["active_preparation"])
                self.assertEqual(evidence["base_sha"], base_sha)
                self.assertEqual(evidence["reason"], reason)

    def test_inspect_pr_ignores_markers_from_other_prs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.create_branch(repository, "foreign-pr", base_sha)
            self.checkout(repository, "foreign-pr")
            foreign_preparation = self.commit(
                repository,
                self.preparation_message(base_sha, pr_number=7),
            )
            combined = self.merge_commit(
                repository,
                base_sha,
                foreign_preparation,
                "test: combine PR histories",
            )
            self.checkout(repository, combined)
            active_preparation = self.preparation_commit(repository, base_sha)

            evidence = self.inspect_pr(
                repository,
                base_sha,
                active_preparation,
            )

            self.assertEqual(evidence["state"], "prepared")
            self.assertEqual(
                evidence["active_preparation"],
                active_preparation,
            )
            self.assertEqual(evidence["base_sha"], base_sha)
            self.assertEqual(evidence["reason"], "active_preparation")
            self.assertEqual(
                self.file_at_revision(repository, active_preparation, "fixture.txt"),
                "base",
            )

    def test_inspect_pr_rejects_cancel_from_sibling_branch(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.create_branch(repository, "preparation", base_sha)
            self.checkout(repository, "preparation")
            preparation = self.preparation_commit(repository, base_sha)
            self.checkout(repository, "main")
            sibling_cancel = self.commit(
                repository,
                f"Didimlog-Release-Cancel: {preparation}",
            )
            head_sha = self.merge_commit(
                repository,
                sibling_cancel,
                preparation,
                "test: combine sibling histories",
            )

            evidence = self.inspect_pr(repository, base_sha, head_sha)

            self.assertEqual(evidence["state"], "invalid")
            self.assertIsNone(evidence["active_preparation"])
            self.assertEqual(evidence["base_sha"], base_sha)
            self.assertEqual(
                evidence["reason"],
                "cancel_target_not_ancestor",
            )

    def test_inspect_pr_rejects_malformed_marker_from_other_pr(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            lines = self.preparation_message(
                base_sha,
                pr_number=7,
            ).splitlines()
            lines.pop()
            head_sha = self.commit(repository, "\n".join(lines))

            evidence = self.inspect_pr(repository, base_sha, head_sha)

            self.assertEqual(evidence["state"], "invalid")
            self.assertIsNone(evidence["active_preparation"])
            self.assertEqual(evidence["base_sha"], base_sha)
            self.assertEqual(
                evidence["reason"],
                "invalid_preparation_marker",
            )

    def test_inspect_pr_rejects_missing_or_duplicate_prep_fields(self):
        marker_names = ("Prep", "Base", "Bump", "PR", "Kind")
        for marker_name in marker_names:
            for mutation in ("missing", "duplicate"):
                with self.subTest(
                    marker=marker_name,
                    mutation=mutation,
                ), tempfile.TemporaryDirectory() as temporary_directory:
                    repository = Path(temporary_directory) / "repository"
                    base_sha = self.initialize_git_repository(repository)
                    lines = self.preparation_message(base_sha).splitlines()
                    marker_prefix = f"Didimlog-Release-{marker_name}:"
                    matching_line = next(
                        line for line in lines if line.startswith(marker_prefix)
                    )
                    if mutation == "missing":
                        lines.remove(matching_line)
                    else:
                        lines.append(matching_line)
                    head_sha = self.commit(repository, "\n".join(lines))

                    evidence = self.inspect_pr(repository, base_sha, head_sha)

                    self.assertEqual(evidence["state"], "invalid")
                    self.assertIsNone(evidence["active_preparation"])
                    self.assertEqual(evidence["base_sha"], base_sha)
                    self.assertEqual(
                        evidence["reason"],
                        "invalid_preparation_marker",
                    )

    def test_inspect_pr_requires_exact_release_file_diff(self):
        cases = (
            ("valid", "prepared", "active_preparation"),
            ("extra-path", "invalid", "preparation_changed_paths"),
            ("multiple-parents", "invalid", "preparation_parent_count"),
        )
        for case, state, reason in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                base_sha = self.initialize_git_repository(repository)
                files = self.release_files("0.0.3", prepared=True)
                if case == "extra-path":
                    files["fixture.txt"] = "unexpected preparation change\n"
                preparation = self.preparation_commit(
                    repository,
                    base_sha,
                    files=files,
                )
                head_sha = preparation
                if case == "multiple-parents":
                    head_sha = self.merge_commit(
                        repository,
                        preparation,
                        base_sha,
                        self.preparation_message(base_sha),
                    )

                evidence = self.inspect_pr(repository, base_sha, head_sha)

                self.assertEqual(evidence["state"], state)
                self.assertEqual(evidence["reason"], reason)
                self.assertEqual(
                    evidence["changed_paths"],
                    sorted(RELEASE_PATHS) if case == "valid" else (
                        sorted((*RELEASE_PATHS, "fixture.txt"))
                        if case == "extra-path"
                        else []
                    ),
                )
                self.assertEqual(evidence["tree_valid"], case == "valid")
                self.assertEqual(evidence["release_kind"], "develop")
                self.assertTrue(evidence["head_is_preparation"])

    def test_inspect_pr_rejects_inconsistent_release_files(self):
        cases = (
            ("bump", "preparation_version_bump_mismatch"),
            ("project", "preparation_project_version_mismatch"),
            ("lock", "preparation_lock_version_mismatch"),
            ("changelog", "preparation_changelog_mismatch"),
        )
        for case, reason in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                base_sha = self.initialize_git_repository(repository)
                marker_version = "0.0.4" if case == "bump" else "0.0.3"
                files = self.release_files(marker_version, prepared=True)
                if case == "project":
                    files["pyproject.toml"] = self.release_files(
                        "0.0.4",
                        prepared=True,
                    )["pyproject.toml"]
                elif case == "lock":
                    files["uv.lock"] = self.release_files(
                        "0.0.4",
                        prepared=True,
                    )["uv.lock"]
                elif case == "changelog":
                    files["CHANGELOG.md"] = self.release_files(
                        "0.0.4",
                        prepared=True,
                    )["CHANGELOG.md"]
                preparation = self.preparation_commit(
                    repository,
                    base_sha,
                    version=marker_version,
                    files=files,
                )

                evidence = self.inspect_pr(
                    repository,
                    base_sha,
                    preparation,
                )

                self.assertEqual(evidence["state"], "invalid")
                self.assertEqual(evidence["reason"], reason)
                self.assertEqual(evidence["changed_paths"], sorted(RELEASE_PATHS))
                self.assertFalse(evidence["tree_valid"])

    def test_inspect_pr_promotes_changelog_from_preparation_parent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            extra_entry = "- develop에서 추가한 변경"
            parent_changelog = self.release_files(
                "0.0.2",
                prepared=False,
            )["CHANGELOG.md"].replace(
                "- 준비 중인 변경",
                f"- 준비 중인 변경\n{extra_entry}",
            )
            preparation_parent = self.commit(
                repository,
                "feat: add an unreleased develop change",
                files={"CHANGELOG.md": parent_changelog},
            )
            preparation_files = self.release_files("0.0.3", prepared=True)
            preparation_files["CHANGELOG.md"] = preparation_files[
                "CHANGELOG.md"
            ].replace(
                "- 준비 중인 변경",
                f"- 준비 중인 변경\n{extra_entry}",
            )
            preparation = self.preparation_commit(
                repository,
                base_sha,
                files=preparation_files,
            )

            evidence = self.inspect_pr(
                repository,
                base_sha,
                preparation,
            )

            self.assertNotEqual(
                self.file_at_revision(
                    repository,
                    base_sha,
                    "CHANGELOG.md",
                ),
                self.file_at_revision(
                    repository,
                    preparation_parent,
                    "CHANGELOG.md",
                ),
            )
            self.assertEqual(evidence["state"], "prepared")
            self.assertEqual(evidence["active_preparation"], preparation)
            self.assertEqual(evidence["reason"], "active_preparation")
            self.assertTrue(evidence["tree_valid"])

    def test_inspect_pr_validates_release_kind_against_head_ref(self):
        cases = (
            ("develop", "develop", "prepared", "active_preparation"),
            ("hotfix/urgent", "hotfix", "prepared", "active_preparation"),
            (
                "develop",
                "hotfix",
                "invalid",
                "preparation_release_kind_mismatch",
            ),
            (
                "feature/example",
                "develop",
                "invalid",
                "preparation_release_kind_mismatch",
            ),
        )
        for head_ref, release_kind, state, reason in cases:
            with self.subTest(
                head_ref=head_ref,
                release_kind=release_kind,
            ), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                base_sha = self.initialize_git_repository(repository)
                preparation = self.preparation_commit(
                    repository,
                    base_sha,
                    release_kind=release_kind,
                )

                evidence = self.inspect_pr(
                    repository,
                    base_sha,
                    preparation,
                    head_ref=head_ref,
                )

                self.assertEqual(evidence["state"], state)
                self.assertEqual(evidence["reason"], reason)
                self.assertEqual(evidence["release_kind"], release_kind)
                self.assertTrue(evidence["tree_valid"])

    def test_inspect_pr_marks_post_prepare_commit_stale(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            preparation = self.preparation_commit(repository, base_sha)
            head_sha = self.commit(
                repository,
                "test: user change after preparation",
                files={"fixture.txt": "user change\n"},
            )

            evidence = self.inspect_pr(repository, base_sha, head_sha)

            self.assertEqual(evidence["state"], "stale")
            self.assertEqual(evidence["active_preparation"], preparation)
            self.assertEqual(evidence["reason"], "preparation_not_head")
            self.assertEqual(evidence["changed_paths"], sorted(RELEASE_PATHS))
            self.assertTrue(evidence["tree_valid"])
            self.assertEqual(evidence["release_kind"], "develop")
            self.assertFalse(evidence["head_is_preparation"])

    def test_cancel_must_restore_only_the_validated_preparation_diff(self):
        cases = (
            ("valid", "none", "no_active_preparation"),
            ("extra-path", "invalid", "cancel_changed_paths"),
            ("tree-mismatch", "invalid", "cancel_tree_mismatch"),
            ("multiple-parents", "invalid", "cancel_parent_count"),
        )
        for case, state, reason in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                base_sha = self.initialize_git_repository(repository)
                preparation = self.preparation_commit(repository, base_sha)
                if case == "valid":
                    head_sha = self.cancel_commit(repository, preparation)
                elif case == "extra-path":
                    self.git(
                        repository,
                        "checkout",
                        f"{preparation}^",
                        "--",
                        *RELEASE_PATHS,
                    )
                    head_sha = self.cancel_commit(
                        repository,
                        preparation,
                        files={"fixture.txt": "unexpected cancel change\n"},
                    )
                elif case == "tree-mismatch":
                    files = self.release_files("0.0.2", prepared=False)
                    files["CHANGELOG.md"] += "\nnot the preparation parent\n"
                    head_sha = self.cancel_commit(
                        repository,
                        preparation,
                        files=files,
                    )
                else:
                    tree = self.git(
                        repository,
                        "rev-parse",
                        f"{base_sha}^{{tree}}",
                    )
                    head_sha = self.git(
                        repository,
                        "commit-tree",
                        tree,
                        "-p",
                        preparation,
                        "-p",
                        base_sha,
                        input_text=(
                            f"Didimlog-Release-Cancel: {preparation}\n"
                        ),
                    )

                evidence = self.inspect_pr(repository, base_sha, head_sha)

                self.assertEqual(evidence["state"], state)
                self.assertEqual(evidence["reason"], reason)
                if case == "valid":
                    preparation_parent = self.git(
                        repository,
                        "rev-parse",
                        f"{preparation}^",
                    )
                    for path in RELEASE_PATHS:
                        self.assertEqual(
                            self.git(
                                repository,
                                "rev-parse",
                                f"{head_sha}:{path}",
                            ),
                            self.git(
                                repository,
                                "rev-parse",
                                f"{preparation_parent}:{path}",
                            ),
                        )

    def test_check_pr_treats_missing_label_as_none(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            head_sha = self.commit(
                repository,
                "test: ordinary PR change",
                files={"fixture.txt": "ordinary change\n"},
            )

            missing = self.pr_policy(
                "check-pr",
                repository,
                base_sha,
                head_sha,
            )
            explicit_none = self.pr_policy(
                "check-pr",
                repository,
                base_sha,
                head_sha,
                labels=("release:none",),
            )

            self.assertEqual(missing["selection"], "none")
            self.assertEqual(missing["verdict"], "PASS")
            self.assertEqual(missing["reason"], "none_valid")
            self.assertFalse(missing["desired_ready"])
            self.assertEqual(
                (
                    missing["verdict"],
                    missing["reason"],
                    missing["action_message"],
                ),
                (
                    explicit_none["verdict"],
                    explicit_none["reason"],
                    explicit_none["action_message"],
                ),
            )

    def test_check_pr_rejects_none_with_manual_version_change(self):
        cases = (
            (
                "version",
                self.release_files("0.0.3", prepared=False),
                "none_version_changed",
            ),
            (
                "lock-version",
                {
                    "uv.lock": self.release_files(
                        "0.0.3",
                        prepared=False,
                    )["uv.lock"],
                },
                "none_version_mismatch",
            ),
            (
                "public-changelog",
                {
                    "CHANGELOG.md": self.release_files(
                        "0.0.3",
                        prepared=True,
                    )["CHANGELOG.md"],
                },
                "none_public_changelog_added",
            ),
        )
        for case, files, reason in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                base_sha = self.initialize_git_repository(repository)
                head_sha = self.commit(
                    repository,
                    f"test: manual {case} change",
                    files=files,
                )

                result = self.pr_policy(
                    "check-pr",
                    repository,
                    base_sha,
                    head_sha,
                    labels=("release:none",),
                )

                self.assertEqual(result["verdict"], "FAIL")
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["desired_ready"])

    def test_check_pr_rejects_conflicting_labels_and_invalid_branch_bumps(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            for labels in (
                ("release:none", "release:patch"),
                ("release:patch", "release:minor"),
            ):
                with self.subTest(labels=labels):
                    result = self.pr_policy(
                        "check-pr",
                        repository,
                        base_sha,
                        base_sha,
                        labels=labels,
                    )
                    self.assertIsNone(result["selection"])
                    self.assertEqual(result["verdict"], "FAIL")
                    self.assertEqual(result["reason"], "selection_conflict")
                    plan = self.pr_policy(
                        "plan-reconcile",
                        repository,
                        base_sha,
                        base_sha,
                        labels=labels,
                    )
                    self.assertEqual(plan["action"], "ERROR")
                    self.assertEqual(plan["reason"], "selection_conflict")

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            head_sha = self.preparation_commit(
                repository,
                base_sha,
                release_kind="hotfix",
            )

            check = self.pr_policy(
                "check-pr",
                repository,
                base_sha,
                head_sha,
                labels=("release:patch",),
                base_ref="develop",
                head_ref="hotfix/urgent",
            )
            plan = self.pr_policy(
                "plan-reconcile",
                repository,
                base_sha,
                head_sha,
                labels=("release:patch",),
                base_ref="develop",
                head_ref="hotfix/urgent",
            )

            self.assertEqual(check["verdict"], "FAIL")
            self.assertEqual(check["reason"], "base_ref_invalid")
            self.assertEqual(plan["action"], "ERROR")
            self.assertEqual(plan["reason"], "base_ref_invalid")

        allowed = (
            ("develop", "patch", "0.0.3", "develop"),
            ("develop", "minor", "0.1.0", "develop"),
            ("develop", "major", "1.0.0", "develop"),
            ("hotfix/urgent", "patch", "0.0.3", "hotfix"),
        )
        for head_ref, bump, version, release_kind in allowed:
            with self.subTest(head_ref=head_ref, bump=bump), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                base_sha = self.initialize_git_repository(repository)
                head_sha = self.preparation_commit(
                    repository,
                    base_sha,
                    version=version,
                    bump=bump,
                    release_kind=release_kind,
                )

                result = self.pr_policy(
                    "check-pr",
                    repository,
                    base_sha,
                    head_sha,
                    labels=(f"release:{bump}",),
                    head_ref=head_ref,
                )

                self.assertEqual(result["verdict"], "PASS")
                self.assertEqual(result["reason"], "preparation_valid")

        for head_ref, bump in (
            ("hotfix/urgent", "minor"),
            ("hotfix/urgent", "major"),
            ("feature/not-releasable", "patch"),
        ):
            with self.subTest(head_ref=head_ref, bump=bump), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                base_sha = self.initialize_git_repository(repository)

                result = self.pr_policy(
                    "check-pr",
                    repository,
                    base_sha,
                    base_sha,
                    labels=(f"release:{bump}",),
                    head_ref=head_ref,
                )

                self.assertEqual(result["verdict"], "FAIL")
                self.assertEqual(result["reason"], "branch_selection_invalid")

    def test_check_pr_requires_current_main_and_current_head_preparation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            main_sha = self.initialize_git_repository(repository)
            current_head = self.preparation_commit(repository, main_sha)

            result = self.pr_policy(
                "check-pr",
                repository,
                main_sha,
                current_head,
                labels=("release:patch",),
            )

            self.assertEqual(result["verdict"], "PASS")
            self.assertEqual(result["reason"], "preparation_valid")
            self.assertTrue(result["desired_ready"])

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            old_main = self.initialize_git_repository(repository)
            current_main = self.commit(
                repository,
                "test: main advances",
                files={"fixture.txt": "current main\n"},
            )
            stale_base_marker = self.preparation_commit(
                repository,
                old_main,
            )

            result = self.pr_policy(
                "check-pr",
                repository,
                current_main,
                stale_base_marker,
                labels=("release:patch",),
            )

            self.assertEqual(result["verdict"], "FAIL")
            self.assertEqual(result["reason"], "preparation_base_not_current")

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            old_main = self.initialize_git_repository(repository)
            self.create_branch(repository, "develop", old_main)
            self.checkout(repository, "develop")
            preparation = self.preparation_commit(repository, old_main)
            self.checkout(repository, "main")
            current_main = self.commit(
                repository,
                "test: newer main",
                files={"fixture.txt": "newer main\n"},
            )

            result = self.pr_policy(
                "check-pr",
                repository,
                current_main,
                preparation,
                labels=("release:patch",),
            )

            self.assertEqual(result["verdict"], "FAIL")
            self.assertEqual(result["reason"], "head_missing_current_main")

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            main_sha = self.initialize_git_repository(repository)
            self.preparation_commit(repository, main_sha)
            post_preparation_head = self.commit(
                repository,
                "test: commit after preparation",
                files={"fixture.txt": "later change\n"},
            )

            result = self.pr_policy(
                "check-pr",
                repository,
                main_sha,
                post_preparation_head,
                labels=("release:patch",),
            )

            self.assertEqual(result["verdict"], "FAIL")
            self.assertEqual(result["reason"], "preparation_not_current_head")

    def test_plan_reconcile_cancels_and_reprepares_stale_current_head(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            main_sha = self.initialize_git_repository(repository)
            preparation = self.preparation_commit(repository, main_sha)
            head_sha = self.commit(
                repository,
                "test: user commit after preparation",
                files={"fixture.txt": "current user change\n"},
            )

            plan = self.pr_policy(
                "plan-reconcile",
                repository,
                main_sha,
                head_sha,
                labels=("release:patch",),
            )

            self.assertEqual(plan["action"], "CANCEL_AND_PREPARE")
            self.assertEqual(plan["reason"], "stale_preparation")
            self.assertEqual(plan["cancel_preparation"], preparation)
            self.assertEqual(plan["prepare_selection"], "patch")
            self.assertTrue(plan["desired_ready"])

            changed_selection = self.pr_policy(
                "plan-reconcile",
                repository,
                main_sha,
                preparation,
                labels=("release:minor",),
            )
            self.assertEqual(
                changed_selection["action"],
                "CANCEL_AND_PREPARE",
            )
            self.assertEqual(changed_selection["reason"], "selection_changed")
            self.assertEqual(
                changed_selection["cancel_preparation"],
                preparation,
            )
            self.assertEqual(
                changed_selection["prepare_selection"],
                "minor",
            )

    def test_plan_reconcile_waits_when_head_lacks_current_main(self):
        for state in ("none", "stale"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                old_main = self.initialize_git_repository(repository)
                self.create_branch(repository, "develop", old_main)
                self.checkout(repository, "develop")
                preparation = None
                if state == "stale":
                    preparation = self.preparation_commit(repository, old_main)
                head_sha = self.commit(
                    repository,
                    "test: develop change",
                    files={"fixture.txt": f"{state} develop\n"},
                )
                self.checkout(repository, "main")
                current_main = self.commit(
                    repository,
                    "test: main advances independently",
                    files={"fixture.txt": "current main\n"},
                )

                plan = self.pr_policy(
                    "plan-reconcile",
                    repository,
                    current_main,
                    head_sha,
                    labels=("release:patch",),
                )

                self.assertEqual(plan["action"], "WAIT_FOR_MAIN")
                self.assertEqual(plan["reason"], "head_missing_current_main")
                self.assertEqual(plan["cancel_preparation"], preparation)
                self.assertIsNone(plan["prepare_selection"])
                self.assertFalse(plan["desired_ready"])

    def test_plan_reconcile_repairs_ready_projection_without_new_commit(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            main_sha = self.initialize_git_repository(repository)
            head_sha = self.preparation_commit(repository, main_sha)

            without_ready = self.pr_policy(
                "plan-reconcile",
                repository,
                main_sha,
                head_sha,
                labels=("release:patch",),
            )
            with_ready = self.pr_policy(
                "plan-reconcile",
                repository,
                main_sha,
                head_sha,
                labels=("release:patch", "release:ready"),
            )

            self.assertEqual(without_ready["action"], "NOOP")
            self.assertEqual(without_ready["reason"], "preparation_current")
            self.assertTrue(without_ready["desired_ready"])
            self.assertIsNone(without_ready["cancel_preparation"])
            self.assertIsNone(without_ready["prepare_selection"])
            self.assertEqual(with_ready, without_ready)

            check_without_ready = self.pr_policy(
                "check-pr",
                repository,
                main_sha,
                head_sha,
                labels=("release:patch",),
            )
            check_with_ready = self.pr_policy(
                "check-pr",
                repository,
                main_sha,
                head_sha,
                labels=("release:patch", "release:ready"),
            )
            self.assertEqual(check_with_ready, check_without_ready)

            cancel = self.pr_policy(
                "plan-reconcile",
                repository,
                main_sha,
                head_sha,
                labels=("release:none", "release:ready"),
            )
            self.assertEqual(cancel["action"], "CANCEL")
            self.assertEqual(cancel["reason"], "cancel_preparation")
            self.assertEqual(cancel["cancel_preparation"], head_sha)
            self.assertFalse(cancel["desired_ready"])

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            main_sha = self.initialize_git_repository(repository)
            none = self.pr_policy(
                "plan-reconcile",
                repository,
                main_sha,
                main_sha,
            )
            prepare = self.pr_policy(
                "plan-reconcile",
                repository,
                main_sha,
                main_sha,
                labels=("release:minor",),
            )

            self.assertEqual(none["action"], "NOOP")
            self.assertEqual(none["reason"], "already_none")
            self.assertFalse(none["desired_ready"])
            self.assertEqual(prepare["action"], "PREPARE")
            self.assertEqual(prepare["reason"], "prepare_selection")
            self.assertEqual(prepare["prepare_selection"], "minor")
            self.assertTrue(prepare["desired_ready"])

    def test_classify_merge_publishes_valid_second_parent_preparation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.create_branch(repository, "release", base_sha)
            self.checkout(repository, "release")
            head_sha = self.preparation_commit(repository, base_sha)
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge prepared release",
                tree_revision=head_sha,
            )

            self.assertEqual(
                self.git(repository, "merge-base", base_sha, head_sha),
                base_sha,
            )
            self.assertEqual(
                self.git(repository, "merge-base", merge_sha, head_sha),
                head_sha,
            )
            self.assertEqual(
                self.classify_merge(repository, merge_sha),
                {
                    "verdict": "PUBLISH",
                    "version": "0.0.3",
                    "kind": "develop",
                    "merge_sha": merge_sha,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "reason": "validated_preparation",
                },
            )
            for path in RELEASE_PATHS:
                self.assertEqual(
                    self.git(repository, "rev-parse", f"{merge_sha}:{path}"),
                    self.git(repository, "rev-parse", f"{head_sha}:{path}"),
                )

            changed_changelog = (
                self.file_at_revision(repository, head_sha, "CHANGELOG.md")
                + "\nmerge-only change\n"
            )
            changed_tree = self.commit(
                repository,
                "test: alter merge result",
                files={"CHANGELOG.md": changed_changelog},
            )
            mismatched_merge = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge mismatched release files",
                tree_revision=changed_tree,
            )
            mismatch = self.classify_merge(repository, mismatched_merge)
            self.assertEqual(mismatch["verdict"], "ERROR")
            self.assertEqual(
                mismatch["reason"],
                "merge_release_files_mismatch",
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            old_base = self.initialize_git_repository(repository)
            self.create_branch(repository, "history", old_base)
            self.checkout(repository, "history")
            old_preparation = self.preparation_commit(
                repository,
                old_base,
            )
            cancellation = self.cancel_commit(
                repository,
                old_preparation,
            )

            self.checkout(repository, "main")
            base_sha = self.commit(
                repository,
                "test: advance main before repeated preparation",
                files={"fixture.txt": "current main\n"},
            )
            combined_history = self.merge_commit(
                repository,
                base_sha,
                cancellation,
                "test: carry cancelled old-base preparation",
                tree_revision=base_sha,
            )
            self.checkout(repository, combined_history)
            head_sha = self.preparation_commit(repository, base_sha)
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge repeated preparation",
                tree_revision=head_sha,
            )

            repeated = self.classify_merge(repository, merge_sha)
            self.assertEqual(repeated["verdict"], "PUBLISH")
            self.assertEqual(repeated["head_sha"], head_sha)
            self.assertEqual(repeated["reason"], "validated_preparation")

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.create_branch(repository, "foreign", base_sha)
            self.checkout(repository, "foreign")
            foreign_preparation = self.preparation_commit(
                repository,
                base_sha,
                pr_number=7,
            )
            combined_history = self.merge_commit(
                repository,
                base_sha,
                foreign_preparation,
                "test: carry valid foreign preparation",
                tree_revision=base_sha,
            )
            self.checkout(repository, combined_history)
            head_sha = self.preparation_commit(
                repository,
                base_sha,
                pr_number=42,
            )
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge target preparation",
                tree_revision=head_sha,
            )

            foreign = self.classify_merge(repository, merge_sha)
            self.assertEqual(foreign["verdict"], "PUBLISH")
            self.assertEqual(foreign["head_sha"], head_sha)
            self.assertEqual(foreign["reason"], "validated_preparation")

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            malformed_foreign = self.commit(
                repository,
                "\n".join(
                    self.preparation_message(
                        base_sha,
                        pr_number=7,
                    ).splitlines()[:-1]
                ),
                files=self.release_files("0.0.3", prepared=True),
            )
            combined_history = self.merge_commit(
                repository,
                base_sha,
                malformed_foreign,
                "test: carry malformed foreign marker",
                tree_revision=base_sha,
            )
            self.checkout(repository, combined_history)
            head_sha = self.preparation_commit(repository, base_sha)
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge after malformed foreign marker",
                tree_revision=head_sha,
            )

            malformed = self.classify_merge(repository, merge_sha)
            self.assertEqual(malformed["verdict"], "ERROR")
            self.assertEqual(
                malformed["reason"],
                "invalid_preparation_marker",
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.commit(
                repository,
                f"Didimlog-Release-Cancel: {'0' * 40}",
            )
            head_sha = self.preparation_commit(repository, base_sha)
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge after dangling cancellation",
                tree_revision=head_sha,
            )

            dangling = self.classify_merge(repository, merge_sha)
            self.assertEqual(dangling["verdict"], "ERROR")
            self.assertEqual(
                dangling["reason"],
                "cancel_target_missing",
            )

    def test_classify_merge_is_unchanged_by_late_cancel_and_label_changes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.create_branch(repository, "release", base_sha)
            self.checkout(repository, "release")
            head_sha = self.preparation_commit(repository, base_sha)
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge before late cancellation",
                tree_revision=head_sha,
            )
            before = self.classify_merge(repository, merge_sha)

            self.checkout(repository, head_sha)
            self.cancel_commit(repository, head_sha)

            self.assertEqual(self.classify_merge(repository, merge_sha), before)
            label_input = self.run_script(
                "classify-merge",
                "--repo",
                str(repository),
                "--merge-sha",
                merge_sha,
                "--label",
                "release:none",
            )
            self.assertNotEqual(label_input.returncode, 0)
            self.assertIn("unrecognized arguments: --label", label_input.stderr)

    def test_classify_merge_returns_no_release_when_cancel_wins(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.create_branch(repository, "release", base_sha)
            self.checkout(repository, "release")
            preparation_sha = self.preparation_commit(repository, base_sha)
            head_sha = self.cancel_commit(repository, preparation_sha)
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge cancellation",
                tree_revision=head_sha,
            )

            self.assertEqual(
                self.classify_merge(repository, merge_sha),
                {
                    "verdict": "NO_RELEASE",
                    "version": "0.0.2",
                    "kind": None,
                    "merge_sha": merge_sha,
                    "base_sha": base_sha,
                    "head_sha": head_sha,
                    "reason": "no_release_changes",
                },
            )

            no_release_cases = {
                "pyproject.toml": (
                    self.file_at_revision(
                        repository,
                        head_sha,
                        "pyproject.toml",
                    )
                    + '\ndescription = "merge-only"\n',
                    "project_file_changed_without_release",
                ),
                "uv.lock": (
                    self.file_at_revision(repository, head_sha, "uv.lock")
                    + "\n# merge-only\n",
                    "lock_file_changed_without_release",
                ),
                "CHANGELOG.md": (
                    self.file_at_revision(
                        repository,
                        head_sha,
                        "CHANGELOG.md",
                    ).replace(
                        "## [Unreleased]\n",
                        (
                            "## [Unreleased]\n\n"
                            "## [9.9.9] - 2026-08-13\n"
                        ),
                        1,
                    ),
                    "public_changelog_without_release",
                ),
            }
            for path, (content, reason) in no_release_cases.items():
                with self.subTest(path=path):
                    self.checkout(repository, head_sha)
                    changed_tree = self.commit(
                        repository,
                        f"test: invalid no-release {path}",
                        files={path: content},
                    )
                    invalid_merge = self.merge_commit(
                        repository,
                        base_sha,
                        head_sha,
                        f"test: merge invalid no-release {path}",
                        tree_revision=changed_tree,
                    )
                    result = self.classify_merge(
                        repository,
                        invalid_merge,
                    )
                    self.assertEqual(result["verdict"], "ERROR")
                    self.assertEqual(result["reason"], reason)

    def test_classify_merge_rejects_wrong_second_parent_or_base(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            old_base = self.initialize_git_repository(repository)
            base_sha = self.commit(
                repository,
                "test: advance current base",
                files={"fixture.txt": "current base\n"},
            )
            self.create_branch(repository, "release", base_sha)
            self.checkout(repository, "release")
            head_sha = self.preparation_commit(repository, base_sha)
            reversed_merge = self.merge_commit(
                repository,
                head_sha,
                base_sha,
                "test: reverse merge parents",
                tree_revision=head_sha,
            )

            reversed_result = self.classify_merge(
                repository,
                reversed_merge,
            )
            self.assertEqual(reversed_result["verdict"], "ERROR")
            self.assertEqual(
                reversed_result["reason"],
                "second_parent_not_based_on_first",
            )

            self.checkout(repository, base_sha)
            wrong_base_head = self.preparation_commit(
                repository,
                old_base,
            )
            wrong_base_merge = self.merge_commit(
                repository,
                base_sha,
                wrong_base_head,
                "test: merge preparation for wrong base",
                tree_revision=wrong_base_head,
            )
            wrong_base_result = self.classify_merge(
                repository,
                wrong_base_merge,
            )
            self.assertEqual(wrong_base_result["verdict"], "ERROR")
            self.assertEqual(
                wrong_base_result["reason"],
                "preparation_base_mismatch",
            )

    def test_classify_merge_rejects_markerless_version_increase(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            head_sha = self.commit(
                repository,
                "test: bump without release marker",
                files=self.release_files("0.0.3", prepared=True),
            )
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge markerless bump",
                tree_revision=head_sha,
            )

            result = self.classify_merge(repository, merge_sha)
            self.assertEqual(result["verdict"], "ERROR")
            self.assertEqual(result["version"], "0.0.3")
            self.assertEqual(result["reason"], "preparation_marker_missing")

    def test_classify_merge_rejects_unchanged_version_with_active_preparation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.create_branch(repository, "release", base_sha)
            self.checkout(repository, "release")
            head_sha = self.preparation_commit(repository, base_sha)
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: drop prepared release from merge tree",
                tree_revision=base_sha,
            )

            result = self.classify_merge(repository, merge_sha)
            self.assertEqual(result["verdict"], "ERROR")
            self.assertEqual(result["version"], "0.0.2")
            self.assertEqual(
                result["reason"],
                "active_preparation_without_version_increase",
            )

    def test_classify_merge_rejects_squash_rebase_and_direct_version_push(self):
        cases = ("squash", "rebase", "direct")
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                repository = Path(temporary_directory) / "repository"
                base_sha = self.initialize_git_repository(repository)
                if case == "direct":
                    pushed_sha = self.commit(
                        repository,
                        "test: direct version push",
                        files=self.release_files("0.0.3", prepared=True),
                    )
                elif case == "squash":
                    pushed_sha = self.commit(
                        repository,
                        (
                            "test: squashed release\n\n"
                            + self.preparation_message(base_sha)
                        ),
                        files=self.release_files("0.0.3", prepared=True),
                    )
                else:
                    pushed_sha = self.preparation_commit(
                        repository,
                        base_sha,
                    )

                result = self.classify_merge(repository, pushed_sha)
                self.assertEqual(result["verdict"], "ERROR")
                self.assertEqual(result["merge_sha"], pushed_sha)
                self.assertEqual(result["base_sha"], base_sha)
                self.assertIsNone(result["head_sha"])
                self.assertEqual(result["reason"], "merge_parent_count")

    def test_classify_merge_returns_hotfix_kind_only_from_validated_marker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.create_branch(repository, "hotfix", base_sha)
            self.checkout(repository, "hotfix")
            head_sha = self.preparation_commit(
                repository,
                base_sha,
                release_kind="hotfix",
            )
            merge_sha = self.merge_commit(
                repository,
                base_sha,
                head_sha,
                "test: merge hotfix",
                tree_revision=head_sha,
            )

            result = self.classify_merge(repository, merge_sha)
            self.assertEqual(result["verdict"], "PUBLISH")
            self.assertEqual(result["version"], "0.0.3")
            self.assertEqual(result["kind"], "hotfix")
            self.assertEqual(result["reason"], "validated_preparation")

        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            invalid_head = self.commit(
                repository,
                self.preparation_message(
                    base_sha,
                    release_kind="emergency",
                ),
                files=self.release_files("0.0.3", prepared=True),
            )
            invalid_merge = self.merge_commit(
                repository,
                base_sha,
                invalid_head,
                "test: merge invalid release kind",
                tree_revision=invalid_head,
            )

            result = self.classify_merge(repository, invalid_merge)
            self.assertEqual(result["verdict"], "ERROR")
            self.assertIsNone(result["kind"])
            self.assertEqual(result["reason"], "invalid_preparation_marker")

    def test_prepare_changelog_promotes_unreleased_and_updates_links(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            changelog = Path(temporary_directory) / "CHANGELOG.md"
            changelog.write_text(
                textwrap.dedent(
                    """\
                    # 변경 이력

                    ## [Unreleased]

                    ### 추가

                    - 새 기능

                    ## [0.0.2] - 2026-08-12

                    - 이전 기능

                    [Unreleased]: https://github.com/zhsks311/didimlog/compare/v0.0.2...HEAD
                    [0.0.2]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.2
                    """
                ),
                encoding="utf-8",
            )

            result = self.run_script(
                "prepare-changelog",
                "--path",
                str(changelog),
                "--version",
                "0.0.3",
                "--date",
                "2026-08-13",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "0.0.3\n")
            self.assertEqual(
                changelog.read_text(encoding="utf-8"),
                textwrap.dedent(
                    """\
                    # 변경 이력

                    ## [Unreleased]

                    ## [0.0.3] - 2026-08-13

                    ### 추가

                    - 새 기능

                    ## [0.0.2] - 2026-08-12

                    - 이전 기능

                    [Unreleased]: https://github.com/zhsks311/didimlog/compare/v0.0.3...HEAD
                    [0.0.3]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.3
                    [0.0.2]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.2
                    """
                ),
            )

    def test_prepare_changelog_rejects_empty_unreleased_section(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            changelog = Path(temporary_directory) / "CHANGELOG.md"
            original = textwrap.dedent(
                """\
                # 변경 이력

                ## [Unreleased]

                ## [0.0.2] - 2026-08-12

                - 이전 기능

                [Unreleased]: https://github.com/zhsks311/didimlog/compare/v0.0.2...HEAD
                [0.0.2]: https://github.com/zhsks311/didimlog/releases/tag/v0.0.2
                """
            )
            changelog.write_text(original, encoding="utf-8")

            result = self.run_script(
                "prepare-changelog",
                "--path",
                str(changelog),
                "--version",
                "0.0.3",
                "--date",
                "2026-08-13",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unreleased section is empty", result.stderr)
            self.assertEqual(changelog.read_text(encoding="utf-8"), original)


    def reconcile_workflow(self):
        workflow_path = REPO / ".github" / "workflows" / "prepare-release.yml"
        self.assertTrue(workflow_path.is_file())
        return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    def reconcile_step(self, workflow, job_name, step_id):
        return next(
            step
            for step in workflow["jobs"][job_name]["steps"]
            if step.get("id") == step_id
        )

    def test_reconcile_workflow_accepts_all_pr_events_and_pr_number_dispatch(self):
        workflow = self.reconcile_workflow()
        triggers = workflow.get("on", workflow.get(True))

        self.assertEqual(
            triggers["pull_request_target"]["types"],
            ["opened", "reopened", "synchronize", "labeled", "unlabeled"],
        )
        self.assertEqual(
            triggers["workflow_dispatch"]["inputs"],
            {
                "pr_number": {
                    "description": "Open pull request number to reconcile",
                    "required": True,
                    "type": "string",
                }
            },
        )
        self.assertEqual(set(triggers), {"pull_request_target", "workflow_dispatch"})
        self.assertEqual(
            set(workflow["jobs"]),
            {
                "snapshot",
                "check-start",
                "compute",
                "mutate",
                "project-ready",
                "dispatch-ci",
                "check-final",
            },
        )
        self.assertNotIn("prepare", workflow["jobs"])
        self.assertNotIn("cancel", workflow["jobs"])

    def test_reconcile_workflow_serializes_each_pr_without_cancelling(self):
        workflow = self.reconcile_workflow()
        concurrency = workflow["concurrency"]
        group = concurrency["group"]

        self.assertFalse(concurrency["cancel-in-progress"])
        self.assertIn("github.event_name == 'workflow_dispatch'", group)
        self.assertIn("inputs.pr_number", group)
        self.assertIn("github.event.pull_request.number", group)
        self.assertIn(
            "github.event_name == 'pull_request_target'",
            workflow["jobs"]["snapshot"]["if"],
        )
        self.assertIn(
            "github.event_name == 'workflow_dispatch'",
            workflow["jobs"]["snapshot"]["if"],
        )

    def test_reconcile_workflow_uses_trusted_code_and_treats_head_as_data(self):
        workflow = self.reconcile_workflow()
        compute = workflow["jobs"]["compute"]
        compute_plan = self.reconcile_step(workflow, "compute", "compute-plan")["run"]
        mutate_branch = self.reconcile_step(
            workflow, "mutate", "mutate-branch"
        )["run"]

        self.assertNotIn("environment", compute)
        self.assertNotIn("secrets.", str(compute))
        self.assertNotIn("github.token", str(compute))
        self.assertIn('"${GITHUB_WORKFLOW_SHA}"', compute_plan)
        self.assertIn('git -C trusted checkout --detach "${GITHUB_WORKFLOW_SHA}"', compute_plan)
        self.assertIn('git -C pr-data checkout --detach "${HEAD_SHA}"', compute_plan)
        self.assertIn("trusted/.github/scripts/release.py plan-reconcile", compute_plan)
        self.assertIn("--repo pr-data", compute_plan)
        self.assertIn("uv lock", compute_plan)
        self.assertIn(
            "readonly release_paths=(CHANGELOG.md pyproject.toml uv.lock)",
            compute_plan,
        )
        self.assertIn(
            'for release_path in "${release_paths[@]}"', compute_plan
        )
        self.assertIn(
            'git -C pr-data rev-parse "${cancel_sha}^:${release_path}"',
            compute_plan,
        )
        self.assertIn(
            'git -C pr-data hash-object "${release_path}"', compute_plan
        )
        self.assertLess(
            compute_plan.index('git -C pr-data revert --no-commit "${cancel_sha}"'),
            compute_plan.index('git -C pr-data hash-object "${release_path}"'),
        )
        self.assertLess(
            compute_plan.index('git -C pr-data hash-object "${release_path}"'),
            compute_plan.index("artifact/cancel.patch"),
        )
        self.assertNotIn("uv sync", compute_plan)
        self.assertNotIn("python -m unittest", compute_plan)
        self.assertNotIn("uv build", compute_plan)
        self.assertNotIn(".github/scripts/release.py", mutate_branch)
        self.assertNotIn("uv ", mutate_branch)
        self.assertNotIn("python ", mutate_branch)
        self.assertNotIn("actions/", mutate_branch)

    def test_reconcile_workflow_separates_compute_mutation_check_and_ci_permissions(self):
        workflow = self.reconcile_workflow()
        jobs = workflow["jobs"]

        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(
            jobs["snapshot"]["permissions"],
            {"contents": "read", "pull-requests": "read"},
        )
        self.assertEqual(
            jobs["check-start"]["permissions"],
            {"checks": "write", "contents": "read", "pull-requests": "read"},
        )
        self.assertEqual(
            jobs["compute"]["permissions"],
            {"contents": "read", "pull-requests": "read"},
        )
        self.assertEqual(jobs["mutate"]["permissions"], {"contents": "write"})
        self.assertEqual(
            jobs["project-ready"]["permissions"],
            {"issues": "write", "pull-requests": "read"},
        )
        self.assertEqual(
            jobs["dispatch-ci"]["permissions"],
            {"actions": "write", "contents": "read"},
        )
        self.assertEqual(
            jobs["check-final"]["permissions"],
            {"checks": "write", "contents": "read", "pull-requests": "read"},
        )
        self.assertEqual(jobs["check-start"]["needs"], ["snapshot"])
        self.assertEqual(jobs["compute"]["needs"], ["snapshot", "check-start"])
        self.assertEqual(jobs["mutate"]["needs"], ["snapshot", "compute"])
        self.assertEqual(
            jobs["project-ready"]["needs"], ["snapshot", "compute", "mutate"]
        )
        self.assertEqual(
            jobs["dispatch-ci"]["needs"], ["snapshot", "mutate", "project-ready"]
        )
        self.assertEqual(
            jobs["check-final"]["needs"],
            [
                "snapshot",
                "check-start",
                "compute",
                "mutate",
                "project-ready",
                "dispatch-ci",
            ],
        )
        self.assertIn(
            "needs.compute.outputs.changed == 'true'", jobs["mutate"]["if"]
        )
        self.assertIn(
            "needs.snapshot.outputs.mutation_allowed == 'true'",
            jobs["mutate"]["if"],
        )
        self.assertIn("always()", jobs["project-ready"]["if"])
        self.assertIn("needs.mutate.result == 'skipped'", jobs["project-ready"]["if"])
        self.assertIn("always()", jobs["check-final"]["if"])

    def test_reconcile_workflow_rechecks_full_snapshot_before_push(self):
        workflow = self.reconcile_workflow()
        snapshot = workflow["jobs"]["snapshot"]
        snapshot_run = self.reconcile_step(
            workflow, "snapshot", "live-snapshot"
        )["run"]
        validate_artifact = self.reconcile_step(
            workflow, "mutate", "validate-artifact"
        )["run"]
        mutate_branch = self.reconcile_step(
            workflow, "mutate", "mutate-branch"
        )["run"]

        self.assertEqual(
            set(snapshot["outputs"]),
            {
                "pr_number",
                "state",
                "base_repo_id",
                "head_repo_id",
                "base_ref",
                "head_ref",
                "head_sha",
                "main_sha",
                "selection",
                "labels",
                "head_repo",
                "mutation_allowed",
                "snapshot",
            },
        )
        for field in (
            "pr_number",
            "state",
            "base_repo_id",
            "head_repo_id",
            "base_ref",
            "head_ref",
            "head_sha",
            "main_sha",
            "selection",
            "labels",
        ):
            self.assertIn(field, snapshot_run)
            self.assertIn(field, mutate_branch)
        self.assertIn("sha256sum --check manifest.sha256", validate_artifact)
        self.assertIn("EXPECTED_MANIFEST_DIGEST", validate_artifact)
        self.assertIn(
            "needs.compute.outputs.manifest_digest",
            str(workflow["jobs"]["mutate"]),
        )
        self.assertIn("git -C work apply --numstat", validate_artifact)
        self.assertIn("mapfile -t patch_paths", validate_artifact)
        self.assertIn("sort -u", validate_artifact)
        self.assertIn("pyproject.toml", validate_artifact)
        self.assertIn("uv.lock", validate_artifact)
        self.assertIn("CHANGELOG.md", validate_artifact)
        self.assertIn("live_snapshot", mutate_branch)
        self.assertIn(
            'gh api "repos/${GITHUB_REPOSITORY}/pulls/${PR_NUMBER}"',
            mutate_branch,
        )
        self.assertNotIn("curl ", mutate_branch)
        self.assertLess(
            mutate_branch.index("test \"${live_snapshot}\" = \"${expected_snapshot}\""),
            mutate_branch.index("git -C work push origin"),
        )
        self.assertNotIn("git push --force", mutate_branch)
        self.assertEqual(mutate_branch.count("git -C work push origin"), 1)

    def test_reconcile_workflow_posts_release_state_to_exact_final_head(self):
        workflow = self.reconcile_workflow()
        start_check = self.reconcile_step(
            workflow, "check-start", "start-check"
        )["run"]
        final_check = self.reconcile_step(
            workflow, "check-final", "final-check"
        )["run"]

        self.assertIn('"name": "release-state"', start_check)
        self.assertIn('"status": "in_progress"', start_check)
        self.assertIn("needs.snapshot.outputs.head_sha", str(workflow["jobs"]["check-start"]))
        self.assertIn("trusted/.github/scripts/release.py check-pr", final_check)
        self.assertIn('--head-sha "${FINAL_HEAD_SHA}"', final_check)
        self.assertIn('"name": "release-state"', final_check)
        self.assertIn('"head_sha": $head_sha', final_check)
        self.assertIn('"conclusion": "failure"', final_check)
        self.assertIn("START_HEAD_SHA", final_check)
        self.assertIn("FINAL_HEAD_SHA", final_check)
        self.assertLess(
            final_check.index('"conclusion": "failure"'),
            final_check.index('"head_sha": $head_sha'),
        )
        self.assertIn("trap finalize_release_state_on_exit EXIT", final_check)
        self.assertIn('active_check_id="${final_check_id}"', final_check)
        self.assertIn('test -n "${active_check_id}"', final_check)
        self.assertIn('"status": "completed"', final_check)
        self.assertNotIn("|| true", final_check)
        self.assertLess(
            final_check.index("trap finalize_release_state_on_exit EXIT"),
            final_check.index("git -C trusted fetch"),
        )
        self.assertLess(
            final_check.rindex('--input - <<<"${final_payload}"'),
            final_check.index("finalized=true"),
        )
        self.assertLess(
            final_check.index("finalized=true"),
            final_check.index("trap - EXIT", final_check.index("finalized=true")),
        )

    def test_reconcile_workflow_projects_ready_and_redispatches_ci_after_mutation(self):
        workflow = self.reconcile_workflow()
        project_ready = self.reconcile_step(
            workflow, "project-ready", "project-ready"
        )["run"]
        dispatch_ci = self.reconcile_step(
            workflow, "dispatch-ci", "dispatch-ci"
        )["run"]
        dispatch_job = workflow["jobs"]["dispatch-ci"]

        self.assertIn("trusted/.github/scripts/release.py check-pr", project_ready)
        self.assertIn('"desired_ready"', project_ready)
        self.assertIn('issues/${PR_NUMBER}/labels', project_ready)
        self.assertIn("--method POST", project_ready)
        self.assertIn("--method DELETE", project_ready)
        self.assertIn("needs.mutate.outputs.changed == 'true'", dispatch_job["if"])
        self.assertIn(
            'gh api "repos/${GITHUB_REPOSITORY}/git/ref/heads/${HEAD_REF}"',
            dispatch_ci,
        )
        self.assertNotIn("git ls-remote", dispatch_ci)
        self.assertIn("needs.project-ready.result == 'success'", dispatch_job["if"])
        self.assertIn("needs.mutate.outputs.final_head", dispatch_ci)
        self.assertIn('gh workflow run ci.yml --ref "${HEAD_REF}"', dispatch_ci)

    def test_main_push_release_workflow_builds_once_and_publishes_verified_files(self):
        workflow_path = REPO / ".github" / "workflows" / "release.yml"
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(workflow_text)
        triggers = workflow.get("on", workflow.get(True))

        self.assertEqual(triggers, {"push": {"branches": ["main"]}})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(workflow["jobs"]), {"detect", "publish"})
        self.assertNotIn("concurrency", workflow)
        self.assertEqual(
            workflow["jobs"]["publish"]["environment"],
            {"name": "pypi", "url": "https://pypi.org/p/didimlog"},
        )
        self.assertEqual(
            workflow["jobs"]["publish"]["permissions"],
            {
                "attestations": "write",
                "contents": "write",
                "id-token": "write",
            },
        )
        self.assertIn("needs.detect.outputs.release == 'true'", workflow["jobs"]["publish"]["if"])
        self.assertEqual(
            workflow["jobs"]["publish"]["concurrency"]["group"],
            "publish-release-${{ needs.detect.outputs.version }}",
        )
        self.assertIn("python -m unittest discover -s tests -v", workflow_text)
        self.assertIn("pulls/${release_pr_number}/commits", workflow_text)
        self.assertIn('release:(patch|minor|major)', workflow_text)
        self.assertIn('"release:none"', workflow_text)
        self.assertIn('"release:ready"', workflow_text)
        self.assertIn("persist-credentials: false", workflow_text)
        self.assertEqual(workflow_text.count("uv build --out-dir dist/packages"), 1)
        self.assertIn("sha256sum --strict --check", workflow_text)
        self.assertIn("gh release create", workflow_text)
        self.assertIn("--draft", workflow_text)
        self.assertIn("gh release edit", workflow_text)
        self.assertIn("[.assets[].name] | sort", workflow_text)
        self.assertIn("--draft=false", workflow_text)
        self.assertIn(".immutable", workflow_text)
        self.assertLess(
            workflow_text.index("draft_assets="),
            workflow_text.index("gh release edit"),
        )
        self.assertIn("packages-dir: dist/packages/", workflow_text)
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", workflow_text)
        self.assertNotIn("PYPI_API_TOKEN", workflow_text)
        self.assertIn("[.urls[].filename] | sort", workflow_text)

    def test_ci_can_be_redispatched_after_the_bot_updates_develop(self):
        workflow = yaml.safe_load(
            (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        triggers = workflow.get("on", workflow.get(True))

        self.assertIn("workflow_dispatch", triggers)
        self.assertIsNone(triggers["workflow_dispatch"])


if __name__ == "__main__":
    unittest.main()
