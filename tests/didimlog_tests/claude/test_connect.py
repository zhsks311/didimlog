import importlib.resources
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from didimlog.claude import connect as connect_module
from didimlog.claude.connect import (
    ClaudeChangePlan,
    apply_connect,
    apply_disconnect,
    plan_connect,
    plan_disconnect,
)
from didimlog.claude.config import render_managed_block
from didimlog.claude.transaction import InstallJournal


RESOURCE_NAMES = (
    "KNOWLEDGE_USAGE.md",
    "LESSON_WRITING_RULES.md",
)
FIRST_USER_EDIT = b"user edit after the first Claude write\n"
SECOND_USER_EDIT = b"user edit before the second Claude write\n"


def packaged_resource_bytes() -> dict[str, bytes]:
    root = importlib.resources.files("didimlog.resources.personal")
    return {name: root.joinpath(name).read_bytes() for name in RESOURCE_NAMES}


def make_launcher(root: Path) -> Path:
    launcher = root / "bin" / "didim"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"#!/bin/sh\n")
    launcher.chmod(0o755)
    return launcher


def make_journal(root: Path, name: str) -> InstallJournal:
    return InstallJournal(root / "state" / f"{name}.json")


def tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def session_start_commands(settings: Path) -> list[str]:
    value = json.loads(settings.read_bytes())
    return [
        hook["command"]
        for matcher in value.get("hooks", {}).get("SessionStart", [])
        for hook in matcher.get("hooks", [])
        if hook.get("type") == "command" and isinstance(hook.get("command"), str)
    ]


def assert_summary_mentions_config(
    test: unittest.TestCase,
    plan: ClaudeChangePlan,
    config: Path,
    home: Path,
) -> str:
    summary = "\n".join(plan.changes)
    relative = config.resolve().relative_to(home.resolve()).as_posix()
    test.assertTrue(
        str(config.resolve()) in summary
        or f"~/{relative}" in summary
        or relative in summary,
        summary,
    )
    return summary


class ConcurrentSecondClaudeWriteJournal(InstallJournal):
    """Inject user edits between the two planned top-level Claude writes."""

    def __init__(self, path: Path, claude_md: Path, settings: Path) -> None:
        super().__init__(path)
        self._claude_md = claude_md
        self._settings = settings
        self.first_target: Path | None = None
        self.second_target: Path | None = None

    def record_installed(self, name: str, data: bytes) -> None:
        super().record_installed(name, data)
        target = Path(self.data["targets"][name]["path"])
        if self.first_target is not None or target.name not in {
            "CLAUDE.md",
            "settings.json",
        }:
            return

        other = self._settings if target == self._claude_md else self._claude_md
        target.write_bytes(FIRST_USER_EDIT)
        other.write_bytes(SECOND_USER_EDIT)
        self.first_target = target
        self.second_target = other


