import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.errors import DidimError
from didimlog.project.artifacts import (
    check_artifact_path_format,
    check_artifact_path_policy,
    verify_artifact_git,
    verify_artifact_local,
)
from didimlog.project.record import (
    GitUnavailable,
    PolicyError,
    SchemaError,
    validate_frontmatter,
)


RECORD_ID = "EVD-20260714-01"
ARTIFACT_PATH = "knowledge/raw/data/report.bin"
ARTIFACT_BYTES = b"didimlog artifact\n"
ARTIFACT_SHA256 = hashlib.sha256(ARTIFACT_BYTES).hexdigest()


class ErrorContractMixin:
    def assert_didim_error(self, error_type, token, exit_code, operation):
        with self.assertRaises(error_type) as raised:
            operation()
        error = raised.exception
        self.assertIsInstance(error, DidimError)
        self.assertEqual(error.token, token)
        self.assertEqual(str(error), token)
        self.assertEqual(error.exit_code, exit_code)


class ArtifactPathContractTests(ErrorContractMixin, unittest.TestCase):
    def test_strict_project_relative_path_is_preserved(self):
        self.assertEqual(
            check_artifact_path_format(ARTIFACT_PATH, RECORD_ID),
            ARTIFACT_PATH,
        )

    def test_noncanonical_or_unsafe_path_shape_is_usage_error(self):
        invalid_values = (
            None,
            "",
            "/knowledge/raw/data/report.bin",
            "./knowledge/raw/data/report.bin",
            "knowledge//raw/data/report.bin",
            "knowledge/./raw/data/report.bin",
            "knowledge/raw/../outside.bin",
            "knowledge/raw/data/",
            "knowledge\\raw\\data\\report.bin",
            "knowledge/raw/data/\x00report.bin",
            "knowledge/raw/data/\nreport.bin",
            "knowledge/raw/" + "x" * 1_025,
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.assert_didim_error(
                    SchemaError,
                    f"INVALID_ARTIFACT_PATH {RECORD_ID}",
                    2,
                    lambda value=value: check_artifact_path_format(value, RECORD_ID),
                )

    def test_policy_requires_knowledge_raw_prefix_and_returns_workspace_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()

            checked = check_artifact_path_policy(
                str(workspace), ARTIFACT_PATH, RECORD_ID
            )

            self.assertEqual(Path(checked), workspace / ARTIFACT_PATH)

    def test_policy_rejects_old_artifacts_and_escape_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            workspace.mkdir()
            paths = (
                "artifacts/report.bin",
                "knowledge/report.bin",
                "knowledge/raw/../outside.bin",
                str(Path(tmp) / "outside.bin"),
            )
            for artifact_path in paths:
                with self.subTest(artifact_path=artifact_path):
                    self.assert_didim_error(
                        PolicyError,
                        f"ARTIFACT_PATH_ESCAPE {RECORD_ID} {artifact_path}",
                        3,
                        lambda artifact_path=artifact_path: check_artifact_path_policy(
                            str(workspace), artifact_path, RECORD_ID
                        ),
                    )

    def test_policy_rejects_symlinks_even_when_target_stays_in_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            artifact_dir = workspace / "knowledge" / "raw" / "data"
            artifact_dir.mkdir(parents=True)
            target = artifact_dir / "target.bin"
            target.write_bytes(ARTIFACT_BYTES)
            link = artifact_dir / "report.bin"
            link.symlink_to(target.name)

            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_PATH_ESCAPE {RECORD_ID} {ARTIFACT_PATH}",
                3,
                lambda: check_artifact_path_policy(
                    str(workspace), ARTIFACT_PATH, RECORD_ID
                ),
            )

    def test_policy_rejects_symlink_escape_through_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "project"
            artifact_root = workspace / "knowledge" / "raw"
            outside = root / "outside"
            artifact_root.mkdir(parents=True)
            outside.mkdir()
            (outside / "report.bin").write_bytes(ARTIFACT_BYTES)
            (artifact_root / "data").symlink_to(outside, target_is_directory=True)

            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_PATH_ESCAPE {RECORD_ID} {ARTIFACT_PATH}",
                3,
                lambda: check_artifact_path_policy(
                    str(workspace), ARTIFACT_PATH, RECORD_ID
                ),
            )


