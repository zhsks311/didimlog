"""lesson·docs·book에서 프로젝트별 개인 지식 목록을 생성한다."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Mapping
from didimlog.file_io import (
    UnsafePathError,
    open_directory_path,
    read_regular_file_beneath,
)
from didimlog.locking import path_lock

from .lesson import (
    parse_frontmatter_text,
    parse_inline_list,
    parse_lesson,
    valid_index_title,
)
from .paths import index_dir, lessons_dir, validate_project


SECTIONS = (
    ("lesson", "작업 규칙"),
    ("docs", "작업 문서"),
    ("book", "해설 자료"),
)
GENERATED_NOTICE = "<!-- Didimlog Personal Knowledge가 자동 생성한다. 직접 수정하지 마라. -->"
SOURCE_MAX_BYTES = 4 * 1024 * 1024


class KnowledgeIndexError(ValueError):
    """지식 원본이나 생성 인덱스가 안전한 생성 계약을 위반했다."""


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


def _detail_path(path: Path, data_root: Path) -> str:
    try:
        relative = path.relative_to(data_root).as_posix()
    except ValueError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: path escapes knowledge root".format(path)
        ) from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in relative):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: path contains a control character".format(path)
        )
    return relative


def _validate_real_directory(path: Path, label: str) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: cannot inspect {}".format(path, label)
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: {} must be a real directory".format(path, label)
        )
    return True


def _project_directories(root: Path) -> dict[str, Path]:
    if not _validate_real_directory(root, root.name):
        return {}
    result = {}
    try:
        entries = root.iterdir()
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID {}: project entry must be a real directory".format(
                        entry
                    )
                )
            try:
                validate_project(entry.name, allow_global=True)
            except ValueError as exc:
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID {}: {}".format(entry, exc)
                ) from exc
            result[entry.name] = entry
    except OSError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: cannot inspect source directory".format(root)
        ) from exc
    return result


def _validate_source_tree(root: Path) -> None:
    try:
        for entry in root.iterdir():
            if entry.is_symlink():
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID {}: source symlinks are not allowed".format(
                        entry
                    )
                )
            if entry.is_dir():
                _validate_source_tree(entry)
            elif not entry.is_file():
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID {}: source must be a regular file".format(
                        entry
                    )
                )
    except OSError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: cannot inspect source tree".format(root)
        ) from exc


def _validate_nonrecursive_layout(
    root: Path, allowed_directories: tuple[str, ...] = ()
) -> None:
    for entry in root.iterdir():
        if entry.is_dir() and entry.name not in allowed_directories:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: nested source directories are not allowed".format(
                    entry
                )
            )


def _lesson_item(path: Path, data_root: Path) -> dict[str, object]:
    parsed = parse_lesson(path, data_root)
    if parsed is None:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: invalid lesson metadata".format(path)
        )
    fields, _, _ = parsed
    tags = parse_inline_list(fields.get("tags", "[]"), unique=False)
    if tags is None:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: invalid tags".format(path)
        )
    find_when = sorted(set([fields["topic"], *tags]), key=_byte_key)
    if not find_when:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: find_when is empty".format(path)
        )
    return {
        "kind": "lesson",
        "title": fields["title"],
        "find_when": find_when,
        "path": _detail_path(path, data_root),
        "review_by": fields.get("review_by"),
    }


def _document_item(path: Path, kind: str, data_root: Path) -> dict[str, object]:
    try:
        relative = path.relative_to(data_root)
        data = read_regular_file_beneath(data_root, relative, SOURCE_MAX_BYTES)
        if len(data) > SOURCE_MAX_BYTES:
            raise UnsafePathError("source exceeds read limit")
        text = data.decode("utf-8")
    except (UnsafePathError, UnicodeDecodeError, ValueError) as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: unreadable Markdown".format(path)
        ) from exc
    parsed = parse_frontmatter_text(path.name, text, ("title", "find_when"))
    if parsed is None:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: missing title or find_when".format(path)
        )
    fields, _, _ = parsed
    title = fields["title"]
    if not valid_index_title(title):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: invalid title".format(path)
        )
    find_when = parse_inline_list(fields["find_when"], canonical=True)
    if not find_when:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: find_when is empty or not canonical".format(path)
        )
    return {
        "kind": kind,
        "title": title,
        "find_when": find_when,
        "path": _detail_path(path, data_root),
        "review_by": None,
    }


def _markdown_files(
    project_dir: Path, *, recursive: bool, ignored: tuple[str, ...] = ()
) -> list[Path]:
    iterator = project_dir.rglob("*.md") if recursive else project_dir.glob("*.md")
    files = []
    for path in iterator:
        relative = path.relative_to(project_dir)
        if relative.parts and relative.parts[0] in ignored:
            continue
        if path.is_symlink() or not path.is_file():
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: source must be a regular file".format(path)
            )
        try:
            resolved = path.resolve(strict=True)
            project_root = project_dir.resolve(strict=True)
        except OSError as exc:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: cannot resolve source path".format(path)
            ) from exc
        if not resolved.is_relative_to(project_root):
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: path escapes project".format(path)
            )
        files.append(path)
    return sorted(
        files,
        key=lambda path: _byte_key(path.relative_to(project_dir).as_posix()),
    )


def collect(data_root=None) -> dict[str, list[dict[str, object]]]:
    """모든 원본을 엄격히 검사하고 프로젝트별 인덱스 항목을 반환한다."""
    root = Path(data_root) if data_root is not None else lessons_dir().parent
    _validate_real_directory(root, "knowledge root")
    roots = {
        "lesson": root / "lessons",
        "docs": root / "docs",
        "book": root / "book",
    }
    directories = {
        kind: _project_directories(source_root)
        for kind, source_root in roots.items()
    }
    for kind, mapping in directories.items():
        for project_dir in mapping.values():
            _validate_source_tree(project_dir)
            if kind == "lesson":
                _validate_nonrecursive_layout(project_dir)
            elif kind == "book":
                _validate_nonrecursive_layout(
                    project_dir, allowed_directories=("assets", "html")
                )

    projects = sorted(
        set().union(*(mapping.keys() for mapping in directories.values())),
        key=_byte_key,
    )
    collected = {}
    for project in projects:
        items = []
        lesson_project = directories["lesson"].get(project)
        if lesson_project is not None:
            for path in _markdown_files(lesson_project, recursive=False):
                items.append(_lesson_item(path, root))
        docs_project = directories["docs"].get(project)
        if docs_project is not None:
            for path in _markdown_files(docs_project, recursive=True):
                items.append(_document_item(path, "docs", root))
        book_project = directories["book"].get(project)
        if book_project is not None:
            for path in _markdown_files(
                book_project,
                recursive=False,
                ignored=("assets", "html"),
            ):
                items.append(_document_item(path, "book", root))
        collected[project] = items
    return collected


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


def build_all(data_root=None) -> dict[str, str]:
    """현재 전체 원본에 대응하는 프로젝트별 출력 문자열을 만든다."""
    return {
        project: render_project(project, items)
        for project, items in collect(data_root).items()
    }


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


def _validate_index_directory(target: Path, expected: set[str]) -> None:
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: index must be a real directory".format(target)
        )
    if not target.exists():
        return
    try:
        entries = tuple(target.iterdir())
    except OSError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: cannot inspect index".format(target)
        ) from exc
    for entry in entries:
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".md":
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: unknown index entry".format(entry)
            )
        if entry.name in expected:
            continue
        try:
            validate_project(entry.stem, allow_global=True)
            notice_limit = len(GENERATED_NOTICE.encode("utf-8")) + 2
            data = read_regular_file_beneath(
                target,
                entry.name,
                notice_limit,
            )
            first_line = (
                data.decode("utf-8")
                .split("\n", 1)[0]
                .removesuffix("\r")
            )
        except (ValueError, UnsafePathError, UnicodeDecodeError) as exc:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: unknown index entry".format(entry)
            ) from exc
        if first_line != GENERATED_NOTICE:
            raise KnowledgeIndexError(
                "KNOWLEDGE_INDEX_INVALID {}: index entry is not generator-owned".format(
                    entry
                )
            )


def _domain_root(data_root=None, target=None) -> Path:
    if data_root is not None:
        return Path(data_root)
    if target is not None:
        return Path(target).parent
    return lessons_dir().parent


def _write_all_locked(outputs=None, data_root=None, target=None) -> Path:
    """잠금을 보유한 호출자가 전체 인덱스를 원자적으로 교체한다."""
    built = build_all(data_root) if outputs is None else outputs
    normalized = _normalized_outputs(built)
    destination = Path(target) if target is not None else index_dir()
    expected = _expected_names(normalized)
    _validate_index_directory(destination, expected)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise KnowledgeIndexError(
            "KNOWLEDGE_INDEX_INVALID {}: cannot create index directory".format(
                destination
            )
        ) from exc
    _validate_index_directory(destination, expected)

    prepared: dict[str, str | None] = {}
    try:
        for project, text in normalized.items():
            descriptor = None
            try:
                descriptor, temporary = tempfile.mkstemp(
                    prefix=".index-", suffix=".tmp", dir=destination
                )
                prepared[project] = temporary
                os.fchmod(descriptor, 0o600)
                handle = os.fdopen(
                    descriptor, "w", encoding="utf-8", newline="\n"
                )
                descriptor = None
                with handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                if descriptor is not None:
                    os.close(descriptor)
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID {}: cannot prepare index".format(project)
                ) from exc

        for project in sorted(prepared, key=_byte_key):
            temporary = prepared[project]
            if temporary is None:
                continue
            try:
                os.replace(temporary, destination / (project + ".md"))
            except OSError as exc:
                raise KnowledgeIndexError(
                    "KNOWLEDGE_INDEX_INVALID {}: cannot replace index".format(project)
                ) from exc
            prepared[project] = None

        _validate_index_directory(destination, expected)
        for entry in destination.iterdir():
            if entry.suffix == ".md" and entry.name not in expected:
                try:
                    entry.unlink()
                except OSError as exc:
                    raise KnowledgeIndexError(
                        "KNOWLEDGE_INDEX_INVALID {}: cannot remove stale index".format(
                            entry
                        )
                    ) from exc
        directory_descriptor = open_directory_path(destination)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        for temporary in prepared.values():
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
    return destination


def write_all(outputs=None, data_root=None, target=None) -> Path:
    """하나의 개인 지식 snapshot에서 전체 인덱스를 교체한다."""
    root = _domain_root(data_root, target)
    with path_lock(root):
        return _write_all_locked(outputs, data_root, target)


def _check_locked(data_root=None, target=None) -> int:
    """잠금을 보유한 호출자가 현재 개인 인덱스를 검사한다."""
    destination = Path(target) if target is not None else index_dir()
    try:
        outputs = _normalized_outputs(build_all(data_root))
        expected = _expected_names(outputs)
        _validate_index_directory(destination, expected)
        if not destination.is_dir():
            raise KnowledgeIndexError("missing index directory")
        actual = {entry.name for entry in destination.iterdir()}
        if actual != expected:
            raise KnowledgeIndexError("index file set is out of date")
        for project, text in outputs.items():
            expected_bytes = text.encode("utf-8")
            if (
                read_regular_file_beneath(
                    destination,
                    project + ".md",
                    len(expected_bytes),
                )
                != expected_bytes
            ):
                raise KnowledgeIndexError("{} is out of date".format(project))
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
