"""lesson·docs·book에서 프로젝트별 개인 지식 목록을 생성한다."""

from __future__ import annotations

import base64
from enum import Enum
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Iterable, Mapping

from didimlog.file_io import (
    UnsafePathError,
    open_child_directory,
    open_directory_path,
    read_regular_file_at,
    read_regular_file_at_with_stat,
    write_all_and_sync,
)
from didimlog.locking import path_lock

from .lesson import (
    LESSON_MAX_BYTES,
    parse_frontmatter_text,
    parse_inline_list,
    parse_lesson_text,
    valid_index_title,
)
from .paths import (
    ProjectDirectory,
    ProjectDirectoryError,
    index_dir,
    lessons_dir,
    project_directory_unchanged,
    resolve_project_directory,
    validate_project,
)


SECTIONS = (
    ("lesson", "작업 규칙"),
    ("docs", "작업 문서"),
    ("book", "해설 자료"),
)
GENERATED_NOTICE = "<!-- Didimlog Personal Knowledge가 자동 생성한다. 직접 수정하지 마라. -->"
SOURCE_MAX_BYTES = 4 * 1024 * 1024


class KnowledgeIndexError(ValueError):
    """지식 원본이나 생성 인덱스가 안전한 생성 계약을 위반했다."""


class IndexCheckState(Enum):
    CURRENT = "current"
    MISSING = "missing"
    EXTRA = "extra"
    STALE = "stale"


class KnowledgeSourceError(KnowledgeIndexError):
    def __init__(self, logical_path: str, reason: str) -> None:
        self.logical_path = logical_path
        self.reason = reason
        super().__init__(
            "KNOWLEDGE_INDEX_INVALID {}: {}".format(logical_path, reason)
        )




def _byte_key(value: str) -> bytes:
    return value.encode("utf-8")


def _markdown_text(value: str) -> str:
    escaped = {"\\", "`", "*", "_", "[", "]", "<", ">"}
    return "".join("\\" + char if char in escaped else char for char in value)


def _code_span(value: str) -> str:
    longest = current = 0
    for char in value:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    delimiter = "`" * (longest + 1)
    return delimiter + value + delimiter


def _logical_source_path(
    project: ProjectDirectory,
    relative: Path | None = None,
) -> str:
    base = Path(project.logical.parent.name) / project.logical.name
    logical = base
    if relative is not None:
        logical /= relative
    value = logical.as_posix()
    try:
        _byte_key(value)
    except UnicodeEncodeError as exc:
        raise KnowledgeSourceError(
            base.as_posix(),
            "source name must be valid UTF-8",
        ) from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise KnowledgeSourceError(
            base.as_posix(),
            "source path contains a control character",
        )
    return value


def _validate_knowledge_root(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID knowledge root: cannot inspect directory"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID knowledge root: must be a real directory"
        )


def _project_directories(root: Path) -> dict[str, ProjectDirectory]:
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise KnowledgeSourceError(
            root.name,
            "cannot inspect source directory",
        ) from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise KnowledgeSourceError(
            root.name,
            "source category must be a real directory",
        )

    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise KnowledgeSourceError(
            root.name,
            "cannot inspect source directory",
        ) from exc

    selected = []
    for entry in entries:
        try:
            validate_project(entry.name, allow_global=True)
        except ValueError:
            continue
        selected.append(entry)

    entries = sorted(selected, key=lambda entry: os.fsencode(entry.name))
    result = {}
    for entry in entries:
        logical_path = "{}/{}".format(root.name, entry.name)
        try:
            entry_info = entry.lstat()
        except OSError as exc:
            raise KnowledgeSourceError(
                logical_path,
                "cannot inspect project entry",
            ) from exc
        if not (
            stat.S_ISDIR(entry_info.st_mode) or stat.S_ISLNK(entry_info.st_mode)
        ):
            raise KnowledgeSourceError(
                logical_path,
                "project entry must point to a directory",
            )
        try:
            resolved = resolve_project_directory(root, entry.name)
        except ProjectDirectoryError as exc:
            raise KnowledgeSourceError(logical_path, exc.reason) from exc
        if resolved is None:
            raise KnowledgeSourceError(
                logical_path,
                "project entry changed during scan",
            )
        result[entry.name] = resolved
    return result


