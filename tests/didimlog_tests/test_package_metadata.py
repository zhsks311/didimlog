import importlib.metadata
import pathlib
import subprocess
import sys
import tomllib
import unittest


REPO = pathlib.Path(__file__).resolve().parents[2]
PROJECT_VERSION = tomllib.loads(
    (REPO / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]


class PackageMetadataTests(unittest.TestCase):
    def test_project_metadata_defines_public_contract(self):
        with (REPO / "pyproject.toml").open("rb") as stream:
            pyproject = tomllib.load(stream)

        self.assertEqual(pyproject["project"]["name"], "didimlog")
        self.assertEqual(pyproject["project"]["version"], PROJECT_VERSION)
        self.assertEqual(pyproject["project"]["requires-python"], ">=3.11")
        self.assertEqual(pyproject["project"]["dependencies"], ["Markdown==3.10.2"])
        self.assertEqual(pyproject["project"]["scripts"], {"didim": "didimlog.cli:main"})
        self.assertEqual(pyproject["build-system"]["build-backend"], "uv_build")
        self.assertEqual(
            pyproject["build-system"]["requires"],
            ["uv_build>=0.12.2,<0.13"],
        )

    def test_runtime_version_comes_from_distribution_metadata(self):
        import didimlog

        installed_version = importlib.metadata.version("didimlog")
        self.assertEqual(installed_version, PROJECT_VERSION)
        self.assertEqual(didimlog.version(), installed_version)

    def test_console_script_points_to_cli_main(self):
        matches = [
            entry_point
            for entry_point in importlib.metadata.entry_points(group="console_scripts")
            if entry_point.name == "didim"
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].value, "didimlog.cli:main")

    def test_installed_console_script_prints_exact_version(self):
        executable = pathlib.Path(sys.executable).with_name("didim")
        self.assertTrue(executable.is_file())

        result = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"Didimlog {PROJECT_VERSION}\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
