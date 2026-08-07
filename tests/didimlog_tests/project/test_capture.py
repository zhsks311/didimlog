import contextlib
import dataclasses
import errno
import hashlib
import importlib
import inspect
import io
import multiprocessing
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import types
import unittest
from unittest import mock

import didimlog.project.record as record_module
import didimlog.project as project_package

from didimlog.errors import DidimError, EXIT_POLICY
from didimlog.project.capture import CaptureRequest, capture
from didimlog.project.record import PolicyError, SchemaError, serialize_record
from didimlog.project.scaffold import apply_scaffold, plan_scaffold
from didimlog.project.tree import validate_record_tree


DATE = "2026-07-14"


def _observation_request(body, **overrides):
    values = {
        "type": "observation",
        "date": DATE,
        "scope": "project",
        "title": "관찰",
        "tags": (),
        "sources": (),
        "fields": {"body": body},
    }
    values.update(overrides)
    return CaptureRequest(**values)


def _read_record(path):
    text = path.read_text(encoding="utf-8")
    closing = text.index("\n+++\n", 4)
    frontmatter = tomllib.loads(text[4:closing])
    body = text[closing + len("\n+++\n") :]
    if body.startswith("\n"):
        body = body[1:]
    return frontmatter, body


def _capture_in_process(workspace, body, ready, start, results):
    ready.put(True)
    if not start.wait(10):
        results.put(("error", "start timeout"))
        return
    try:
        path = capture(pathlib.Path(workspace), _observation_request(body))
    except BaseException as error:
        results.put(("error", type(error).__name__, str(error)))
    else:
        results.put(("ok", path.name))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.git = shutil.which("git")
        if self.git is None:
            self.skipTest("git is required for project capture tests")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Didimlog Test")
        self._git("config", "user.email", "didimlog@example.invalid")
        apply_scaffold(plan_scaffold(self.workspace))

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *arguments, workspace=None):
        selected = self.workspace if workspace is None else pathlib.Path(workspace)
        return subprocess.run(
            [self.git, *arguments],
            cwd=selected,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def _record_path(self, record_type, record_id):
        return (
            self.workspace
            / "knowledge"
            / "records"
            / record_type
            / f"{record_id}.md"
        )

    def _capture_local_evidence(self, name="local.txt", content=b"local evidence\n"):
        artifact = self.workspace / "artifacts" / name
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        request = CaptureRequest(
            type="evidence",
            date=DATE,
            scope="project",
            title="로컬 증거",
            tags=("evidence",),
            sources=(),
            fields={
                "artifact": f"artifacts/{name}",
                "origin": "격리 테스트 fixture",
                "collection": "테스트가 직접 생성",
                "artifact_sha256": digest,
            },
        )
        return capture(self.workspace, request)

    def test_public_contract_requires_explicit_date_and_exposes_no_id_or_output_knobs(self):
        self.assertEqual(
            [field.name for field in dataclasses.fields(CaptureRequest)],
            ["type", "date", "scope", "title", "tags", "sources", "fields"],
        )
        date_field = next(
            field for field in dataclasses.fields(CaptureRequest) if field.name == "date"
        )
        self.assertIs(date_field.default, dataclasses.MISSING)
        self.assertTrue(CaptureRequest.__dataclass_params__.frozen)

        parameters = inspect.signature(capture).parameters
        self.assertEqual(list(parameters), ["workspace", "request", "max_id_retries"])
        self.assertEqual(
            parameters["max_id_retries"].kind,
            inspect.Parameter.KEYWORD_ONLY,
        )
        self.assertEqual(parameters["max_id_retries"].default, 8)

        values = dataclasses.asdict(_observation_request("원문"))
        for forbidden in ("id", "output", "workspace"):
            with self.subTest(forbidden=forbidden), self.assertRaises(TypeError):
                CaptureRequest(**values, **{forbidden: "caller-controlled"})
        with self.assertRaises(TypeError):
            capture(
                self.workspace,
                _observation_request("원문"),
                id="OBS-20260714-90",
            )

    def test_next_id_is_per_prefix_and_explicit_date_and_matches_record_metadata(self):
        first = capture(self.workspace, _observation_request("첫 기록"))
        second = capture(self.workspace, _observation_request("둘째 기록"))
        next_date = capture(
            self.workspace,
            _observation_request("다음 날짜", date="2026-07-15"),
        )

        self.assertEqual(first, self._record_path("observation", "OBS-20260714-01"))
        self.assertEqual(second, self._record_path("observation", "OBS-20260714-02"))
        self.assertEqual(
            next_date,
            self._record_path("observation", "OBS-20260715-01"),
        )
        for path, record_id, created in (
            (first, "OBS-20260714-01", DATE),
            (second, "OBS-20260714-02", DATE),
            (next_date, "OBS-20260715-01", "2026-07-15"),
        ):
            with self.subTest(record_id=record_id):
                frontmatter, _ = _read_record(path)
                self.assertEqual(frontmatter["id"], record_id)
                self.assertEqual(frontmatter["created"], created)
                self.assertEqual(frontmatter["updated"], created)

        with self.assertRaisesRegex(SchemaError, r"^INVALID_DATE"):
            capture(self.workspace, _observation_request("날짜 없음", date=""))
        self.assertEqual(
            sorted(path.name for path in first.parent.iterdir()),
            ["OBS-20260714-01.md", "OBS-20260714-02.md", "OBS-20260715-01.md"],
        )

    def test_observation_experiment_and_local_evidence_use_canonical_bodies_and_binding(self):
        observation = capture(self.workspace, _observation_request("관찰 원문\n둘째 줄"))
        evidence = self._capture_local_evidence()
        evidence_meta, evidence_body = _read_record(evidence)
        experiment = capture(
            self.workspace,
            CaptureRequest(
                type="experiment",
                date=DATE,
                scope="task:capture",
                title="실험",
                tags=("experiment",),
                sources=(evidence_meta["id"],),
                fields={
                    "hypothesis": "충돌 없이 ID를 할당한다.",
                    "method": "같은 날짜에 연속 생성한다.",
                    "result": "success",
                    "contradicts": "OBS-20260714-01",
                    "interpretation": "원자적 생성이 관찰을 반박한다.",
                },
            ),
        )

        _, observation_body = _read_record(observation)
        experiment_meta, experiment_body = _read_record(experiment)
        self.assertEqual(
            observation_body,
            "## Observation\n\n관찰 원문\n둘째 줄\n",
        )
        self.assertEqual(
            experiment_body,
            "## Hypothesis\n\n충돌 없이 ID를 할당한다.\n\n"
            "## Method\n\n같은 날짜에 연속 생성한다.\n\n"
            "## Result\n\nsuccess\n\n"
            "## Interpretation\n\nContradicts: OBS-20260714-01\n\n"
            "원자적 생성이 관찰을 반박한다.\n",
        )
        self.assertEqual(
            evidence_body,
            "## Artifact\n\nartifacts/local.txt\n\n"
            "## Origin\n\n격리 테스트 fixture\n\n"
            "## Collection\n\n테스트가 직접 생성\n",
        )
        self.assertEqual(experiment_meta["sources"], ["EVD-20260714-01"])
        self.assertEqual(evidence_meta["artifact_path"], "artifacts/local.txt")
        self.assertEqual(
            evidence_meta["artifact_sha256"],
            hashlib.sha256(b"local evidence\n").hexdigest(),
        )
        self.assertNotIn("artifact_git", evidence_meta)
        self.assertEqual(
            {record["id"] for record in validate_record_tree(self.workspace)},
            {"OBS-20260714-01", "EXP-20260714-01", "EVD-20260714-01"},
        )

    def test_evidence_git_binding_uses_the_exact_committed_artifact(self):
        artifact = self.workspace / "artifacts" / "committed.txt"
        artifact.parent.mkdir(exist_ok=True)
        artifact.write_bytes(b"committed evidence\n")
        self._git("add", "--", "artifacts/committed.txt")
        self._git("commit", "-q", "-m", "add evidence")
        commit = self._git("rev-parse", "HEAD")

        path = capture(
            self.workspace,
            CaptureRequest(
                type="evidence",
                date=DATE,
                scope="project",
                title="Git 증거",
                tags=(),
                sources=(),
                fields={
                    "artifact": "artifacts/committed.txt",
                    "origin": "fixture commit",
                    "collection": "git commit",
                    "artifact_git": commit,
                },
            ),
        )

        frontmatter, body = _read_record(path)
        self.assertEqual(frontmatter["artifact_git"], commit)
        self.assertNotIn("artifact_sha256", frontmatter)
        self.assertEqual(
            body,
            "## Artifact\n\nartifacts/committed.txt\n\n"
            "## Origin\n\nfixture commit\n\n"
            "## Collection\n\ngit commit\n",
        )
        validate_record_tree(self.workspace)

    def test_tags_and_sources_are_canonical_and_sorted_unique_before_write(self):
        evidence = self._capture_local_evidence()
        evidence_id = evidence.stem
        experiment = capture(
            self.workspace,
            CaptureRequest(
                type="experiment",
                date=DATE,
                scope="project",
                title="source fixture",
                tags=(),
                sources=(evidence_id,),
                fields={
                    "hypothesis": "가설",
                    "method": "방법",
                    "result": "inconclusive",
                    "contradicts": "none",
                    "interpretation": "해석",
                },
            ),
        )
        experiment_id = experiment.stem

        path = capture(
            self.workspace,
            _observation_request(
                "정본 배열",
                tags=("alpha", "ＢETA", "한글"),
                sources=(evidence_id, experiment_id),
            ),
        )
        frontmatter, _ = _read_record(path)
        self.assertEqual(frontmatter["tags"], ["alpha", "beta", "한글"])
        self.assertEqual(frontmatter["sources"], [evidence_id, experiment_id])

        before = set(path.parent.iterdir())
        with self.assertRaisesRegex(SchemaError, r"^DUPLICATE tags"):
            capture(
                self.workspace,
                _observation_request("중복 태그", tags=("Alpha", "alpha")),
            )
        with self.assertRaisesRegex(SchemaError, r"^UNSORTED sources"):
            capture(
                self.workspace,
                _observation_request(
                    "정렬되지 않은 출처",
                    sources=(experiment_id, evidence_id),
                ),
            )
        self.assertEqual(set(path.parent.iterdir()), before)

    def test_dangling_wrong_type_sources_and_contradictions_fail_before_create(self):
        observation = capture(self.workspace, _observation_request("기존 관찰"))
        record_directory = observation.parent

        cases = (
            (
                _observation_request(
                    "OBS는 source가 될 수 없음",
                    sources=(observation.stem,),
                ),
                SchemaError,
                r"^INVALID_SOURCE",
            ),
            (
                _observation_request(
                    "없는 source",
                    sources=("EVD-20260714-99",),
                ),
                PolicyError,
                r"^DANGLING_SOURCE",
            ),
            (
                CaptureRequest(
                    type="experiment",
                    date=DATE,
                    scope="project",
                    title="없는 반박 대상",
                    tags=(),
                    sources=(),
                    fields={
                        "hypothesis": "가설",
                        "method": "방법",
                        "result": "failure",
                        "contradicts": "OBS-20260714-99",
                        "interpretation": "해석",
                    },
                ),
                PolicyError,
                r"^DANGLING_SOURCE",
            ),
        )
        for request, error_type, token in cases:
            with self.subTest(title=request.title), self.assertRaisesRegex(
                error_type, token
            ):
                capture(self.workspace, request)

        self.assertEqual(list(record_directory.iterdir()), [observation])
        self.assertEqual(
            list((record_directory.parent / "experiment").iterdir()),
            [],
        )

    def test_git_root_without_didimlog_scaffold_is_rejected_without_creating_knowledge(self):
        workspace = self.root / "unconfigured"
        workspace.mkdir()
        self._git("init", "-q", workspace=workspace)
        sentinel = workspace / "user.txt"
        sentinel.write_bytes(b"user data\n")

        with self.assertRaises(DidimError) as raised:
            capture(workspace, _observation_request("쓰이면 안 됨"))

        self.assertEqual(raised.exception.exit_code, EXIT_POLICY)
        self.assertEqual(raised.exception.token, "PROJECT_SCAFFOLD_MISSING")
        self.assertIn("didim setup", raised.exception.help_text)
        self.assertFalse((workspace / "knowledge").exists())
        self.assertEqual(sentinel.read_bytes(), b"user data\n")

    def test_atomic_collision_retries_with_the_next_id(self):
        real_open = os.open
        collided = []
        competitor = serialize_record(
            "OBS-20260714-01",
            "observation",
            "동시 작성자",
            "project",
            DATE,
            [],
            [],
            {"body": "## Observation\n\n경쟁 프로세스 원문\n"},
        ).encode("utf-8")

        def collide_once(path, flags, *args, **kwargs):
            if (
                not collided
                and flags & os.O_EXCL
                and pathlib.Path(path).name == "OBS-20260714-01.md"
            ):
                collided.append(pathlib.Path(path).name)
                descriptor = real_open(path, flags, *args, **kwargs)
                try:
                    os.write(descriptor, competitor)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                raise FileExistsError(errno.EEXIST, "simulated collision", path)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(record_module.os, "open", side_effect=collide_once):
            path = capture(self.workspace, _observation_request("재시도 원문"))

        collision_path = self._record_path("observation", "OBS-20260714-01")
        self.assertEqual(collided, ["OBS-20260714-01.md"])
        self.assertEqual(path.name, "OBS-20260714-02.md")
        self.assertIn("경쟁 프로세스 원문", collision_path.read_text(encoding="utf-8"))
        self.assertIn("재시도 원문", path.read_text(encoding="utf-8"))

    def test_collision_retry_cap_stops_without_deleting_or_overwriting_data(self):
        real_open = os.open
        attempts = []
        sentinel = self.workspace / "knowledge" / "records" / "observation" / "mine.txt"
        sentinel.write_bytes(b"user-owned\n")

        def always_collide(path, flags, *args, **kwargs):
            candidate = pathlib.Path(path)
            if flags & os.O_EXCL and candidate.suffix == ".md":
                attempts.append(candidate.name)
                record_id = candidate.stem
                competitor = serialize_record(
                    record_id,
                    "observation",
                    "동시 작성자",
                    "project",
                    DATE,
                    [],
                    [],
                    {"body": f"## Observation\n\n경쟁 원문 {record_id}\n"},
                ).encode("utf-8")
                descriptor = real_open(path, flags, *args, **kwargs)
                try:
                    os.write(descriptor, competitor)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                raise FileExistsError(errno.EEXIST, "simulated collision", path)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(record_module.os, "open", side_effect=always_collide):
            with self.assertRaises(PolicyError):
                capture(
                    self.workspace,
                    _observation_request("생성되면 안 됨"),
                    max_id_retries=2,
                )

        self.assertEqual(
            attempts,
            ["OBS-20260714-01.md", "OBS-20260714-02.md"],
        )
        self.assertEqual(sentinel.read_bytes(), b"user-owned\n")
        self.assertEqual(
            sorted(path.name for path in sentinel.parent.iterdir()),
            ["OBS-20260714-01.md", "OBS-20260714-02.md", "mine.txt"],
        )
        for record_id in ("OBS-20260714-01", "OBS-20260714-02"):
            self.assertIn(
                f"경쟁 원문 {record_id}",
                self._record_path("observation", record_id).read_text(encoding="utf-8"),
            )

    def test_two_processes_receive_distinct_ids_and_preserve_both_bodies(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        start = context.Event()
        results = context.Queue()
        bodies = ("첫 프로세스 원문", "둘째 프로세스 원문")
        processes = [
            context.Process(
                target=_capture_in_process,
                args=(str(self.workspace), body, ready, start, results),
            )
            for body in bodies
        ]
        for process in processes:
            process.start()
        for _ in processes:
            self.assertTrue(ready.get(timeout=10))
        start.set()
        for process in processes:
            process.join(15)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
            self.assertEqual(process.exitcode, 0)

        outcomes = [results.get(timeout=5) for _ in processes]
        self.assertEqual([outcome[0] for outcome in outcomes], ["ok", "ok"])
        self.assertEqual(
            {outcome[1] for outcome in outcomes},
            {"OBS-20260714-01.md", "OBS-20260714-02.md"},
        )
        documents = [
            path.read_text(encoding="utf-8")
            for path in sorted(
                (self.workspace / "knowledge" / "records" / "observation").glob(
                    "OBS-*.md"
                )
            )
        ]
        self.assertEqual(len(documents), 2)
        for body in bodies:
            self.assertEqual(sum(body in document for document in documents), 1)

    def test_index_callback_failure_preserves_record_and_reports_stable_recovery_line(self):
        try:
            index_module = importlib.import_module("didimlog.project.index")
        except ModuleNotFoundError:
            index_module = types.ModuleType("didimlog.project.index")
            real_index_module = False
        else:
            real_index_module = True

        def fail_index(_workspace):
            raise RuntimeError("forced project index failure")

        errors = io.StringIO()
        with contextlib.ExitStack() as stack:
            if real_index_module:
                stack.enter_context(
                    mock.patch.object(index_module, "write_index", side_effect=fail_index)
                )
            else:
                index_module.write_index = fail_index
                stack.enter_context(
                    mock.patch.dict(
                        sys.modules,
                        {"didimlog.project.index": index_module},
                    )
                )
            stack.enter_context(
                mock.patch.object(
                    project_package,
                    "index",
                    index_module,
                    create=True,
                )
            )
            stack.enter_context(contextlib.redirect_stderr(errors))
            path = capture(self.workspace, _observation_request("색인이 실패해도 남는 원문"))

        self.assertEqual(path.name, "OBS-20260714-01.md")
        self.assertIn("색인이 실패해도 남는 원문", path.read_text(encoding="utf-8"))
        self.assertEqual(
            errors.getvalue(),
            "PROJECT_INDEX_STALE: run didim index\n",
        )
        self.assertEqual(validate_record_tree(self.workspace)[0]["id"], path.stem)


if __name__ == "__main__":
    unittest.main()
