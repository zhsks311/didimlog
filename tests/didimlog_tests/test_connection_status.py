import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.claude.connect import apply_connect, plan_connect
from didimlog.claude.probe import Problem
from didimlog.claude.status import doctor_text, status_snapshot, status_text
from didimlog.claude.transaction import InstallJournal
from didimlog.connections import (
    apply_connections,
    load_state,
    plan_connections,
)
from didimlog.gui import _health_payload


class SelectedConnectionStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "knowledge/index").mkdir(parents=True)
        self.omp = self.home / ".omp/agent"
        self.omp.mkdir(parents=True)
        self.launcher = self.root / "bin/didim"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o755)

    def _plan(self, connect):
        return plan_connections(
            (("omp", self.omp),),
            launcher=self.launcher,
            home=self.home,
            environ={},
            connect=connect,
            require_storage=True,
        )

    def _apply(self, plan, name):
        apply_connections(
            plan,
            InstallJournal(self.root / f"{name}.json", reset=True),
        )

    def _snapshot(self):
        with mock.patch(
            "didimlog.claude.status._personal_check",
            return_value="PERSONAL_INDEX_CURRENT",
        ), mock.patch(
            "didimlog.claude.status._discover_project",
            return_value=(None, None),
        ):
            return status_snapshot(home=self.home, cwd=self.root)

    def test_omp_only_selection_does_not_report_unrelated_claude_failure(self):
        self._apply(self._plan(True), "connect")
        snapshot = self._snapshot()
        self.assertEqual(snapshot.claude_token, "CLAUDE_UNSELECTED")
        self.assertNotIn(
            "CLAUDE_CONFIG_INVALID",
            {problem.token for problem in snapshot.problems},
        )
        self.assertEqual(
            {status.token for status in snapshot.client_statuses},
            {"OMP_INSTALLED_UNVERIFIED"},
        )

    def test_first_omp_selection_preserves_existing_claude_diagnosis(self):
        config = self.home / ".claude"
        config.mkdir()
        claude_plan = plan_connect(
            config,
            launcher=self.launcher,
            home=self.home,
        )
        apply_connect(
            claude_plan,
            InstallJournal(self.root / "claude-connect.json", reset=True),
        )

        self._apply(self._plan(True), "omp-connect")
        state, _ = load_state(self.home)
        self.assertEqual(
            state.clients["claude"][str(config.resolve())].intent,
            "connected",
        )

        (config / "settings.json").unlink()
        snapshot = self._snapshot()
        self.assertEqual(snapshot.claude_token, "CLAUDE_PROBLEMS")
        self.assertNotEqual(snapshot.claude_token, "CLAUDE_UNSELECTED")

    def test_first_omp_selection_survives_unreadable_legacy_claude_child(self):
        config = self.home / ".claude"
        config.mkdir()
        user_source = self.root / "user-claude.md"
        user_bytes = b"# User-owned Claude instructions\n"
        user_source.write_bytes(user_bytes)
        (config / "CLAUDE.md").symlink_to(user_source)

        def refuse_symlink_read(path, _maximum):
            self.assertNotEqual(path, config / "CLAUDE.md")
            return None

        with mock.patch(
            "didimlog.claude.connect.read_optional_regular_file",
            side_effect=refuse_symlink_read,
        ):
            plan = self._plan(True)
        self._apply(plan, "omp-connect-with-unreadable-claude")

        self.assertTrue((config / "CLAUDE.md").is_symlink())
        self.assertEqual(user_source.read_bytes(), user_bytes)
        state, _ = load_state(self.home)
        self.assertNotIn("claude", state.clients)
        snapshot = self._snapshot()
        self.assertEqual(snapshot.claude_token, "CLAUDE_STATUS_UNKNOWN")
        self.assertIn(
            "CLAUDE_CONFIG_INVALID",
            {problem.token for problem in snapshot.problems},
        )

    def test_explicit_claude_config_remains_the_diagnostic_target(self):
        recorded = self.home / ".claude-recorded"
        explicit = self.home / ".claude-explicit"
        recorded.mkdir()
        explicit.mkdir()
        self._apply(
            plan_connections(
                (("claude", recorded.resolve()),),
                launcher=self.launcher,
                home=self.home,
                environ={},
                connect=True,
                require_storage=True,
            ),
            "record-claude",
        )
        inspected = []

        def diagnose(**arguments):
            selected = Path(arguments["config"])
            inspected.append(selected)
            if selected == explicit:
                return (
                    Problem(
                        token="CLAUDE_CONFIG_INVALID",
                        impact="broken",
                        action="repair",
                    ),
                )
            return ()

        with mock.patch(
            "didimlog.claude.status._personal_check",
            return_value="PERSONAL_INDEX_CURRENT",
        ), mock.patch(
            "didimlog.claude.status._discover_project",
            return_value=(None, None),
        ), mock.patch(
            "didimlog.claude.status._diagnostic_problems",
            side_effect=diagnose,
        ):
            snapshot = status_snapshot(
                home=self.home,
                cwd=self.root,
                config=explicit,
            )

        self.assertEqual(inspected, [explicit])
        self.assertEqual(snapshot.claude_token, "CLAUDE_PROBLEMS")
        self.assertIn(
            "CLAUDE_CONFIG_INVALID",
            {problem.token for problem in snapshot.problems},
        )

    def test_missing_selected_asset_is_broken_not_unselected(self):
        self._apply(self._plan(True), "connect")
        (self.omp / "extensions/didimlog.js").unlink()
        snapshot = self._snapshot()
        self.assertIn(
            "OMP_CONNECTION_BROKEN",
            {status.token for status in snapshot.client_statuses},
        )
        self.assertIn(
            "OMP_CONNECTION_BROKEN",
            {problem.token for problem in snapshot.problems},
        )

    def test_approved_disconnect_is_disabled_and_gui_keeps_old_keys(self):
        self._apply(self._plan(True), "connect")
        self._apply(self._plan(False), "disconnect")
        snapshot = self._snapshot()
        self.assertEqual(
            {status.token for status in snapshot.client_statuses},
            {"OMP_DISABLED"},
        )
        payload = _health_payload(snapshot)
        self.assertTrue(
            {"version", "personal_index", "project", "claude", "issues", "read_only"}
            <= set(payload)
        )
        self.assertEqual(payload["connections"]["omp"]["state"], "disabled")

    def test_disconnected_omp_reports_retained_shared_skill_as_residual(self):
        codex = self.home / ".codex"
        codex.mkdir()

        def plan(client, root, connect):
            return plan_connections(
                ((client, root),),
                launcher=self.launcher,
                home=self.home,
                environ={},
                connect=connect,
                require_storage=True,
            )

        self._apply(plan("codex", codex, True), "connect-codex")
        shared_skill = self.home / ".agents/skills/didimlog/SKILL.md"
        retained = b"user-retained shared skill\n"
        shared_skill.write_bytes(retained)
        self._apply(plan("codex", codex, False), "disconnect-codex")
        self._apply(self._plan(True), "connect-omp")
        self._apply(self._plan(False), "disconnect-omp")

        snapshot = self._snapshot()
        omp_tokens = {
            status.token
            for status in snapshot.client_statuses
            if status.client == "omp"
        }
        self.assertEqual(omp_tokens, {"OMP_RESIDUAL_DISCOVERY"})
        self.assertIn(
            "OMP_RESIDUAL_DISCOVERY",
            {problem.token for problem in snapshot.problems},
        )
        self.assertEqual(shared_skill.read_bytes(), retained)

        with mock.patch(
            "didimlog.claude.status._personal_check",
            return_value="PERSONAL_INDEX_CURRENT",
        ), mock.patch(
            "didimlog.claude.status._discover_project",
            return_value=(None, None),
        ):
            status = status_text(home=self.home, cwd=self.root)
            doctor_exit, doctor = doctor_text(home=self.home, cwd=self.root)
        self.assertIn("OMP 연결: 해제했지만 자동 발견 가능", status)
        self.assertNotIn("OMP 연결: 해제됨", status)
        self.assertNotEqual(doctor_exit, 0)
        self.assertIn("무엇: OMP_RESIDUAL_DISCOVERY", doctor)

    def test_invalid_selection_state_is_unknown_and_never_healthy(self):
        state_path = self.home / "knowledge/.didimlog/connections.json"
        state_path.parent.mkdir()
        state_path.write_text("{", encoding="utf-8")
        snapshot = self._snapshot()
        self.assertEqual(snapshot.claude_token, "CLAUDE_STATUS_UNKNOWN")
        self.assertIn(
            "CONNECTION_STATE_INVALID",
            {problem.token for problem in snapshot.problems},
        )
        payload = _health_payload(snapshot)
        self.assertEqual(payload["claude"]["state"], "unknown")

    def test_status_text_does_not_disclose_selected_root(self):
        self._apply(self._plan(True), "connect")
        with mock.patch(
            "didimlog.claude.status._personal_check",
            return_value="PERSONAL_INDEX_CURRENT",
        ), mock.patch(
            "didimlog.claude.status._discover_project",
            return_value=(None, None),
        ):
            text = status_text(home=self.home, cwd=self.root)
        self.assertIn("OMP 연결: 설치됨, 실행 미확인", text)
        self.assertNotIn(str(self.home), text)


if __name__ == "__main__":
    unittest.main()
