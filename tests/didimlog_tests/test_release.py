import os
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[2]
VERSION = "0.0.1"
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

    def test_release_uses_published_immutable_release_oidc_and_protected_environment(self):
        workflow_text = (REPO / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        workflow = yaml.safe_load(workflow_text)
        triggers = workflow.get("on", workflow.get(True))

        self.assertEqual(triggers, {"release": {"types": ["published"]}})
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        for job in workflow["jobs"].values():
            self.assertEqual(job.get("permissions", {}).get("contents", "read"), "read")
            self.assertFalse(self._contains_key(job, "password"))

        publish = workflow["jobs"]["publish"]
        self.assertEqual(publish["environment"]["name"], "pypi")
        self.assertEqual(publish["permissions"]["id-token"], "write")
        self.assertIn(".immutable", workflow_text)
        self.assertIn("gh release download", workflow_text)
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", workflow_text)
        self.assertNotIn("PYPI_API_TOKEN", workflow_text)

    def test_release_checksum_manifest_exactly_covers_wheel_and_sdist(self):
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
        checksum = "0" * 64

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            packages = root / "packages"
            verify = root / "verify"
            binaries = root / "bin"
            packages.mkdir()
            verify.mkdir()
            binaries.mkdir()
            (packages / wheel).touch()
            (packages / sdist).touch()

            checksum_marker = root / "sha256sum-called"
            fake_sha256sum = binaries / "sha256sum"
            fake_sha256sum.write_text(
                '#!/bin/sh\n'
                'touch "$SHA256SUM_MARKER"\n'
                "exit 0\n",
                encoding="utf-8",
            )
            fake_sha256sum.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "TAG": f"v{VERSION}",
                    "PATH": f"{binaries}{os.pathsep}{environment['PATH']}",
                    "SHA256SUM_MARKER": str(checksum_marker),
                }
            )

            def run_verification(manifest: str):
                (verify / "SHA256SUMS").write_text(
                    manifest,
                    encoding="utf-8",
                )
                checksum_marker.unlink(missing_ok=True)
                result = subprocess.run(
                    ["bash", "-e", "-c", verification_script],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                return result, checksum_marker.exists()

            complete, checked_complete = run_verification(
                f"{checksum}  {wheel}\n{checksum}  {sdist}\n"
            )
            self.assertEqual(complete.returncode, 0, complete.stderr)
            self.assertTrue(checked_complete)

            incomplete, checked_incomplete = run_verification(
                f"{checksum}  {wheel}\n"
            )
            self.assertNotEqual(
                incomplete.returncode,
                0,
                "verification accepted SHA256SUMS without the sdist",
            )
            self.assertFalse(
                checked_incomplete,
                "sha256sum --check ran before manifest coverage was rejected",
            )

            extra, checked_extra = run_verification(
                f"{checksum}  {wheel}\n"
                f"{checksum}  {sdist}\n"
                f"{checksum}  unexpected-package.whl\n"
            )
            self.assertNotEqual(
                extra.returncode,
                0,
                "verification accepted an extra manifest entry",
            )
            self.assertFalse(
                checked_extra,
                "sha256sum --check ran before extra manifest data was rejected",
            )

            bsd_tagged, checked_bsd_tagged = run_verification(
                f"{checksum}  {wheel}\n"
                f"{checksum}  {sdist}\n"
                f"SHA256 ({wheel}) = {checksum}\n"
            )
            self.assertNotEqual(
                bsd_tagged.returncode,
                0,
                "verification accepted a BSD-style tagged duplicate checksum entry",
            )
            self.assertFalse(
                checked_bsd_tagged,
                "sha256sum --check ran before BSD-style manifest data was rejected",
            )

            malformed, checked_malformed = run_verification(
                f"{checksum}  {wheel}\n{sdist}\n"
            )
            self.assertNotEqual(
                malformed.returncode,
                0,
                "verification accepted a filename without a checksum",
            )
            self.assertFalse(
                checked_malformed,
                "sha256sum --check ran before malformed manifest data was rejected",
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
