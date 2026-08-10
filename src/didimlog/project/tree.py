"""Fail-closed project record tree, reference, and digest validation."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tomllib

from didimlog.file_io import (
    UnsafePathError,
    open_child_directory,
    read_regular_file_at,
)
from .artifacts import verify_artifact_git, verify_artifact_local
from .record import (
    CONTRADICTS_PREFIXES,
    RECORD_MAX_BYTES,
    RECORD_MAX_LINES,
    SOURCE_PREFIXES,
    TYPE_BY_PREFIX,
    PolicyError,
    SchemaError,
    _render_frontmatter,
    validate_body,
    validate_frontmatter,
)


_TYPE_DIRECTORIES = ("observation", "experiment", "evidence")


def _path_escape(path: Path) -> PolicyError:
    return PolicyError("PATH_ESCAPE {}".format(path))


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_child_directory(
    parent_descriptor: int,
    name: str,
    path: Path,
) -> int:
    try:
        return open_child_directory(parent_descriptor, name)
    except UnsafePathError as error:
        raise _path_escape(path) from error


def _open_directory_path(path: Path) -> tuple[int, Path]:
    absolute = Path(os.path.abspath(path))
    try:
        linked = absolute.lstat()
        descriptor = os.open(absolute, _directory_flags())
    except OSError as error:
        raise _path_escape(absolute) from error
    opened = os.fstat(descriptor)
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or opened.st_dev != linked.st_dev
        or opened.st_ino != linked.st_ino
    ):
        os.close(descriptor)
        raise _path_escape(absolute)
    return descriptor, absolute


def _read_regular_file_at(
    directory_descriptor: int,
    name: str,
    path: Path,
) -> bytes:
    try:
        return read_regular_file_at(
            directory_descriptor,
            name,
            RECORD_MAX_BYTES,
        )
    except UnsafePathError as error:
        raise _path_escape(path) from error


def _walk_record_documents(directory_descriptor: int, directory: Path):
    try:
        names = sorted(
            os.listdir(directory_descriptor),
            key=lambda value: value.encode("utf-8"),
        )
    except OSError as error:
        raise _path_escape(directory) from error
    for name in names:
        path = directory / name
        try:
            entry = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise _path_escape(path) from error
        if stat.S_ISLNK(entry.st_mode):
            raise _path_escape(path)
        if stat.S_ISDIR(entry.st_mode):
            child = _open_child_directory(directory_descriptor, name, path)
            try:
                yield from _walk_record_documents(child, path)
            finally:
                os.close(child)
        elif name.endswith(".md"):
            yield path, _read_regular_file_at(
                directory_descriptor,
                name,
                path,
            )


def _iter_record_documents(workspace: Path):
    workspace_descriptor, absolute_workspace = _open_directory_path(workspace)
    knowledge = absolute_workspace / "knowledge"
    records = knowledge / "records"
    knowledge_descriptor: int | None = None
    records_descriptor: int | None = None
    try:
        try:
            os.stat(
                "knowledge",
                dir_fd=workspace_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        knowledge_descriptor = _open_child_directory(
            workspace_descriptor,
            "knowledge",
            knowledge,
        )
        try:
            os.stat(
                "records",
                dir_fd=knowledge_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        records_descriptor = _open_child_directory(
            knowledge_descriptor,
            "records",
            records,
        )
        yield from _walk_record_documents(records_descriptor, records)
    finally:
        if records_descriptor is not None:
            os.close(records_descriptor)
        if knowledge_descriptor is not None:
            os.close(knowledge_descriptor)
        os.close(workspace_descriptor)


def _parse_document(raw: bytes):
    if raw.startswith(b"\xef\xbb\xbf"):
        raise SchemaError("BOM not allowed")
    if b"\r" in raw:
        raise SchemaError("CR not allowed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SchemaError("invalid UTF-8") from error

    lines = text.split("\n")
    if not lines or lines[0] != "+++":
        raise SchemaError("missing opening +++")
    try:
        closing = lines.index("+++", 1)
    except ValueError as error:
        raise SchemaError("missing closing +++") from error
    frontmatter_lines = lines[1:closing]
    try:
        fields = tomllib.loads("\n".join(frontmatter_lines))
    except tomllib.TOMLDecodeError as error:
        raise SchemaError("invalid frontmatter: {}".format(error)) from error
    body = "\n".join(lines[closing + 1 :])
    return fields, frontmatter_lines, body


def _load_record(workspace: Path, path: Path, raw: bytes):
    fields, frontmatter_lines, body = _parse_document(raw)
    record = validate_frontmatter(fields)
    canonical = _render_frontmatter(list(fields.items()))
    actual = "\n".join(["+++", *frontmatter_lines, "+++"]) + "\n"
    if actual != canonical:
        raise SchemaError("NONCANONICAL_FRONTMATTER {}".format(record["id"]))
    if len(raw) > RECORD_MAX_BYTES:
        raise SchemaError("RECORD_TOO_LARGE {}".format(record["id"]))
    if raw.count(b"\n") > RECORD_MAX_LINES:
        raise SchemaError("RECORD_TOO_MANY_LINES {}".format(record["id"]))
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise SchemaError("INVALID_TERMINAL_LF {}".format(record["id"]))
    validate_body(record, body)

    expected_directory = workspace / "knowledge" / "records" / record["type"]
    if path.parent != expected_directory:
        raise PolicyError(
            "WRONG_DIRECTORY {} {}".format(record["id"], path)
        )

    if record["type"] == "evidence":
        if record["artifact_mode"] == "local":
            verify_artifact_local(
                workspace,
                record["artifact_path"],
                record["artifact_sha256"],
                record["id"],
            )
        else:
            verify_artifact_git(
                workspace,
                record["artifact_path"],
                record["artifact_git"],
                record["id"],
            )
    record["content_bytes"] = raw
    record["path"] = str(path)
    return record


def resolve_reference(ref_id, current_id, allowed_prefixes, records_by_id):
    """Resolve only through a complete, already-validated record map."""

    def dangling():
        return PolicyError(
            "DANGLING_SOURCE {} -> {}".format(current_id, ref_id)
        )

    if ref_id == current_id or not isinstance(ref_id, str):
        raise dangling()
    prefix = ref_id[:3]
    if prefix not in allowed_prefixes:
        raise dangling()
    target = records_by_id.get(ref_id)
    if target is None or target.get("type") != TYPE_BY_PREFIX.get(prefix):
        raise dangling()
    return target["path"]


def _validate_references(record, records_by_id) -> None:
    for source_id in record["sources"]:
        resolve_reference(
            source_id,
            record["id"],
            SOURCE_PREFIXES,
            records_by_id,
        )
    if record["type"] == "experiment":
        for contradicted_id in record["contradicts"]:
            resolve_reference(
                contradicted_id,
                record["id"],
                CONTRADICTS_PREFIXES,
                records_by_id,
            )


def validate_supersession_integrity(records) -> None:
    """Require existing, reciprocal same-type supersession links."""

    records_by_id = {record["id"]: record for record in records}
    for record in sorted(records, key=lambda item: item["id"]):
        record_id = record["id"]
        successor_id = record.get("superseded_by")
        if successor_id is not None:
            successor = records_by_id.get(successor_id)
            if successor is None:
                raise PolicyError(
                    "DANGLING_SUPERSEDED_BY {} -> {}".format(
                        record_id, successor_id
                    )
                )
            if record["status"] != "superseded":
                raise PolicyError("NOT_SUPERSEDED {}".format(record_id))
            if successor.get("supersedes") != record_id:
                raise PolicyError(
                    "NONRECIPROCAL_SUPERSESSION {} -> {}".format(
                        record_id, successor_id
                    )
                )
            if successor.get("type") != record.get("type"):
                raise PolicyError(
                    "CROSS_TYPE_SUPERSESSION {} superseded_by".format(
                        record_id
                    )
                )

        predecessor_id = record.get("supersedes")
        if predecessor_id is not None:
            predecessor = records_by_id.get(predecessor_id)
            if predecessor is None:
                raise PolicyError(
                    "DANGLING_SUPERSEDES {} -> {}".format(
                        record_id, predecessor_id
                    )
                )
            if predecessor["status"] != "superseded":
                raise PolicyError(
                    "PREDECESSOR_NOT_SUPERSEDED {} -> {}".format(
                        record_id, predecessor_id
                    )
                )
            if predecessor.get("superseded_by") != record_id:
                raise PolicyError(
                    "NONRECIPROCAL_SUPERSESSION {} -> {}".format(
                        record_id, predecessor_id
                    )
                )
            if predecessor.get("type") != record.get("type"):
                raise PolicyError(
                    "CROSS_TYPE_SUPERSESSION {} supersedes".format(record_id)
                )


def record_tree_digest(records) -> str:
    """Return SHA-256 of canonical sorted ID/content-digest rows."""

    parts = []
    for record in sorted(records, key=lambda item: item["id"]):
        content_sha256 = hashlib.sha256(record["content_bytes"]).hexdigest()
        parts.append(
            "{}\t{}\n".format(record["id"], content_sha256).encode("utf-8")
        )
    return hashlib.sha256(b"".join(parts)).hexdigest()


def validate_record_tree(workspace, collision_id=None):
    """Validate the complete record tree before returning any record map."""

    root = Path(workspace)
    records = [
        _load_record(root, path, raw)
        for path, raw in _iter_record_documents(root)
    ]

    identifiers = set()
    for record in sorted(records, key=lambda item: item["id"]):
        if record["id"] in identifiers:
            raise PolicyError("DUPLICATE_ID {}".format(record["id"]))
        identifiers.add(record["id"])
    if collision_id is not None and collision_id in identifiers:
        raise PolicyError("COLLISION {}".format(collision_id))

    for record in sorted(records, key=lambda item: item["id"]):
        path = Path(record["path"])
        if path.name != record["id"] + ".md":
            raise PolicyError(
                "NONCANONICAL_FILENAME {} {}".format(record["id"], path)
            )

    records_by_id = {record["id"]: record for record in records}
    for record in sorted(records, key=lambda item: item["id"]):
        _validate_references(record, records_by_id)
    validate_supersession_integrity(records)
    return records