def _open_project_directory(project: ProjectDirectory) -> int:
    descriptor = None
    try:
        descriptor = open_directory_path(project.physical)
        opened = os.fstat(descriptor)
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
        )
        if opened_identity != project.target_identity:
            raise UnsafePathError("project target changed")
        return descriptor
    except (OSError, UnsafePathError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise KnowledgeSourceError(
            _logical_source_path(project),
            "project link changed during scan",
        ) from exc


def _read_pinned_markdown(
    root_descriptor: int,
    relative: Path,
    maximum_bytes: int,
) -> bytes:
    if relative.is_absolute() or any(
        part in ("", ".", "..") for part in relative.parts
    ):
        raise UnsafePathError("unsafe relative path")
    descriptors = []
    try:
        root_copy = os.dup(root_descriptor)
        descriptors.append(root_copy)
        if not stat.S_ISDIR(os.fstat(root_copy).st_mode):
            raise UnsafePathError("unsafe project descriptor")
        for component in relative.parts[:-1]:
            descriptors.append(
                open_child_directory(descriptors[-1], component)
            )
        return read_regular_file_at(
            descriptors[-1],
            relative.name,
            maximum_bytes,
        )
    except UnsafePathError:
        raise
    except OSError as exc:
        raise UnsafePathError("cannot traverse project directory") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _lesson_item(
    project: ProjectDirectory,
    root_descriptor: int,
    relative: Path,
    *,
    read_relative: Path | None = None,
    include_content: bool = False,
) -> dict[str, object]:
    logical_path = _logical_source_path(project, relative)
    try:
        data = _read_pinned_markdown(
            root_descriptor,
            relative if read_relative is None else read_relative,
            LESSON_MAX_BYTES,
        )
        if len(data) > LESSON_MAX_BYTES:
            raise UnsafePathError("source exceeds read limit")
        text = data.decode("utf-8")
        parsed = parse_lesson_text(relative.name, text)
    except (UnsafePathError, UnicodeDecodeError, ValueError) as exc:
        raise KnowledgeSourceError(
            logical_path,
            "invalid lesson metadata",
        ) from exc
    if parsed is None:
        raise KnowledgeSourceError(logical_path, "invalid lesson metadata")
    fields, lines, closing = parsed
    tags = parse_inline_list(fields.get("tags", "[]"), canonical=True)
    if tags is None:
        raise KnowledgeSourceError(logical_path, "invalid tags")
    find_when = sorted(set([fields["topic"], *tags]), key=_byte_key)
    if not find_when:
        raise KnowledgeSourceError(logical_path, "find_when is empty")
    item: dict[str, object] = {
        "kind": "lesson",
        "title": fields["title"],
        "find_when": find_when,
        "path": logical_path,
        "review_by": fields.get("review_by"),
    }
    if include_content:
        booked = parse_inline_list(fields.get("booked", "[]"))
        if booked is None:
            raise KnowledgeSourceError(logical_path, "invalid booked topics")
        item.update(
            {
                "slug": relative.stem,
                "topic": fields["topic"],
                "summary": fields["summary"],
                "tags": tags,
                "date": fields["date"],
                "booked": booked,
                "body": "\n".join(lines[closing + 1 :]),
            }
        )
    return item


def _document_item(
    project: ProjectDirectory,
    root_descriptor: int,
    relative: Path,
    kind: str,
    *,
    read_relative: Path | None = None,
) -> dict[str, object]:
    logical_path = _logical_source_path(project, relative)
    try:
        data = _read_pinned_markdown(
            root_descriptor,
            relative if read_relative is None else read_relative,
            SOURCE_MAX_BYTES,
        )
        if len(data) > SOURCE_MAX_BYTES:
            raise UnsafePathError("source exceeds read limit")
        text = data.decode("utf-8")
    except (UnsafePathError, UnicodeDecodeError, ValueError) as exc:
        raise KnowledgeSourceError(logical_path, "unreadable Markdown") from exc
    parsed = parse_frontmatter_text(relative.name, text, ("title", "find_when"))
    if parsed is None:
        raise KnowledgeSourceError(logical_path, "missing title or find_when")
    fields, _, _ = parsed
    title = fields["title"]
    if not valid_index_title(title):
        raise KnowledgeSourceError(logical_path, "invalid title")
    find_when = parse_inline_list(fields["find_when"], canonical=True)
    if not find_when:
        raise KnowledgeSourceError(
            logical_path,
            "find_when is empty or not canonical",
        )
    return {
        "kind": kind,
        "title": title,
        "find_when": find_when,
        "path": logical_path,
        "review_by": None,
    }


def _symlink_points_to_directory(
    parent_descriptor: int,
    name: str,
) -> bool:
    try:
        return stat.S_ISDIR(
            os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=True,
            ).st_mode
        )
    except OSError:
        return False


