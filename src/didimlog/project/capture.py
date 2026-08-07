"""Create one canonical project record with a service-owned ID and path."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import unicodedata

from didimlog.errors import DidimError, EXIT_GIT, EXIT_POLICY
from .artifacts import (
    check_artifact_path_format,
    verify_artifact_git,
    verify_artifact_local,
)
from .record import (
    CONTRADICTS_PREFIXES,
    SOURCE_PREFIXES,
    TYPE_BY_PREFIX,
    PolicyError,
    SchemaError,
    _check_sorted_unique,
    _parse_contradicts,
    _validate_stored_sources,
    _validate_tag,
    parse_date,
    parse_scope,
    parse_title,
    serialize_record,
    validate_body,
    validate_frontmatter,
)
from .scaffold import plan_scaffold
from .tree import resolve_reference, validate_record_tree


_PREFIX_BY_TYPE = {value: key for key, value in TYPE_BY_PREFIX.items()}
_TYPE_FIELDS = {
    "observation": frozenset({"body"}),
    "experiment": frozenset(
        {"hypothesis", "method", "result", "contradicts", "interpretation"}
    ),
    "evidence": frozenset(
        {
            "artifact",
            "origin",
            "collection",
            "artifact_sha256",
            "artifact_git",
        }
    ),
}
_GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class CaptureRequest:
    type: str
    date: str
    scope: str
    title: str
    tags: tuple[str, ...]
    sources: tuple[str, ...]
    fields: dict[str, str]


def _project_error(token: str, help_text: str) -> DidimError:
    return DidimError(token, exit_code=EXIT_POLICY, help_text=help_text)


def _require_git_root(workspace: Path) -> Path:
    root = Path(os.path.abspath(workspace))
    git = shutil.which("git")
    if git is None:
        raise DidimError("GIT_UNAVAILABLE", exit_code=EXIT_GIT)
    try:
        result = subprocess.run(
            [git, "-C", os.fspath(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            text=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise DidimError("GIT_UNAVAILABLE", exit_code=EXIT_GIT) from error
    if result.returncode != 0:
        raise DidimError("GIT_NOT_A_REPOSITORY", exit_code=EXIT_GIT)
    discovered = Path(result.stdout.strip())
    try:
        is_root = discovered.is_absolute() and os.path.samefile(root, discovered)
    except OSError:
        is_root = False
    if not is_root:
        raise _project_error(
            "PROJECT_ROOT_REQUIRED",
            "Git 저장소 최상위에서 다시 실행하세요.",
        )
    return root


def _require_scaffold(workspace: Path) -> None:
    try:
        expected = plan_scaffold(workspace)
        for directory in expected.directories:
            entry = directory.lstat()
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                raise OSError("unsafe scaffold directory")
        for path, content in expected.files:
            entry = path.lstat()
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
                raise OSError("unsafe scaffold file")
            if path.read_bytes() != content:
                raise OSError("stale scaffold file")
    except (DidimError, OSError):
        raise _project_error(
            "PROJECT_SCAFFOLD_MISSING",
            "먼저 didim setup을 실행해 프로젝트 지식 저장소를 준비하세요.",
        ) from None


def _canonical_tags(values) -> list[str]:
    if not isinstance(values, (tuple, list)):
        raise SchemaError("INVALID_TAGS")
    canonical = []
    for value in values:
        if not isinstance(value, str):
            raise SchemaError("INVALID_TAG {}".format(value))
        normalized = unicodedata.normalize("NFKC", value)
        normalized = "".join(
            character.lower() if "A" <= character <= "Z" else character
            for character in normalized
        )
        canonical.append(_validate_tag(normalized))
    _check_sorted_unique(canonical, "tags")
    return canonical


def _sources(values) -> list[str]:
    if not isinstance(values, (tuple, list)):
        raise SchemaError("INVALID_SOURCES")
    return _validate_stored_sources(list(values))


def _clean_section(value, name: str) -> str:
    if not isinstance(value, str):
        raise SchemaError("MISSING_SECTION {}".format(name))
    if "\r" in value or "\x00" in value:
        raise SchemaError("INVALID_SECTION {}".format(name))
    cleaned = value.rstrip("\n")
    if cleaned.strip() == "":
        raise SchemaError("MISSING_SECTION {}".format(name))
    if any(line.startswith("## ") for line in cleaned.split("\n")):
        raise SchemaError("INVALID_SECTION {}".format(name))
    return cleaned


def _require_fields(record_type: str, fields) -> dict[str, str]:
    if not isinstance(fields, dict):
        raise SchemaError("INVALID_FIELDS")
    allowed = _TYPE_FIELDS.get(record_type)
    if allowed is None:
        raise SchemaError("FUTURE_TYPE {}".format(record_type))
    keys = set(fields)
    if record_type == "evidence":
        required = {"artifact", "origin", "collection"}
        bindings = keys & {"artifact_sha256", "artifact_git"}
        if not required <= keys or len(bindings) != 1 or not keys <= allowed:
            raise SchemaError("INVALID_FIELDS evidence")
    elif keys != allowed:
        raise SchemaError("INVALID_FIELDS {}".format(record_type))
    return fields


def _build_type_fields(record_type: str, fields: dict[str, str], record_id: str):
    fields = _require_fields(record_type, fields)
    if record_type == "observation":
        body = "## Observation\n\n{}\n".format(
            _clean_section(fields["body"], "body")
        )
        return {"body": body}, []
    if record_type == "experiment":
        hypothesis = _clean_section(fields["hypothesis"], "hypothesis")
        method = _clean_section(fields["method"], "method")
        result = _clean_section(fields["result"], "result")
        interpretation = _clean_section(
            fields["interpretation"],
            "interpretation",
        )
        contradicts = _parse_contradicts(fields["contradicts"])
        contradiction_line = (
            "Contradicts: none"
            if not contradicts
            else "Contradicts: " + ", ".join(contradicts)
        )
        body = (
            "## Hypothesis\n\n{}\n\n"
            "## Method\n\n{}\n\n"
            "## Result\n\n{}\n\n"
            "## Interpretation\n\n{}\n\n{}\n"
        ).format(hypothesis, method, result, contradiction_line, interpretation)
        return {"body": body}, contradicts

    artifact_path = check_artifact_path_format(fields["artifact"], record_id)
    origin = _clean_section(fields["origin"], "origin")
    collection = _clean_section(fields["collection"], "collection")
    body = (
        "## Artifact\n\n{}\n\n"
        "## Origin\n\n{}\n\n"
        "## Collection\n\n{}\n"
    ).format(artifact_path, origin, collection)
    if "artifact_sha256" in fields:
        type_fields = {
            "body": body,
            "artifact_path": artifact_path,
            "mode": "local",
            "artifact_sha256": fields["artifact_sha256"],
        }
    else:
        type_fields = {
            "body": body,
            "artifact_path": artifact_path,
            "mode": "git",
            "artifact_git": fields["artifact_git"],
        }
    return type_fields, []


def _candidate_record(
    record_id: str,
    request: CaptureRequest,
    tags: list[str],
    sources: list[str],
    type_fields,
):
    frontmatter = {
        "schema_version": 1,
        "id": record_id,
        "type": request.type,
        "title": request.title,
        "status": "draft",
        "scope": request.scope,
        "created": request.date,
        "updated": request.date,
        "version": 1,
        "tags": tags,
        "sources": sources,
    }
    if request.type == "evidence":
        frontmatter["artifact_path"] = type_fields["artifact_path"]
        if type_fields["mode"] == "local":
            frontmatter["artifact_sha256"] = type_fields["artifact_sha256"]
        else:
            frontmatter["artifact_git"] = type_fields["artifact_git"]
    record = validate_frontmatter(frontmatter)
    validate_body(record, type_fields["body"])
    return record


def _validate_candidate(
    workspace: Path,
    candidate,
    records,
    contradicts: list[str],
) -> None:
    records_by_id = {record["id"]: record for record in records}
    for source_id in candidate["sources"]:
        resolve_reference(
            source_id,
            candidate["id"],
            SOURCE_PREFIXES,
            records_by_id,
        )
    for observation_id in contradicts:
        resolve_reference(
            observation_id,
            candidate["id"],
            CONTRADICTS_PREFIXES,
            records_by_id,
        )
    if candidate["type"] == "evidence":
        if candidate["artifact_mode"] == "local":
            verify_artifact_local(
                workspace,
                candidate["artifact_path"],
                candidate["artifact_sha256"],
                candidate["id"],
            )
        else:
            verify_artifact_git(
                workspace,
                candidate["artifact_path"],
                candidate["artifact_git"],
                candidate["id"],
            )


def _next_id(records, prefix: str, compact_date: str) -> str:
    stem = "{}-{}-".format(prefix, compact_date)
    suffixes = [
        int(record["id"][-2:])
        for record in records
        if record["id"].startswith(stem)
    ]
    next_suffix = max(suffixes, default=0) + 1
    if next_suffix > 99:
        raise PolicyError("ID_SPACE_EXHAUSTED {}".format(stem[:-1]))
    return "{}{:02d}".format(stem, next_suffix)


def _write_create_only(path: Path, data: bytes) -> None:
    directory = path.parent
    try:
        linked = directory.lstat()
        directory_descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise PolicyError("PATH_ESCAPE {}".format(directory)) from error
    try:
        opened = os.fstat(directory_descriptor)
        if (
            stat.S_ISLNK(linked.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
        ):
            raise PolicyError("PATH_ESCAPE {}".format(directory))
        descriptor = os.open(
            path.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o644,
            dir_fd=directory_descriptor,
        )
        try:
            remaining = memoryview(data)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("short write")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _refresh_index(workspace: Path) -> None:
    try:
        from . import index

        index.write_index(workspace)
    except Exception:
        print("PROJECT_INDEX_STALE: run didim index", file=sys.stderr)


def capture(
    workspace: Path,
    request: CaptureRequest,
    *,
    max_id_retries: int = 8,
) -> Path:
    """Validate and create one record without exposing its ID or output path."""
    if not isinstance(request, CaptureRequest):
        raise SchemaError("INVALID_CAPTURE_REQUEST")
    if not isinstance(max_id_retries, int) or isinstance(max_id_retries, bool):
        raise SchemaError("INVALID_ID_RETRIES")
    if max_id_retries < 1:
        raise SchemaError("INVALID_ID_RETRIES")

    root = _require_git_root(Path(workspace))
    _require_scaffold(root)
    record_type = request.type
    if record_type not in _PREFIX_BY_TYPE:
        raise SchemaError("FUTURE_TYPE {}".format(record_type))
    parse_date(request.date)
    parse_scope(request.scope)
    parse_title(request.title)
    tags = _canonical_tags(request.tags)
    sources = _sources(request.sources)
    prefix = _PREFIX_BY_TYPE[record_type]
    compact_date = request.date.replace("-", "")

    for _ in range(max_id_retries):
        records = validate_record_tree(root)
        record_id = _next_id(records, prefix, compact_date)
        type_fields, contradicts = _build_type_fields(
            record_type,
            request.fields,
            record_id,
        )
        candidate = _candidate_record(
            record_id,
            request,
            tags,
            sources,
            type_fields,
        )
        _validate_candidate(root, candidate, records, contradicts)
        document = serialize_record(
            record_id,
            record_type,
            request.title,
            request.scope,
            request.date,
            tags,
            sources,
            type_fields,
        ).encode("utf-8")
        path = (
            root
            / "knowledge"
            / "records"
            / record_type
            / "{}.md".format(record_id)
        )
        try:
            _write_create_only(path, document)
        except FileExistsError:
            continue
        _refresh_index(root)
        return path

    raise PolicyError("ID_ALLOCATION_RETRY_EXHAUSTED")
