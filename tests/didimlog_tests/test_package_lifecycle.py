import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from didimlog.claude.probe import inspect


REPO = Path(__file__).resolve().parents[2]
DATE = "2026-08-05"
VERSION = "0.0.1"


def _tree_bytes(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() or path.is_symlink()
    }


class PackageLifecycleTests(unittest.TestCase):
    def test_local_wheel_lifecycle_preserves_user_data_after_uninstall(self):
        uv = shutil.which("uv")
        git = shutil.which("git")
        if uv is None or git is None:
            self.skipTest("uv and git are required for the package lifecycle smoke")

        with tempfile.TemporaryDirectory(prefix="didimlog-lifecycle-") as temporary_directory:
            root = Path(temporary_directory)
            build = root / "dist"
            home = root / "home"
            config = home / ".claude"
            workspace = root / "workspace"
            virtual_environment = root / "venv"
            home.mkdir()
            config.mkdir()
            workspace.mkdir()

            subprocess.run(
                [uv, "build", "--out-dir", str(build)],
                cwd=REPO,
                check=True,
                capture_output=True,
                text=True,
            )
            wheel = next(build.glob("didimlog-0.0.1-*.whl"))
            subprocess.run(
                [uv, "venv", str(virtual_environment), "--python", sys.executable],
                check=True,
                capture_output=True,
                text=True,
            )
            python = virtual_environment / "bin" / "python"
            didim = virtual_environment / "bin" / "didim"
            subprocess.run(
                [uv, "pip", "install", "--python", str(python), str(wheel)],
                check=True,
                capture_output=True,
                text=True,
            )

            subprocess.run([git, "init", "-q"], cwd=workspace, check=True)
            subprocess.run(
                [git, "config", "user.name", "Didimlog Lifecycle"],
                cwd=workspace,
                check=True,
            )
            subprocess.run(
                [git, "config", "user.email", "didimlog@example.invalid"],
                cwd=workspace,
                check=True,
            )

            environment = os.environ.copy()
            environment.update(
                {
                    "CLAUDE_CONFIG_DIR": str(config),
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "HOME": str(home),
                    "PATH": str(virtual_environment / "bin") + os.pathsep + os.defpath,
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            environment.pop("PYTHONPATH", None)

            steps = []

            def run(*arguments, stdin=None, expected=0):
                result = subprocess.run(
                    [str(didim), "--explain-errors", *arguments],
                    cwd=workspace,
                    env=environment,
                    input=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    expected,
                    "{}\nstdout:\n{}\nstderr:\n{}".format(
                        " ".join(arguments), result.stdout, result.stderr
                    ),
                )
                return result

            self.assertEqual(run("--version").stdout.strip(), "Didimlog 0.0.1")
            steps.append("version")
            dry_run = run("setup", "--dry-run", "--config-dir", str(config))
            self.assertIn("개인 지식", dry_run.stdout)
            self.assertIn("프로젝트 근거", dry_run.stdout)
            run("setup", "--yes", "--config-dir", str(config))
            steps.extend(("setup-dry-run", "setup-apply"))

            lesson = """---
topic: lifecycle
title: 패키지 수명주기에서 원문을 보존한다
summary: 도구를 제거해도 개인 지식 원문은 남아야 한다
tags: [lifecycle, package]
date: 2026-08-05
---
## 상황
격리 wheel을 설치하고 제거한다.
## 교훈
도구의 수명과 사용자 원문의 수명을 분리한다.
## 근거
격리 환경에서 제거 전후 bytes를 비교했다.
"""
            lesson_result = run(
                "add",
                "lesson",
                "package-data-survives-uninstall",
                "--date",
                DATE,
                "--project",
                "lifecycle",
                stdin=lesson,
            )
            self.assertTrue(lesson_result.stdout.strip().endswith("package-data-survives-uninstall.md"))
            steps.append("add-lesson")

            artifact = workspace / "knowledge" / "raw" / "lifecycle.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"didimlog lifecycle evidence\n")
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            evidence_result = run(
                "add",
                "evidence",
                "--date",
                DATE,
                "--title",
                "격리 lifecycle 증거",
                "--tags",
                "lifecycle,package",
                stdin=json.dumps(
                    {
                        "artifact": "knowledge/raw/lifecycle.txt",
                        "origin": "격리 package lifecycle smoke",
                        "collection": "테스트가 직접 생성",
                        "artifact_sha256": digest,
                    },
                    ensure_ascii=False,
                ),
            )
            evidence_id = Path(evidence_result.stdout.strip()).stem
            self.assertRegex(evidence_id, r"^EVD-20260805-\d{2}$")
            steps.append("add-evidence")

            observation_result = run(
                "add",
                "observation",
                "--date",
                DATE,
                "--title",
                "제거 뒤 사용자 원문 보존",
                "--tags",
                "lifecycle,package",
                "--sources",
                evidence_id,
                stdin=json.dumps(
                    {"body": "패키지 제거 전 개인·프로젝트 원문 bytes를 보존했다."},
                    ensure_ascii=False,
                ),
            )
            observation_id = Path(observation_result.stdout.strip()).stem
            self.assertRegex(observation_id, r"^OBS-20260805-\d{2}$")
            steps.append("add-observation")

            experiment_result = run(
                "add",
                "experiment",
                "--date",
                DATE,
                "--title",
                "격리 wheel 제거 실험",
                "--tags",
                "lifecycle,package",
                "--sources",
                evidence_id,
                stdin=json.dumps(
                    {
                        "hypothesis": "패키지를 제거해도 사용자 원문은 남는다.",
                        "method": "격리 wheel을 설치해 원문을 만든 뒤 제거한다.",
                        "result": "success",
                        "contradicts": "none",
                        "interpretation": "도구 파일과 사용자 데이터의 수명이 분리됐다.",
                    },
                    ensure_ascii=False,
                ),
            )
            self.assertRegex(Path(experiment_result.stdout.strip()).stem, r"^EXP-20260805-\d{2}$")
            steps.append("add-experiment")

            index_check = run("index", "--check")
            self.assertIn("PERSONAL_INDEX_CURRENT", index_check.stdout)
            self.assertIn("PROJECT_INDEX_CURRENT", index_check.stdout)
            status = run("status", "--config-dir", str(config))
            self.assertIn("Didimlog 0.0.1", status.stdout)
            doctor = run("doctor", "--config-dir", str(config))
            self.assertIn("문제 없음", doctor.stdout)
            steps.extend(("index-check", "status", "doctor"))

            personal_root = home / "knowledge"
            project_root = workspace / "knowledge"
            personal_before = _tree_bytes(personal_root)
            project_before = _tree_bytes(project_root)

            subprocess.run(
                [uv, "pip", "uninstall", "--python", str(python), "didimlog"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertFalse(didim.exists())
            self.assertEqual(_tree_bytes(personal_root), personal_before)
            self.assertEqual(_tree_bytes(project_root), project_before)
            problems = inspect(home=home, cwd=workspace, config=config)
            self.assertIn("CLAUDE_LAUNCHER_INVALID", {problem.token for problem in problems})
            steps.extend(("uninstall", "data-preserved", "stale-launcher-diagnosed"))

            artifact_path = os.environ.get("DIDIMLOG_LIFECYCLE_ARTIFACT")
            if artifact_path:
                output = Path(artifact_path)
                output.parent.mkdir(parents=True, exist_ok=True)
                report = {
                    "artifact": "didimlog-package-lifecycle",
                    "package": "didimlog",
                    "schema_version": 1,
                    "steps": steps,
                    "user_data_preserved": True,
                    "stale_launcher_problem": "CLAUDE_LAUNCHER_INVALID",
                    "version": VERSION,
                }
                output.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )


if __name__ == "__main__":
    unittest.main()
