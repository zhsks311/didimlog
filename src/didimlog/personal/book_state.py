"""프로젝트 lesson의 book 후보와 반영 상태를 관리한다."""

from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path
import stat
from didimlog import file_io
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
from .paths import (
    ProjectDirectory,
    ProjectDirectoryError,
    lessons_dir,
    project_directory_unchanged,
    resolve_project,
    resolve_project_directory,
)


def _project_lessons(project, root, cwd) -> ProjectDirectory | None:
    selected = resolve_project(project, cwd=cwd)
    base = lessons_dir() if root is None else Path(root)
    return resolve_project_directory(base, selected)


def _read_regular_file(path: Path) -> bytes | None:
    """심볼릭 링크와 파일 교체 경쟁을 따라가지 않고 일반 파일만 읽는다."""
    try:
        data = file_io.read_regular_file_beneath(
            path.parent,
            path.name,
            LESSON_MAX_BYTES,
        )
    except (file_io.UnsafePathError, ValueError):
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


def _lessons(directory: ProjectDirectory):
    try:
        paths = sorted(
            directory.physical.glob("*.md"),
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
                "path": str(directory.logical / path.name),
                "topic": topic,
                "title": fields.get("title", ""),
                "summary": fields.get("summary", ""),
                "date": fields.get("date", ""),
            }
        )

    rows.sort(key=lambda row: row["id"].encode("utf-8"))
    rows.sort(key=lambda row: row["date"], reverse=True)
    if not project_directory_unchanged(directory):
        raise ProjectDirectoryError(
            directory.logical,
            "project link changed during operation",
        )
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


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _file_revision(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _mark_booked_locked(values, directory: ProjectDirectory):
    selected = []
    skipped = []
    valid = []
    for slug in values:
        if not isinstance(slug, str) or SLUG.fullmatch(slug) is None:
            skipped.append(slug)
            continue
        valid.append(slug)

    if not valid:
        return {"marked": [], "skipped": skipped}

    descriptor: int | None = None
    try:
        if not project_directory_unchanged(directory):
            skipped.extend(valid)
            return {"marked": [], "skipped": skipped}
        try:
            descriptor = file_io.open_directory_path(directory.physical)
            if (
                _directory_identity(os.fstat(descriptor))
                != directory.target_identity
                or not project_directory_unchanged(directory)
            ):
                skipped.extend(valid)
                return {"marked": [], "skipped": skipped}
        except (OSError, file_io.UnsafePathError):
            skipped.extend(valid)
            return {"marked": [], "skipped": skipped}

        for slug in valid:
            if not project_directory_unchanged(directory):
                skipped.append(slug)
                continue
            try:
                name = f"{slug}.md"
                entry = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
                    skipped.append(slug)
                    continue
                data = file_io.read_regular_file_at(
                    descriptor,
                    name,
                    LESSON_MAX_BYTES,
                )
                if len(data) > LESSON_MAX_BYTES:
                    skipped.append(slug)
                    continue
                parsed = _parse_lesson(directory.physical / name, data)
                if parsed is None:
                    skipped.append(slug)
                    continue
                selected.append((slug, name, entry, data, parsed))
            except (OSError, file_io.UnsafePathError):
                skipped.append(slug)

        marked = []
        for slug, name, entry, data, parsed in selected:
            if not project_directory_unchanged(directory):
                skipped.append(slug)
                continue
            try:
                current = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
                if (
                    stat.S_ISLNK(current.st_mode)
                    or not stat.S_ISREG(current.st_mode)
                    or _file_revision(current) != _file_revision(entry)
                ):
                    skipped.append(slug)
                    continue
                replacement = _booked_bytes(data, parsed)
                if (
                    replacement != data
                    and not file_io.replace_regular_file_at_if_unchanged(
                        descriptor,
                        name,
                        data,
                        replacement,
                        stat.S_IMODE(entry.st_mode),
                        expected_info=entry,
                    )
                ):
                    skipped.append(slug)
                    continue
            except (OSError, file_io.UnsafePathError):
                skipped.append(slug)
                continue
            marked.append(str(directory.logical / name))
        return {"marked": marked, "skipped": skipped}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def mark_booked(slugs: Iterable[str], project=None, root=None, cwd=None):
    """선택한 lesson의 자체 topic을 canonical ``booked`` 값에 기록한다."""
    values = list(slugs)
    directory = _project_lessons(project, root, cwd)
    if directory is None:
        return {"marked": [], "skipped": values}

    with path_lock(directory.logical.parent.parent):
        return _mark_booked_locked(values, directory)
