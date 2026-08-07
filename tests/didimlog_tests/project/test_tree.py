import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from didimlog.errors import DidimError
from didimlog.project.record import (
    CONTRADICTS_PREFIXES,
    SOURCE_PREFIXES,
    PolicyError,
    SchemaError,
)
from didimlog.project.tree import (
    record_tree_digest,
    resolve_reference,
    validate_record_tree,
    validate_supersession_integrity,
)


DATE = "2026-07-14"


def _toml_value(value):
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[{}]".format(
            ", ".join(json.dumps(item, ensure_ascii=False) for item in value)
        )
    raise TypeError("unsupported fixture value: {!r}".format(value))


def _record_document(
    record_id,
    record_type,
    *,
    title="record",
    status="draft",
    version=1,
    sources=(),
    supersedes=None,
    superseded_by=None,
    contradicts=(),
    artifact_path=None,
    artifact_sha256=None,
):
    fields = [
        ("schema_version", 1),
        ("id", record_id),
        ("type", record_type),
        ("title", title),
        ("status", status),
        ("scope", "project"),
        ("created", DATE),
        ("updated", DATE),
        ("version", version),
        ("tags", []),
        ("sources", list(sources)),
    ]
    if supersedes is not None:
        fields.append(("supersedes", supersedes))
    if superseded_by is not None:
        fields.append(("superseded_by", superseded_by))
    if record_type == "evidence":
        if artifact_path is None:
            artifact_path = "artifacts/data/{}.bin".format(record_id)
        if artifact_sha256 is None:
            artifact_sha256 = hashlib.sha256(b"artifact\n").hexdigest()
        fields.extend(
            (
                ("artifact_path", artifact_path),
                ("artifact_sha256", artifact_sha256),
            )
        )

    frontmatter = "\n".join(
        ["+++" ]
        + ["{} = {}".format(key, _toml_value(value)) for key, value in fields]
        + ["+++"]
    )
    if record_type == "observation":
        body = "## Observation\n\n{} body\n".format(title)
    elif record_type == "experiment":
        contradiction_value = (
            "none" if not contradicts else ", ".join(contradicts)
        )
        body = (
            "## Hypothesis\n\nH\n\n"
            "## Method\n\nM\n\n"
            "## Result\n\nfailure\n\n"
            "## Interpretation\n\n"
            "Contradicts: {}\n\nI\n".format(contradiction_value)
        )
    elif record_type == "evidence":
        body = (
            "## Artifact\n\n{}\n\n"
            "## Origin\n\nlocal fixture\n\n"
            "## Collection\n\ntest\n".format(artifact_path)
        )
    else:
        body = "## Unknown\n\nvalue\n"
    return (frontmatter + "\n\n" + body).encode("utf-8")


def _record_path(workspace, record_type, filename):
    return workspace / "knowledge" / "records" / record_type / filename


def _write_record(
    workspace,
    record_id,
    record_type,
    *,
    filename=None,
    directory_type=None,
    artifact_bytes=b"artifact\n",
    create_artifact=True,
    artifact_sha256=None,
    **document_options,
):
    artifact_path = document_options.get("artifact_path")
    if record_type == "evidence":
        if artifact_path is None:
            artifact_path = "artifacts/data/{}.bin".format(record_id)
            document_options["artifact_path"] = artifact_path
        if create_artifact:
            artifact = workspace / artifact_path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(artifact_bytes)
        if artifact_sha256 is None:
            artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        document_options["artifact_sha256"] = artifact_sha256

    path = _record_path(
        workspace,
        record_type if directory_type is None else directory_type,
        record_id + ".md" if filename is None else filename,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _record_document(record_id, record_type, **document_options)
    )
    return path


def _supersession_pair():
    predecessor_id = "OBS-20260714-70"
    successor_id = "OBS-20260714-71"
    predecessor = {
        "id": predecessor_id,
        "type": "observation",
        "status": "superseded",
        "version": 2,
        "supersedes": None,
        "superseded_by": successor_id,
    }
    successor = {
        "id": successor_id,
        "type": "observation",
        "status": "validated",
        "version": 1,
        "supersedes": predecessor_id,
        "superseded_by": None,
    }
    return predecessor, successor


