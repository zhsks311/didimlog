"""Didimlog 조회 지침 A/B 하네스의 판정과 보존 evidence 계약."""

import contextlib
import io
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "tools/didim-retrieval-ab"
USAGE = REPO / "src/didimlog/resources/personal/KNOWLEDGE_USAGE.md"
EVIDENCE = REPO / "artifacts/didimlog-v0.0.1-retrieval-ab.json"


def load_harness():
    spec = importlib.util.spec_from_loader(
        "didim_retrieval_ab",
        importlib.machinery.SourceFileLoader("didim_retrieval_ab", str(HARNESS)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stream(tool_calls, result="done", is_error=False, model="claude-test"):
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": model,
                "claude_code_version": "test-runtime",
            }
        )
    ]
    for name, payload in tool_calls:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": name, "input": payload}
                        ]
                    },
                }
            )
        )
    lines.append(
        json.dumps({"type": "result", "result": result, "is_error": is_error})
    )
    return "\n".join(lines)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.harness = load_harness()

    def test_keychain_service_uses_canonical_config_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            canonical = Path(temporary).resolve()
            alias = canonical.parent / "config-alias"
            alias.symlink_to(canonical)
            self.addCleanup(alias.unlink)

            self.assertEqual(
                self.harness.keychain_service(alias),
                self.harness.keychain_service(canonical),
            )

    def test_active_metadata_prefers_native_home_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root_metadata = home / ".claude.json"
            scoped = home / "profile"
            scoped.mkdir()
            root_metadata.write_text("{}", encoding="utf-8")
            (scoped / ".claude.json").write_text("{}", encoding="utf-8")

            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(scoped)}),
            ):
                self.assertEqual(
                    self.harness.active_metadata_path(),
                    root_metadata,
                )

    def test_active_secret_prefers_the_active_config_service(self):
        completed = mock.Mock(returncode=0, stdout="credential\n")
        config = Path("/active-config")
        with (
            mock.patch.dict(
                os.environ,
                {"CLAUDE_CODE_OAUTH_TOKEN": "ambient-token"},
            ),
            mock.patch.object(
                self.harness.subprocess,
                "run",
                return_value=completed,
            ) as run,
            mock.patch.object(
                self.harness,
                "active_config_dir",
                return_value=config,
            ),
        ):
            self.assertEqual(self.harness.read_active_secret(), "credential")

        self.assertEqual(
            run.call_args.args[0],
            [
                "security",
                "find-generic-password",
                "-s",
                self.harness.keychain_service(config),
                "-w",
            ],
        )
        self.assertNotIn(
            "CLAUDE_CODE_OAUTH_TOKEN",
            run.call_args.kwargs["env"],
        )

    def test_oauth_access_token_is_extracted_without_other_credential_fields(self):
        secret = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "oauth-access",
                    "refreshToken": "do-not-export",
                },
                "mcpOAuth": {"server": "do-not-export"},
            }
        )

        self.assertEqual(
            self.harness.oauth_access_token(secret),
            "oauth-access",
        )
        self.assertIsNone(self.harness.oauth_access_token("not-json"))

    def test_invalid_oauth_secret_stops_before_creating_a_sandbox(self):
        stderr = io.StringIO()
        with (
            mock.patch.object(
                self.harness.shutil,
                "which",
                return_value="/bin/tool",
            ),
            mock.patch.object(
                self.harness,
                "claude_executable",
                return_value="/bin/claude",
            ),
            mock.patch.object(
                self.harness,
                "read_active_secret",
                return_value="{}",
            ),
            mock.patch.object(self.harness.tempfile, "mkdtemp") as mkdtemp,
            contextlib.redirect_stderr(stderr),
        ):
            result = self.harness.main([])

        self.assertEqual(result, 2)
        mkdtemp.assert_not_called()
        self.assertEqual(
            stderr.getvalue(),
            "Claude 로그인 정보에서 OAuth access token을 찾을 수 없습니다\n",
        )

    def test_seed_auth_copies_metadata_without_creating_a_keychain_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "active.json"
            source.write_text(
                json.dumps(
                    {
                        "userID": "user",
                        "oauthAccount": {"accountUuid": "account"},
                        "claudeCodeFirstTokenDate": "2026-01-01",
                    }
                ),
                encoding="utf-8",
            )
            config = root / "case" / ".claude"
            with (
                mock.patch.object(
                    self.harness,
                    "active_metadata_path",
                    return_value=source,
                ),
                mock.patch.object(
                    self.harness.subprocess,
                    "run",
                ) as run,
            ):
                self.harness.seed_auth(config)

            run.assert_not_called()
            self.assertEqual(
                json.loads((config / ".claude.json").read_text(encoding="utf-8")),
                {
                    "userID": "user",
                    "oauthAccount": {"accountUuid": "account"},
                    "claudeCodeFirstTokenDate": "2026-01-01",
                    "hasCompletedOnboarding": True,
                },
            )

    def test_non_claude_run_removes_ambient_oauth_token(self):
        completed = mock.Mock(returncode=0, stdout="")
        with (
            mock.patch.dict(
                os.environ,
                {"CLAUDE_CODE_OAUTH_TOKEN": "ambient-token"},
            ),
            mock.patch.object(
                self.harness.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            self.harness.run(["git", "--version"])

        self.assertNotIn(
            "CLAUDE_CODE_OAUTH_TOKEN",
            run.call_args.kwargs["env"],
        )

    def test_claude_executable_skips_frogprogsy_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed_dir = root / "managed"
            native_dir = root / "native"
            managed_dir.mkdir()
            native_dir.mkdir()
            managed = managed_dir / "claude"
            native = native_dir / "claude"
            managed.write_text(
                "#!/usr/bin/env bun\nrunClaudeLauncherProcess(process.argv)\n",
                encoding="utf-8",
            )
            native.write_bytes(b"\x00native-claude")
            managed.chmod(0o755)
            native.chmod(0o755)

            with mock.patch.dict(
                "os.environ",
                {"PATH": "{}{}{}".format(managed_dir, os.pathsep, native_dir)},
            ):
                self.assertEqual(self.harness.claude_executable(), str(native.resolve()))

    def test_session_pins_home_config_and_path_to_the_case_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            home = root / "home"
            config = home / ".claude"
            binary = root / "bin"
            log = root / "session.jsonl"
            for path in (project, config, binary):
                path.mkdir(parents=True)
            completed = mock.Mock(
                stdout=stream([]),
                returncode=0,
            )

            with (
                mock.patch.dict(
                    os.environ,
                    {"CLAUDE_CODE_OAUTH_TOKEN": "ambient-token"},
                ),
                mock.patch.object(
                    self.harness.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                self.harness.session(
                    project,
                    home,
                    config,
                    "claude",
                    "prompt",
                    log,
                    oauth_token="oauth-access",
                    path_prefix=binary,
                )

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["HOME"], str(home))
        self.assertEqual(environment["CLAUDE_CONFIG_DIR"], str(config))
        self.assertEqual(
            environment["CLAUDE_CODE_OAUTH_TOKEN"],
            "oauth-access",
        )
        self.assertEqual(
            environment["PATH"].split(os.pathsep)[0],
            str(binary),
        )

    def test_session_passes_only_required_environment_and_access_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            home = root / "home"
            config = home / ".claude"
            log = root / "session.jsonl"
            for path in (project, config):
                path.mkdir(parents=True)
            completed = mock.Mock(stdout=stream([]), returncode=0)

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": "/usr/bin",
                        "LANG": "ko_KR.UTF-8",
                        "ANTHROPIC_API_KEY": "must-not-leak",
                        "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                        "CLAUDE_CODE_OAUTH_TOKEN": "ambient-token",
                    },
                    clear=True,
                ),
                mock.patch.object(
                    self.harness.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                self.harness.session(
                    project,
                    home,
                    config,
                    "claude",
                    "prompt",
                    log,
                    oauth_token="oauth-access",
                )

        self.assertEqual(
            run.call_args.kwargs["env"],
            {
                "PATH": "/usr/bin",
                "LANG": "ko_KR.UTF-8",
                "HOME": str(home),
                "CLAUDE_CONFIG_DIR": str(config),
                "PYTHONDONTWRITEBYTECODE": "1",
                "CLAUDE_CODE_OAUTH_TOKEN": "oauth-access",
            },
        )


    def test_each_trial_case_receives_a_distinct_home_and_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            sandbox = Path(temporary)
            logs = sandbox / "logs"
            logs.mkdir()
            observed = []

            def case_paths(case_root, *_args):
                home = case_root / "home"
                return home / ".claude", home

            def installed_case(case_root, *_args):
                config, home = case_paths(case_root)
                return (
                    config,
                    home,
                    {
                        "usage_only": True,
                        "embedded_index": False,
                        "embedded_body": False,
                    },
                    case_root / "bin",
                )

            def observed_session(
                _project,
                home,
                config,
                _claude,
                _prompt,
                _log,
                oauth_token,
                path_prefix=None,
            ):
                observed.append((home, config, oauth_token))
                return {}

            with (
                mock.patch.object(
                    self.harness,
                    "seed_case",
                    side_effect=case_paths,
                ) as seed_case,
                mock.patch.object(
                    self.harness,
                    "install",
                    side_effect=installed_case,
                ) as install,
                mock.patch.object(self.harness, "make_git_project"),
                mock.patch.object(
                    self.harness,
                    "session",
                    side_effect=observed_session,
                ),
                mock.patch.object(self.harness, "judge", return_value={}),
                mock.patch.object(
                    self.harness,
                    "failure_reasons",
                    return_value=[],
                ),
            ):
                self.harness.run_trial(
                    sandbox,
                    "claude",
                    logs,
                    1,
                    "oauth-access",
                )
            self.assertEqual(
                [len(call.args) for call in seed_case.call_args_list],
                [1],
            )
            self.assertEqual(
                [len(call.args) for call in install.call_args_list],
                [1, 1],
            )

        self.assertEqual(len({home for home, _, _ in observed}), 3)
        self.assertEqual(len({config for _, config, _ in observed}), 3)
        for home, config, oauth_token in observed:
            self.assertEqual(config, home / ".claude")
            self.assertEqual(oauth_token, "oauth-access")

class ParseTests(unittest.TestCase):
    def setUp(self):
        self.harness = load_harness()

    def test_collects_tool_calls_result_and_runtime_metadata(self):
        observed = self.harness.parse(
            stream(
                [("Read", {"file_path": "/x/index/proj-alpha.md"})],
                result="ok",
            )
        )

        self.assertEqual(len(observed["tools"]), 1)
        self.assertEqual(observed["result"], "ok")
        self.assertFalse(observed["is_error"])
        self.assertEqual(observed["model"], "claude-test")
        self.assertEqual(observed["runtime"], "test-runtime")

    def test_ignores_non_json_lines_and_reports_session_error(self):
        observed = self.harness.parse(
            "not json\n" + stream([], result="Not logged in", is_error=True)
        )

        self.assertEqual(observed["tools"], [])
        self.assertTrue(observed["is_error"])


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.harness = load_harness()

    def judge(self, tool_calls, files):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for name in files:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            return self.harness.judge(
                self.harness.parse(stream(tool_calls)),
                root,
                root,
            )

    def test_sanitizes_explicit_sandbox_without_assuming_project_depth(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            sandbox = Path(temporary_directory)
            project = sandbox / "proj-alpha"
            project.mkdir()
            observed = self.harness.parse(
                stream([], result=str(sandbox / "outside.txt"))
            )
            verdict = self.harness.judge(observed, project, sandbox)

        self.assertNotIn(str(sandbox), verdict["result"])
        self.assertIn("<sandbox>", verdict["result"])

    def test_detects_sentinel_output_and_rejects_similar_prefix(self):
        followed = self.judge([], ["README.md", "zeta-out/report.md"])
        similar = self.judge([], ["zeta-output/report.md"])

        self.assertTrue(followed["followed_sentinel"])
        self.assertFalse(similar["followed_sentinel"])

    def test_distinguishes_project_global_and_unrelated_indexes(self):
        verdict = self.judge(
            [
                ("Read", {"file_path": "/home/knowledge/index/proj-alpha.md"}),
                ("Read", {"file_path": "/home/knowledge/index/_global.md"}),
            ],
            ["README.md"],
        )

        self.assertTrue(verdict["searched_project_index"])
        self.assertTrue(verdict["searched_global_index"])
        self.assertFalse(verdict["read_decoy"])

    def test_counts_detail_reads_and_detects_decoy_project(self):
        verdict = self.judge(
            [
                (
                    "Read",
                    {
                        "file_path": (
                            "/home/knowledge/lessons/proj-alpha/"
                            "report-output-directory.md"
                        )
                    },
                ),
                (
                    "Read",
                    {
                        "file_path": (
                            "/home/knowledge/lessons/proj-beta/"
                            "report-output-directory.md"
                        )
                    },
                ),
            ],
            ["README.md"],
        )

        self.assertEqual(verdict["detail_reads"], 2)
        self.assertTrue(verdict["read_sentinel"])
        self.assertTrue(verdict["read_decoy"])


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.harness = load_harness()

    @staticmethod
    def valid_cases():
        common = {
            "followed_sentinel": True,
            "searched_project_index": True,
            "searched_global_index": True,
            "read_sentinel": True,
            "read_decoy": False,
            "detail_reads": 1,
            "is_error": False,
            "returncode": 0,
            "model": "claude-test",
            "runtime": "test-runtime",
        }
        return {
            "control": {
                **common,
                "followed_sentinel": False,
                "searched_project_index": False,
                "searched_global_index": False,
                "read_sentinel": False,
                "detail_reads": 0,
            },
            "treatment": dict(common),
            "forced": dict(common),
        }

    def test_clean_cases_pass(self):
        self.assertEqual(self.harness.failure_reasons(self.valid_cases()), [])

    def test_session_error_missing_index_decoy_or_excess_reads_invalidates_ab(self):
        mutations = (
            ("control", "is_error", True),
            ("treatment", "searched_project_index", False),
            ("treatment", "searched_global_index", False),
            ("treatment", "read_sentinel", False),
            ("treatment", "read_decoy", True),
            ("treatment", "detail_reads", 6),
            ("forced", "returncode", 1),
        )
        for case, field, value in mutations:
            with self.subTest(case=case, field=field):
                cases = self.valid_cases()
                cases[case][field] = value
                self.assertTrue(self.harness.failure_reasons(cases))

    def test_three_clean_runs_build_current_evidence_schema(self):
        run = {"passed": True, "failures": [], "cases": self.valid_cases()}
        report = self.harness.build_evidence(
            [run, run, run],
            "test-version",
            "abc123",
            {"usage_only": True, "embedded_index": False, "embedded_body": False},
        )

        self.assertEqual(report["surface"], "tools/didim-retrieval-ab")
        self.assertEqual(report["knowledgeUsageSha256"], "abc123")
        self.assertEqual(report["afterFix"]["verdict"], "PASS")
        self.assertEqual(report["afterFix"]["consecutivePasses"], 3)
        self.assertEqual(report["models"], ["claude-test"])
        self.assertEqual(report["runtimes"], ["test-runtime"])

    def test_failed_live_run_preserves_previous_passing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "retrieval-ab.json"
            original = b'{"afterFix":{"verdict":"PASS"}}\n'
            output.write_bytes(original)
            failed_run = {
                "passed": False,
                "failures": ["authentication failed"],
                "cases": self.valid_cases(),
            }
            failed_report = {
                "afterFix": {"verdict": "FAIL", "consecutivePasses": 0}
            }
            completed = mock.Mock(stdout="test-version")

            with (
                mock.patch.object(self.harness.shutil, "which", return_value="/bin/tool"),
                mock.patch.object(
                    self.harness,
                    "claude_executable",
                    return_value="claude",
                ),
                mock.patch.object(
                    self.harness,
                    "read_active_secret",
                    return_value=json.dumps(
                        {"claudeAiOauth": {"accessToken": "oauth-access"}}
                    ),
                ),
                mock.patch.object(
                    self.harness,
                    "run_trial",
                    return_value=(
                        failed_run,
                        {
                            "usage_only": True,
                            "embedded_index": False,
                            "embedded_body": False,
                        },
                    ),
                ),
                mock.patch.object(
                    self.harness,
                    "build_evidence",
                    return_value=failed_report,
                ),
                mock.patch.object(self.harness, "run", return_value=completed),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = self.harness.main(["--output", str(output)])

            self.assertEqual(result, 1)
            self.assertEqual(output.read_bytes(), original)


class UsageContractTests(unittest.TestCase):
    def setUp(self):
        self.text = USAGE.read_text(encoding="utf-8")

    def test_trigger_lists_observable_actions_not_subjective_difficulty(self):
        self.assertNotIn("비자명한 작업", self.text)
        for phrase in ("파일을 만들거나 고치기 전", "명령을 실행하기 전"):
            self.assertIn(phrase, self.text)
        self.assertIn("건너뛰지 않는다", self.text)


    def test_preflight_and_detail_read_are_explicitly_mandatory(self):
        self.assertIn("이 조회를 생략한 채 작업을 시작하지 않는다", self.text)
        self.assertIn("두 index를 각각 text search한다", self.text)
        self.assertIn("index 제목만으로 적용하지 않는다", self.text)
        self.assertIn("반드시 `상세` 파일을 읽은 뒤", self.text)
    def test_scope_budget_and_once_per_task_are_explicit(self):
        self.assertIn("최대 5건", self.text)
        self.assertIn("index/_global.md", self.text)
        self.assertIn("전부 읽지 않는다", self.text)
        self.assertIn("한 작업에서 한 번", self.text)
        self.assertNotIn("한 세션에서 한 번", self.text)

    def test_always_loaded_resource_does_not_embed_entries(self):
        self.assertNotIn("찾을 때:", self.text)
        self.assertNotIn("zeta-out", self.text)
        self.assertLess(len(self.text.encode("utf-8")), 2048)


class EvidenceTests(unittest.TestCase):
    def test_recorded_result_matches_current_usage_resource(self):
        report = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        digest = hashlib.sha256(USAGE.read_bytes()).hexdigest()

        self.assertEqual(
            report["knowledgeUsageSha256"],
            digest,
            "KNOWLEDGE_USAGE.md가 바뀌었다. tools/didim-retrieval-ab를 다시 실행해 evidence를 갱신하라",
        )
        self.assertEqual(report["afterFix"]["verdict"], "PASS")
        self.assertGreaterEqual(report["afterFix"]["consecutivePasses"], 3)
        self.assertEqual(
            report["contextContract"],
            {"embedded_body": False, "embedded_index": False, "usage_only": True},
        )


if __name__ == "__main__":
    unittest.main()
