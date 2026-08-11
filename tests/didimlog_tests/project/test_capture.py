import contextlib
import dataclasses
from concurrent.futures import ThreadPoolExecutor
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
import stat
import sys
import threading
import tempfile
import tomllib
import types
import unittest
from unittest import mock

import didimlog.project.record as record_module
import didimlog.project as project_package
from didimlog.project import index as project_index_module
import didimlog.project.capture as capture_module

from didimlog.errors import DidimError, EXIT_POLICY
from didimlog.project.capture import CaptureRequest, _write_create_only, capture
from didimlog.project.record import PolicyError, SchemaError, serialize_record
from didimlog.project.scaffold import apply_scaffold, plan_scaffold
from didimlog.project.tree import validate_record_tree


DATE = "2026-07-14"


def _legacy_readme(current):
    replacements = (
        (
            (
                "- ID 형식은 `PREFIX-YYYYMMDD-NN` (예: `OBS-20260714-01`). "
                "`didim add`는\n"
                "  `--date YYYY-MM-DD`와 기존 record를 기준으로 ID의 날짜와 두 자리 "
                "순번을 자동 할당한다."
            ),
            (
                "- ID 형식은 `PREFIX-YYYYMMDD-NN` (예: `OBS-20260714-01`). "
                "날짜와 2자리 순번을\n"
                "  사람이 직접 지정하며 자동 추측하지 않는다."
            ),
        ),
        (
            (
                "`didim add experiment`는 JSON stdin의 `contradicts` 필드로 모순 ID를 "
                "입력한다. 값은\n"
                '모순이 없으면 `"none"`, 있으면 `"<ID>, <ID>, ..."`인 문자열이다. '
                "이 필드는 필수이며\n"
                "기본값도 추론도 없다."
            ),
            (
                "`didim add experiment`는 `--contradicts`가 필수이며 기본값도 "
                "추론도 없다."
            ),
        ),
    )
    text = current.decode("utf-8")
    for current_paragraph, legacy_paragraph in replacements:
        assert text.count(current_paragraph) == 1
        text = text.replace(current_paragraph, legacy_paragraph)
    legacy = text.encode("utf-8")
    assert len(legacy) == 16_336
    assert (
        hashlib.sha256(legacy).hexdigest()
        == "6347d06afaab04f94c9f409717e0539add7252d000e5b0d51ea68d00036b0961"
    )
    return legacy


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


