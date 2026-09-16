import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.claude.transaction import InstallJournal
from didimlog.errors import DidimError
import didimlog.connections as connections_module
from didimlog.connections import (
    apply_connections,
    inspect_connections,
    load_state,
    parse_state,
    plan_connections,
)


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.knowledge = self.home / "knowledge"
        self.home.mkdir()
        self.knowledge.mkdir()
        (self.knowledge / "index").mkdir()
        (self.knowledge / "index/_global.md").write_text("# Global\n", encoding="utf-8")
        self.launcher = self.root / "bin/didim"
        self.launcher.parent.mkdir()
        self.launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.launcher.chmod(0o755)

    def _root(self, name):
        path = self.home / name
        path.mkdir()
        return path

    def _apply(self, plan, name):
        journal = InstallJournal(self.root / f"{name}.json", reset=True)
        apply_connections(plan, journal)

    def _plan(self, client, root, *, connect):
        return plan_connections(
            ((client, root),),
            launcher=self.launcher,
            home=self.home,
            environ={},
            connect=connect,
            require_storage=True,
        )

    def test_planning_never_executes_host_client_binaries(self):
        omp = self._root("quiet-omp")
        codex = self._root("quiet-codex")
        host_bin = self.root / "host-bin"
        host_bin.mkdir()
        marker = self.root / "host-invoked"
        for name, version in (("omp", "18.1.16"), ("codex", "0.154.0")):
            executable = host_bin / name
            executable.write_text(
                "#!/bin/sh\n"
                + f"printf invoked >> {str(marker)!r}\n"
                + f"printf '%s\\n' {version!r}\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
        path = str(host_bin) + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {"PATH": path}):
            plan_connections(
                (("omp", omp), ("codex", codex)),
                launcher=self.launcher,
                home=self.home,
                environ={},
                connect=True,
                require_storage=True,
            )
        self.assertFalse(marker.exists())


    def test_same_client_roots_are_additive_and_disconnect_is_root_scoped(self):
        first = self._root("omp-first")
        second = self._root("omp-second")
        self._apply(self._plan("omp", first, connect=True), "first")
        self._apply(self._plan("omp", second, connect=True), "second")

        state, _ = load_state(self.home)
        self.assertEqual(
            set(state.clients["omp"]),
            {str(first.resolve()), str(second.resolve())},
        )
        self.assertTrue((first / "extensions/didimlog.js").is_file())
        self.assertTrue((second / "extensions/didimlog.js").is_file())

        self._apply(self._plan("omp", second, connect=False), "disconnect-second")
        state, _ = load_state(self.home)
        self.assertEqual(state.clients["omp"][str(first.resolve())].intent, "connected")
        self.assertEqual(state.clients["omp"][str(second.resolve())].intent, "disconnected")
        self.assertTrue((first / "extensions/didimlog.js").is_file())
        self.assertFalse((second / "extensions/didimlog.js").exists())

    def test_codex_shared_skill_remains_until_every_connected_root_is_disconnected(self):
        first = self._root("codex-first")
        second = self._root("codex-second")
        self._apply(self._plan("codex", first, connect=True), "codex-first")
        self._apply(self._plan("codex", second, connect=True), "codex-second")
        shared = self.home / ".agents/skills/didimlog/SKILL.md"
        self.assertTrue(shared.is_file())

        self._apply(self._plan("codex", first, connect=False), "codex-disconnect-first")
        self.assertTrue(shared.is_file())
        self.assertNotIn("didim", (first / "hooks.json").read_text(encoding="utf-8"))
        self.assertIn("didim", (second / "hooks.json").read_text(encoding="utf-8"))

        self._apply(self._plan("codex", second, connect=False), "codex-disconnect-second")
        self.assertFalse(shared.exists())

    def test_disconnect_preserves_modified_managed_file_and_reports_residual(self):
        omp = self._root("omp")
        self._apply(self._plan("omp", omp, connect=True), "connect")
        skill = omp / "skills/didimlog/SKILL.md"
        skill.write_text("user replacement\n", encoding="utf-8")

        plan = self._plan("omp", omp, connect=False)
        self._apply(plan, "disconnect")

        self.assertEqual(skill.read_text(encoding="utf-8"), "user replacement\n")
        state, _ = load_state(self.home)
        statuses, problems = inspect_connections(state, home=self.home)
        self.assertIn("OMP_RESIDUAL_DISCOVERY", {status.token for status in statuses})
        self.assertIn("OMP_RESIDUAL_DISCOVERY", {problem[0] for problem in problems})

    def test_disconnect_never_claims_or_removes_unowned_identical_file(self):
        omp = self._root("foreign-omp")
        source_root = self._root("source-omp")
        self._apply(
            self._plan("omp", source_root, connect=True),
            "install-source",
        )
        expected = (source_root / "skills/didimlog/SKILL.md").read_bytes()
        foreign = omp / "skills/didimlog/SKILL.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(expected)

        self._apply(self._plan("omp", omp, connect=False), "first-disconnect")
        self._apply(self._plan("omp", omp, connect=False), "second-disconnect")
        self.assertEqual(foreign.read_bytes(), expected)
        state, _ = load_state(self.home)
        statuses, problems = inspect_connections(state, home=self.home)
        self.assertIn("OMP_RESIDUAL_DISCOVERY", {status.token for status in statuses})
        self.assertIn("OMP_RESIDUAL_DISCOVERY", {problem[0] for problem in problems})

    def test_connect_refuses_existing_file_without_owned_digest(self):
        omp = self._root("foreign-conflict")
        extension = omp / "extensions/didimlog.js"
        extension.parent.mkdir()
        extension.write_text("foreign extension\n", encoding="utf-8")
        with self.assertRaises(DidimError) as caught:
            self._plan("omp", omp, connect=True)
        self.assertEqual(caught.exception.token, "CONNECTION_ASSET_CONFLICT")

    def test_reconnect_refuses_ambiguous_managed_codex_hooks(self):
        codex = self._root("ambiguous-codex")
        self._apply(self._plan("codex", codex, connect=True), "connect-codex")
        path = codex / "hooks.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        session = value["hooks"]["SessionStart"]
        session.append(dict(session[0]))
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(DidimError) as caught:
            self._plan("codex", codex, connect=True)
        self.assertEqual(caught.exception.token, "CONNECTION_ASSET_CONFLICT")

        disconnect = self._plan("codex", codex, connect=False)
        self._apply(disconnect, "disconnect-ambiguous-codex")
        state, _ = load_state(self.home)
        statuses, problems = inspect_connections(state, home=self.home)
        self.assertIn("CODEX_RESIDUAL_DISCOVERY", {status.token for status in statuses})
        self.assertIn("CODEX_RESIDUAL_DISCOVERY", {problem[0] for problem in problems})
        self.assertEqual(
            len(json.loads(path.read_text(encoding="utf-8"))["hooks"]["SessionStart"]),
            2,
        )

    def test_apply_rejects_swapped_personal_root_without_writing_outside(self):
        omp = self._root("swapped-root")
        plan = self._plan("omp", omp, connect=True)
        moved = self.home / "knowledge-original"
        self.knowledge.rename(moved)
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.knowledge, target_is_directory=True)

        with self.assertRaises(DidimError) as caught:
            self._apply(plan, "swapped-apply")
        self.assertEqual(caught.exception.token, "CONNECTION_PLAN_CHANGED")
        self.assertFalse((outside / ".didimlog/connections.json").exists())

    def test_selection_cap_is_rejected_before_publishing_new_state(self):
        clients = {
            "omp": {
                str(self.home / f"record-{number}"): {
                    "intent": "disconnected",
                    "assets": [],
                }
                for number in range(32)
            }
        }
        state_path = self.knowledge / ".didimlog/connections.json"
        state_path.parent.mkdir()
        state_path.write_text(
            json.dumps({"version": 1, "clients": clients}) + "\n",
            encoding="utf-8",
        )
        codex = self._root("thirty-third")
        with self.assertRaises(DidimError) as caught:
            self._plan("codex", codex, connect=False)
        self.assertEqual(caught.exception.token, "CONNECTION_STATE_TOO_LARGE")
        self.assertEqual(len(json.loads(state_path.read_text())["clients"]["omp"]), 32)

    def test_matching_shared_skill_can_transfer_between_client_owners(self):
        codex = self._root("owner-codex")
        self._apply(self._plan("codex", codex, connect=True), "owner-codex")
        omp = self.home / ".agents"
        self._apply(self._plan("omp", omp, connect=True), "shared-omp")
        self.assertTrue((omp / "skills/didimlog/SKILL.md").is_file())
        state, _ = load_state(self.home)
        self.assertEqual(state.clients["omp"][str(omp.resolve())].intent, "connected")

    def test_plan_is_write_free_and_state_is_published_only_on_apply(self):
        omp = self._root("omp")
        before = tuple(sorted(path.relative_to(self.root) for path in self.root.rglob("*")))
        plan = self._plan("omp", omp, connect=True)
        after = tuple(sorted(path.relative_to(self.root) for path in self.root.rglob("*")))
        self.assertEqual(after, before)
        self.assertFalse((self.knowledge / ".didimlog/connections.json").exists())
        self._apply(plan, "apply")
        self.assertTrue((self.knowledge / ".didimlog/connections.json").is_file())

    def test_postcheck_rejects_concurrent_change_to_unchanged_dependency(self):
        omp = self._root("stable-omp")
        self._apply(self._plan("omp", omp, connect=True), "connect-stable-omp")
        codex = self._root("new-codex")
        plan = plan_connections(
            (("omp", omp), ("codex", codex)),
            launcher=self.launcher,
            home=self.home,
            environ={},
            connect=True,
            require_storage=True,
        )
        extension = omp / "extensions/didimlog.js"
        real_write = connections_module._journaled_write

        def write_then_remove_dependency(*args, **kwargs):
            real_write(*args, **kwargs)
            if kwargs["name"] == "connection-state":
                extension.unlink()

        with mock.patch.object(
            connections_module,
            "_journaled_write",
            side_effect=write_then_remove_dependency,
        ), self.assertRaises(DidimError) as caught:
            self._apply(plan, "concurrent-unchanged-dependency")

        self.assertEqual(caught.exception.token, "CONNECTION_POSTCHECK_FAILED")
        state, _ = load_state(self.home)
        self.assertNotIn("codex", state.clients)
        self.assertFalse((codex / "hooks.json").exists())

    def test_unknown_or_duplicate_state_is_rejected_without_reset(self):
        invalid_values = (
            b'{"version":2,"clients":{}}\n',
            b'{"version":1,"version":1,"clients":{}}\n',
            json.dumps({"version": 1, "clients": {"future": {}}}).encode(),
            json.dumps(
                {
                    "version": 1,
                    "clients": {
                        "omp": {
                            "/tmp/agent": {
                                "intent": "connected",
                                "assets": [
                                    {
                                        "id": "omp-extension",
                                        "scope": "root",
                                        "target": "extensions/didimlog.js",
                                        "sha256": None,
                                        "kind": "file",
                                    }
                                ],
                            }
                        }
                    },
                }
            ).encode(),
        )
        for raw in invalid_values:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_state(raw)


if __name__ == "__main__":
    unittest.main()
