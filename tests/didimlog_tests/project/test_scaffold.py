import dataclasses
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import didimlog.project.scaffold as scaffold_module

from didimlog.errors import DidimError, EXIT_POLICY
from didimlog.project.scaffold import ScaffoldPlan, apply_scaffold, plan_scaffold


EXPECTED_DIRECTORIES = (
    pathlib.Path("knowledge"),
    pathlib.Path("knowledge/records"),
    pathlib.Path("knowledge/records/observation"),
    pathlib.Path("knowledge/records/experiment"),
    pathlib.Path("knowledge/records/evidence"),
    pathlib.Path("knowledge/raw"),
    pathlib.Path("knowledge/index"),
    pathlib.Path("knowledge/schema"),
    pathlib.Path("knowledge/active"),
)
EXPECTED_FILES = (
    pathlib.Path("knowledge/README.md"),
    pathlib.Path("knowledge/POINTER.md"),
    pathlib.Path("knowledge/schema/record.schema.json"),
    pathlib.Path("knowledge/active/harness.md"),
)
ACTIVE_GUIDANCE = b"# Active Guidance\n"
POINTER = (
    "# POINTER\n"
    "읽기 순서: 이 파일 → active/harness.md → index/INDEX.md → records/ 해당 기록(최대 5건).\n"
    "규칙과 용어 정의는 README.md에서 본다. 이 파일은 규칙을 정의하지 않는다.\n"
    "records/ 안의 기록만이 유일한 진실 출처다.\n"
    "`review_by`가 조회 기준일보다 이르면 STALE로 취급해 참고만 하고, 부정(refuted/failure) 결과는 지우지 않는다.\n"
    "active/harness.md에 사람이 직접 규칙을 쓰지 않는다(승격 게이트 전용, v1에서는 비어 있음).\n"
).encode("utf-8")


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self._make_empty_git_repository(self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _make_empty_git_repository(workspace):
        git = workspace / ".git"
        (git / "objects").mkdir(parents=True)
        (git / "refs/heads").mkdir(parents=True)
        (git / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (git / "config").write_bytes(
            b"[core]\n"
            b"\trepositoryformatversion = 0\n"
            b"\tbare = false\n"
        )

    def _relative_directories(self, plan):
        return tuple(path.relative_to(self.workspace) for path in plan.directories)

    def _relative_files(self, plan):
        return tuple(path.relative_to(self.workspace) for path, _ in plan.files)

    def _planned_bytes(self, plan):
        return {
            path.relative_to(self.workspace): content
            for path, content in plan.files
        }

    def _assert_policy_error(self, operation):
        with self.assertRaises(DidimError) as raised:
            operation()
        self.assertEqual(raised.exception.exit_code, EXIT_POLICY)

    def _symlink(self, link, target, *, target_is_directory=False):
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (NotImplementedError, OSError) as error:
            self.skipTest(f"symlinks unavailable: {error}")

    def test_empty_git_workspace_has_exact_deterministic_plan(self):
        plan = plan_scaffold(self.workspace)
        repeated = plan_scaffold(self.workspace)

        self.assertEqual(repeated, plan)
        self.assertFalse((self.workspace / "knowledge").exists())

        self.assertIsInstance(plan, ScaffoldPlan)
        self.assertEqual(self._relative_directories(plan), EXPECTED_DIRECTORIES)
        self.assertEqual(self._relative_files(plan), EXPECTED_FILES)
        self.assertTrue(all(isinstance(content, bytes) for _, content in plan.files))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.directories = ()

        planned = self._planned_bytes(plan)
        self.assertTrue(planned[pathlib.Path("knowledge/README.md")].startswith(
            b"# Knowledge Harness (v1)"
        ))
        self.assertEqual(planned[pathlib.Path("knowledge/POINTER.md")], POINTER)
        schema = json.loads(
            planned[pathlib.Path("knowledge/schema/record.schema.json")].decode("utf-8")
        )
        self.assertEqual(schema["$schema"], "http://json-schema.org/draft-07/schema#")
        self.assertEqual(
            schema["properties"]["type"]["enum"],
            ["observation", "experiment", "evidence"],
        )
        self.assertEqual(
            planned[pathlib.Path("knowledge/active/harness.md")],
            ACTIVE_GUIDANCE,
        )

    def test_apply_creates_only_the_planned_scaffold(self):
        plan = plan_scaffold(self.workspace)
        apply_scaffold(plan)

        knowledge = self.workspace / "knowledge"
        actual_directories = {pathlib.Path("knowledge")}
        actual_directories.update(
            path.relative_to(self.workspace)
            for path in knowledge.rglob("*")
            if path.is_dir()
        )
        actual_files = {
            path.relative_to(self.workspace)
            for path in knowledge.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual_directories, set(EXPECTED_DIRECTORIES))
        self.assertEqual(actual_files, set(EXPECTED_FILES))
        for relative, expected in self._planned_bytes(plan).items():
            self.assertEqual((self.workspace / relative).read_bytes(), expected)

    def test_compatible_partial_scaffold_is_completed_without_rewriting_it(self):
        initial_plan = plan_scaffold(self.workspace)
        planned = self._planned_bytes(initial_plan)
        existing = self.workspace / "knowledge/README.md"
        existing.parent.mkdir()
        existing.write_bytes(planned[pathlib.Path("knowledge/README.md")])
        old_timestamp = 1_600_000_000_000_000_000
        os.utime(existing, ns=(old_timestamp, old_timestamp))
        (self.workspace / "knowledge/records/observation").mkdir(parents=True)

        apply_scaffold(plan_scaffold(self.workspace))

        self.assertEqual(existing.read_bytes(), planned[pathlib.Path("knowledge/README.md")])
        self.assertEqual(existing.stat().st_mtime_ns, old_timestamp)
        self.assertTrue((self.workspace / "knowledge/records/experiment").is_dir())
        self.assertTrue((self.workspace / "knowledge/active/harness.md").is_file())

    def test_rerun_preserves_every_scaffold_file_byte_and_timestamp(self):
        first_plan = plan_scaffold(self.workspace)
        apply_scaffold(first_plan)
        expected_bytes = {}
        expected_timestamps = {}
        for offset, relative in enumerate(EXPECTED_FILES):
            path = self.workspace / relative
            timestamp = 1_600_000_000_000_000_000 + offset
            os.utime(path, ns=(timestamp, timestamp))
            expected_bytes[relative] = path.read_bytes()
            expected_timestamps[relative] = path.stat().st_mtime_ns

        apply_scaffold(plan_scaffold(self.workspace))

        for relative in EXPECTED_FILES:
            path = self.workspace / relative
            self.assertEqual(path.read_bytes(), expected_bytes[relative])
            self.assertEqual(path.stat().st_mtime_ns, expected_timestamps[relative])

    def test_regular_file_where_directory_is_required_blocks_all_writes(self):
        knowledge = self.workspace / "knowledge"
        knowledge.mkdir()
        conflict = knowledge / "records"
        conflict.write_bytes(b"user data\n")

        self._assert_policy_error(lambda: plan_scaffold(self.workspace))

        self.assertEqual(list(knowledge.iterdir()), [conflict])
        self.assertEqual(conflict.read_bytes(), b"user data\n")

    def test_different_regular_file_at_final_target_blocks_all_writes(self):
        knowledge = self.workspace / "knowledge"
        knowledge.mkdir()
        conflict = knowledge / "README.md"
        conflict.write_bytes(b"user-owned readme\n")

        self._assert_policy_error(lambda: plan_scaffold(self.workspace))

        self.assertEqual(list(knowledge.iterdir()), [conflict])
        self.assertEqual(conflict.read_bytes(), b"user-owned readme\n")

    def test_file_appearing_after_plan_is_preserved_and_aborts_apply(self):
        plan = plan_scaffold(self.workspace)
        knowledge = self.workspace / "knowledge"
        knowledge.mkdir()
        conflict = knowledge / "README.md"
        conflict.write_bytes(b"concurrent user file\n")

        self._assert_policy_error(lambda: apply_scaffold(plan))

        self.assertEqual(list(knowledge.iterdir()), [conflict])
        self.assertEqual(conflict.read_bytes(), b"concurrent user file\n")

    def test_parent_replaced_by_symlink_during_apply_cannot_escape_workspace(self):
        plan = plan_scaffold(self.workspace)
        outside = self.root / "outside"
        outside.mkdir()
        original_create_file = scaffold_module._create_file
        replaced = False

        def replace_schema_parent(path, content, *args):
            nonlocal replaced
            if path.name == "record.schema.json" and not replaced:
                path.parent.rmdir()
                self._symlink(path.parent, outside, target_is_directory=True)
                replaced = True
            return original_create_file(path, content, *args)

        with mock.patch.object(
            scaffold_module,
            "_create_file",
            side_effect=replace_schema_parent,
        ):
            self._assert_policy_error(lambda: apply_scaffold(plan))

        self.assertFalse((outside / "record.schema.json").exists())

    def test_symlinked_parent_is_rejected_without_touching_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        self._symlink(
            self.workspace / "knowledge",
            outside,
            target_is_directory=True,
        )

        self._assert_policy_error(lambda: plan_scaffold(self.workspace))

        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.workspace / "knowledge").is_symlink())

    def test_symlinked_final_file_is_rejected_without_touching_target(self):
        outside = self.root / "outside.md"
        outside.write_bytes(b"outside sentinel\n")
        knowledge = self.workspace / "knowledge"
        knowledge.mkdir()
        final = knowledge / "README.md"
        self._symlink(final, outside)

        self._assert_policy_error(lambda: plan_scaffold(self.workspace))

        self.assertTrue(final.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside sentinel\n")
        self.assertEqual(list(knowledge.iterdir()), [final])

    def test_apply_rejects_forged_path_escape_before_writing_any_target(self):
        plan = plan_scaffold(self.workspace)
        escaped = self.workspace / "knowledge/../../escaped.md"
        forged = ScaffoldPlan(
            directories=plan.directories,
            files=plan.files + ((escaped, b"escaped\n"),),
        )

        self._assert_policy_error(lambda: apply_scaffold(forged))

        self.assertFalse((self.workspace / "knowledge").exists())
        self.assertFalse((self.root / "escaped.md").exists())

    def test_symlinked_workspace_alias_is_rejected_as_path_escape(self):
        alias = self.root / "workspace-alias"
        self._symlink(alias, self.workspace, target_is_directory=True)

        self._assert_policy_error(lambda: plan_scaffold(alias))

        self.assertFalse((self.workspace / "knowledge").exists())

    def test_active_guidance_remains_header_only(self):
        apply_scaffold(plan_scaffold(self.workspace))

        active = self.workspace / "knowledge/active/harness.md"
        self.assertEqual(active.read_bytes(), ACTIVE_GUIDANCE)
        self.assertEqual(active.read_text(encoding="utf-8").splitlines(), ["# Active Guidance"])


if __name__ == "__main__":
    unittest.main()
