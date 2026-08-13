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

    def initialize_git_repository(self, repository):
        repository.mkdir()
        self.git(repository, "init")
        self.git(repository, "config", "user.name", "Didimlog Test")
        self.git(repository, "config", "user.email", "didimlog@example.invalid")
        self.git(repository, "branch", "-M", "main")
        (repository / "fixture.txt").write_text("base\n", encoding="utf-8")
        self.git(repository, "add", "fixture.txt")
        self.git(repository, "commit", "-m", "test: base")
        return self.git(repository, "rev-parse", "HEAD")

    def commit(self, repository, message):
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
            first_preparation = self.commit(
                repository,
                self.preparation_message(base_sha),
            )
            self.commit(
                repository,
                f"Didimlog-Release-Cancel: {first_preparation}",
            )
            active_preparation = self.commit(
                repository,
                self.preparation_message(base_sha),
            )

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
            self.checkout(repository, "main")
            active_preparation = self.commit(
                repository,
                self.preparation_message(base_sha),
            )
            head_sha = self.merge_commit(
                repository,
                active_preparation,
                foreign_preparation,
                "test: combine PR histories",
            )

            evidence = self.inspect_pr(repository, base_sha, head_sha)

            self.assertEqual(evidence["state"], "prepared")
            self.assertEqual(
                evidence["active_preparation"],
                active_preparation,
            )
            self.assertEqual(evidence["base_sha"], base_sha)
            self.assertEqual(evidence["reason"], "active_preparation")
            self.assertEqual(
                self.file_at_revision(repository, head_sha, "fixture.txt"),
                "base",
            )

    def test_inspect_pr_rejects_cancel_from_sibling_branch(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            base_sha = self.initialize_git_repository(repository)
            self.create_branch(repository, "preparation", base_sha)
            self.checkout(repository, "preparation")
            preparation = self.commit(
                repository,
                self.preparation_message(base_sha),
            )
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