def _markdown_files(
    project: ProjectDirectory,
    *,
    recursive: bool,
    root_descriptor: int,
    item_reader=None,
) -> list[object]:
    files = []

    def scan(directory_descriptor: int, parent_relative: Path) -> None:
        try:
            names = sorted(os.listdir(directory_descriptor), key=os.fsencode)
        except OSError as exc:
            raise KnowledgeSourceError(
                _logical_source_path(project, parent_relative),
                "cannot inspect source directory",
            ) from exc

        selected = []
        for name in names:
            try:
                name.encode("utf-8")
            except UnicodeEncodeError as exc:
                if name.endswith(".md"):
                    raise KnowledgeSourceError(
                        _logical_source_path(project, parent_relative),
                        "source name must be valid UTF-8",
                    ) from exc
                continue
            relative = parent_relative / name
            try:
                entry_info = os.stat(
                    name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise KnowledgeSourceError(
                    _logical_source_path(project, relative),
                    "cannot inspect source entry",
                ) from exc

            if stat.S_ISLNK(entry_info.st_mode):
                if relative.suffix == ".md":
                    action = "invalid-file"
                elif _symlink_points_to_directory(directory_descriptor, name):
                    action = "invalid-directory"
                else:
                    continue
            elif stat.S_ISDIR(entry_info.st_mode):
                if not recursive:
                    continue
                try:
                    child_descriptor = open_child_directory(
                        directory_descriptor,
                        name,
                    )
                except UnsafePathError as exc:
                    raise KnowledgeSourceError(
                        _logical_source_path(project, relative),
                        "cannot inspect source directory",
                    ) from exc
                try:
                    scan(child_descriptor, relative)
                finally:
                    os.close(child_descriptor)
                continue
            elif relative.suffix != ".md":
                continue
            elif not stat.S_ISREG(entry_info.st_mode):
                action = "invalid-file"
            else:
                action = "file"

            logical_path = _logical_source_path(project, relative)
            selected.append(
                (
                    _byte_key(relative.as_posix()),
                    relative,
                    logical_path,
                    action,
                )
            )

        for _, relative, logical_path, action in sorted(selected):
            if action == "invalid-file":
                raise KnowledgeSourceError(
                    logical_path,
                    "source must be a regular file",
                )
            if action == "invalid-directory":
                raise KnowledgeSourceError(
                    logical_path,
                    "source directory must be a real directory",
                )
            if item_reader is None:
                files.append((relative, relative))
            else:
                files.append(
                    (
                        relative,
                        item_reader(
                            directory_descriptor,
                            relative,
                            Path(relative.name),
                        ),
                    )
                )

    scan(root_descriptor, Path())
    return [
        item
        for _, item in sorted(
            files,
            key=lambda pair: _byte_key(pair[0].as_posix()),
        )
    ]


def _require_projects_unchanged(
    projects: Iterable[ProjectDirectory],
) -> None:
    for project in projects:
        if not project_directory_unchanged(project):
            raise KnowledgeSourceError(
                _logical_source_path(project),
                "project link changed during scan",
            )


def _collect_with_projects(
    data_root=None,
    *,
    include_content: bool = False,
) -> tuple[
    dict[str, list[dict[str, object]]],
    tuple[ProjectDirectory, ...],
]:
    root = Path(data_root) if data_root is not None else lessons_dir().parent
    _validate_knowledge_root(root)
    roots = {
        "lesson": root / "lessons",
        "docs": root / "docs",
        "book": root / "book",
    }
    directories = {
        kind: _project_directories(source_root)
        for kind, source_root in roots.items()
    }
    snapshots = tuple(
        project
        for mapping in directories.values()
        for project in mapping.values()
    )

    projects = sorted(
        set().union(*(mapping.keys() for mapping in directories.values())),
        key=_byte_key,
    )
    collected = {}
    for project_name in projects:
        items = []
        lesson_project = directories["lesson"].get(project_name)
        if lesson_project is not None:
            descriptor = _open_project_directory(lesson_project)
            try:
                items.extend(
                    _markdown_files(
                        lesson_project,
                        recursive=False,
                        root_descriptor=descriptor,
                        item_reader=lambda current_descriptor, relative, leaf: (
                            _lesson_item(
                                lesson_project,
                                current_descriptor,
                                relative,
                                read_relative=leaf,
                                include_content=include_content,
                            )
                        ),
                    )
                )
                _require_projects_unchanged((lesson_project,))
            finally:
                os.close(descriptor)

        docs_project = directories["docs"].get(project_name)
        if docs_project is not None:
            descriptor = _open_project_directory(docs_project)
            try:
                items.extend(
                    _markdown_files(
                        docs_project,
                        recursive=True,
                        root_descriptor=descriptor,
                        item_reader=lambda current_descriptor, relative, leaf: (
                            _document_item(
                                docs_project,
                                current_descriptor,
                                relative,
                                "docs",
                                read_relative=leaf,
                            )
                        ),
                    )
                )
                _require_projects_unchanged((docs_project,))
            finally:
                os.close(descriptor)

        book_project = directories["book"].get(project_name)
        if book_project is not None:
            descriptor = _open_project_directory(book_project)
            try:
                items.extend(
                    _markdown_files(
                        book_project,
                        recursive=False,
                        root_descriptor=descriptor,
                        item_reader=lambda current_descriptor, relative, leaf: (
                            _document_item(
                                book_project,
                                current_descriptor,
                                relative,
                                "book",
                                read_relative=leaf,
                            )
                        ),
                    )
                )
                _require_projects_unchanged((book_project,))
            finally:
                os.close(descriptor)
        collected[project_name] = items
    _require_projects_unchanged(snapshots)
    return collected, snapshots


def collect(
    data_root=None,
    *,
    include_content: bool = False,
) -> dict[str, list[dict[str, object]]]:
    """Return validated source rows, optionally including lesson view content."""
    collected, _ = _collect_with_projects(
        data_root,
        include_content=include_content,
    )
    return collected


def collect_snapshot(
    data_root=None,
    *,
    include_content: bool = False,
) -> tuple[dict[str, list[dict[str, object]]], IndexCheckState]:
    """Collect one source snapshot and inspect its exact derived index under one lock."""
    root = Path(data_root) if data_root is not None else lessons_dir().parent
    with path_lock(root, shared=True):
        collected, _ = _collect_with_projects(
            root,
            include_content=include_content,
        )
        outputs = {
            project: render_project(project, items)
            for project, items in collected.items()
        }
        state = inspect_index(outputs, root / "index")
        return collected, state


def render_project(project: str, items: Iterable[Mapping[str, object]]) -> str:
    """한 프로젝트의 항목을 결정론적인 LF Markdown으로 렌더한다."""
    try:
        validate_project(project, allow_global=True)
    except ValueError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: invalid project".format(project)
        ) from exc

    materialized = list(items)
    labels = dict(SECTIONS)
    lines = [GENERATED_NOTICE, "# {} 지식 목록".format(project)]
    for kind, _ in SECTIONS:
        selected = sorted(
            (item for item in materialized if item["kind"] == kind),
            key=lambda item: (
                _byte_key(str(item["title"])),
                _byte_key(str(item["path"])),
            ),
        )
        if not selected:
            continue
        lines.extend(["", "## {}".format(labels[kind])])
        for item in selected:
            title = _markdown_text(str(item["title"]))
            if item.get("review_by"):
                title += " (검토 기준일: {})".format(item["review_by"])
            find_when = item["find_when"]
            lines.extend(
                [
                    "",
                    "- **{}**".format(title),
                    "  - 찾을 때: {}".format(
                        ", ".join(_markdown_text(str(value)) for value in find_when)
                    ),
                    "  - 상세: {}".format(_code_span(str(item["path"]))),
                ]
            )
    return "\n".join(lines) + "\n"


