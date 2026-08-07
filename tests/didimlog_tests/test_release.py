import subprocess
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path


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
        workflow = (REPO / ".github/workflows/release.yml").read_text(encoding="utf-8")

        self.assertIn("release:\n    types: [published]", workflow)
        self.assertIn("environment:\n      name: pypi", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn(".immutable", workflow)
        self.assertIn("gh release download", workflow)
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", workflow)
        for forbidden in ("PYPI_API_TOKEN", "password:", "workflow_dispatch:", "push:\n    tags:"):
            self.assertNotIn(forbidden, workflow)

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
