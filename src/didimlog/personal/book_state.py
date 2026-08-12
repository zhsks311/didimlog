"""프로젝트 lesson의 book 후보와 반영 상태를 관리한다."""

from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path
import stat
from didimlog.file_io import (
    UnsafePathError,
    read_regular_file_beneath,
    replace_regular_file_at_if_unchanged,
)
from didimlog.locking import path_lock


from .lesson import (
    LESSON_MAX_BYTES,
    REQUIRED,
    SLUG,
    parse_booked,
    parse_frontmatter_text,
    parse_inline_list,
    parse_lesson_text,
)
from .paths import lessons_dir, resolve_project


def _project_lessons(project, root, cwd) -> Path | None:
    selected = resolve_project(project, cwd=cwd)
    base = lessons_dir() if root is None else Path(root)
    directory = base / selected
    try:
        if base.is_symlink() or directory.is_symlink() or not directory.is_dir():
            return None
    except OSError:
        return None
    return directory


def _read_regular_file(path: Path) -> bytes | None:
    """심볼릭 링크와 파일 교체 경쟁을 따라가지 않고 일반 파일만 읽는다."""
    try:
        data = read_regular_file_beneath(path.parent, path.name, LESSON_MAX_BYTES)
    except (UnsafePathError, ValueError):
        return None
    return None if len(data) > LESSON_MAX_BYTES else data


def _parse_lesson(path: Path, data: bytes):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None

    parsed = parse_lesson_text(path.name, text)
    if parsed is not None:
        return parsed

    # parse_lesson_text intentionally returns None for every malformed lesson.
    # These two fields are state inputs, so distinguish their invalid forms to
    # prevent callers from silently treating corrupted book state as unbooked.
    frontmatter = parse_frontmatter_text(path.name, text, REQUIRED)
    if frontmatter is None:
        return None
    fields, _, _ = frontmatter
    topic = fields.get("topic", "")
    if SLUG.fullmatch(topic) is None:
        raise ValueError("lesson topic must be a portable slug")
    raw_booked = fields.get("booked")
    if raw_booked is not None:
        booked = parse_inline_list(raw_booked)
        if booked is None or any(SLUG.fullmatch(value) is None for value in booked):
            raise ValueError("lesson booked must be a unique inline list of topic slugs")
    return None


def _lessons(directory: Path):
    try:
        paths = sorted(
            directory.glob("*.md"),
            key=lambda item: item.name.encode("utf-8"),
        )
    except OSError:
        return
    for path in paths:
        data = _read_regular_file(path)
        if data is None:
            continue
        parsed = _parse_lesson(path, data)
        if parsed is not None:
            yield path, data, parsed


def candidates(project=None, root=None, cwd=None):
    """선택한 프로젝트에서 아직 자체 topic에 반영되지 않은 lesson을 반환한다."""
    directory = _project_lessons(project, root, cwd)
    if directory is None:
        return []

    rows = []
    for path, _, parsed in _lessons(directory):
        fields, _, _ = parsed
        topic = fields["topic"]
        if topic in parse_booked(fields.get("booked")):
            continue
        rows.append(
            {
                "id": path.stem,
                "path": str(path),
                "topic": topic,
                "title": fields.get("title", ""),
                "summary": fields.get("summary", ""),
                "date": fields.get("date", ""),
            }
        )

    rows.sort(key=lambda row: row["id"].encode("utf-8"))
    rows.sort(key=lambda row: row["date"], reverse=True)
    return rows


def _booked_bytes(data: bytes, parsed) -> bytes:
    fields, lines, closing = parsed
    topic = fields["topic"]
    booked = parse_booked(fields.get("booked"))
    canonical = sorted(
        set(booked) | {topic}, key=lambda value: value.encode("utf-8")
    )
    replacement = "booked: [{}]".format(", ".join(canonical))

    byte_lines = data.split(b"\n")
    for index in range(1, closing):
        if lines[index].startswith("booked:"):
            byte_lines[index] = replacement.encode("utf-8")
            break
    else:
        byte_lines.insert(closing, replacement.encode("utf-8"))
    return b"\n".join(byte_lines)


def _replace_regular_file_locked(path: Path, original: bytes, replacement: bytes) -> bool:
    if replacement == original:
        return True

    parent_descriptor: int | None = None
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        entry = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            return False
        return replace_regular_file_at_if_unchanged(
            parent_descriptor,
            path.name,
            original,
            replacement,
            stat.S_IMODE(entry.st_mode),
            expected_info=entry,
        )
    except (OSError, UnsafePathError):
        return False
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _mark_booked_locked(values, directory):
    selected = []
    skipped = []
    for slug in values:
        if not isinstance(slug, str) or SLUG.fullmatch(slug) is None:
            skipped.append(slug)
            continue
        path = directory / f"{slug}.md"
        data = _read_regular_file(path)
        if data is None:
            skipped.append(slug)
            continue
        parsed = _parse_lesson(path, data)
        if parsed is None:
            skipped.append(slug)
            continue
        selected.append((slug, path, data, parsed))

    marked = []
    for slug, path, data, parsed in selected:
        replacement = _booked_bytes(data, parsed)
        if not _replace_regular_file_locked(path, data, replacement):
            skipped.append(slug)
            continue
        marked.append(str(path))
    return {"marked": marked, "skipped": skipped}


def mark_booked(slugs: Iterable[str], project=None, root=None, cwd=None):
    """선택한 lesson의 자체 topic을 canonical ``booked`` 값에 기록한다."""
    values = list(slugs)
    directory = _project_lessons(project, root, cwd)
    if directory is None:
        return {"marked": [], "skipped": values}

    with path_lock(directory.parent.parent):
        return _mark_booked_locked(values, directory)