def _build_all_with_projects(
    data_root=None,
) -> tuple[dict[str, str], tuple[ProjectDirectory, ...]]:
    collected, projects = _collect_with_projects(data_root)
    outputs = {
        project: render_project(project, items)
        for project, items in collected.items()
    }
    _require_projects_unchanged(projects)
    return outputs, projects


def build_all(data_root=None) -> dict[str, str]:
    """현재 전체 원본에 대응하는 프로젝트별 출력 문자열을 만든다."""
    outputs, _ = _build_all_with_projects(data_root)
    return outputs


def _normalized_outputs(outputs: Mapping[str, str]) -> dict[str, str]:
    normalized = {}
    try:
        projects = sorted(outputs, key=_byte_key)
    except (AttributeError, TypeError, UnicodeEncodeError) as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: output project names must be UTF-8 strings"
        ) from exc
    for project in projects:
        try:
            validate_project(project, allow_global=True)
        except (TypeError, ValueError) as exc:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: invalid output project".format(project)
            ) from exc
        text = outputs[project]
        if not isinstance(text, str):
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: output must be text".format(project)
            )
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: output is not valid UTF-8 text".format(
                    project
                )
            ) from exc
        normalized[project] = text
    return normalized


def _expected_names(outputs: Mapping[str, str]) -> set[str]:
    return {project + ".md" for project in outputs}


def _decode_recovery_record_bytes(raw: bytes) -> tuple[str, bytes, int]:
    record = json.loads(raw.decode("utf-8"))
    if (
        not isinstance(record, dict)
        or set(record) != {
            "data_base64",
            "logical_name",
            "mode",
            "version",
        }
        or record["version"] != 1
        or not isinstance(record["logical_name"], str)
        or not isinstance(record["data_base64"], str)
        or not isinstance(record["mode"], int)
        or isinstance(record["mode"], bool)
        or record["mode"] < 0
        or record["mode"] > 0o7777
    ):
        raise ValueError("invalid recovery record")
    logical_name = record["logical_name"]
    logical = Path(logical_name)
    if logical.name != logical_name or logical.suffix != ".md":
        raise ValueError("invalid logical name")
    validate_project(logical.stem, allow_global=True)
    data = base64.b64decode(
        record["data_base64"].encode("ascii"),
        validate=True,
    )
    return logical_name, data, record["mode"]




def _recovery_record_name_matches(name: str, kind: str) -> bool:
    prefix = ".index-{}-".format(kind)
    if not name.startswith(prefix) or not name.endswith(".json"):
        return False
    remainder = name[len(prefix) : -len(".json")]
    parts = remainder.split("-")
    if len(parts) != 2:
        return False
    digest, token = parts
    return (
        len(digest) == 16
        and all(character in "0123456789abcdef" for character in digest)
        and len(token) == 24
        and all(character in "0123456789abcdef" for character in token)
    )

def _decode_named_recovery_record_bytes(
    raw: bytes,
    name: str,
    kind: str,
) -> tuple[str, bytes, int]:
    try:
        logical_name, data, mode = _decode_recovery_record_bytes(raw)
    except (
        UnicodeDecodeError,
        UnicodeEncodeError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: invalid index recovery record"
        ) from exc
    digest = hashlib.sha256(logical_name.encode("utf-8")).hexdigest()[:16]
    if not name.startswith(".index-{}-{}-".format(kind, digest)):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: invalid index recovery record"
        )
    return logical_name, data, mode




def _legacy_artifact_snapshot_at(
    directory_descriptor: int,
    name: str,
) -> tuple[bytes, tuple[int, ...]] | None:
    try:
        linked_info = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(linked_info.st_mode):
            return None
        data, opened_info = read_regular_file_at_with_stat(
            directory_descriptor,
            name,
            linked_info.st_size,
        )
        current_info = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except (OSError, UnsafePathError):
        return None
    opened_revision = _index_publication_revision(opened_info)
    if (
        len(data) != opened_info.st_size
        or not stat.S_ISREG(current_info.st_mode)
        or _index_publication_revision(current_info) != opened_revision
    ):
        return None
    return data, opened_revision


def _paired_recovery_artifact_is_valid(
    name: str,
    entries: set[str],
    kind: str,
    snapshots: Mapping[str, tuple[bytes, tuple[int, ...]]],
) -> bool:
    suffix = Path(name).suffix
    if suffix not in (".tmp", ".name"):
        return False
    base_name = name.removesuffix(suffix)
    temporary_name = base_name + ".tmp"
    metadata_name = base_name + ".name"
    if (
        temporary_name not in entries
        or metadata_name not in entries
        or temporary_name not in snapshots
        or metadata_name not in snapshots
    ):
        return False
    prefix = ".index-{}-".format(kind)
    if not base_name.startswith(prefix):
        return False
    parts = base_name[len(prefix) :].split("-")
    if len(parts) != 2:
        return False
    digest, token = parts
    if (
        len(digest) != 16
        or len(token) != 24
        or any(
            character not in "0123456789abcdef"
            for character in digest + token
        )
    ):
        return False
    try:
        raw = snapshots[metadata_name][0]
        logical_name = raw.decode("utf-8").removesuffix("\n")
        if raw != (logical_name + "\n").encode("utf-8"):
            return False
        logical = Path(logical_name)
        if logical.name != logical_name or logical.suffix != ".md":
            return False
        validate_project(logical.stem, allow_global=True)
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        return False
    data = snapshots[temporary_name][0]
    expected_digest = hashlib.sha256(
        logical_name.encode("utf-8")
    ).hexdigest()[:16]
    generated_prefix = (GENERATED_NOTICE + "\n").encode("utf-8")
    content_is_owned = data.startswith(generated_prefix) or (
        kind == "quarantine" and data == b""
    )
    return digest == expected_digest and content_is_owned


