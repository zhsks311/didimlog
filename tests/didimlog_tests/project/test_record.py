import json
import tempfile
import unittest
from pathlib import Path

from didimlog.errors import DidimError
from didimlog.project.record import (
    GitUnavailable,
    PolicyError,
    RECORD_MAX_BYTES,
    RECORD_MAX_LINES,
    SchemaError,
    UsageError,
    parse_date,
    parse_scope,
    parse_stage1_id,
    parse_title,
    serialize_record,
    validate_body,
    validate_frontmatter,
)


DATE = "2026-07-14"
IDS = {
    "observation": "OBS-20260714-01",
    "experiment": "EXP-20260714-01",
    "evidence": "EVD-20260714-01",
}
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "record_cases.json"


def frontmatter(record_type="observation", *, tags=None, sources=None):
    record_id = IDS[record_type]
    fields = {
        "schema_version": 1,
        "id": record_id,
        "type": record_type,
        "title": f"{record_type} title",
        "status": "draft",
        "scope": "project",
        "created": DATE,
        "updated": DATE,
        "version": 1,
        "tags": [] if tags is None else tags,
        "sources": [] if sources is None else sources,
    }
    if record_type == "evidence":
        fields["artifact_path"] = "knowledge/raw/log.txt"
        fields["artifact_sha256"] = "a" * 64
    return fields


def observation_document(body_text):
    return serialize_record(
        "OBS-20260714-50",
        "observation",
        "cap",
        "project",
        DATE,
        [],
        [],
        {"body": f"## Observation\n\n{body_text}\n"},
    )


def fixture_cases(*names):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    by_name = {case["name"]: case for case in fixture["cases"]}
    return [by_name[name] for name in names]


def serialize_fixture_case(case):
    args = case["args"]
    record_type = case["type"]
    if record_type == "observation":
        body = f"## Observation\n\n{args['--body']}\n"
        type_fields = {"body": body}
    elif record_type == "experiment":
        contradicts = args["--contradicts"]
        body = (
            f"## Hypothesis\n\n{args['--hypothesis']}\n\n"
            f"## Method\n\n{args['--method']}\n\n"
            f"## Result\n\n{args['--result']}\n\n"
            "## Interpretation\n\n"
            f"Contradicts: {contradicts}\n\n{args['--interpretation']}\n"
        )
        type_fields = {"body": body}
    else:
        body = (
            f"## Artifact\n\n{args['--artifact']}\n\n"
            f"## Origin\n\n{args['--origin']}\n\n"
            f"## Collection\n\n{args['--collection']}\n"
        )
        type_fields = {
            "artifact_path": args["--artifact"],
            "artifact_sha256": args["--artifact-sha256"],
            "mode": "local",
            "body": body,
        }
    return serialize_record(
        args["--id"],
        record_type,
        args["--title"],
        args["--scope"],
        args["--date"],
        json.loads(args["--tags-json"]),
        json.loads(args["--sources-json"]),
        type_fields,
    )


class RecordErrorContractTests(unittest.TestCase):
    def test_record_errors_are_didim_errors_with_stable_exit_codes(self):
        for error_type, exit_code in (
            (UsageError, 2),
            (SchemaError, 2),
            (PolicyError, 3),
            (GitUnavailable, 7),
        ):
            with self.subTest(error_type=error_type.__name__):
                self.assertTrue(issubclass(error_type, DidimError))
                error = error_type("TOKEN")
                self.assertEqual(error.token, "TOKEN")
                self.assertEqual(error.exit_code, exit_code)


class ScalarContractTests(unittest.TestCase):
    def test_date_accepts_real_gregorian_boundary(self):
        self.assertEqual(parse_date("2024-02-29"), "2024-02-29")

    def test_date_rejects_bad_shape_and_nonexistent_day(self):
        for value in ("2024-2-29", "2026-02-29", None):
            with self.subTest(value=value), self.assertRaisesRegex(
                SchemaError, r"^INVALID_DATE"
            ):
                parse_date(value)

    def test_stage1_id_accepts_only_obs_exp_evd_shape(self):
        for record_id in IDS.values():
            with self.subTest(record_id=record_id):
                self.assertEqual(parse_stage1_id(record_id), record_id)
        for record_id in (
            "FIN-20260714-01",
            "OBS-20260714-1",
            "OBS-2026-07-14-01",
            None,
        ):
            with self.subTest(record_id=record_id), self.assertRaisesRegex(
                SchemaError, r"^INVALID_ID"
            ):
                parse_stage1_id(record_id)

    def test_scope_accepts_project_and_task_scalar_boundaries(self):
        longest_task_scope = "task:" + "a" * 64
        for scope in ("project", "task:a", longest_task_scope):
            with self.subTest(scope=scope):
                self.assertEqual(parse_scope(scope), scope)

    def test_scope_distinguishes_forbidden_and_malformed_values(self):
        for scope in ("global", "tech:python"):
            with self.subTest(scope=scope), self.assertRaisesRegex(
                PolicyError, r"^FORBIDDEN_SCOPE"
            ):
                parse_scope(scope)
        for scope in ("task:", "task:" + "a" * 65, "team:a", None):
            with self.subTest(scope=scope), self.assertRaisesRegex(
                SchemaError, r"^INVALID_SCOPE"
            ):
                parse_scope(scope)

    def test_title_counts_unicode_scalars_and_rejects_controls(self):
        longest_title = "가" * 120
        self.assertEqual(parse_title(longest_title), longest_title)
        for title in ("", "가" * 121, "line\nbreak", "nul\x00", None):
            with self.subTest(title=title), self.assertRaisesRegex(
                SchemaError, r"^INVALID_TITLE"
            ):
                parse_title(title)


