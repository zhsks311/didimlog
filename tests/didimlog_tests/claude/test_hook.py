import io
import json
import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path
from unittest import mock

from didimlog.claude.connect import apply_connect, plan_connect
from didimlog.claude.hook import session_start
from didimlog.claude.probe import Problem, inspect
from didimlog.claude.transaction import InstallJournal
from didimlog.indexing import run_index
from didimlog.project.scaffold import apply_scaffold, plan_scaffold


class HookProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.config = self.home / ".claude"
        self.project = self.root / "project"
        self.home.mkdir()
        self.config.mkdir()
        self.project.mkdir()
        self.launcher = self.root / "bin" / "didim"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o755)

        knowledge = self.home / "knowledge"
        for relative in (
            "lessons/_global",
            "docs/_global",
            "book/_global",
            "index",
        ):
            (knowledge / relative).mkdir(parents=True, exist_ok=True)
        if shutil.which("git") is None:
            self.skipTest("git is required")
        subprocess.run(
            ["git", "init", "-q"],
            cwd=self.project,
            check=True,
            capture_output=True,
        )
        (knowledge / "MY-RULES.md").write_bytes(b"# mine\n")
        apply_scaffold(plan_scaffold(self.project))
        run_index(check=False, home=self.home, cwd=self.project)
        connect = plan_connect(
            self.config,
            launcher=self.launcher,
            home=self.home,
        )
        journal = InstallJournal(self.root / "journal.json", reset=True)
        apply_connect(connect, journal)

    def _inspect(self):
        return inspect(home=self.home, cwd=self.project, config=self.config)

    def _payload(self, stdin="{}"):
        source = io.StringIO(stdin)
        output = io.StringIO()
        with mock.patch(
            "didimlog.claude.hook.inspect",
            side_effect=lambda **_: self._inspect(),
        ):
            code = session_start(source, output)
        self.assertEqual(code, 0)
        return json.loads(output.getvalue()), source.tell()

    def test_healthy_probe_has_no_problems(self):
        self.assertEqual(self._inspect(), ())

    def test_managed_import_resource_and_launcher_problems_are_distinct(self):
        (self.config / "CLAUDE.md").write_bytes(b"# user only\n")
        (self.config / "didimlog" / "KNOWLEDGE_USAGE.md").write_bytes(b"changed\n")
        self.launcher.unlink()

        problems = self._inspect()

        self.assertIn(
            Problem(
                token="CLAUDE_IMPORT_MISSING",
                impact="Claude가 필요한 지식의 위치와 조회 절차를 받지 못합니다.",
                action="didim setup",
            ),
            problems,
        )
        self.assertIn(
            Problem(
                token="CLAUDE_RESOURCE_INVALID",
                impact="Claude 지식 사용 지침이 설치본과 일치하지 않습니다.",
                action="didim setup",
            ),
            problems,
        )
        self.assertIn(
            Problem(
                token="CLAUDE_LAUNCHER_INVALID",
                impact="새 Claude 세션에서 Didimlog 상태 확인을 실행할 수 없습니다.",
                action="didim setup",
            ),
            problems,
        )

    def test_stale_personal_and_project_indexes_are_reported_without_source_body(self):
        lesson = self.home / "knowledge" / "lessons" / "_global" / "one.md"
        lesson.write_text(
            """---
topic: hook
title: hook index
summary: sentinel summary
tags: [hook]
date: 2026-08-05
---
## 교훈
SECRET-SENTINEL-BODY
""",
            encoding="utf-8",
        )
        project_index = self.project / "knowledge" / "index" / "INDEX.md"
        project_index.write_bytes(b"stale\n")

        problems = self._inspect()

        self.assertIn("PERSONAL_INDEX_STALE", {problem.token for problem in problems})
        self.assertIn("PROJECT_INDEX_STALE", {problem.token for problem in problems})
        self.assertNotIn(
            "SECRET-SENTINEL-BODY",
            "\n".join(problem.impact for problem in problems),
        )

    def test_unconfigured_project_is_not_a_session_blocking_problem(self):
        outside = self.root / "unconfigured"
        outside.mkdir()

        problems = inspect(home=self.home, cwd=outside, config=self.config)

        self.assertNotIn(
            "PROJECT_INDEX_MISSING",
            {problem.token for problem in problems},
        )

    def test_healthy_hook_consumes_stdin_and_outputs_only_continue(self):
        payload, consumed = self._payload('{"session_id":"abc"}')

        self.assertEqual(payload, {"continue": True})
        self.assertGreater(consumed, 0)

    def test_problem_hook_uses_one_repair_command_and_leaks_no_home_or_body(self):
        (self.config / "CLAUDE.md").write_bytes(b"# missing\n")
        stdin = json.dumps(
            {
                "session_id": "secret-token-value",
                "email": "reader@example.com",
                "cwd": str(self.project),
            }
        )

        with mock.patch(
            "didimlog.claude.hook.inspect",
            side_effect=lambda **_: self._inspect(),
        ):
            payload, _ = self._payload(stdin)

        self.assertTrue(payload["continue"])
        message = payload["systemMessage"]
        self.assertEqual(message.count("didim setup"), 1)
        self.assertNotIn(str(self.home), message)
        self.assertNotIn("secret-token-value", message)
        self.assertNotIn("reader@example.com", message)
        self.assertNotIn("SECRET-SENTINEL-BODY", message)

    def test_unexpected_probe_exception_is_fail_open(self):
        source = io.StringIO("not-json-but-consumed")
        output = io.StringIO()
        with mock.patch(
            "didimlog.claude.hook.inspect",
            side_effect=RuntimeError("secret /raw/home reader@example.com"),
        ):
            code = session_start(source, output)

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["continue"])
        self.assertIn("didim doctor", payload["systemMessage"])
        self.assertNotIn("/raw/home", payload["systemMessage"])
        self.assertNotIn("reader@example.com", payload["systemMessage"])


if __name__ == "__main__":
    unittest.main()