def _generated_temporary_name_matches(name: str) -> bool:
    return (
        name.startswith(".index-")
        and name.endswith(".tmp")
        and len(name) == len(".index-") + 24 + len(".tmp")
        and all(
            character in "0123456789abcdef"
            for character in name[len(".index-") : -len(".tmp")]
        )
    )


def _legacy_index_artifacts_at(
    directory_descriptor: int,
    entries: set[str],
) -> dict[str, tuple[bytes, tuple[int, ...]]]:
    recovery_kinds = ("recovery", "resolved")
    candidate_names = {
        name
        for name in entries
        if any(
            _recovery_record_name_matches(name, kind)
            for kind in recovery_kinds
        )
        or _generated_temporary_name_matches(name)
        or any(
            name.startswith(".index-{}-".format(kind))
            and Path(name).suffix in (".tmp", ".name")
            for kind in ("retired", "quarantine")
        )
    }
    snapshots = {
        name: snapshot
        for name in candidate_names
        if (
            snapshot := _legacy_artifact_snapshot_at(
                directory_descriptor,
                name,
            )
        )
        is not None
    }
    artifacts = {}
    generated_prefix = (GENERATED_NOTICE + "\n").encode("utf-8")
    for name in entries:
        recovery_kind = next(
            (
                kind
                for kind in recovery_kinds
                if _recovery_record_name_matches(name, kind)
            ),
            None,
        )
        if recovery_kind is not None:
            snapshot = snapshots.get(name)
            if snapshot is None:
                continue
            _, data, _ = _decode_named_recovery_record_bytes(
                snapshot[0],
                name,
                recovery_kind,
            )
            if data.startswith(generated_prefix):
                artifacts[name] = snapshot
            continue
        if any(
            _paired_recovery_artifact_is_valid(
                name,
                entries,
                kind,
                snapshots,
            )
            for kind in ("retired", "quarantine")
        ):
            artifacts[name] = snapshots[name]
            continue
        if _generated_temporary_name_matches(name):
            snapshot = snapshots.get(name)
            if snapshot is not None and snapshot[0].startswith(
                generated_prefix
            ):
                artifacts[name] = snapshot
    return artifacts




def _validate_index_directory(
    directory_descriptor: int,
    expected: set[str],
    *,
    cleanup_artifacts: dict[
        str,
        tuple[bytes, tuple[int, ...]],
    ] | None = None,
    stale_snapshots: dict[
        str,
        tuple[bytes, tuple[int, ...]],
    ] | None = None,
) -> set[str]:
    try:
        names = tuple(os.listdir(directory_descriptor))
    except OSError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: cannot inspect index"
        ) from exc
    entry_names = set(names)
    legacy_artifacts = _legacy_index_artifacts_at(
        directory_descriptor,
        entry_names,
    )
    if cleanup_artifacts is not None:
        cleanup_artifacts.update(legacy_artifacts)
    for name in names:
        try:
            entry_info = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: "
                "cannot inspect index entry".format(name)
            ) from exc
        if name in legacy_artifacts:
            continue
        entry = Path(name)
        if entry.suffix != ".md":
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: unknown index entry".format(name)
            )
        if name in expected:
            if stat.S_ISREG(entry_info.st_mode) or stat.S_ISLNK(
                entry_info.st_mode
            ):
                continue
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: unknown index entry".format(name)
            )
        if not stat.S_ISREG(entry_info.st_mode):
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: unknown index entry".format(name)
            )
        try:
            validate_project(entry.stem, allow_global=True)
            snapshot = _legacy_artifact_snapshot_at(
                directory_descriptor,
                name,
            )
            if snapshot is None:
                raise ValueError("index changed during inspection")
            first_line = (
                snapshot[0]
                .decode("utf-8")
                .split("\n", 1)[0]
                .removesuffix("\r")
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: unknown index entry".format(name)
            ) from exc
        if first_line != GENERATED_NOTICE:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: "
                "index entry is not generator-owned".format(name)
            )
        if stale_snapshots is not None:
            stale_snapshots[name] = snapshot
    return {
        name
        for name in names
        if Path(name).suffix == ".md"
    }




def _domain_root(data_root=None, target=None) -> Path:
    if data_root is not None:
        return Path(data_root)
    if target is not None:
        return Path(target).parent
    return lessons_dir().parent




def _index_publication_revision(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
    )


def _index_directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _require_index_directory_unchanged(
    destination: Path,
    identity: tuple[int, int, int],
) -> None:
    try:
        current = destination.lstat()
    except OSError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: index directory changed during publish"
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or _index_directory_identity(current) != identity
    ):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: index directory changed during publish"
        )


def _cleanup_created_index_directory(
    parent_descriptor: int,
    temporary_name: str,
    identity: tuple[int, int, int],
) -> None:
    from .render import _rename_entry_no_replace

    staged_name = None
    for _ in range(32):
        candidate = ".index-preserved-directory-{}".format(
            secrets.token_hex(12)
        )
        try:
            _rename_entry_no_replace(
                parent_descriptor,
                temporary_name,
                candidate,
            )
        except FileExistsError:
            continue
        except FileNotFoundError:
            return
        except OSError as exc:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID: "
                "cannot preserve temporary index directory"
            ) from exc
        staged_name = candidate
        break
    if staged_name is None:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: "
            "cannot preserve temporary index directory"
        )

    def restore_staged() -> None:
        try:
            _rename_entry_no_replace(
                parent_descriptor,
                staged_name,
                temporary_name,
            )
        except OSError:
            return
        try:
            os.fsync(parent_descriptor)
        except OSError:
            pass

    try:
        staged_info = os.stat(
            staged_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        restore_staged()
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: "
            "cannot inspect temporary index directory"
        ) from exc
    if (
        not stat.S_ISDIR(staged_info.st_mode)
        or _index_directory_identity(staged_info) != identity
    ):
        restore_staged()
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: "
            "temporary index directory changed during creation"
        )

    try:
        os.rmdir(staged_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except OSError as exc:
        restore_staged()
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: "
            "cannot remove temporary index directory"
        ) from exc