class LocalArtifactContractTests(ErrorContractMixin, unittest.TestCase):
    def _workspace_with_artifact(self, root):
        workspace = Path(root) / "project"
        artifact = workspace / ARTIFACT_PATH
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(ARTIFACT_BYTES)
        return workspace, artifact

    def test_matching_regular_file_sha256_returns_bound_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, artifact = self._workspace_with_artifact(tmp)

            verified = verify_artifact_local(
                str(workspace), ARTIFACT_PATH, ARTIFACT_SHA256, RECORD_ID
            )

            self.assertEqual(Path(verified), artifact)

    def test_digest_mismatch_is_stable_policy_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _ = self._workspace_with_artifact(tmp)
            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_DIGEST_MISMATCH {RECORD_ID} {ARTIFACT_PATH}",
                3,
                lambda: verify_artifact_local(
                    str(workspace), ARTIFACT_PATH, "0" * 64, RECORD_ID
                ),
            )

    def test_missing_or_non_file_artifact_is_stable_policy_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "project"
            (workspace / "knowledge" / "raw" / "data").mkdir(parents=True)
            cases = (
                ARTIFACT_PATH,
                "knowledge/raw/data",
            )
            for artifact_path in cases:
                with self.subTest(artifact_path=artifact_path):
                    self.assert_didim_error(
                        PolicyError,
                        f"ARTIFACT_MISSING {RECORD_ID} {artifact_path}",
                        3,
                        lambda artifact_path=artifact_path: verify_artifact_local(
                            str(workspace), artifact_path, ARTIFACT_SHA256, RECORD_ID
                        ),
                    )

    def test_local_verification_does_not_follow_artifact_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, artifact = self._workspace_with_artifact(tmp)
            target = artifact.with_name("target.bin")
            artifact.replace(target)
            artifact.symlink_to(target.name)

            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_PATH_ESCAPE {RECORD_ID} {ARTIFACT_PATH}",
                3,
                lambda: verify_artifact_local(
                    str(workspace), ARTIFACT_PATH, ARTIFACT_SHA256, RECORD_ID
                ),
            )
    def test_parent_swap_after_policy_check_cannot_redirect_hashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace, artifact = self._workspace_with_artifact(tmp)
            outside = root / "outside"
            outside.mkdir()
            (outside / artifact.name).write_bytes(ARTIFACT_BYTES)
            original_parent = artifact.parent.with_name("data-original")
            real_check = check_artifact_path_policy

            def swap_parent(workspace_arg, artifact_path, record_id):
                full_path = real_check(workspace_arg, artifact_path, record_id)
                artifact.parent.rename(original_parent)
                artifact.parent.symlink_to(outside, target_is_directory=True)
                return full_path

            with mock.patch(
                "didimlog.project.artifacts.check_artifact_path_policy",
                side_effect=swap_parent,
            ):
                self.assert_didim_error(
                    PolicyError,
                    f"ARTIFACT_PATH_ESCAPE {RECORD_ID} {ARTIFACT_PATH}",
                    3,
                    lambda: verify_artifact_local(
                        str(workspace),
                        ARTIFACT_PATH,
                        ARTIFACT_SHA256,
                        RECORD_ID,
                    ),
                )


    def test_local_verification_rejects_path_escape_before_file_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "project"
            workspace.mkdir()
            (root / "outside.bin").write_bytes(ARTIFACT_BYTES)
            artifact_path = "knowledge/raw/../../outside.bin"

            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_PATH_ESCAPE {RECORD_ID} {artifact_path}",
                3,
                lambda: verify_artifact_local(
                    str(workspace), artifact_path, ARTIFACT_SHA256, RECORD_ID
                ),
            )