class ReferenceContractTests(unittest.TestCase):
    def test_reference_resolves_only_from_the_validated_map(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "validated" / "EVD-20260714-01.md"
            records_by_id = {
                "EVD-20260714-01": {
                    "id": "EVD-20260714-01",
                    "type": "evidence",
                    "path": str(target_path),
                }
            }

            resolved = resolve_reference(
                "EVD-20260714-01",
                "OBS-20260714-01",
                SOURCE_PREFIXES,
                records_by_id,
            )

            self.assertEqual(resolved, str(target_path))

    def test_filesystem_record_is_dangling_when_absent_from_validated_map(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_id = "EVD-20260714-01"
            target_path = _write_record(workspace, target_id, "evidence")
            self.assertTrue(target_path.is_file())

            with self.assertRaisesRegex(
                PolicyError,
                r"^DANGLING_SOURCE OBS-20260714-01 -> EVD-20260714-01$",
            ):
                resolve_reference(
                    target_id,
                    "OBS-20260714-01",
                    SOURCE_PREFIXES,
                    {},
                )

    def test_self_reference_and_forbidden_source_type_are_dangling(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = str(Path(temp_dir) / "record.md")
            cases = (
                (
                    "EXP-20260714-01",
                    "EXP-20260714-01",
                    SOURCE_PREFIXES,
                    {
                        "EXP-20260714-01": {
                            "type": "experiment",
                            "path": path,
                        }
                    },
                ),
                (
                    "OBS-20260714-02",
                    "OBS-20260714-01",
                    SOURCE_PREFIXES,
                    {
                        "OBS-20260714-02": {
                            "type": "observation",
                            "path": path,
                        }
                    },
                ),
            )
            for reference_id, current_id, prefixes, records in cases:
                with self.subTest(reference_id=reference_id), self.assertRaisesRegex(
                    PolicyError,
                    "^{}$".format(
                        re.escape(
                            "DANGLING_SOURCE {} -> {}".format(
                                current_id, reference_id
                            )
                        )
                    ),
                ):
                    resolve_reference(
                        reference_id, current_id, prefixes, records
                    )

    def test_reference_rejects_map_entry_with_wrong_validated_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            reference_id = "EVD-20260714-01"
            records = {
                reference_id: {
                    "id": reference_id,
                    "type": "observation",
                    "path": str(Path(temp_dir) / "EVD-20260714-01.md"),
                }
            }
            with self.assertRaisesRegex(
                PolicyError,
                r"^DANGLING_SOURCE OBS-20260714-01 -> EVD-20260714-01$",
            ):
                resolve_reference(
                    reference_id,
                    "OBS-20260714-01",
                    SOURCE_PREFIXES,
                    records,
                )

    def test_contradiction_accepts_observation_from_validated_map(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "OBS-20260714-01.md"
            records = {
                "OBS-20260714-01": {
                    "id": "OBS-20260714-01",
                    "type": "observation",
                    "path": str(target_path),
                }
            }

            resolved = resolve_reference(
                "OBS-20260714-01",
                "EXP-20260714-01",
                CONTRADICTS_PREFIXES,
                records,
            )

            self.assertEqual(resolved, str(target_path))


class SupersessionIntegrityTests(unittest.TestCase):
    def test_reciprocal_same_type_pair_with_superseded_predecessor_is_valid(self):
        predecessor, successor = _supersession_pair()

        result = validate_supersession_integrity([successor, predecessor])

        self.assertIsNone(result)
        self.assertEqual(predecessor["status"], "superseded")
        self.assertEqual(predecessor["version"], 2)

    def test_both_supersession_directions_reject_dangling_ids(self):
        predecessor, successor = _supersession_pair()
        cases = (
            (
                [predecessor],
                "DANGLING_SUPERSEDED_BY OBS-20260714-70 -> OBS-20260714-71",
            ),
            (
                [successor],
                "DANGLING_SUPERSEDES OBS-20260714-71 -> OBS-20260714-70",
            ),
        )
        for records, token in cases:
            with self.subTest(token=token), self.assertRaisesRegex(
                PolicyError, "^{}$".format(re.escape(token))
            ):
                validate_supersession_integrity(records)

    def test_both_supersession_directions_must_be_reciprocal(self):
        predecessor, successor = _supersession_pair()
        successor_without_link = dict(successor, supersedes=None)
        with self.assertRaisesRegex(
            PolicyError,
            r"^NONRECIPROCAL_SUPERSESSION OBS-20260714-70 -> OBS-20260714-71$",
        ):
            validate_supersession_integrity(
                [predecessor, successor_without_link]
            )

        predecessor_without_link = dict(predecessor, superseded_by=None)
        with self.assertRaisesRegex(
            PolicyError,
            r"^NONRECIPROCAL_SUPERSESSION OBS-20260714-71 -> OBS-20260714-70$",
        ):
            validate_supersession_integrity(
                [predecessor_without_link, successor]
            )

    def test_superseded_by_requires_superseded_status(self):
        predecessor, successor = _supersession_pair()
        predecessor["status"] = "validated"

        with self.assertRaisesRegex(
            PolicyError, r"^NOT_SUPERSEDED OBS-20260714-70$"
        ):
            validate_supersession_integrity([predecessor, successor])

    def test_supersedes_requires_superseded_predecessor(self):
        predecessor, successor = _supersession_pair()
        predecessor["status"] = "draft"
        predecessor["superseded_by"] = None

        with self.assertRaisesRegex(
            PolicyError,
            r"^PREDECESSOR_NOT_SUPERSEDED OBS-20260714-71 -> OBS-20260714-70$",
        ):
            validate_supersession_integrity([predecessor, successor])

    def test_direct_integrity_check_rejects_cross_type_pair(self):
        predecessor, successor = _supersession_pair()
        successor["type"] = "experiment"

        with self.assertRaisesRegex(
            PolicyError,
            r"^CROSS_TYPE_SUPERSESSION OBS-20260714-70 superseded_by$",
        ):
            validate_supersession_integrity([predecessor, successor])


class RecordTreePathTests(unittest.TestCase):
    def test_records_root_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            outside = root / "outside-records"
            (workspace / "knowledge").mkdir(parents=True)
            outside.mkdir()
            records_root = workspace / "knowledge" / "records"
            records_root.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                PolicyError,
                "^{}$".format(re.escape("PATH_ESCAPE {}".format(records_root))),
            ):
                validate_record_tree(str(workspace))

    def test_type_directory_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            records_root = workspace / "knowledge" / "records"
            outside = root / "outside-observations"
            records_root.mkdir(parents=True)
            outside.mkdir()
            type_link = records_root / "observation"
            type_link.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                PolicyError,
                "^{}$".format(re.escape("PATH_ESCAPE {}".format(type_link))),
            ):
                validate_record_tree(str(workspace))

    def test_record_file_symlink_is_rejected_even_when_target_is_valid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            record_id = "OBS-20260714-01"
            target = root / "outside.md"
            target.write_bytes(
                _record_document(record_id, "observation")
            )
            link = _record_path(
                workspace, "observation", record_id + ".md"
            )
            link.parent.mkdir(parents=True)
            link.symlink_to(target)

            with self.assertRaisesRegex(
                PolicyError,
                "^{}$".format(re.escape("PATH_ESCAPE {}".format(link))),
            ):
                validate_record_tree(str(workspace))

    def test_record_in_wrong_type_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            path = _write_record(
                workspace,
                "OBS-20260714-01",
                "observation",
                directory_type="experiment",
            )

            with self.assertRaisesRegex(
                PolicyError,
                "^{}$".format(
                    re.escape(
                        "WRONG_DIRECTORY OBS-20260714-01 {}".format(path)
                    )
                ),
            ):
                validate_record_tree(str(workspace))

    def test_nested_record_layout_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            record_id = "OBS-20260714-01"
            path = (
                workspace
                / "knowledge"
                / "records"
                / "observation"
                / "nested"
                / (record_id + ".md")
            )
            path.parent.mkdir(parents=True)
            path.write_bytes(_record_document(record_id, "observation"))

            with self.assertRaisesRegex(
                PolicyError,
                "^{}$".format(
                    re.escape("WRONG_DIRECTORY {} {}".format(record_id, path))
                ),
            ):
                validate_record_tree(str(workspace))
    def test_knowledge_parent_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            _write_record(
                outside,
                "OBS-20260714-01",
                "observation",
            )
            try:
                (workspace / "knowledge").symlink_to(
                    outside / "knowledge",
                    target_is_directory=True,
                )
            except (NotImplementedError, OSError) as error:
                self.skipTest("directory symlinks unavailable: {}".format(error))

            with self.assertRaisesRegex(PolicyError, r"^PATH_ESCAPE "):
                validate_record_tree(str(workspace))


    def test_noncanonical_basename_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            record_id = "OBS-20260714-01"
            path = _write_record(
                workspace,
                record_id,
                "observation",
                filename=record_id + "-copy.md",
            )

            with self.assertRaisesRegex(
                PolicyError,
                "^{}$".format(
                    re.escape(
                        "NONCANONICAL_FILENAME {} {}".format(record_id, path)
                    )
                ),
            ):
                validate_record_tree(str(workspace))


class RecordTreeValidationTests(unittest.TestCase):
    def test_empty_tree_is_valid(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()

            self.assertEqual(validate_record_tree(str(workspace)), [])

    def test_malformed_document_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            path = _record_path(
                workspace, "observation", "OBS-20260714-01.md"
            )
            path.parent.mkdir(parents=True)
            path.write_bytes(b"+++\nschema_version = 1\n")

            with self.assertRaises(DidimError) as raised:
                validate_record_tree(str(workspace))

            self.assertEqual(str(raised.exception), "missing closing +++")
            self.assertEqual(raised.exception.exit_code, 2)

    def test_malformed_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            path = _record_path(workspace, "observation", "OBS-bad.md")
            path.parent.mkdir(parents=True)
            path.write_bytes(_record_document("OBS-bad", "observation"))

            with self.assertRaisesRegex(SchemaError, r"^INVALID_ID OBS-bad$"):
                validate_record_tree(str(workspace))

    def test_duplicate_id_precedes_noncanonical_basename_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            record_id = "OBS-20260714-01"
            _write_record(workspace, record_id, "observation")
            _write_record(
                workspace,
                record_id,
                "observation",
                filename=record_id + "-duplicate.md",
                title="duplicate",
            )

            with self.assertRaisesRegex(
                PolicyError, r"^DUPLICATE_ID OBS-20260714-01$"
            ):
                validate_record_tree(str(workspace))

    def test_collision_id_is_rejected_for_create_only_callers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            record_id = "OBS-20260714-01"
            _write_record(workspace, record_id, "observation")

            with self.assertRaisesRegex(
                PolicyError, r"^COLLISION OBS-20260714-01$"
            ):
                validate_record_tree(str(workspace), collision_id=record_id)

    def test_local_evidence_artifact_is_validated_as_part_of_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            record_id = "EVD-20260714-01"
            artifact_bytes = b"bound artifact\n"
            path = _write_record(
                workspace,
                record_id,
                "evidence",
                artifact_bytes=artifact_bytes,
            )

            records = validate_record_tree(str(workspace))

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["id"], record_id)
            self.assertEqual(records[0]["artifact_mode"], "local")
            self.assertEqual(Path(records[0]["path"]), path)
            self.assertEqual(records[0]["content_bytes"], path.read_bytes())

    def test_missing_and_digest_mismatched_artifacts_fail_closed(self):
        cases = (
            (False, hashlib.sha256(b"artifact\n").hexdigest(), "ARTIFACT_MISSING"),
            (True, "0" * 64, "ARTIFACT_DIGEST_MISMATCH"),
        )
        for create_artifact, expected_sha, token in cases:
            with self.subTest(token=token), tempfile.TemporaryDirectory() as temp_dir:
                workspace = Path(temp_dir) / "workspace"
                workspace.mkdir()
                record_id = "EVD-20260714-01"
                artifact_path = "artifacts/data/{}.bin".format(record_id)
                _write_record(
                    workspace,
                    record_id,
                    "evidence",
                    create_artifact=create_artifact,
                    artifact_sha256=expected_sha,
                )

                with self.assertRaisesRegex(
                    PolicyError,
                    "^{}$".format(
                        re.escape(
                            "{} {} {}".format(
                                token, record_id, artifact_path
                            )
                        )
                    ),
                ):
                    validate_record_tree(str(workspace))

    def test_sources_and_contradictions_resolve_against_complete_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            evidence_id = "EVD-20260714-01"
            observation_id = "OBS-20260714-01"
            experiment_id = "EXP-20260714-01"
            _write_record(workspace, evidence_id, "evidence")
            _write_record(workspace, observation_id, "observation")
            _write_record(
                workspace,
                experiment_id,
                "experiment",
                sources=(evidence_id,),
                contradicts=(observation_id,),
            )

            records = validate_record_tree(str(workspace))
            records_by_id = {record["id"]: record for record in records}

            self.assertEqual(records_by_id[experiment_id]["sources"], [evidence_id])
            self.assertEqual(
                records_by_id[experiment_id]["contradicts"], [observation_id]
            )

    def test_dangling_source_and_contradiction_fail_closed(self):
        cases = (
            (
                "OBS-20260714-01",
                "observation",
                {"sources": ("EVD-20260714-99",)},
                "DANGLING_SOURCE OBS-20260714-01 -> EVD-20260714-99",
            ),
            (
                "EXP-20260714-01",
                "experiment",
                {"contradicts": ("OBS-20260714-99",)},
                "DANGLING_SOURCE EXP-20260714-01 -> OBS-20260714-99",
            ),
        )
        for record_id, record_type, options, token in cases:
            with self.subTest(token=token), tempfile.TemporaryDirectory() as temp_dir:
                workspace = Path(temp_dir) / "workspace"
                workspace.mkdir()
                _write_record(workspace, record_id, record_type, **options)

                with self.assertRaisesRegex(
                    PolicyError, "^{}$".format(re.escape(token))
                ):
                    validate_record_tree(str(workspace))

    def test_self_and_cross_type_supersession_are_rejected(self):
        cases = (
            (
                "OBS-20260714-01",
                "observation",
                "OBS-20260714-01",
                "SELF_SUPERSESSION OBS-20260714-01 supersedes",
            ),
            (
                "OBS-20260714-01",
                "observation",
                "EXP-20260714-01",
                "CROSS_TYPE_SUPERSESSION OBS-20260714-01 supersedes",
            ),
        )
        for record_id, record_type, supersedes, token in cases:
            with self.subTest(token=token), tempfile.TemporaryDirectory() as temp_dir:
                workspace = Path(temp_dir) / "workspace"
                workspace.mkdir()
                _write_record(
                    workspace,
                    record_id,
                    record_type,
                    supersedes=supersedes,
                )

                with self.assertRaisesRegex(
                    PolicyError, "^{}$".format(re.escape(token))
                ):
                    validate_record_tree(str(workspace))

    def test_invalid_version_is_rejected_before_supersession_integrity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            _write_record(
                workspace,
                "OBS-20260714-70",
                "observation",
                status="superseded",
                version=0,
                superseded_by="OBS-20260714-71",
            )

            with self.assertRaisesRegex(
                SchemaError, r"^INVALID_VERSION OBS-20260714-70$"
            ):
                validate_record_tree(str(workspace))

    def test_unverified_negative_and_superseded_history_remain_in_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            _write_record(
                workspace,
                "OBS-20260714-01",
                "observation",
                status="draft",
            )
            _write_record(
                workspace,
                "EXP-20260714-01",
                "experiment",
                status="running",
            )
            _write_record(
                workspace,
                "EXP-20260714-02",
                "experiment",
                status="refuted",
            )
            _write_record(
                workspace,
                "OBS-20260714-70",
                "observation",
                status="superseded",
                version=2,
                superseded_by="OBS-20260714-71",
            )
            _write_record(
                workspace,
                "OBS-20260714-71",
                "observation",
                status="validated",
                supersedes="OBS-20260714-70",
            )

            records = validate_record_tree(str(workspace))
            by_id = {record["id"]: record for record in records}

            self.assertEqual(
                {record["status"] for record in records},
                {"draft", "running", "refuted", "superseded", "validated"},
            )
            self.assertEqual(by_id["OBS-20260714-70"]["version"], 2)
            self.assertEqual(
                by_id["OBS-20260714-70"]["superseded_by"],
                "OBS-20260714-71",
            )
            self.assertEqual(
                by_id["OBS-20260714-71"]["supersedes"],
                "OBS-20260714-70",
            )


class RecordTreeDigestTests(unittest.TestCase):
    @staticmethod
    def _expected(records):
        parts = []
        for record in sorted(records, key=lambda item: item["id"]):
            content_sha256 = hashlib.sha256(record["content_bytes"]).hexdigest()
            parts.append(
                "{}\t{}\n".format(record["id"], content_sha256).encode("utf-8")
            )
        return hashlib.sha256(b"".join(parts)).hexdigest()

    def test_empty_digest_is_sha256_of_empty_canonical_input(self):
        self.assertEqual(record_tree_digest([]), hashlib.sha256(b"").hexdigest())

    def test_digest_matches_independent_canonical_byte_construction(self):
        records = [
            {"id": "OBS-20260714-02", "content_bytes": b"second\n"},
            {"id": "EVD-20260714-01", "content_bytes": b"first\n"},
        ]

        digest = record_tree_digest(records)

        self.assertEqual(digest, self._expected(records))
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_digest_is_deterministic_regardless_of_input_order(self):
        first = {"id": "OBS-20260714-01", "content_bytes": b"one\n"}
        second = {"id": "EXP-20260714-01", "content_bytes": b"two\n"}

        self.assertEqual(
            record_tree_digest([first, second]),
            record_tree_digest([second, first]),
        )
        self.assertEqual(
            record_tree_digest([first, second]),
            record_tree_digest([first, second]),
        )

    def test_digest_changes_when_record_content_changes(self):
        original = [
            {"id": "OBS-20260714-01", "content_bytes": b"original\n"}
        ]
        changed = [
            {"id": "OBS-20260714-01", "content_bytes": b"changed\n"}
        ]

        self.assertNotEqual(
            record_tree_digest(original), record_tree_digest(changed)
        )


if __name__ == "__main__":
    unittest.main()