def _create_index_directory_no_replace(
    destination: Path,
) -> tuple[int, tuple[int, int, int]]:
    from .render import _rename_entry_no_replace

    parent = destination.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: index parent does not exist"
        ) from exc
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
    ):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: index parent must be a real directory"
        )

    parent_identity = _index_directory_identity(parent_info)
    parent_descriptor = open_directory_path(parent)
    directory_descriptor = None
    temporary_name = None
    try:
        opened_parent_identity = _index_directory_identity(
            os.fstat(parent_descriptor)
        )
        if opened_parent_identity != parent_identity:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID: "
                "index parent changed while opening"
            )

        try:
            os.stat(
                destination.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID: "
                "index directory changed during creation"
            )

        for _ in range(32):
            candidate = ".index-directory-{}".format(secrets.token_hex(12))
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_name is None:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID: "
                "cannot create temporary index directory"
            )

        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        directory_descriptor = os.open(
            temporary_name,
            flags,
            dir_fd=parent_descriptor,
        )
        identity = _index_directory_identity(
            os.fstat(directory_descriptor)
        )
        temporary_info = os.stat(
            temporary_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(temporary_info.st_mode)
            or _index_directory_identity(temporary_info) != identity
        ):
            _cleanup_created_index_directory(
                parent_descriptor,
                temporary_name,
                identity,
            )
            temporary_name = None
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID: "
                "temporary index directory changed while opening"
            )

        try:
            _rename_entry_no_replace(
                parent_descriptor,
                temporary_name,
                destination.name,
            )
        except OSError as exc:
            try:
                _cleanup_created_index_directory(
                    parent_descriptor,
                    temporary_name,
                    identity,
                )
            except KnowledgeIndexError as cleanup_error:
                raise cleanup_error from exc
            temporary_name = None
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID: "
                "index directory changed during creation"
            ) from exc
        temporary_name = None

        installed_info = os.stat(
            destination.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(installed_info.st_mode)
            or _index_directory_identity(installed_info) != identity
        ):
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID: "
                "index directory changed during creation"
            )
        _require_index_directory_unchanged(destination, identity)

        result = directory_descriptor
        directory_descriptor = None
        return result, identity
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        os.close(parent_descriptor)

def inspect_index(
    outputs: Mapping[str, str],
    target: Path,
) -> IndexCheckState:
    normalized = _normalized_outputs(outputs)
    destination = Path(target)
    try:
        initial_info = destination.lstat()
    except FileNotFoundError:
        return IndexCheckState.MISSING
    except OSError:
        return IndexCheckState.EXTRA
    if (
        stat.S_ISLNK(initial_info.st_mode)
        or not stat.S_ISDIR(initial_info.st_mode)
    ):
        return IndexCheckState.EXTRA

    try:
        directory_descriptor = open_directory_path(destination)
    except (OSError, UnsafePathError):
        return IndexCheckState.EXTRA
    try:
        identity = None
        try:
            identity = _index_directory_identity(
                os.fstat(directory_descriptor)
            )
            expected = _expected_names(normalized)
            actual = _validate_index_directory(
                directory_descriptor,
                expected,
            )
        except (
            KnowledgeIndexError,
            OSError,
            UnsafePathError,
            UnicodeDecodeError,
        ):
            status = IndexCheckState.EXTRA
        else:
            if expected - actual:
                status = IndexCheckState.MISSING
            elif actual - expected:
                status = IndexCheckState.EXTRA
            else:
                status = IndexCheckState.CURRENT
                for project, text in normalized.items():
                    expected_bytes = text.encode("utf-8")
                    try:
                        current = read_regular_file_at(
                            directory_descriptor,
                            project + ".md",
                            len(expected_bytes),
                        )
                    except (OSError, UnsafePathError):
                        status = IndexCheckState.MISSING
                        break
                    if current != expected_bytes:
                        status = IndexCheckState.STALE
                        break

        if identity is None:
            return IndexCheckState.EXTRA
        try:
            _require_index_directory_unchanged(destination, identity)
        except (KnowledgeIndexError, OSError):
            return IndexCheckState.EXTRA
        return status
    finally:
        os.close(directory_descriptor)