@unittest.skipUnless(shutil.which("git"), "git not available")
class GitArtifactContractTests(ErrorContractMixin, unittest.TestCase):
    def _git(self, git, workspace, args, env):
        return subprocess.run(
            [git, *args],
            cwd=workspace,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def _seed_repository(self, root):
        git = shutil.which("git")
        workspace = Path(root) / "project"
        home = Path(root) / "home"
        artifact = workspace / ARTIFACT_PATH
        artifact.parent.mkdir(parents=True)
        home.mkdir()
        artifact.write_bytes(ARTIFACT_BYTES)
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Didimlog Tests",
                "GIT_AUTHOR_EMAIL": "didimlog-tests@example.invalid",
                "GIT_COMMITTER_NAME": "Didimlog Tests",
                "GIT_COMMITTER_EMAIL": "didimlog-tests@example.invalid",
            }
        )
        self._git(git, workspace, ["init", "-q"], env)
        self._git(git, workspace, ["add", "--", ARTIFACT_PATH], env)
        self._git(
            git,
            workspace,
            ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
            env,
        )
        commit = self._git(git, workspace, ["rev-parse", "HEAD"], env).stdout.strip()
        blob = self._git(
            git, workspace, ["rev-parse", f"HEAD:{ARTIFACT_PATH}"], env
        ).stdout.strip()
        return workspace, commit, blob

    def test_verified_commit_and_blob_path_binding_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, commit, _ = self._seed_repository(tmp)

            result = verify_artifact_git(
                str(workspace), ARTIFACT_PATH, commit, RECORD_ID
            )

            self.assertIsNone(result)

    def test_non_commit_object_is_not_accepted_as_binding_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, blob = self._seed_repository(tmp)

            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_GIT_MISSING {RECORD_ID} {blob}",
                3,
                lambda: verify_artifact_git(
                    str(workspace), ARTIFACT_PATH, blob, RECORD_ID
                ),
            )

    def test_missing_object_is_stable_policy_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _ = self._seed_repository(tmp)
            missing_object = "0" * 40

            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_GIT_MISSING {RECORD_ID} {missing_object}",
                3,
                lambda: verify_artifact_git(
                    str(workspace), ARTIFACT_PATH, missing_object, RECORD_ID
                ),
            )

    def test_commit_must_bind_the_exact_artifact_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, commit, _ = self._seed_repository(tmp)
            missing_path = "knowledge/raw/data/missing.bin"

            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_GIT_PATH {RECORD_ID} {missing_path}",
                3,
                lambda: verify_artifact_git(
                    str(workspace), missing_path, commit, RECORD_ID
                ),
            )

    def test_git_binding_rejects_tree_at_artifact_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, commit, _ = self._seed_repository(tmp)
            directory_path = "knowledge/raw/data"

            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_GIT_PATH {RECORD_ID} {directory_path}",
                3,
                lambda: verify_artifact_git(
                    str(workspace), directory_path, commit, RECORD_ID
                ),
            )

    def test_git_binding_rejects_symlink_blob_at_artifact_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, _, _ = self._seed_repository(tmp)
            git = shutil.which("git")
            artifact = workspace / ARTIFACT_PATH
            target = artifact.with_name("target.bin")
            artifact.unlink()
            target.write_bytes(ARTIFACT_BYTES)
            artifact.symlink_to(target.name)
            env = os.environ.copy()
            env.update(
                {
                    "GIT_AUTHOR_NAME": "Didimlog Tests",
                    "GIT_AUTHOR_EMAIL": "didimlog-tests@example.invalid",
                    "GIT_COMMITTER_NAME": "Didimlog Tests",
                    "GIT_COMMITTER_EMAIL": "didimlog-tests@example.invalid",
                }
            )
            self._git(git, workspace, ["add", "--", ARTIFACT_PATH], env)
            self._git(
                git,
                workspace,
                ["-c", "commit.gpgsign=false", "commit", "-q", "-m", "symlink"],
                env,
            )
            commit = self._git(
                git,
                workspace,
                ["rev-parse", "HEAD"],
                env,
            ).stdout.strip()
            artifact.unlink()


            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_GIT_PATH {RECORD_ID} {ARTIFACT_PATH}",
                3,
                lambda: verify_artifact_git(
                    str(workspace), ARTIFACT_PATH, commit, RECORD_ID
                ),
            )

    def test_git_verification_rejects_old_artifacts_path_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, commit, _ = self._seed_repository(tmp)
            artifact_path = "artifacts/report.bin"

            self.assert_didim_error(
                PolicyError,
                f"ARTIFACT_PATH_ESCAPE {RECORD_ID} {artifact_path}",
                3,
                lambda: verify_artifact_git(
                    str(workspace), artifact_path, commit, RECORD_ID
                ),
            )


