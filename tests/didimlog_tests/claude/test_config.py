import json
import tempfile
import unittest
from pathlib import Path

from didimlog.claude.config import (
    plan_claude_md,
    plan_settings,
    render_managed_block,
)


START = b"<!-- DIDIMLOG:START version=1 -->\n"
END = b"<!-- DIDIMLOG:END -->\n"


def expected_managed_block(config: Path) -> bytes:
    return (
        START
        + b"@~/knowledge/MY-RULES.md\n"
        + f"@{config}/didimlog/KNOWLEDGE_USAGE.md\n".encode("utf-8")
        + f"@{config}/didimlog/LESSON_WRITING_RULES.md\n".encode("utf-8")
        + END
    )


class ManagedBlockTests(unittest.TestCase):
    def test_render_managed_block_has_exact_markers_and_three_imports(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / ".claude-profile"

            rendered = render_managed_block(config)

            self.assertEqual(rendered, expected_managed_block(config))
            self.assertEqual(rendered.count(b"\n@"), 3)

    def test_absent_claude_md_is_exactly_the_managed_block(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / ".claude"

            intended = plan_claude_md(b"", config)

            self.assertEqual(intended, expected_managed_block(config))

    def test_existing_user_bytes_are_preserved_when_block_is_appended(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / ".claude"
            original = b"# user instructions\n\xffbinary-user-byte"

            intended = plan_claude_md(original, config)

            self.assertEqual(
                intended,
                original + b"\n\n" + expected_managed_block(config),
            )

    def test_existing_managed_block_is_replaced_without_changing_surrounding_bytes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / ".claude-new"
            old_block = (
                START
                + b"@~/knowledge/old-rules.md\n"
                + b"@/old/config/didimlog/KNOWLEDGE_USAGE.md\n"
                + b"@/old/config/didimlog/LESSON_WRITING_RULES.md\n"
                + END
            )
            prefix = b"\xff# user prefix\r\n"
            suffix = b"\r\n# user suffix\n"

            intended = plan_claude_md(prefix + old_block + suffix, config)

            self.assertEqual(
                intended,
                prefix + expected_managed_block(config) + suffix,
            )

    def test_duplicate_managed_blocks_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / ".claude"
            block = expected_managed_block(config)

            with self.assertRaises(ValueError):
                plan_claude_md(block + b"\n" + block, config)

    def test_mismatched_or_version_changed_markers_are_refused(self):
        malformed_values = (
            START + b"managed content without an end\n",
            b"managed content without a start\n" + END,
            b"<!-- DIDIMLOG:START version=2 -->\n" + END,
            START + b"<!-- DIDIMLOG:END version=1 -->\n",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / ".claude"

            for original in malformed_values:
                with self.subTest(original=original):
                    with self.assertRaises(ValueError):
                        plan_claude_md(original, config)

    def test_fenced_fake_imports_do_not_replace_the_real_managed_block(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = Path(temporary_directory) / ".claude"
            original = (
                b"# Example only\n\n"
                b"```text\n"
                b"@~/knowledge/MY-RULES.md\n"
                + f"@{config}/didimlog/KNOWLEDGE_USAGE.md\n".encode("utf-8")
                + f"@{config}/didimlog/LESSON_WRITING_RULES.md\n".encode("utf-8")
                + b"```\n"
            )

            intended = plan_claude_md(original, config)

            self.assertEqual(
                intended,
                original + b"\n" + expected_managed_block(config),
            )
            self.assertEqual(intended.count(START), 1)
            self.assertEqual(intended.count(END), 1)


class SettingsPlanTests(unittest.TestCase):
    def test_absent_settings_adds_one_session_start_hook_with_absolute_launcher(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            launcher = Path(temporary_directory) / "bin" / "didim"

            intended = plan_settings(b"", launcher)

            self.assertEqual(
                json.loads(intended),
                {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f"{launcher} hook session-start",
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
            self.assertTrue(intended.endswith(b"\n"))

    def test_relative_launcher_is_refused(self):
        with self.assertRaises(ValueError):
            plan_settings(b"{}\n", Path("bin/didim"))

    def test_other_settings_keys_and_hooks_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            launcher = Path(temporary_directory) / "bin" / "didim"
            stop_hooks = [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": "/user/bin/on-stop"}
                    ],
                }
            ]
            user_session_start = {
                "matcher": "startup",
                "hooks": [
                    {"type": "command", "command": "/user/bin/on-start"}
                ],
            }
            original_value = {
                "env": {"KEEP": "한글 값"},
                "permissions": {"allow": ["Read"]},
                "hooks": {
                    "Stop": stop_hooks,
                    "SessionStart": [user_session_start],
                },
            }
            original = json.dumps(
                original_value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

            intended_value = json.loads(plan_settings(original, launcher))

            self.assertEqual(intended_value["env"], original_value["env"])
            self.assertEqual(
                intended_value["permissions"], original_value["permissions"]
            )
            self.assertEqual(intended_value["hooks"]["Stop"], stop_hooks)
            self.assertIn(
                user_session_start,
                intended_value["hooks"]["SessionStart"],
            )
            self.assertEqual(
                intended_value["hooks"]["SessionStart"][-1],
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{launcher} hook session-start",
                        }
                    ]
                },
            )

    def test_duplicate_didimlog_hooks_are_replaced_by_one_current_hook(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            launcher = Path(temporary_directory) / "current" / "didim"
            user_hook = {"type": "command", "command": "/user/bin/on-start"}
            stop_hooks = [
                {"hooks": [{"type": "command", "command": "/user/bin/on-stop"}]}
            ]
            original_value = {
                "theme": "dark",
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "startup",
                            "custom": "preserve me",
                            "hooks": [
                                user_hook,
                                {
                                    "type": "command",
                                    "command": "/old/one/didim hook session-start",
                                },
                            ],
                        },
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/old/two/didim hook session-start",
                                }
                            ]
                        },
                    ],
                    "Stop": stop_hooks,
                },
            }
            original = (json.dumps(original_value) + "\n").encode("utf-8")

            intended_value = json.loads(plan_settings(original, launcher))

            session_start = intended_value["hooks"]["SessionStart"]
            commands = [
                hook["command"]
                for matcher in session_start
                for hook in matcher["hooks"]
                if hook.get("type") == "command"
            ]
            self.assertEqual(
                commands,
                [
                    "/user/bin/on-start",
                    f"{launcher} hook session-start",
                ],
            )
            self.assertEqual(
                session_start[0],
                {
                    "matcher": "startup",
                    "custom": "preserve me",
                    "hooks": [user_hook],
                },
            )
            self.assertEqual(intended_value["hooks"]["Stop"], stop_hooks)
            self.assertEqual(intended_value["theme"], "dark")

    def test_invalid_json_root_and_session_start_shapes_fail_closed(self):
        malformed_values = (
            b"{",
            b"\xff",
            b"null",
            b"[]",
            b'"settings"',
            b'{"hooks": null}',
            b'{"hooks": []}',
            b'{"hooks": {"SessionStart": null}}',
            b'{"hooks": {"SessionStart": {}}}',
            b'{"hooks": {"SessionStart": [1]}}',
            b'{"hooks": {"SessionStart": [{}]}}',
            b'{"hooks": {"SessionStart": [{"hooks": null}]}}',
            b'{"hooks": {"SessionStart": [{"hooks": {}}]}}',
            b'{"hooks": {"SessionStart": [{"hooks": [1]}]}}',
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            launcher = Path(temporary_directory) / "bin" / "didim"

            for original in malformed_values:
                with self.subTest(original=original):
                    with self.assertRaises(ValueError):
                        plan_settings(original, launcher)


if __name__ == "__main__":
    unittest.main()