def _cleanup_owned_legacy_index_artifacts(
    directory_descriptor: int,
    snapshots: Mapping[str, tuple[bytes, tuple[int, ...]]],
    *,
    allow_missing: bool = False,
) -> None:
    from .render import _rename_entry_no_replace

    if not snapshots:
        return

    remaining = set(snapshots)
    groups = []
    while remaining:
        name = min(remaining, key=_byte_key)
        suffix = Path(name).suffix
        counterpart = None
        if (
            suffix in (".tmp", ".name")
            and name.startswith((".index-retired-", ".index-quarantine-"))
        ):
            counterpart = name.removesuffix(suffix) + (
                ".name" if suffix == ".tmp" else ".tmp"
            )
        if counterpart in remaining:
            group = tuple(sorted((name, counterpart), key=_byte_key))
            remaining.difference_update(group)
        else:
            group = (name,)
            remaining.remove(name)
        groups.append(group)

    staged = []

    def restore_staged() -> None:
        for original_name, staged_name, _ in reversed(staged):
            try:
                _rename_entry_no_replace(
                    directory_descriptor,
                    staged_name,
                    original_name,
                )
            except FileNotFoundError:
                continue
            except OSError:
                pass
        try:
            os.fsync(directory_descriptor)
        except OSError:
            pass

    try:
        for group in groups:
            digest = hashlib.sha256(
                b"\x00".join(os.fsencode(name) for name in group)
            ).hexdigest()[:16]
            token = secrets.token_hex(12)
            base_name = ".index-preserved-{}-{}".format(digest, token)
            for name in group:
                staged_name = base_name + Path(name).suffix
                try:
                    _rename_entry_no_replace(
                        directory_descriptor,
                        name,
                        staged_name,
                    )
                except FileNotFoundError:
                    if allow_missing:
                        continue
                    raise
                staged.append((name, staged_name, snapshots[name]))

        for _, staged_name, expected_snapshot in staged:
            current_snapshot = _legacy_artifact_snapshot_at(
                directory_descriptor,
                staged_name,
            )
            if current_snapshot != expected_snapshot:
                restore_staged()
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID: "
                    "legacy index artifact changed during cleanup"
                )

        for _, staged_name, _ in staged:
            os.unlink(staged_name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except KnowledgeIndexError:
        raise
    except OSError as exc:
        restore_staged()
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: cannot remove legacy index artifact"
        ) from exc


def _cleanup_index_temporaries(
    directory_descriptor: int,
    snapshots: Mapping[str, tuple[bytes, tuple[int, ...]]],
) -> None:
    _cleanup_owned_legacy_index_artifacts(
        directory_descriptor,
        snapshots,
        allow_missing=True,
    )


def _snapshot_index_temporary_descriptor(
    descriptor: int,
    maximum_bytes: int,
) -> tuple[bytes, tuple[int, ...]] | None:
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_size < 0
        or info.st_size > maximum_bytes
    ):
        return None
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = info.st_size
    chunks = bytearray()
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            return None
        chunks.extend(chunk)
        remaining -= len(chunk)
    return bytes(chunks), _index_publication_revision(info)


def _prepare_index_temporary(
    directory_descriptor: int,
    project: str,
    data: bytes,
) -> tuple[str, tuple[bytes, tuple[int, ...]]]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(32):
        name = ".index-{}.tmp".format(secrets.token_hex(12))
        descriptor = None
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            os.fchmod(descriptor, 0o600)
            write_all_and_sync(descriptor, data)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != len(data):
                raise OSError("invalid prepared index")
            prepared_snapshot = (
                data,
                _index_publication_revision(info),
            )
            os.close(descriptor)
            descriptor = None
            return name, prepared_snapshot
        except FileExistsError:
            continue
        except OSError as exc:
            failed_snapshot = None
            if descriptor is not None:
                try:
                    observed_snapshot = _snapshot_index_temporary_descriptor(
                        descriptor,
                        len(data),
                    )
                    if (
                        observed_snapshot is not None
                        and data.startswith(observed_snapshot[0])
                    ):
                        failed_snapshot = observed_snapshot
                except OSError:
                    pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                descriptor = None
            if failed_snapshot is not None:
                _cleanup_index_temporaries(
                    directory_descriptor,
                    {name: failed_snapshot},
                )
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: cannot prepare index".format(
                    project
                )
            ) from exc
    raise KnowledgeIndexError(
        "KNOWLEDGE_INDEX_INVALID {}: cannot prepare index".format(project)
    )


def _publish_index_temporary(
    directory_descriptor: int,
    project: str,
    temporary_name: str,
    temporary_snapshot: tuple[bytes, tuple[int, ...]],
    output_name: str,
) -> None:
    from .render import (
        _remove_entry_if_unchanged,
        _rename_entry_no_replace,
        _snapshot_output_entry,
    )

    def restore_staged(staged_name: str) -> None:
        try:
            _rename_entry_no_replace(
                directory_descriptor,
                staged_name,
                output_name,
            )
        except OSError as exc:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: "
                "cannot preserve replaced index entry".format(project)
            ) from exc

    if (
        _legacy_artifact_snapshot_at(
            directory_descriptor,
            temporary_name,
        )
        != temporary_snapshot
    ):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: "
            "prepared index changed during publish".format(project)
        )

    try:
        existing = _snapshot_output_entry(
            directory_descriptor,
            output_name,
        )
    except FileNotFoundError:
        existing = None
    except (OSError, ValueError) as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: "
            "cannot inspect index entry".format(project)
        ) from exc

    if existing is not None and not (
        stat.S_ISREG(existing.revision[2])
        or stat.S_ISLNK(existing.revision[2])
    ):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: unknown index entry".format(project)
        )

    staged_name = None
    if existing is not None:
        for _ in range(32):
            candidate = ".index-preserved-output-{}".format(
                secrets.token_hex(12)
            )
            try:
                _rename_entry_no_replace(
                    directory_descriptor,
                    output_name,
                    candidate,
                )
            except FileExistsError:
                continue
            except OSError as exc:
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID {}: "
                    "index entry changed during publish".format(project)
                ) from exc
            staged_name = candidate
            break
        if staged_name is None:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: "
                "cannot stage replaced index entry".format(project)
            )
        try:
            moved = _snapshot_output_entry(
                directory_descriptor,
                staged_name,
            )
        except (OSError, ValueError) as exc:
            try:
                restore_staged(staged_name)
            except KnowledgeIndexError as restore_error:
                raise restore_error from exc
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: "
                "cannot inspect replaced index entry".format(project)
            ) from exc
        if moved != existing:
            restore_staged(staged_name)
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: "
                "index entry changed during publish".format(project)
            )

    if (
        _legacy_artifact_snapshot_at(
            directory_descriptor,
            temporary_name,
        )
        != temporary_snapshot
    ):
        if staged_name is not None:
            restore_staged(staged_name)
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: "
            "prepared index changed during publish".format(project)
        )

    try:
        _rename_entry_no_replace(
            directory_descriptor,
            temporary_name,
            output_name,
        )
    except OSError as exc:
        if staged_name is not None:
            try:
                restore_staged(staged_name)
            except KnowledgeIndexError as restore_error:
                raise restore_error from exc
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: cannot replace index".format(project)
        ) from exc

    if (
        _legacy_artifact_snapshot_at(
            directory_descriptor,
            output_name,
        )
        != temporary_snapshot
    ):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: "
            "published index changed during publish".format(project)
        )

    if staged_name is not None and not _remove_entry_if_unchanged(
        directory_descriptor,
        staged_name,
        existing,
    ):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: "
            "replaced index entry changed during publish".format(project)
        )

