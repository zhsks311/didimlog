#!/usr/bin/env python3
"""Git index에 staged된 내용만 검사하는 secret scanner 계약 테스트."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from didimlog.personal import secret_scan


SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src"


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.repo = os.path.join(self.temp_dir, "repo")
        self.home = os.path.join(self.temp_dir, "home")
        os.makedirs(self.repo)
        os.makedirs(self.home)

        self.env = dict(os.environ)
        self.env["HOME"] = self.home
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["PYTHONPATH"] = str(SOURCE_ROOT) + os.pathsep + self.env.get("PYTHONPATH", "")

        self.run_command("git", "init", "-q", check=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def run_command(self, *args, check=False):
        return subprocess.run(
            args,
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
        )

    def write(self, name, content):
        path = os.path.join(self.repo, name)
        with open(path, "w", encoding="utf-8") as file:
            file.write(content)
        return path

    def stage(self, name):
        self.run_command("git", "add", "--", name, check=True)

    def scan(self):
        return self.run_command(sys.executable, "-m", secret_scan.__name__)

    def assert_blocked(self, content, expected_kind):
        self.write("lesson.md", content)
        self.stage("lesson.md")

        result = self.scan()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("SECRET_SCAN_BLOCKED", result.stderr)
        self.assertIn(expected_kind, result.stderr)
        self.assertIn("lesson.md", result.stderr)
        return result

    @staticmethod
    def synthetic(prefix, body):
        # scanner가 이 테스트 소스 자체를 차단하지 않도록 토큰 모양을 런타임에만 조합한다.
        return "token=" + prefix + body + "\n"

    def test_normal_content_passes(self):
        self.write("lesson.md", "환경변수를 사용하고 실제 키는 기록하지 않는다.\n")
        self.stage("lesson.md")

        self.assertEqual(self.scan().returncode, 0)

    def test_openai_style_key_blocked(self):
        self.assert_blocked(
            self.synthetic("s" + "k-", "abcdefghijklmnopqrstuv"),
            "API secret key",
        )

    def test_stripe_secret_key_blocked(self):
        self.assert_blocked(
            self.synthetic("s" + "k_live_", "abcdefghijklmnop1234"),
            "API secret key",
        )

    def test_stripe_restricted_key_blocked(self):
        self.assert_blocked(
            self.synthetic("r" + "k_test_", "abcdefghijklmnop1234"),
            "API secret key",
        )

    def test_stripe_organization_key_blocked(self):
        self.assert_blocked(
            self.synthetic("s" + "k_org_", "abcdefghijklmnop1234"),
            "API secret key",
        )

    def test_secret_embedded_in_word_is_blocked(self):
        key = self.synthetic("s" + "k_live_", "abcdefghijklmnop1234").strip()

        self.assert_blocked("prefix_" + key + "_suffix\n", "API secret key")

    def test_stripe_publishable_key_passes(self):
        self.write(
            "lesson.md",
            self.synthetic("p" + "k_live_", "abcdefghijklmnop1234"),
        )
        self.stage("lesson.md")

        self.assertEqual(self.scan().returncode, 0)

    def test_github_token_blocked(self):
        self.assert_blocked(
            self.synthetic("g" + "hp_", "abcdefghijklmnopqrstuvwxyz123456"),
            "GitHub token",
        )

    def test_slack_token_blocked(self):
        self.assert_blocked(
            self.synthetic("xox" + "b-", "1234567890-abcdefghijkl"),
            "Slack token",
        )

    def test_aws_key_blocked(self):
        self.assert_blocked(
            self.synthetic("AK" + "IA", "ABCDEFGHIJKLMNOP"),
            "AWS access key",
        )

    def test_jwt_blocked(self):
        self.assert_blocked(
            self.synthetic(
                "ey" + "J",
                "abcdefghijk.abcdefghijklmno.abcdefghijklmnop",
            ),
            "JWT",
        )

    def test_finding_output_contains_kind_and_path_but_not_raw_secret(self):
        secret = ("s" + "k-") + "abcdefghijklmnopqrstuv"

        result = self.assert_blocked("token=" + secret + "\n", "API secret key")

        self.assertEqual(result.stdout, "")
        self.assertNotIn(secret, result.stderr)
        self.assertNotIn("token=" + secret, result.stderr)

    def test_unstaged_secret_is_not_scanned(self):
        self.write("lesson.md", "안전한 staged 내용\n")
        self.stage("lesson.md")
        self.write(
            "lesson.md",
            self.synthetic("s" + "k-", "abcdefghijklmnopqrstuv"),
        )

        self.assertEqual(self.scan().returncode, 0)

    def test_removed_staged_file_is_not_scanned(self):
        self.write(
            "lesson.md",
            self.synthetic("s" + "k-", "abcdefghijklmnopqrstuv"),
        )
        self.stage("lesson.md")
        os.remove(os.path.join(self.repo, "lesson.md"))
        self.run_command("git", "add", "-u", check=True)

        self.assertEqual(self.scan().returncode, 0)

    def test_colon_prefixed_path_reads_exact_staged_blob(self):
        secret = ("s" + "k-") + "abcdefghijklmnopqrstuv"
        self.write("notes.md", "안전한 내용\n")
        self.write("0:notes.md", "token=" + secret + "\n")
        self.stage("notes.md")
        self.stage("0:notes.md")

        result = self.scan()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("SECRET_SCAN_BLOCKED", result.stderr)
        self.assertIn("API secret key", result.stderr)
        self.assertIn("0:notes.md", result.stderr)
        self.assertNotIn(secret, result.stderr)

    def test_git_reads_have_a_bounded_timeout(self):
        completed = subprocess.CompletedProcess(
            args=("git", "status"),
            returncode=0,
            stdout=b"",
            stderr=b"",
        )
        with mock.patch.object(
            secret_scan.subprocess,
            "run",
            return_value=completed,
        ) as run:
            secret_scan.git("status")

        self.assertEqual(run.call_args.kwargs["timeout"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
