import os
import re
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[2]
VERSION = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))[
    "project"
]["version"]
DIST_ROOT = f"didimlog-{VERSION}"
PUBLIC_SOURCE_FILES = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "pyproject.toml",
    "PKG-INFO",
    "pyproject.toml.orig",
    "uv.lock",
}
PRIVATE_PAYLOAD_MARKERS = (
    b"/Users/",
    b"/home/",
    b"orca/workspaces/",
)

class ReleaseContractTests(unittest.TestCase):
    def test_public_metadata_and_legal_documents_are_complete(self):
        project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]

        self.assertEqual(project["name"], "didimlog")
        self.assertEqual(project["version"], VERSION)
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["license-files"], ["LICENSE", "THIRD_PARTY_NOTICES.md"])
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["dependencies"], ["Markdown==3.10.2"])
        self.assertEqual(project["scripts"], {"didim": "didimlog.cli:main"})
        self.assertEqual(project["urls"]["Source"], "https://github.com/zhsks311/didimlog")
        self.assertIn("Operating System :: MacOS", project["classifiers"])
        self.assertIn("Operating System :: POSIX :: Linux", project["classifiers"])
        for minor in range(11, 15):
            self.assertIn(f"Programming Language :: Python :: 3.{minor}", project["classifiers"])

        license_text = (REPO / "LICENSE").read_text(encoding="utf-8")
        notices = (REPO / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Didimlog contributors", license_text)
        self.assertIn("MIT License", license_text)
        self.assertIn("Python-Markdown 3.10.2", notices)
        self.assertIn("BSD 3-Clause License", notices)
        self.assertIn("Mermaid 11.15.0", notices)
        self.assertIn("The MIT License (MIT)", notices)
        for name in ("CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"):
            self.assertTrue((REPO / name).is_file(), name)

    def test_ci_covers_supported_matrix_canonical_suite_build_and_wheel_smoke(self):
        workflow = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")

        self.assertIn("ubuntu-latest", workflow)
        self.assertIn("macos-latest", workflow)
        for minor in range(11, 15):
            self.assertIn(f'"3.{minor}"', workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertIn("uv build", workflow)
        self.assertIn("didim --version", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)

    def test_release_uses_main_push_oidc_and_protected_environment(self):
        workflow_text = (REPO / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        workflow = yaml.safe_load(workflow_text)
        triggers = workflow.get("on", workflow.get(True))

        self.assertEqual(triggers, {"push": {"branches": ["main"]}})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        for job in workflow["jobs"].values():
            self.assertFalse(self._contains_key(job, "password"))
        self.assertEqual(
            workflow["jobs"]["detect"].get("permissions", {}).get("contents", "read"),
            "read",
        )

        publish = workflow["jobs"]["publish"]
        self.assertEqual(publish["environment"]["name"], "pypi")
        self.assertEqual(publish["permissions"]["id-token"], "write")
        self.assertIn(".immutable", workflow_text)
        self.assertIn("gh release create", workflow_text)
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", workflow_text)
        self.assertNotIn("PYPI_API_TOKEN", workflow_text)

    def test_release_guide_matches_reconciliation_and_delivery_workflows(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        release_guide = readme.split("### 릴리스", 1)[1].split("\n## ", 1)[0]
        changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
        reconcile = yaml.safe_load(
            (REPO / ".github/workflows/prepare-release.yml").read_text(
                encoding="utf-8"
            )
        )
        delivery = yaml.safe_load(
            (REPO / ".github/workflows/release.yml").read_text(encoding="utf-8")
        )
        reconcile_triggers = reconcile.get("on", reconcile.get(True))
        delivery_triggers = delivery.get("on", delivery.get(True))

        self.assertEqual(
            reconcile_triggers["pull_request_target"]["types"],
            ["opened", "reopened", "synchronize", "labeled", "unlabeled"],
        )
        self.assertEqual(delivery_triggers, {"push": {"branches": ["main"]}})
        self.assertIn("직접 적용해야 합니다", release_guide)
        self.assertIn(
            "workflow 파일만 병합해도 이 설정은 생기지 않습니다.",
            release_guide,
        )

        final_check = next(
            step["run"]
            for step in reconcile["jobs"]["check-final"]["steps"]
            if step.get("id") == "final-check"
        )
        check_name_match = re.search(r'"name": "([^"]+)"', final_check)
        self.assertIsNotNone(check_name_match)
        check_name = check_name_match.group(1)
        self.assertEqual(check_name, "release-state")
        self.assertIn('--head-sha "${FINAL_HEAD_SHA}"', final_check)
        self.assertIn('"head_sha": $head_sha', final_check)
        self.assertIn(f"`{check_name}` 통과를 필수", release_guide)
        self.assertIn("현재 PR 커밋의 Git 이력", release_guide)
        self.assertIn("바로 그 커밋", release_guide)

        classify = next(
            step["run"]
            for step in delivery["jobs"]["detect"]["steps"]
            if step.get("name") == "Classify the immutable merge evidence"
        )
        self.assertIn("classify-merge", classify)
        self.assertIn("두 부모를 가진 merge commit만", release_guide)
        for unsupported_merge in ("squash", "rebase", "direct push"):
            self.assertIn(unsupported_merge, release_guide)

        reconcile_open_prs = delivery["jobs"]["reconcile-open-prs"]
        self.assertEqual(reconcile_open_prs["needs"], ["detect", "publish"])
        self.assertIn("준비 뒤 PR에 커밋을 추가하면", release_guide)
        self.assertIn(
            "이전 준비를 취소하고 새 커밋 기준으로 다시 준비",
            release_guide,
        )
        self.assertIn("`main`이 전진", release_guide)
        self.assertIn("최신 `main`을 반영할 때까지 기다립니다", release_guide)

        hotfix_sync = delivery["jobs"]["sync-hotfix-to-develop"]
        self.assertEqual(hotfix_sync["needs"], ["detect", "publish"])
        self.assertIn("needs.publish.result == 'success'", hotfix_sync["if"])
        self.assertIn("needs.detect.outputs.kind == 'hotfix'", hotfix_sync["if"])
        sync_run = next(
            step["run"]
            for step in hotfix_sync["steps"]
            if step.get("name") == "Sync published hotfix to develop"
        )
        self.assertIn('-f "base=develop"', sync_run)
        self.assertIn('-f "head=main"', sync_run)
        self.assertIn("patch 배포에 성공하면", release_guide)
        self.assertIn(
            "`hotfix/*` → `main` PR은 `release:patch`만 지원",
            release_guide,
        )
        self.assertIn("`main` → `develop` 동기화 PR", release_guide)

        unreleased_items = [
            line for line in unreleased.splitlines() if line.startswith("- ")
        ]
        self.assertEqual(len(unreleased_items), 1)
        for documented_outcome in (
            "PR별로 준비·취소 기록",
            "취소가 먼저 오든 병합이 먼저 오든",
            "여러 릴리스 PR을 최신 기준으로 다시 계산",
            "`main` → `develop` 동기화 PR",
        ):
            self.assertIn(documented_outcome, unreleased_items[0])

    def test_release_generates_a_verified_manifest_for_exactly_wheel_and_sdist(self):
        workflow = yaml.safe_load(
            (REPO / ".github/workflows/release.yml").read_text(encoding="utf-8")
        )
        verification_script = next(
            step["run"]
            for step in workflow["jobs"]["publish"]["steps"]
            if step.get("name") == "Verify tag, filenames, and checksums"
        )
        wheel = f"didimlog-{VERSION}-py3-none-any.whl"
        sdist = f"didimlog-{VERSION}.tar.gz"

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            packages = root / "dist" / "packages"
            packages.mkdir(parents=True)
            (packages / wheel).write_bytes(b"wheel")
            (packages / sdist).write_bytes(b"sdist")
            environment = os.environ.copy()
            environment["VERSION"] = VERSION

            complete = subprocess.run(
                ["bash", "-c", verification_script],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(complete.returncode, 0, complete.stderr)
            manifest = (root / "dist" / "SHA256SUMS").read_text(
                encoding="ascii"
            ).splitlines()
            self.assertEqual(len(manifest), 2)
            self.assertEqual(
                {line.split("  ", 1)[1] for line in manifest},
                {wheel, sdist},
            )

            (packages / "unexpected-package.whl").touch()
            extra = subprocess.run(
                ["bash", "-c", verification_script],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(
                extra.returncode,
                0,
                "verification accepted an unexpected package file",
            )

    @classmethod
    def _contains_key(cls, value, expected):
        if isinstance(value, dict):
            return expected in value or any(
                cls._contains_key(child, expected) for child in value.values()
            )
        if isinstance(value, list):
            return any(cls._contains_key(child, expected) for child in value)
        return False

    def test_wheel_and_sdist_follow_the_public_allowlist(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            subprocess.run(
                ["uv", "build", "--out-dir", str(output)],
                cwd=REPO,
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(output.glob("*.whl"))
            sdist = next(output.glob("*.tar.gz"))

            with zipfile.ZipFile(wheel) as archive:
                wheel_names = set(archive.namelist())
                wheel_payload = b"\n".join(archive.read(name) for name in sorted(wheel_names))
            self.assertTrue(any(name == "didimlog/cli.py" for name in wheel_names))
            self.assertTrue(any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_names))
            self.assertTrue(
                any(name.endswith(".dist-info/licenses/THIRD_PARTY_NOTICES.md") for name in wheel_names)
            )
            self.assertTrue(all(name.startswith(("didimlog/", f"{DIST_ROOT}.dist-info/")) for name in wheel_names))

            with tarfile.open(sdist, "r:gz") as archive:
                files = {member.name for member in archive.getmembers() if member.isfile()}
                sdist_payload = b"\n".join(
                    archive.extractfile(member).read()
                    for member in archive.getmembers()
                    if member.isfile()
                )
            relative_files = {name.removeprefix(f"{DIST_ROOT}/") for name in files}
            root_files = {name for name in relative_files if "/" not in name}
            self.assertEqual(root_files, PUBLIC_SOURCE_FILES)
            self.assertTrue(
                all(name in PUBLIC_SOURCE_FILES or name.startswith("src/didimlog/") for name in relative_files)
            )
            self.assertIn("src/didimlog/resources/personal/MERMAID-LICENSE", relative_files)
            self.assertIn("src/didimlog/resources/personal/MERMAID-VERSION", relative_files)
            self.assertIn("src/didimlog/resources/personal/mermaid.min.js.sha256", relative_files)

            payload = wheel_payload + b"\n" + sdist_payload
            for marker in PRIVATE_PAYLOAD_MARKERS:
                self.assertNotIn(marker, payload, marker.decode("ascii"))


if __name__ == "__main__":
    unittest.main()