class FrontmatterContractTests(unittest.TestCase):
    def test_obs_exp_evd_common_scalars_are_preserved(self):
        for record_type, record_id in IDS.items():
            with self.subTest(record_type=record_type):
                record = validate_frontmatter(frontmatter(record_type))
                self.assertEqual(record["type"], record_type)
                self.assertEqual(record["id"], record_id)
                self.assertEqual(record["created"], DATE)
                self.assertEqual(record["scope"], "project")
                self.assertEqual(record["title"], f"{record_type} title")

    def test_type_prefix_and_date_must_agree(self):
        wrong_type = frontmatter("observation")
        wrong_type["id"] = "EXP-20260714-01"
        with self.assertRaisesRegex(SchemaError, r"^ID_TYPE_MISMATCH"):
            validate_frontmatter(wrong_type)

        wrong_date = frontmatter("observation")
        wrong_date["created"] = "2026-07-15"
        with self.assertRaisesRegex(SchemaError, r"^ID_DATE_MISMATCH"):
            validate_frontmatter(wrong_date)

    def test_future_type_is_rejected(self):
        fields = frontmatter("observation")
        fields["type"] = "finding"
        with self.assertRaisesRegex(SchemaError, r"^FUTURE_TYPE finding$"):
            validate_frontmatter(fields)

    def test_frontmatter_requires_exact_key_order(self):
        items = list(frontmatter().items())
        items[3], items[4] = items[4], items[3]
        with self.assertRaisesRegex(SchemaError, r"^FRONTMATTER_ORDER"):
            validate_frontmatter(dict(items))

    def test_frontmatter_rejects_unknown_keys(self):
        fields = frontmatter()
        fields["future_field"] = "value"
        with self.assertRaisesRegex(
            SchemaError, r"^UNKNOWN_KEY OBS-20260714-01 future_field$"
        ):
            validate_frontmatter(fields)

    def test_evidence_artifact_path_is_canonical_project_relative(self):
        for artifact_path in (
            "/etc/passwd",
            "knowledge/raw/../outside.bin",
            "./knowledge/raw/report.bin",
            "knowledge//raw/report.bin",
        ):
            fields = frontmatter("evidence")
            fields["artifact_path"] = artifact_path
            with self.subTest(artifact_path=artifact_path), self.assertRaisesRegex(
                SchemaError,
                r"^INVALID_ARTIFACT_PATH EVD-20260714-01$",
            ):
                validate_frontmatter(fields)

    def test_tags_and_sources_accept_utf8_byte_order(self):
        fields = frontmatter(
            tags=["retry", "재시도"],
            sources=["EVD-20260714-01", "EXP-20260714-01"],
        )
        record = validate_frontmatter(fields)
        self.assertEqual(record["tags"], ["retry", "재시도"])
        self.assertEqual(
            record["sources"], ["EVD-20260714-01", "EXP-20260714-01"]
        )

    def test_stored_tags_must_already_be_canonical(self):
        for tags, token in (
            (["ＲＥＴＲＹ"], "NONCANONICAL_TAG"),
            (["재시도", "retry"], "UNSORTED tags"),
            (["retry", "retry"], "DUPLICATE tags"),
        ):
            with self.subTest(tags=tags), self.assertRaisesRegex(
                SchemaError, rf"^{token}"
            ):
                validate_frontmatter(frontmatter(tags=tags))

    def test_sources_reject_unsorted_and_duplicate_ids(self):
        for sources, token in (
            (["EXP-20260714-01", "EVD-20260714-01"], "UNSORTED sources"),
            (["EVD-20260714-01", "EVD-20260714-01"], "DUPLICATE sources"),
        ):
            with self.subTest(sources=sources), self.assertRaisesRegex(
                SchemaError, rf"^{token}$"
            ):
                validate_frontmatter(frontmatter(sources=sources))