class GitFailureContractTests(ErrorContractMixin, unittest.TestCase):
    def test_git_unavailable_is_stable_exit_seven_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "didimlog.project.artifacts.shutil.which", return_value=None
        ):
            self.assert_didim_error(
                GitUnavailable,
                f"GIT_UNAVAILABLE {RECORD_ID}",
                7,
                lambda: verify_artifact_git(
                    tmp, ARTIFACT_PATH, "a" * 40, RECORD_ID
                ),
            )

    def test_git_timeout_is_bounded_and_fails_closed(self):
        observed_timeouts = []

        def time_out(argv, **kwargs):
            timeout = kwargs.get("timeout")
            observed_timeouts.append(timeout)
            if timeout is None or timeout <= 0 or timeout > 30:
                self.fail("Git verification must use a positive bounded timeout")
            raise subprocess.TimeoutExpired(argv, timeout)

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "didimlog.project.artifacts.shutil.which", return_value="/usr/bin/git"
        ), mock.patch(
            "didimlog.project.artifacts.subprocess.run", side_effect=time_out
        ):
            self.assert_didim_error(
                GitUnavailable,
                f"GIT_UNVERIFIABLE {RECORD_ID}",
                7,
                lambda: verify_artifact_git(
                    tmp, ARTIFACT_PATH, "a" * 40, RECORD_ID
                ),
            )
        self.assertEqual(len(observed_timeouts), 1)


class ArtifactBindingModeContractTests(ErrorContractMixin, unittest.TestCase):
    def _evidence_frontmatter(self):
        return {
            "schema_version": 1,
            "id": RECORD_ID,
            "type": "evidence",
            "title": "Bound artifact",
            "status": "draft",
            "scope": "project",
            "created": "2026-07-14",
            "updated": "2026-07-14",
            "version": 1,
            "tags": [],
            "sources": [],
            "artifact_path": ARTIFACT_PATH,
        }

    def test_evidence_requires_exactly_one_local_or_git_binding(self):
        base = self._evidence_frontmatter()
        both = {
            **base,
            "artifact_sha256": ARTIFACT_SHA256,
            "artifact_git": "a" * 40,
        }
        for frontmatter in (base, both):
            with self.subTest(keys=tuple(frontmatter)):
                self.assert_didim_error(
                    SchemaError,
                    f"ARTIFACT_MODE {RECORD_ID}",
                    2,
                    lambda frontmatter=frontmatter: validate_frontmatter(frontmatter),
                )

    def test_each_exclusive_binding_selects_its_mode(self):
        base = self._evidence_frontmatter()
        local = validate_frontmatter(
            {**base, "artifact_sha256": ARTIFACT_SHA256}
        )
        git = validate_frontmatter({**base, "artifact_git": "a" * 40})

        self.assertEqual(local["artifact_mode"], "local")
        self.assertEqual(local["artifact_sha256"], ARTIFACT_SHA256)
        self.assertEqual(git["artifact_mode"], "git")
        self.assertEqual(git["artifact_git"], "a" * 40)


if __name__ == "__main__":
    unittest.main()