def _capture_in_process(
    workspace,
    body,
    ready,
    start,
    results,
    pause_after_scaffold_plan=False,
):
    if pause_after_scaffold_plan:
        real_require_scaffold = capture_module._require_scaffold
        paused = False

        def require_scaffold_then_pause(scaffold_workspace):
            nonlocal paused
            plan = real_require_scaffold(scaffold_workspace)
            if not paused:
                paused = True
                ready.put(plan.updates)
                if not start.wait(10):
                    raise RuntimeError("start timeout")
            return plan

        capture_context = mock.patch.object(
            capture_module,
            "_require_scaffold",
            side_effect=require_scaffold_then_pause,
        )
    else:
        ready.put(True)
        if not start.wait(10):
            results.put(("error", "start timeout"))
            return
        capture_context = contextlib.nullcontext()

    try:
        with capture_context:
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
        artifact = self.workspace / "knowledge" / "raw" / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
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
                "artifact": f"knowledge/raw/{name}",
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
            "## Artifact\n\nknowledge/raw/local.txt\n\n"
            "## Origin\n\n격리 테스트 fixture\n\n"
            "## Collection\n\n테스트가 직접 생성\n",
        )
        self.assertEqual(experiment_meta["sources"], ["EVD-20260714-01"])
        self.assertEqual(evidence_meta["artifact_path"], "knowledge/raw/local.txt")
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
        artifact = self.workspace / "knowledge" / "raw" / "committed.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"committed evidence\n")
        self._git("add", "--", "knowledge/raw/committed.txt")
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
                    "artifact": "knowledge/raw/committed.txt",
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
            "## Artifact\n\nknowledge/raw/committed.txt\n\n"
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

    def test_capture_migrates_exact_legacy_readme_and_creates_record(self):
        readme = self.workspace / "knowledge/README.md"
        current = readme.read_bytes()
        readme.write_bytes(_legacy_readme(current))

        path = capture(self.workspace, _observation_request("이전 README 마이그레이션"))

        frontmatter, body = _read_record(path)
        self.assertEqual(
            path,
            self._record_path("observation", "OBS-20260714-01"),
        )
        self.assertEqual(frontmatter["id"], "OBS-20260714-01")
        self.assertEqual(body, "## Observation\n\n이전 README 마이그레이션\n")
        self.assertEqual(readme.read_bytes(), current)

    def test_capture_accepts_current_readme_when_legacy_plan_turns_stale(self):
        readme = self.workspace / "knowledge/README.md"
        current = readme.read_bytes()
        readme.write_bytes(_legacy_readme(current))

        def migrate_before_return(workspace):
            stale_plan = plan_scaffold(workspace)
            apply_scaffold(stale_plan)
            return stale_plan

        with mock.patch.object(
            capture_module,
            "plan_scaffold",
            side_effect=migrate_before_return,
        ):
            path = capture(
                self.workspace,
                _observation_request("계획 직후 README 마이그레이션"),
            )

        self.assertEqual(path.name, "OBS-20260714-01.md")
        self.assertIn(
            "계획 직후 README 마이그레이션",
            path.read_text(encoding="utf-8"),
        )
        self.assertEqual(readme.read_bytes(), current)


    def test_atomic_collision_retries_with_the_next_id(self):
        real_link = os.link
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

        def collide_once(source, destination, *args, **kwargs):
            if not collided and destination == "OBS-20260714-01.md":
                collided.append(destination)
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                    dir_fd=kwargs["dst_dir_fd"],
                )
                try:
                    os.write(descriptor, competitor)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                raise FileExistsError(
                    errno.EEXIST,
                    "simulated collision",
                    destination,
                )
            return real_link(source, destination, *args, **kwargs)

        with mock.patch.object(capture_module.os, "link", side_effect=collide_once):
            path = capture(self.workspace, _observation_request("재시도 원문"))

        collision_path = self._record_path("observation", "OBS-20260714-01")
        self.assertEqual(collided, ["OBS-20260714-01.md"])
        self.assertEqual(path.name, "OBS-20260714-02.md")
        self.assertIn("경쟁 프로세스 원문", collision_path.read_text(encoding="utf-8"))
        self.assertIn("재시도 원문", path.read_text(encoding="utf-8"))

    def test_collision_retry_cap_stops_without_deleting_or_overwriting_data(self):
        real_link = os.link
        attempts = []
        sentinel = self.workspace / "knowledge" / "records" / "observation" / "mine.txt"
        sentinel.write_bytes(b"user-owned\n")

        def always_collide(source, destination, *args, **kwargs):
            if destination.endswith(".md"):
                attempts.append(destination)
                record_id = pathlib.Path(destination).stem
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
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o644,
                    dir_fd=kwargs["dst_dir_fd"],
                )
                try:
                    os.write(descriptor, competitor)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                raise FileExistsError(
                    errno.EEXIST,
                    "simulated collision",
                    destination,
                )
            return real_link(source, destination, *args, **kwargs)

        with mock.patch.object(capture_module.os, "link", side_effect=always_collide):
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

    def test_partial_write_failure_never_exposes_a_final_record(self):
        target = self.workspace / "knowledge" / "records" / "observation" / "OBS-20260714-01.md"
        real_write = os.write
        writes = []

        def partial_then_fail(descriptor, data):
            if not writes:
                writes.append(True)
                return real_write(descriptor, data[:3])
            raise OSError("injected write failure")

        with mock.patch.object(
            capture_module.os,
            "write",
            side_effect=partial_then_fail,
        ):
            with self.assertRaises(OSError):
                _write_create_only(target, b"complete record bytes")

        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_link_failure_removes_the_complete_temporary_record(self):
        target = self.workspace / "knowledge" / "records" / "observation" / "OBS-20260714-01.md"
        with mock.patch.object(
            capture_module.os,
            "link",
            side_effect=OSError("injected link failure"),
        ):
            with self.assertRaises(OSError):
                _write_create_only(target, b"complete record bytes")

        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_directory_fsync_failure_rolls_back_the_published_record(self):
        target = self.workspace / "knowledge" / "records" / "observation" / "OBS-20260714-01.md"
        real_fsync = os.fsync
        failed = []

        def fail_first_directory_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not failed:
                failed.append(True)
                raise OSError("injected directory fsync failure")
            return real_fsync(descriptor)

        with mock.patch.object(
            capture_module.os,
            "fsync",
            side_effect=fail_first_directory_fsync,
        ):
            with self.assertRaises(OSError):
                _write_create_only(target, b"complete record bytes")

        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_two_publishers_expose_one_complete_winner(self):
        target = self.workspace / "knowledge" / "records" / "observation" / "OBS-20260714-01.md"
        payloads = (b"first complete record", b"second complete record")

        def publish(payload):
            try:
                _write_create_only(target, payload)
                return "created"
            except FileExistsError:
                return "collision"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(publish, payloads))

        self.assertCountEqual(outcomes, ["created", "collision"])
        self.assertIn(target.read_bytes(), payloads)
        self.assertEqual(
            [path.name for path in target.parent.iterdir()],
            [target.name],
        )

    def test_capture_cannot_be_overwritten_by_an_older_index_snapshot(self):
        project_index_module.write_index(self.workspace)
        real_build = project_index_module.build_index_bytes
        real_publish = capture_module._write_create_only
        snapshot_ready = threading.Event()
        capture_reached_publish = threading.Event()
        capture_finished = threading.Event()
        errors = []

        def pause_old_snapshot(workspace):
            data = real_build(workspace)
            if threading.current_thread().name == "old-index-writer":
                snapshot_ready.set()
                if capture_reached_publish.wait(0.3):
                    capture_finished.wait(5)
            return data

        def observe_capture_publish(path, data):
            capture_reached_publish.set()
            return real_publish(path, data)

        def write_old_index():
            try:
                project_index_module.write_index(self.workspace)
            except BaseException as error:
                errors.append(error)

        def publish_record():
            try:
                capture(self.workspace, _observation_request("최신 기록"))
            except BaseException as error:
                errors.append(error)
            finally:
                capture_finished.set()

        with (
            mock.patch.object(
                project_index_module,
                "build_index_bytes",
                side_effect=pause_old_snapshot,
            ),
            mock.patch.object(
                capture_module,
                "_write_create_only",
                side_effect=observe_capture_publish,
            ),
        ):
            old_writer = threading.Thread(
                target=write_old_index,
                name="old-index-writer",
            )
            old_writer.start()
            self.assertTrue(snapshot_ready.wait(5))
            record_writer = threading.Thread(target=publish_record)
            record_writer.start()
            old_writer.join(10)
            record_writer.join(10)

        self.assertFalse(old_writer.is_alive())
        self.assertFalse(record_writer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(project_index_module.check_index(self.workspace), 0)
        index_text = (
            self.workspace / "knowledge" / "index" / "INDEX.md"
        ).read_text(encoding="utf-8")
        self.assertIn("OBS-20260714-01", index_text)

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

    def test_two_processes_migrate_same_legacy_readme_and_preserve_both_captures(
        self,
    ):
        readme = self.workspace / "knowledge/README.md"
        current_readme = readme.read_bytes()
        readme.write_bytes(_legacy_readme(current_readme))
        context = multiprocessing.get_context("spawn")
        plan_ready = context.Queue()
        migrate = context.Event()
        results = context.Queue()
        bodies = ("첫 legacy migration 원문", "둘째 legacy migration 원문")
        processes = [
            context.Process(
                target=_capture_in_process,
                args=(
                    str(self.workspace),
                    body,
                    plan_ready,
                    migrate,
                    results,
                    True,
                ),
            )
            for body in bodies
        ]
        for process in processes:
            process.start()
        plans = [plan_ready.get(timeout=10) for _ in processes]
        migrate.set()
        self.assertEqual(plans[0], plans[1])
        self.assertEqual(len(plans[0]), 1)
        for process in processes:
            process.join(15)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
            self.assertEqual(process.exitcode, 0)

        outcomes = [results.get(timeout=5) for _ in processes]
        self.assertEqual(
            {outcome[0] for outcome in outcomes},
            {"ok"},
            outcomes,
        )
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
        self.assertEqual(readme.read_bytes(), current_readme)

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
                    mock.patch.object(
                        index_module,
                        "_write_index_locked",
                        side_effect=fail_index,
                    )
                )
            else:
                index_module._write_index_locked = fail_index
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