class ConnectTests(unittest.TestCase):
    def test_fresh_connect_plans_without_writing_then_applies_all_managed_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            launcher = make_launcher(home)

            plan = plan_connect(
                config,
                launcher=launcher,
                environ={},
                home=home,
            )

            self.assertIsInstance(plan, ClaudeChangePlan)
            self.assertEqual(plan.config_dir, config.resolve())
            self.assertIsInstance(plan.changes, tuple)
            self.assertTrue(plan.changes)
            assert_summary_mentions_config(self, plan, config, home)
            self.assertEqual(tree_bytes(config), {})

            apply_connect(plan, make_journal(root, "fresh-connect"))

            self.assertEqual(
                (config / "CLAUDE.md").read_bytes(),
                render_managed_block(config.resolve()),
            )
            self.assertEqual(
                session_start_commands(config / "settings.json"),
                [f"{launcher} hook session-start"],
            )
            expected_resources = packaged_resource_bytes()
            self.assertEqual(
                {
                    name: (config / "didimlog" / name).read_bytes()
                    for name in RESOURCE_NAMES
                },
                expected_resources,
            )

    def test_repeated_connect_is_an_empty_plan_and_does_not_rewrite_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            launcher = make_launcher(home)
            first = plan_connect(config, launcher=launcher, environ={}, home=home)
            apply_connect(first, make_journal(root, "first-connect"))
            managed_paths = (
                config / "CLAUDE.md",
                config / "settings.json",
                *(config / "didimlog" / name for name in RESOURCE_NAMES),
            )
            before = {
                path: (
                    path.read_bytes(),
                    path.stat().st_ino,
                    path.stat().st_mtime_ns,
                )
                for path in managed_paths
            }

            repeated = plan_connect(
                config,
                launcher=launcher,
                environ={},
                home=home,
            )
            self.assertEqual(repeated.changes, ())
            apply_connect(repeated, make_journal(root, "repeated-connect"))

            after = {
                path: (
                    path.read_bytes(),
                    path.stat().st_ino,
                    path.stat().st_mtime_ns,
                )
                for path in managed_paths
            }
            self.assertEqual(after, before)

    def test_unowned_markers_are_preserved_as_user_content(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            original = (
                b"# user rules\n"
                b"<!-- IMPROVER-PERSONAL-KNOWLEDGE:START version=1 -->\n"
                b"@/old/RULES.md\n"
                b"<!-- IMPROVER-PERSONAL-KNOWLEDGE:END -->\n"
            )
            (config / "CLAUDE.md").write_bytes(original)
            launcher = make_launcher(home)

            plan = plan_connect(
                config,
                launcher=launcher,
                environ={},
                home=home,
            )
            apply_connect(plan, make_journal(root, "unowned-markers"))

            self.assertEqual(
                (config / "CLAUDE.md").read_bytes(),
                original + b"\n" + render_managed_block(config.resolve()),
            )

    def test_explicit_profile_is_the_only_selected_profile(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            default = home / ".claude"
            environment = home / ".claude-environment"
            explicit = home / ".claude-explicit"
            for config in (default, environment, explicit):
                config.mkdir(parents=True)
                (config / "CLAUDE.md").write_bytes(
                    f"# user profile {config.name}\n".encode("utf-8")
                )
            default_before = tree_bytes(default)
            environment_before = tree_bytes(environment)
            launcher = make_launcher(home)

            plan = plan_connect(
                explicit,
                launcher=launcher,
                environ={"CLAUDE_CONFIG_DIR": str(environment)},
                home=home,
            )
            self.assertEqual(plan.config_dir, explicit.resolve())
            self.assertEqual(tree_bytes(default), default_before)
            self.assertEqual(tree_bytes(environment), environment_before)
            self.assertNotIn(b"<!-- DIDIMLOG:START", (explicit / "CLAUDE.md").read_bytes())

            apply_connect(plan, make_journal(root, "explicit-profile"))

            self.assertEqual(tree_bytes(default), default_before)
            self.assertEqual(tree_bytes(environment), environment_before)
            self.assertIn(b"<!-- DIDIMLOG:START", (explicit / "CLAUDE.md").read_bytes())
            self.assertEqual(
                session_start_commands(explicit / "settings.json"),
                [f"{launcher} hook session-start"],
            )


class DisconnectTests(unittest.TestCase):
    def test_disconnect_only_removes_owned_wiring_and_unmodified_resources(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            launcher = make_launcher(home)
            user_claude = b"# user rules\r\nuser bytes stay exact\xff\n"
            (config / "CLAUDE.md").write_bytes(user_claude)
            user_hook = {"type": "command", "command": "/user/bin/session-start"}
            original_settings_value = {
                "theme": "dark",
                "hooks": {
                    "SessionStart": [
                        {"matcher": "startup", "hooks": [user_hook]}
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "/user/bin/on-stop"}
                            ]
                        }
                    ],
                },
            }
            (config / "settings.json").write_text(
                json.dumps(original_settings_value, ensure_ascii=False),
                encoding="utf-8",
            )
            connect_plan = plan_connect(
                config,
                launcher=launcher,
                environ={},
                home=home,
            )
            apply_connect(connect_plan, make_journal(root, "before-disconnect"))

            usage = config / "didimlog" / "KNOWLEDGE_USAGE.md"
            rules = config / "didimlog" / "LESSON_WRITING_RULES.md"
            custom_rules = b"user customized managed rules\n"
            rules.write_bytes(custom_rules)
            user_resource = config / "didimlog" / "USER_NOTES.md"
            user_resource.write_bytes(b"user resource must remain\n")
            before_plan = tree_bytes(config)

            disconnect_plan = plan_disconnect(config, environ={}, home=home)

            self.assertEqual(disconnect_plan.config_dir, config.resolve())
            self.assertTrue(disconnect_plan.changes)
            assert_summary_mentions_config(self, disconnect_plan, config, home)
            self.assertEqual(tree_bytes(config), before_plan)

            apply_disconnect(
                disconnect_plan,
                make_journal(root, "disconnect"),
            )

            self.assertEqual((config / "CLAUDE.md").read_bytes(), user_claude)
            disconnected_settings = json.loads((config / "settings.json").read_bytes())
            self.assertEqual(disconnected_settings["theme"], "dark")
            self.assertEqual(disconnected_settings["hooks"]["Stop"], original_settings_value["hooks"]["Stop"])
            self.assertEqual(
                session_start_commands(config / "settings.json"),
                [user_hook["command"]],
            )
            self.assertFalse(usage.exists())
            self.assertEqual(rules.read_bytes(), custom_rules)
            self.assertEqual(user_resource.read_bytes(), b"user resource must remain\n")

    def test_resource_restore_failure_still_rolls_back_top_level_files_and_reraises_original_disconnect_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            launcher = make_launcher(home)
            claude_md = config / "CLAUDE.md"
            settings = config / "settings.json"
            claude_md.write_bytes(b"# user rules\n")
            settings.write_bytes(b'{"theme":"dark"}\n')
            apply_connect(
                plan_connect(
                    config,
                    launcher=launcher,
                    environ={},
                    home=home,
                ),
                make_journal(root, "before-failed-disconnect"),
            )
            connected_claude = claude_md.read_bytes()
            connected_settings = settings.read_bytes()
            disconnect_plan = plan_disconnect(config, environ={}, home=home)
            removals = tuple(
                change
                for change in disconnect_plan._files
                if change.intended is None
            )
            self.assertEqual(len(removals), len(RESOURCE_NAMES))
            removal_to_restore, removal_to_fail = removals
            removal_to_fail.path.write_bytes(
                b"user changed resource after disconnect planning\n"
            )
            original_write = connect_module.write_regular_file_if_unchanged
            restoration_failure = OSError("forced managed resource restoration failure")
            restoration_attempts = 0

            def fail_deleted_resource_restoration(
                path: Path,
                expected: bytes | None,
                intended: bytes | None,
            ) -> None:
                nonlocal restoration_attempts
                if path == removal_to_restore.path and expected is None:
                    restoration_attempts += 1
                    raise restoration_failure
                original_write(path, expected, intended)

            with mock.patch.object(
                connect_module,
                "write_regular_file_if_unchanged",
                side_effect=fail_deleted_resource_restoration,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "^managed resource changed after planning$",
                ):
                    apply_disconnect(
                        disconnect_plan,
                        make_journal(root, "failed-disconnect"),
                    )

            self.assertEqual(restoration_attempts, 1)
            self.assertEqual(claude_md.read_bytes(), connected_claude)
            self.assertEqual(settings.read_bytes(), connected_settings)


    def test_disconnect_rollback_preserves_user_rename_after_second_digest_check(self):
        from didimlog.claude import transaction as transaction_module

        user_bytes = {
            "CLAUDE.md": b"# user rules\n",
            "settings.json": b'{"theme":"dark"}\n',
        }
        replacements = {
            "CLAUDE.md": b"# concurrent user replacement\n",
            "settings.json": b'{"concurrent":"user replacement"}\n',
        }
        for raced_name, user_replacement in replacements.items():
            with self.subTest(raced_name=raced_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    home = root / "home"
                    config = home / ".claude"
                    config.mkdir(parents=True)
                    launcher = make_launcher(home)
                    claude_md = config / "CLAUDE.md"
                    settings = config / "settings.json"
                    claude_md.write_bytes(user_bytes["CLAUDE.md"])
                    settings.write_bytes(user_bytes["settings.json"])
                    apply_connect(
                        plan_connect(
                            config,
                            launcher=launcher,
                            environ={},
                            home=home,
                        ),
                        make_journal(root, "before-raced-disconnect"),
                    )
                    connected_bytes = {
                        "CLAUDE.md": claude_md.read_bytes(),
                        "settings.json": settings.read_bytes(),
                    }
                    disconnect_plan = plan_disconnect(config, environ={}, home=home)
                    raced_path = disconnect_plan.config_dir / raced_name
                    raced_change = next(
                        change
                        for change in disconnect_plan._files
                        if change.path == raced_path
                    )
                    self.assertIsNotNone(raced_change.original)
                    self.assertIsNotNone(raced_change.intended)
                    removals = tuple(
                        change
                        for change in disconnect_plan._files
                        if change.intended is None
                    )
                    self.assertEqual(len(removals), len(RESOURCE_NAMES))
                    removal_to_restore, removal_to_fail = removals
                    removal_to_fail.path.write_bytes(
                        b"user changed resource after disconnect planning\n"
                    )
                    replacement_path = config / f".{raced_name}.user-replacement"
                    replacement_path.write_bytes(user_replacement)
                    journal = make_journal(root, f"raced-{raced_name}")
                    original_write = connect_module.write_regular_file_if_unchanged
                    original_replace = (
                        transaction_module.replace_regular_file_at_if_unchanged
                    )
                    restoration_attempts = 0
                    conditional_publish_results: list[bool] = []

                    def fail_deleted_resource_restoration(
                        path: Path,
                        expected: bytes | None,
                        intended: bytes | None,
                    ) -> None:
                        nonlocal restoration_attempts
                        if path == removal_to_restore.path and expected is None:
                            restoration_attempts += 1
                            raise OSError(
                                "forced managed resource restoration failure"
                            )
                        original_write(path, expected, intended)

                    def replace_target_before_conditional_publish(
                        parent_descriptor: int,
                        name: str,
                        expected: bytes,
                        replacement: bytes,
                        mode: int,
                        *,
                        expected_info=None,
                    ) -> bool:
                        if name == raced_path.name:
                            replacement_path.replace(raced_path)
                        replaced = original_replace(
                            parent_descriptor,
                            name,
                            expected,
                            replacement,
                            mode,
                            expected_info=expected_info,
                        )
                        if name == raced_path.name:
                            conditional_publish_results.append(replaced)
                        return replaced

                    with (
                        mock.patch.object(
                            connect_module,
                            "write_regular_file_if_unchanged",
                            side_effect=fail_deleted_resource_restoration,
                        ),
                        mock.patch.object(
                            transaction_module,
                            "replace_regular_file_at_if_unchanged",
                            side_effect=replace_target_before_conditional_publish,
                        ),
                        self.assertRaisesRegex(
                            ValueError,
                            "^managed resource changed after planning$",
                        ),
                    ):
                        apply_disconnect(disconnect_plan, journal)

                    self.assertEqual(restoration_attempts, 1)
                    self.assertEqual(conditional_publish_results, [False])
                    expected_bytes = dict(connected_bytes)
                    expected_bytes[raced_name] = user_replacement
                    self.assertEqual(
                        claude_md.read_bytes(),
                        expected_bytes["CLAUDE.md"],
                    )
                    self.assertEqual(
                        settings.read_bytes(),
                        expected_bytes["settings.json"],
                    )


    def test_disconnect_of_an_unconnected_profile_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config = home / ".claude-work"
            config.mkdir(parents=True)
            (config / "CLAUDE.md").write_bytes(b"# user-only config\n")
            before = tree_bytes(config)

            plan = plan_disconnect(config, environ={}, home=home)

            self.assertEqual(plan.changes, ())
            apply_disconnect(plan, make_journal(root, "empty-disconnect"))
            self.assertEqual(tree_bytes(config), before)


class ConnectRollbackTests(unittest.TestCase):
    def test_second_claude_write_failure_rolls_back_owned_bytes_but_preserves_user_edits_and_data(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config = home / ".claude"
            managed = config / "didimlog"
            managed.mkdir(parents=True)
            claude_md = config / "CLAUDE.md"
            settings = config / "settings.json"
            original_claude = b"# original user rules\n"
            original_settings = b'{"theme":"user"}\n'
            claude_md.write_bytes(original_claude)
            settings.write_bytes(original_settings)
            stale_resources = {
                name: f"stale user-visible resource {name}\n".encode("utf-8")
                for name in RESOURCE_NAMES
            }
            for name, content in stale_resources.items():
                (managed / name).write_bytes(content)

            personal_data = home / "knowledge" / "lessons" / "private.md"
            personal_data.parent.mkdir(parents=True)
            personal_data.write_bytes(b"private lesson data\n")
            user_data_before = personal_data.read_bytes()
            launcher = make_launcher(home)
            plan = plan_connect(
                config,
                launcher=launcher,
                environ={},
                home=home,
            )
            journal = ConcurrentSecondClaudeWriteJournal(
                root / "state" / "forced-second-write.json",
                claude_md,
                settings,
            )

            with self.assertRaises(ValueError):
                apply_connect(plan, journal)

            self.assertIsNotNone(journal.first_target)
            self.assertIsNotNone(journal.second_target)
            self.assertEqual(journal.first_target.read_bytes(), FIRST_USER_EDIT)
            self.assertEqual(journal.second_target.read_bytes(), SECOND_USER_EDIT)
            self.assertEqual(
                {
                    name: (managed / name).read_bytes()
                    for name in RESOURCE_NAMES
                },
                stale_resources,
            )
            self.assertEqual(personal_data.read_bytes(), user_data_before)


if __name__ == "__main__":
    unittest.main()
