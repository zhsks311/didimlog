"""Didimlog 조회 지침 A/B 하네스의 판정과 보존 evidence 계약."""

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

    def test_active_secret_prefers_native_unscoped_service(self):
        completed = mock.Mock(returncode=0, stdout="credential\n")
        with mock.patch.object(
            self.harness.subprocess,
            "run",
            return_value=completed,
        ) as run:
            self.assertEqual(self.harness.read_active_secret(), "credential")

        self.assertEqual(
            run.call_args.args[0],
            [
                "security",
                "find-generic-password",
                "-s",
                self.harness.KEYCHAIN_PREFIX,
                "-w",
            ],
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
            )

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


class UsageContractTests(unittest.TestCase):
    def setUp(self):
        self.text = USAGE.read_text(encoding="utf-8")

    def test_trigger_lists_observable_actions_not_subjective_difficulty(self):
        self.assertNotIn("비자명한 작업", self.text)
        for phrase in ("파일을 만들거나 고치기 전", "명령을 실행하기 전"):
            self.assertIn(phrase, self.text)
        self.assertIn("건너뛰지 않는다", self.text)

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