class BodyContractTests(unittest.TestCase):
    def test_exact_obs_exp_evd_headings_are_accepted(self):
        cases = (
            (
                {"type": "observation", "id": IDS["observation"]},
                "## Observation\n\nObserved value.\n",
            ),
            (
                {"type": "experiment", "id": IDS["experiment"]},
                "## Hypothesis\n\nA\n\n"
                "## Method\n\nB\n\n"
                "## Result\n\nsuccess\n\n"
                "## Interpretation\n\nContradicts: none\n\nC\n",
            ),
            (
                {
                    "type": "evidence",
                    "id": IDS["evidence"],
                    "artifact_path": "knowledge/raw/log.txt",
                },
                "## Artifact\n\nknowledge/raw/log.txt\n\n"
                "## Origin\n\nCI log\n\n"
                "## Collection\n\nmanual\n",
            ),
        )
        for record, body in cases:
            with self.subTest(record_type=record["type"]):
                validated = validate_body(record, body)
                self.assertIs(validated, record)
        self.assertEqual(cases[1][0]["result"], "success")
        self.assertEqual(cases[1][0]["contradicts"], [])

    def test_additional_or_reordered_h2_heading_is_rejected(self):
        bodies = (
            "## Observation\n\nvalue\n\n## Notes\n\nextra\n",
            "## Method\n\nB\n\n## Hypothesis\n\nA\n\n"
            "## Result\n\nsuccess\n\n## Interpretation\n\n"
            "Contradicts: none\n\nC\n",
        )
        records = (
            {"type": "observation", "id": IDS["observation"]},
            {"type": "experiment", "id": IDS["experiment"]},
        )
        for record, body in zip(records, bodies):
            with self.subTest(record_type=record["type"]), self.assertRaisesRegex(
                SchemaError, r"^INVALID_HEADINGS"
            ):
                validate_body(record, body)

    def test_each_record_type_rejects_an_empty_required_section(self):
        cases = (
            (
                {"type": "observation", "id": IDS["observation"]},
                "## Observation\n\n",
                "Observation",
            ),
            (
                {"type": "experiment", "id": IDS["experiment"]},
                "## Hypothesis\n\nA\n\n## Method\n\n\n"
                "## Result\n\nsuccess\n\n## Interpretation\n\n"
                "Contradicts: none\n\nC\n",
                "Method",
            ),
            (
                {
                    "type": "evidence",
                    "id": IDS["evidence"],
                    "artifact_path": "knowledge/raw/log.txt",
                },
                "## Artifact\n\nknowledge/raw/log.txt\n\n"
                "## Origin\n\n\n## Collection\n\nmanual\n",
                "Origin",
            ),
        )
        for record, body, section in cases:
            with self.subTest(record_type=record["type"]), self.assertRaisesRegex(
                SchemaError, rf"^EMPTY_SECTION {record['id']} {section}$"
            ):
                validate_body(record, body)


class SerializationContractTests(unittest.TestCase):
    def test_representative_obs_exp_evd_bytes_match_existing_goldens(self):
        for case in fixture_cases(
            "obs_ascii_success",
            "exp_contradicts_none_success",
            "evd_local_success",
        ):
            with self.subTest(case=case["name"]):
                serialized = serialize_fixture_case(case)
                expected = case["expect"]["created"]["content"].encode("utf-8")
                self.assertIsInstance(serialized, str)
                self.assertEqual(serialized.encode("utf-8"), expected)

    def test_serialization_uses_lf_and_exactly_one_terminal_lf(self):
        serialized = observation_document("한 줄")
        encoded = serialized.encode("utf-8")
        self.assertNotIn(b"\r", encoded)
        self.assertTrue(encoded.endswith(b"\n"))
        self.assertFalse(encoded.endswith(b"\n\n"))
        self.assertIn(b"+++\n\n## Observation\n\n", encoded)

    def test_exact_byte_limit_is_accepted_and_one_byte_over_is_rejected(self):
        probe = observation_document("x")
        body_size = 1 + (RECORD_MAX_BYTES - len(probe.encode("utf-8")))
        at_limit = observation_document("x" * body_size)
        self.assertEqual(RECORD_MAX_BYTES, 12_000)
        self.assertEqual(len(at_limit.encode("utf-8")), RECORD_MAX_BYTES)

        with self.assertRaisesRegex(
            SchemaError, r"^RECORD_TOO_LARGE OBS-20260714-50$"
        ):
            observation_document("x" * (body_size + 1))

    def test_exact_lf_limit_is_accepted_and_one_lf_over_is_rejected(self):
        at_limit_body = "\n".join(["x"] * 184)
        at_limit = observation_document(at_limit_body)
        self.assertEqual(RECORD_MAX_LINES, 200)
        self.assertEqual(at_limit.encode("utf-8").count(b"\n"), RECORD_MAX_LINES)

        with self.assertRaisesRegex(
            SchemaError, r"^RECORD_TOO_MANY_LINES OBS-20260714-50$"
        ):
            observation_document("\n".join(["x"] * 185))




if __name__ == "__main__":
    unittest.main()
