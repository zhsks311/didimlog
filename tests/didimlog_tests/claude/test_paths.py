import tempfile
import unittest
from pathlib import Path

from didimlog.claude.paths import (
    ConfigPathError,
    config_dir,
    config_target,
)


class ConfigDirTests(unittest.TestCase):
    def test_explicit_config_takes_priority_over_environment(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            explicit = home / ".claude-explicit"
            environment = home / ".claude-environment"
            explicit.mkdir(parents=True)
            environment.mkdir()

            selected = config_dir(
                explicit,
                environ={"CLAUDE_CONFIG_DIR": str(environment)},
                home=home,
            )

            self.assertEqual(selected, explicit.resolve())

    def test_environment_config_takes_priority_over_default(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            environment = home / ".claude-environment"
            default = home / ".claude"
            environment.mkdir(parents=True)
            default.mkdir()

            selected = config_dir(
                environ={"CLAUDE_CONFIG_DIR": str(environment)},
                home=home,
            )

            self.assertEqual(selected, environment.resolve())

    def test_default_config_is_dot_claude_inside_home(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            default = home / ".claude"
            default.mkdir(parents=True)

            selected = config_dir(environ={}, home=home)

            self.assertEqual(selected, default.resolve())

    def test_missing_config_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            home.mkdir()

            with self.assertRaises(ValueError):
                config_dir(home / ".claude", environ={}, home=home)


    def test_config_must_be_a_regular_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            home.mkdir()
            regular_file = home / ".claude"
            regular_file.write_text("not a directory", encoding="utf-8")

            with self.assertRaises(ValueError):
                config_dir(regular_file, environ={}, home=home)

    def test_config_outside_home_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            outside = root / "outside" / ".claude"
            home.mkdir()
            outside.mkdir(parents=True)

            with self.assertRaises(ValueError):
                config_dir(outside, environ={}, home=home)

    def test_config_escape_through_dot_dot_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            escaped = root / "escaped"
            home.mkdir()
            escaped.mkdir()

            with self.assertRaises(ValueError):
                config_dir(home / ".." / "escaped", environ={}, home=home)

    def test_symlinked_config_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            real_config = home / "real-config"
            real_config.mkdir(parents=True)
            linked_config = home / ".claude"
            linked_config.symlink_to(real_config, target_is_directory=True)

            with self.assertRaises(ValueError):
                config_dir(linked_config, environ={}, home=home)

    def test_symlinked_config_parent_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            real_parent = home / "real-parent"
            real_config = real_parent / "profile"
            real_config.mkdir(parents=True)
            linked_parent = home / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaises(ValueError):
                config_dir(linked_parent / "profile", environ={}, home=home)


class ConfigTargetTests(unittest.TestCase):
    def test_only_managed_top_level_and_direct_didimlog_markdown_targets_are_allowed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            config.mkdir(parents=True)

            for name in (
                "CLAUDE.md",
                "settings.json",
                "didimlog/KNOWLEDGE_USAGE.md",
                "didimlog/LESSON_WRITING_RULES.md",
                "didimlog/FUTURE.md",
            ):
                with self.subTest(name=name):
                    self.assertEqual(
                        config_target(config, name, home=home),
                        config / name,
                    )

    def test_unmanaged_nested_traversal_and_non_markdown_targets_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            config.mkdir(parents=True)

            for name in (
                "CLAUDE.local.md",
                "hooks.json",
                "didimlog",
                "didimlog/.md",
                "didimlog/rules.txt",
                "didimlog/nested/rules.md",
                "didimlog/../CLAUDE.md",
                "../CLAUDE.md",
                str(home / "absolute.md"),
            ):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        config_target(config, name, home=home)

    def test_target_config_outside_home_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config = root / "outside" / ".claude"
            home.mkdir()
            config.mkdir(parents=True)

            with self.assertRaises(ValueError):
                config_target(config, "CLAUDE.md", home=home)

    def test_existing_target_must_be_a_regular_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            target = config / "CLAUDE.md"
            target.mkdir(parents=True)

            with self.assertRaises(ValueError):
                config_target(config, "CLAUDE.md", home=home)

    def test_final_target_symlink_is_refused_even_when_it_points_inside_home(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            regular_file = home / "user-claude.md"
            regular_file.write_text("# User rules\n", encoding="utf-8")
            (config / "CLAUDE.md").symlink_to(regular_file)

            with self.assertRaises(ValueError):
                config_target(config, "CLAUDE.md", home=home)

    def test_didimlog_parent_symlink_is_refused_even_when_it_points_inside_home(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            real_managed_directory = home / "managed-resources"
            config.mkdir(parents=True)
            real_managed_directory.mkdir()
            (config / "didimlog").symlink_to(
                real_managed_directory,
                target_is_directory=True,
            )

            with self.assertRaises(ValueError):
                config_target(
                    config,
                    "didimlog/KNOWLEDGE_USAGE.md",
                    home=home,
                )


class ConfigPathErrorTests(unittest.TestCase):
    """A refused path must say why, so callers can explain it to the user."""

    def test_config_path_error_is_a_value_error(self):
        self.assertTrue(issubclass(ConfigPathError, ValueError))

    def test_target_symlink_reports_reason_and_link_destination(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            other_profile = home / ".claude-other"
            other_profile.mkdir()
            shared = other_profile / "CLAUDE.md"
            shared.write_text("# shared rules\n", encoding="utf-8")
            (config / "CLAUDE.md").symlink_to(shared)

            with self.assertRaises(ConfigPathError) as raised:
                config_target(config, "CLAUDE.md", home=home)

            self.assertEqual(raised.exception.reason, "target-symlink")
            self.assertEqual(raised.exception.name, "CLAUDE.md")
            self.assertEqual(raised.exception.destination, shared.resolve())

    def test_target_symlink_outside_home_reports_no_destination(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            home = root / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            outside = root / "outside.md"
            outside.write_text("# outside\n", encoding="utf-8")
            (config / "CLAUDE.md").symlink_to(outside)

            with self.assertRaises(ConfigPathError) as raised:
                config_target(config, "CLAUDE.md", home=home)

            self.assertEqual(raised.exception.reason, "target-symlink")
            self.assertIsNone(raised.exception.destination)

    def test_target_directory_reports_a_distinct_reason(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            (config / "CLAUDE.md").mkdir(parents=True)

            with self.assertRaises(ConfigPathError) as raised:
                config_target(config, "CLAUDE.md", home=home)

            self.assertEqual(raised.exception.reason, "target-not-regular")
            self.assertIsNone(raised.exception.destination)

    def test_managed_parent_symlink_reports_its_own_reason(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            real_managed_directory = home / "managed-resources"
            config.mkdir(parents=True)
            real_managed_directory.mkdir()
            (config / "didimlog").symlink_to(
                real_managed_directory,
                target_is_directory=True,
            )

            with self.assertRaises(ConfigPathError) as raised:
                config_target(
                    config,
                    "didimlog/KNOWLEDGE_USAGE.md",
                    home=home,
                )

            self.assertEqual(raised.exception.reason, "parent-not-regular")

    def test_refused_paths_never_expose_the_link_destination_in_the_message(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            config = home / ".claude"
            config.mkdir(parents=True)
            other_profile = home / ".claude-other"
            other_profile.mkdir()
            shared = other_profile / "CLAUDE.md"
            shared.write_text("# shared rules\n", encoding="utf-8")
            (config / "CLAUDE.md").symlink_to(shared)

            with self.assertRaises(ConfigPathError) as raised:
                config_target(config, "CLAUDE.md", home=home)

            self.assertNotIn(str(home), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
