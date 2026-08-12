"""Create one canonical project record with a service-owned ID and path."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import secrets
import stat
import subprocess
import sys
import unicodedata

from didimlog.errors import DidimError, EXIT_GIT, EXIT_POLICY
from didimlog.locking import acquire_directory_lock, path_lock
from didimlog.file_io import (
    UnsafePathError,
    open_child_directory,
    open_directory_path,
    read_regular_file_at,
    read_regular_file_at_with_stat,
    read_regular_file_beneath,
)
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
from .scaffold import ScaffoldPlan, _apply_scaffold_updates, plan_scaffold
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


@dataclass(frozen=True)
class _PinnedWorkspacePath:
    path: Path
    workspace_descriptor: int

    def __fspath__(self) -> str:
        return os.fspath(self.path)


@dataclass
class _PinnedRecordTarget:
    path: Path
    directory_descriptor: int
    publication: os.stat_result | None = None
    publication_descriptor: int | None = None


def _project_error(token: str, help_text: str) -> DidimError:
    return DidimError(token, exit_code=EXIT_POLICY, help_text=help_text)


def _scaffold_update_error(error: DidimError) -> DidimError:
    token = error.token.partition(" ")[0]
    if token == error.token:
        return error
    if isinstance(error, PolicyError):
        return PolicyError(token)
    return DidimError(
        token,
        exit_code=error.exit_code,
        help_text=error.help_text,
    )


def _is_missing_scaffold_failure(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, FileNotFoundError):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


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


def _require_scaffold(workspace: Path) -> ScaffoldPlan:
    try:
        expected = plan_scaffold(workspace)
        with path_lock(workspace / "knowledge", shared=True):
            updates = {
                path: (original, intended)
                for path, original, intended in expected.updates
            }
            for directory in expected.directories:
                entry = directory.lstat()
                if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                    raise OSError("unsafe scaffold directory")
            for path, content in expected.files:
                accepted = updates.get(path, (content,))
                relative = path.relative_to(workspace)
                actual = read_regular_file_beneath(
                    workspace,
                    relative,
                    max(len(value) for value in accepted),
                )
                if actual not in accepted:
                    raise OSError("stale scaffold file")
        return expected
    except (DidimError, OSError, UnsafePathError, ValueError):
        raise _project_error(
            "PROJECT_SCAFFOLD_MISSING",
            "먼저 didim setup을 실행해 프로젝트 지식 저장소를 준비하세요.",
        ) from None


def _workspace_replaced_error() -> DidimError:
    return _project_error(
        "PROJECT_SCAFFOLD_MISSING",
        "프로젝트 경로가 바뀌지 않은 상태에서 다시 실행하세요.",
    )


def _require_pinned_workspace(
    workspace: Path,
    workspace_descriptor: int,
    knowledge_descriptor: int,
) -> None:
    try:
        linked_workspace = workspace.lstat()
        opened_workspace = os.fstat(workspace_descriptor)
        linked_knowledge = os.stat(
            "knowledge",
            dir_fd=workspace_descriptor,
            follow_symlinks=False,
        )
        opened_knowledge = os.fstat(knowledge_descriptor)
    except OSError as error:
        raise _workspace_replaced_error() from error

    if (
        stat.S_ISLNK(linked_workspace.st_mode)
        or not stat.S_ISDIR(linked_workspace.st_mode)
        or not stat.S_ISDIR(opened_workspace.st_mode)
        or (linked_workspace.st_dev, linked_workspace.st_ino)
        != (opened_workspace.st_dev, opened_workspace.st_ino)
        or stat.S_ISLNK(linked_knowledge.st_mode)
        or not stat.S_ISDIR(linked_knowledge.st_mode)
        or not stat.S_ISDIR(opened_knowledge.st_mode)
        or (linked_knowledge.st_dev, linked_knowledge.st_ino)
        != (opened_knowledge.st_dev, opened_knowledge.st_ino)
    ):
        raise _workspace_replaced_error()


def _require_pinned_record_directories(
    knowledge_descriptor: int,
    records_descriptor: int,
    record_type: str,
    record_type_descriptor: int,
) -> None:
    try:
        linked_records = os.stat(
            "records",
            dir_fd=knowledge_descriptor,
            follow_symlinks=False,
        )
        opened_records = os.fstat(records_descriptor)
        linked_record_type = os.stat(
            record_type,
            dir_fd=records_descriptor,
            follow_symlinks=False,
        )
        opened_record_type = os.fstat(record_type_descriptor)
    except OSError as error:
        raise _workspace_replaced_error() from error

    if (
        stat.S_ISLNK(linked_records.st_mode)
        or not stat.S_ISDIR(linked_records.st_mode)
        or not stat.S_ISDIR(opened_records.st_mode)
        or (linked_records.st_dev, linked_records.st_ino)
        != (opened_records.st_dev, opened_records.st_ino)
        or stat.S_ISLNK(linked_record_type.st_mode)
        or not stat.S_ISDIR(linked_record_type.st_mode)
        or not stat.S_ISDIR(opened_record_type.st_mode)
        or (linked_record_type.st_dev, linked_record_type.st_ino)
        != (opened_record_type.st_dev, opened_record_type.st_ino)
    ):
        raise _workspace_replaced_error()


def _open_scaffold_directory_at(
    knowledge_descriptor: int,
    relative: Path,
) -> int:
    descriptor = os.dup(knowledge_descriptor)
    try:
        for component in relative.parts:
            child = open_child_directory(descriptor, component)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_scaffold_at(
    workspace: Path,
    scaffold: ScaffoldPlan,
    knowledge_descriptor: int,
) -> None:
    knowledge = workspace / "knowledge"
    try:
        for directory in scaffold.directories:
            relative = directory.relative_to(knowledge)
            descriptor = _open_scaffold_directory_at(
                knowledge_descriptor,
                relative,
            )
            os.close(descriptor)

        for path, expected in scaffold.files:
            relative = path.relative_to(knowledge)
            parent_descriptor = _open_scaffold_directory_at(
                knowledge_descriptor,
                relative.parent,
            )
            try:
                actual = read_regular_file_at(
                    parent_descriptor,
                    relative.name,
                    len(expected),
                )
            finally:
                os.close(parent_descriptor)
            if actual != expected:
                raise UnsafePathError("stale scaffold file")
    except (OSError, UnsafePathError, ValueError):
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
    workspace_descriptor: int,
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
                workspace_descriptor=workspace_descriptor,
            )
        else:
            verify_artifact_git(
                workspace,
                candidate["artifact_path"],
                candidate["artifact_git"],
                candidate["id"],
                workspace_descriptor=workspace_descriptor,
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


def _write_create_only(
    path: Path | _PinnedRecordTarget,
    data: bytes,
) -> os.stat_result:
    target = path.path if isinstance(path, _PinnedRecordTarget) else path
    directory = target.parent
    directory_descriptor: int | None = None
    try:
        if isinstance(path, _PinnedRecordTarget):
            directory_descriptor = os.dup(path.directory_descriptor)
            opened = os.fstat(directory_descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                raise OSError("record parent is not a directory")
        else:
            linked = directory.lstat()
            directory_descriptor = os.open(
                directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(directory_descriptor)
            if (
                stat.S_ISLNK(linked.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or opened.st_dev != linked.st_dev
                or opened.st_ino != linked.st_ino
            ):
                raise OSError("record parent changed")
    except OSError as error:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        raise PolicyError("PATH_ESCAPE {}".format(directory)) from error

    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    published = False
    try:
        for _ in range(32):
            candidate = ".didim-record-{}.tmp".format(secrets.token_hex(12))
            try:
                temporary_descriptor = os.open(
                    candidate,
                    os.O_RDWR
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o644,
                    dir_fd=directory_descriptor,
                )
                temporary_name = candidate
                break
            except FileExistsError:
                continue
        if temporary_descriptor is None or temporary_name is None:
            raise OSError("could not allocate record temporary file")

        remaining = memoryview(data)
        while remaining:
            written = os.write(temporary_descriptor, remaining)
            if written <= 0:
                raise OSError("short write")
            remaining = remaining[written:]
        os.fsync(temporary_descriptor)

        os.link(
            temporary_name,
            target.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        published = True
        os.fsync(directory_descriptor)
        os.unlink(temporary_name, dir_fd=directory_descriptor)
        temporary_name = None
        os.fsync(directory_descriptor)

        published_data, publication = read_regular_file_at_with_stat(
            directory_descriptor,
            target.name,
            len(data),
        )
        owned = os.fstat(temporary_descriptor)
        if (
            published_data != data
            or not _same_publication(owned, publication)
        ):
            raise OSError("record publication changed")

        if isinstance(path, _PinnedRecordTarget):
            path.publication = publication
            path.publication_descriptor = temporary_descriptor
            temporary_descriptor = None
        return publication
    except BaseException:
        raise
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_descriptor)
            except OSError:
                pass
        os.close(directory_descriptor)


def _same_publication(
    current: os.stat_result,
    publication: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(current.st_mode)
        and current.st_dev == publication.st_dev
        and current.st_ino == publication.st_ino
        and current.st_mode == publication.st_mode
        and current.st_uid == publication.st_uid
        and current.st_gid == publication.st_gid
        and current.st_nlink == publication.st_nlink
        and current.st_size == publication.st_size
        and current.st_mtime_ns == publication.st_mtime_ns
        and current.st_ctime_ns == publication.st_ctime_ns
    )


def _require_record_rollback(
    target: _PinnedRecordTarget,
    publication: os.stat_result,
    intended: bytes,
) -> None:
    """Fail closed without deleting a record another process may still edit."""
    raise _workspace_replaced_error()


def _refresh_index(
    workspace: Path,
    workspace_descriptor: int,
    knowledge_descriptor: int,
) -> None:
    try:
        from . import index

        index._write_index_at(
            workspace,
            workspace_descriptor,
            knowledge_descriptor,
        )
    except Exception:
        print("PROJECT_INDEX_STALE: run didim index", file=sys.stderr)


def _capture_locked(
    root: Path,
    workspace_descriptor: int,
    knowledge_descriptor: int,
    request: CaptureRequest,
    max_id_retries: int,
) -> Path:
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

    records_descriptor: int | None = None
    record_type_descriptor: int | None = None
    try:
        records_descriptor = open_child_directory(
            knowledge_descriptor,
            "records",
        )
        record_type_descriptor = open_child_directory(
            records_descriptor,
            record_type,
        )
    except UnsafePathError as error:
        if record_type_descriptor is not None:
            os.close(record_type_descriptor)
        if records_descriptor is not None:
            os.close(records_descriptor)
        raise PolicyError(
            "PATH_ESCAPE {}".format(
                root / "knowledge" / "records" / record_type
            )
        ) from error

    pinned_workspace = _PinnedWorkspacePath(root, workspace_descriptor)
    try:
        for _ in range(max_id_retries):
            records = validate_record_tree(pinned_workspace)
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
            _validate_candidate(
                root,
                workspace_descriptor,
                candidate,
                records,
                contradicts,
            )
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
            _require_pinned_workspace(
                root,
                workspace_descriptor,
                knowledge_descriptor,
            )
            _require_pinned_record_directories(
                knowledge_descriptor,
                records_descriptor,
                record_type,
                record_type_descriptor,
            )
            target = _PinnedRecordTarget(path, record_type_descriptor)
            try:
                try:
                    publication = _write_create_only(target, document)
                except FileExistsError:
                    continue
                if target.publication is not None:
                    publication = target.publication
                if not isinstance(publication, os.stat_result):
                    raise OSError("record publication identity unavailable")

                try:
                    _require_pinned_workspace(
                        root,
                        workspace_descriptor,
                        knowledge_descriptor,
                    )
                    _require_pinned_record_directories(
                        knowledge_descriptor,
                        records_descriptor,
                        record_type,
                        record_type_descriptor,
                    )
                except DidimError:
                    try:
                        _require_record_rollback(
                            target,
                            publication,
                            document,
                        )
                    finally:
                        _refresh_index(
                            root,
                            workspace_descriptor,
                            knowledge_descriptor,
                        )
                    raise

                _refresh_index(
                    root,
                    workspace_descriptor,
                    knowledge_descriptor,
                )
                try:
                    _require_pinned_workspace(
                        root,
                        workspace_descriptor,
                        knowledge_descriptor,
                    )
                    _require_pinned_record_directories(
                        knowledge_descriptor,
                        records_descriptor,
                        record_type,
                        record_type_descriptor,
                    )
                except DidimError:
                    try:
                        _require_record_rollback(
                            target,
                            publication,
                            document,
                        )
                    finally:
                        _refresh_index(
                            root,
                            workspace_descriptor,
                            knowledge_descriptor,
                        )
                    raise
                return path
            finally:
                if target.publication_descriptor is not None:
                    publication_descriptor = target.publication_descriptor
                    target.publication_descriptor = None
                    os.close(publication_descriptor)
    finally:
        os.close(record_type_descriptor)
        os.close(records_descriptor)

    raise PolicyError("ID_ALLOCATION_RETRY_EXHAUSTED")


def capture(
    workspace: Path,
    request: CaptureRequest,
    *,
    max_id_retries: int = 8,
) -> Path:
    """Validate and create one record from one exclusive project snapshot."""
    if not isinstance(request, CaptureRequest):
        raise SchemaError("INVALID_CAPTURE_REQUEST")
    if not isinstance(max_id_retries, int) or isinstance(max_id_retries, bool):
        raise SchemaError("INVALID_ID_RETRIES")
    if max_id_retries < 1:
        raise SchemaError("INVALID_ID_RETRIES")

    root = _require_git_root(Path(workspace))
    workspace_descriptor: int | None = None
    knowledge_descriptor: int | None = None
    try:
        workspace_descriptor = open_directory_path(root)
        knowledge_descriptor = open_child_directory(
            workspace_descriptor,
            "knowledge",
        )
    except (OSError, UnsafePathError):
        if workspace_descriptor is not None:
            os.close(workspace_descriptor)
        raise _project_error(
            "PROJECT_SCAFFOLD_MISSING",
            "먼저 didim setup을 실행해 프로젝트 지식 저장소를 준비하세요.",
        ) from None

    knowledge_lock: int | None = None
    try:
        scaffold = _require_scaffold(root)
        _require_pinned_workspace(
            root,
            workspace_descriptor,
            knowledge_descriptor,
        )
        # The conditional writer locks knowledge itself; migrate before the
        # snapshot lock so this process never acquires that flock twice.
        if scaffold.updates:
            try:
                _apply_scaffold_updates(scaffold, knowledge_descriptor)
            except DidimError as error:
                if _is_missing_scaffold_failure(error):
                    raise _project_error(
                        "PROJECT_SCAFFOLD_MISSING",
                        "먼저 didim setup을 실행해 프로젝트 지식 저장소를 준비하세요.",
                    ) from None
                stable_error = _scaffold_update_error(error)
                if stable_error is error:
                    raise
                raise stable_error from error
            except (OSError, UnsafePathError, ValueError) as error:
                if _is_missing_scaffold_failure(error):
                    raise _project_error(
                        "PROJECT_SCAFFOLD_MISSING",
                        "먼저 didim setup을 실행해 프로젝트 지식 저장소를 준비하세요.",
                    ) from None
                raise PolicyError("SCAFFOLD_CONFLICT") from error

        _require_pinned_workspace(
            root,
            workspace_descriptor,
            knowledge_descriptor,
        )
        knowledge_lock = acquire_directory_lock(knowledge_descriptor)
        _require_pinned_workspace(
            root,
            workspace_descriptor,
            knowledge_descriptor,
        )
        _require_scaffold_at(root, scaffold, knowledge_descriptor)
        return _capture_locked(
            root,
            workspace_descriptor,
            knowledge_descriptor,
            request,
            max_id_retries,
        )
    finally:
        if knowledge_lock is not None:
            os.close(knowledge_lock)
        os.close(knowledge_descriptor)
        os.close(workspace_descriptor)
