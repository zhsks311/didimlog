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

    def merge_commit(self, repository, first_parent, second_parent, message):
        tree = self.git(repository, "rev-parse", f"{first_parent}^{{tree}}")
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

    def test_check_release_accepts_one_increased_locked_version(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            previous = root / "previous.toml"
            current = root / "current.toml"
            lock = root / "uv.lock"
            previous.write_text('[project]\nname = "didimlog"\nversion = "0.0.2"\n', encoding="utf-8")
            current.write_text('[project]\nname = "didimlog"\nversion = "0.1.0"\n', encoding="utf-8")
            lock.write_text(
                'version = 1\n\n[[package]]\nname = "didimlog"\nversion = "0.1.0"\nsource = { editable = "." }\n',
                encoding="utf-8",
            )

            result = self.run_script(
                "check-release",
                "--previous-pyproject",
                str(previous),
                "--current-pyproject",
                str(current),
                "--lock",
                str(lock),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "0.1.0\n")

    def test_check_release_rejects_unchanged_downgraded_and_unlocked_versions(self):
        cases = (
            ("0.0.2", "0.0.2", "0.0.2", "must increase"),
            ("0.1.0", "0.0.9", "0.0.9", "must increase"),
            ("0.0.2", "0.0.3", "0.0.2", "uv.lock version"),
        )
        for previous_version, current_version, lock_version, message in cases:
            with self.subTest(
                previous=previous_version,
                current=current_version,
                lock=lock_version,
            ), tempfile.TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                previous = root / "previous.toml"
                current = root / "current.toml"
                lock = root / "uv.lock"
                previous.write_text(
                    f'[project]\nname = "didimlog"\nversion = "{previous_version}"\n',
                    encoding="utf-8",
                )
                current.write_text(
                    f'[project]\nname = "didimlog"\nversion = "{current_version}"\n',
                    encoding="utf-8",
                )
                lock.write_text(
                    'version = 1\n\n[[package]]\nname = "didimlog"\n'
                    f'version = "{lock_version}"\nsource = {{ editable = "." }}\n',
                    encoding="utf-8",
                )

                result = self.run_script(
                    "check-release",
                    "--previous-pyproject",
                    str(previous),
                    "--current-pyproject",
                    str(current),
                    "--lock",
                    str(lock),
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_release_label_workflow_prepares_and_reverts_one_release_commit(self):
        workflow_path = REPO / ".github" / "workflows" / "prepare-release.yml"
        self.assertTrue(workflow_path.is_file())
        workflow_text = workflow_path.read_text(encoding="utf-8")
        workflow = yaml.safe_load(workflow_text)
        triggers = workflow.get("on", workflow.get(True))

        self.assertEqual(
            triggers,
            {
                "pull_request_target": {"types": ["labeled", "unlabeled"]},
                "push": {"branches": ["main"]},
                "workflow_dispatch": None,
            },
        )
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(workflow["jobs"]), {"bootstrap-labels", "prepare", "cancel"})

        prepare = workflow["jobs"]["prepare"]
        self.assertEqual(
            prepare["permissions"],
            {
                "actions": "write",
                "contents": "write",
                "issues": "write",
                "pull-requests": "write",
            },
        )
        self.assertIn("github.event.pull_request.base.ref == 'main'", prepare["if"])
        self.assertIn("github.event.pull_request.head.ref == 'develop'", prepare["if"])
        self.assertIn("github.event.pull_request.state == 'open'", prepare["if"])
        for label in (
            "release:none",
            "release:patch",
            "release:minor",
            "release:major",
            "release:ready",
        ):
            self.assertIn(f'gh label create "{label}"', workflow_text)
        self.assertIn("uv version --bump", workflow_text)
        self.assertIn("prepare-changelog", workflow_text)
        self.assertIn("Didimlog-Release-Prep:", workflow_text)
        self.assertIn("git push origin HEAD:develop", workflow_text)
        self.assertIn("gh workflow run ci.yml --ref develop", workflow_text)
        self.assertIn("release:ready", workflow_text)
        self.assertIn("persist-credentials: false", workflow_text)
        self.assertNotIn("GH_TOKEN: ${{ github.token }}\n    steps:", workflow_text)

        cancel = workflow["jobs"]["cancel"]
        self.assertIn("github.event.action == 'unlabeled'", cancel["if"])
        self.assertIn("github.event.label.name == 'release:none'", cancel["if"])
        self.assertIn("git revert", workflow_text)
        self.assertIn("github.event.pull_request.state == 'open'", cancel["if"])
        self.assertGreaterEqual(workflow_text.count('"OPEN"'), 2)
        cancel_push = next(
            step["run"]
            for step in cancel["steps"]
            if step.get("name") == "Push restoration and clear ready state"
        )
        self.assertLess(
            cancel_push.index("--json state"),
            cancel_push.index("git push origin HEAD:develop"),
        )
        self.assertIn("--remove-label \"release:ready\"", workflow_text)
        self.assertNotIn("git push --force", workflow_text)

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
        self.assertIn("check-release", workflow_text)
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