def _write_all_locked(outputs=None, data_root=None, target=None) -> Path:
    """잠금을 보유한 호출자가 전체 인덱스를 한 방향으로 다시 만든다."""
    if outputs is None:
        built, source_projects = _build_all_with_projects(data_root)
    else:
        built = outputs
        source_projects = ()
    normalized = _normalized_outputs(built)
    destination = Path(target) if target is not None else index_dir()
    expected = _expected_names(normalized)
    directory_descriptor = None
    try:
        try:
            initial_info = destination.lstat()
        except FileNotFoundError:
            directory_descriptor, identity = (
                _create_index_directory_no_replace(destination)
            )
        else:
            if not stat.S_ISDIR(initial_info.st_mode):
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID: "
                    "index must be a real directory"
                )
            identity = _index_directory_identity(initial_info)
            directory_descriptor = open_directory_path(destination)
            opened_identity = _index_directory_identity(
                os.fstat(directory_descriptor)
            )
            if opened_identity != identity:
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID: "
                    "index directory changed while opening"
                )
    except KnowledgeIndexError:
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
        raise
    except (OSError, UnsafePathError) as exc:
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID: cannot create or open index directory"
        ) from exc
    prepared: dict[
        str,
        tuple[str, tuple[bytes, tuple[int, ...]]],
    ] = {}
    try:
        cleanup_artifacts = {}
        stale_snapshots = {}
        _validate_index_directory(
            directory_descriptor,
            expected,
            cleanup_artifacts=cleanup_artifacts,
            stale_snapshots=stale_snapshots,
        )
        _require_index_directory_unchanged(destination, identity)
        _cleanup_owned_legacy_index_artifacts(
            directory_descriptor,
            cleanup_artifacts,
        )
        _require_index_directory_unchanged(destination, identity)
        output_bytes = {
            project: text.encode("utf-8")
            for project, text in normalized.items()
        }
        try:
            for project in sorted(output_bytes, key=_byte_key):
                prepared[project] = _prepare_index_temporary(
                    directory_descriptor,
                    project,
                    output_bytes[project],
                )

            _require_projects_unchanged(source_projects)
            _require_index_directory_unchanged(destination, identity)

            for project in sorted(prepared, key=_byte_key):
                temporary_name, temporary_snapshot = prepared[project]
                _publish_index_temporary(
                    directory_descriptor,
                    project,
                    temporary_name,
                    temporary_snapshot,
                    project + ".md",
                )
                del prepared[project]

            try:
                os.fsync(directory_descriptor)
            except OSError as exc:
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID: cannot persist index"
                ) from exc
            _require_index_directory_unchanged(destination, identity)
            _require_projects_unchanged(source_projects)
            _require_index_directory_unchanged(destination, identity)
            _cleanup_owned_legacy_index_artifacts(
                directory_descriptor,
                stale_snapshots,
            )
            _require_index_directory_unchanged(destination, identity)
        finally:
            _cleanup_index_temporaries(
                directory_descriptor,
                {
                    temporary_name: snapshot
                    for temporary_name, snapshot in prepared.values()
                },
            )
        return destination
    finally:
        os.close(directory_descriptor)


def write_all(outputs=None, data_root=None, target=None) -> Path:
    """하나의 개인 지식 snapshot에서 전체 인덱스를 교체한다."""
    root = _domain_root(data_root, target)
    with path_lock(root):
        return _write_all_locked(outputs, data_root, target)


def _check_locked(data_root=None, target=None) -> int:
    """잠금을 보유한 호출자가 현재 개인 인덱스를 검사한다."""
    destination = Path(target) if target is not None else index_dir()
    try:
        state = inspect_index(build_all(data_root), destination)
        if state is not IndexCheckState.CURRENT:
            raise KnowledgeIndexError(
                "index is {}".format(state.value)
            )
    except (KnowledgeIndexError, OSError, UnicodeDecodeError) as exc:
        print(
            "KNOWLEDGE_INDEX_STALE: {}. 재생성 가능한 파생 파일이며 원본은 "
            "lessons/docs/book입니다.".format(exc),
            file=sys.stderr,
        )
        return 1
    return 0


def check(data_root=None, target=None) -> int:
    """공유 잠금으로 일관된 개인 지식 snapshot을 검사한다."""
    root = _domain_root(data_root, target)
    with path_lock(root, shared=True):
        return _check_locked(data_root, target)
