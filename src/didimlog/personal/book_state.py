"""프로젝트 lesson의 book 후보와 반영 상태를 관리한다."""

from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path
import stat
import tempfile
from didimlog.locking import acquire_directory_lock


from .lesson import (
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
        entry = path.lstat()
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != entry.st_dev
            or opened.st_ino != entry.st_ino
        ):
            return None
        chunks = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.extend(chunk)
        return bytes(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)


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


def _replace_regular_file(path: Path, original: bytes, replacement: bytes) -> bool:
    if replacement == original:
        return True

    parent_descriptor: int | None = None
    lock_descriptor: int | None = None
    temporary: Path | None = None
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        lock_descriptor = acquire_directory_lock(parent_descriptor)
        entry = path.lstat()
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            return False
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), stat.S_IMODE(entry.st_mode))
            handle.write(replacement)
            handle.flush()
            os.fsync(handle.fileno())

        # The directory lock serializes Didimlog writers; this final comparison
        # also rejects edits made by other programs before replacement.
        if _read_regular_file(path) != original:
            return False
        os.replace(temporary, path)
        temporary = None
        os.fsync(parent_descriptor)
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        if lock_descriptor is not None:
            os.close(lock_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def mark_booked(slugs: Iterable[str], project=None, root=None, cwd=None):
    """선택한 lesson의 자체 topic을 canonical ``booked`` 값에 기록한다."""
    values = list(slugs)
    directory = _project_lessons(project, root, cwd)
    if directory is None:
        return {"marked": [], "skipped": values}

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
        if not _replace_regular_file(path, data, replacement):
            skipped.append(slug)
            continue
        marked.append(str(path))
    return {"marked": marked, "skipped": skipped}
