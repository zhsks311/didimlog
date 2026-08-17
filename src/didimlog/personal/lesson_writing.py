"""검증된 lesson을 프로젝트 디렉터리에 create-only로 저장한다."""

from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path
import secrets
import stat
import sys

from didimlog.file_io import (
    open_directory_path,
    read_regular_file_at_with_stat,
)
from didimlog.locking import path_lock
from .lesson import SLUG, parse_lesson_text
from .paths import (
    ProjectDirectory,
    ProjectDirectoryError,
    lessons_dir,
    project_directory_unchanged,
    resolve_project,
    resolve_project_directory,
)


MAX_INPUT_BYTES = 64 * 1024


class LessonInvalid(ValueError):
    pass


class LessonExists(FileExistsError):
    pass


class LessonError(OSError):
    pass


class LessonSecret(ValueError):
    pass


def _reject_secrets(data: bytes) -> None:
    from .secret_scan import scan_bytes

    labels = scan_bytes(data)
    if labels:
        raise LessonSecret(
            "lesson may contain secrets: " + ", ".join(sorted(set(labels)))
        )


def _encode_input(text: str) -> bytes:
    if not isinstance(text, str):
        raise LessonInvalid("lesson must be text")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise LessonInvalid("lesson must be valid UTF-8 text") from error
    if len(data) > MAX_INPUT_BYTES:
        raise LessonInvalid("lesson exceeds {} bytes".format(MAX_INPUT_BYTES))
    return data


def _open_real_directory(path: Path, message: str) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    absolute = Path(os.path.abspath(path))
    anchor = absolute.parent.parent
    descriptor: int | None = None
    try:
        anchor_linked = anchor.lstat()
        descriptor = os.open(anchor, flags)
        anchor_opened = os.fstat(descriptor)
        if (
            stat.S_ISLNK(anchor_linked.st_mode)
            or not stat.S_ISDIR(anchor_opened.st_mode)
            or anchor_opened.st_dev != anchor_linked.st_dev
            or anchor_opened.st_ino != anchor_linked.st_ino
        ):
            raise LessonInvalid(message)
        for part in absolute.relative_to(anchor).parts:
            linked = os.stat(
                part,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            child = os.open(part, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if (
                stat.S_ISLNK(linked.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or opened.st_dev != linked.st_dev
                or opened.st_ino != linked.st_ino
            ):
                os.close(child)
                raise LessonInvalid(message)
            os.close(descriptor)
            descriptor = child
    except LessonInvalid:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, ValueError) as error:
        if descriptor is not None:
            os.close(descriptor)
        raise LessonInvalid(message) from error
    if descriptor is None:
        raise LessonInvalid(message)
    return descriptor


def _open_project_directory(
    base: Path,
    base_descriptor: int,
    project: str,
) -> tuple[int, ProjectDirectory]:
    message = "project lessons directory must be a real directory"
    try:
        resolved = resolve_project_directory(base, project)
    except ProjectDirectoryError as error:
        raise LessonInvalid(message) from error

    if resolved is None:
        try:
            os.mkdir(project, 0o700, dir_fd=base_descriptor)
        except FileExistsError:
            pass
        except OSError as error:
            raise LessonError(
                "unable to create project lessons directory"
            ) from error
        try:
            resolved = resolve_project_directory(base, project)
        except ProjectDirectoryError as error:
            raise LessonInvalid(message) from error
        if resolved is None:
            raise LessonInvalid(message)

    if resolved.physical != resolved.logical:
        descriptor: int | None = None
        try:
            linked = os.stat(
                project,
                dir_fd=base_descriptor,
                follow_symlinks=False,
            )
            descriptor = open_directory_path(resolved.physical)
            opened = os.fstat(descriptor)
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            raise LessonInvalid(message) from error
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
        )
        linked_identity = (
            linked.st_dev,
            linked.st_ino,
            stat.S_IFMT(linked.st_mode),
        )
        if (
            not stat.S_ISLNK(linked.st_mode)
            or linked_identity != resolved.entry_identity
            or not stat.S_ISDIR(opened.st_mode)
            or opened_identity != resolved.target_identity
        ):
            os.close(descriptor)
            raise LessonInvalid(message)
        return descriptor, resolved

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(project, flags, dir_fd=base_descriptor)
        opened = os.fstat(descriptor)
        linked = os.stat(project, dir_fd=base_descriptor, follow_symlinks=False)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise LessonInvalid(message) from error
    opened_identity = (
        opened.st_dev,
        opened.st_ino,
        stat.S_IFMT(opened.st_mode),
    )
    linked_identity = (
        linked.st_dev,
        linked.st_ino,
        stat.S_IFMT(linked.st_mode),
    )
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(linked.st_mode)
        or opened_identity != linked_identity
        or linked_identity != resolved.entry_identity
        or opened_identity != resolved.target_identity
    ):
        os.close(descriptor)
        raise LessonInvalid(message)
    return descriptor, resolved


def _temporary_file(directory_descriptor: int) -> tuple[str, int]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    for _ in range(32):
        name = ".lesson-" + secrets.token_hex(12) + ".tmp"
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=directory_descriptor,
            )
            os.fchmod(descriptor, 0o600)
            return name, descriptor
        except FileExistsError:
            continue
        except OSError as error:
            raise LessonError("unable to create lesson temporary file") from error
    raise LessonError("unable to allocate lesson temporary file")


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]
    os.fsync(descriptor)


def _publication_revision(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
    )


def _published_lesson_unchanged(
    directory_descriptor: int,
    name: str,
    data: bytes,
    published_revision: tuple[int, ...],
) -> bool:
    try:
        published, published_info = read_regular_file_at_with_stat(
            directory_descriptor,
            name,
            len(data),
        )
    except OSError:
        return False
    return (
        published == data
        and _publication_revision(published_info) == published_revision
    )


def _rollback_lesson_publication(
    directory_descriptor: int,
    name: str,
    data: bytes,
    published_revision: tuple[int, ...],
) -> bool:
    recovery_name: str | None = None
    keep_recovery = False
    try:
        try:
            recovery_name, recovery_descriptor = _temporary_file(
                directory_descriptor
            )
        except (LessonError, OSError):
            return False
        os.close(recovery_descriptor)

        try:
            os.rename(
                name,
                recovery_name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return True
        except OSError:
            return False

        if _published_lesson_unchanged(
            directory_descriptor,
            recovery_name,
            data,
            published_revision,
        ):
            return True

        try:
            os.link(
                recovery_name,
                name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            keep_recovery = True
            return False
        except OSError:
            keep_recovery = True
            return False
        return True
    finally:
        if recovery_name is not None and not keep_recovery:
            try:
                os.unlink(recovery_name, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass


def _refresh_index(base: Path) -> None:
    try:
        from . import index

        data_root = base.parent
        index._write_all_locked(
            data_root=data_root,
            target=data_root / "index",
        )
    except Exception:
        print("KNOWLEDGE_INDEX_STALE: run didim index", file=sys.stderr)


def _publish_lesson_locked(
    base: Path,
    selected: str,
    name: str,
    data: bytes,
) -> Path:
    base_descriptor = _open_real_directory(
        base,
        "lessons directory must be a real directory",
    )
    project_descriptor: int | None = None
    project_directory: ProjectDirectory | None = None
    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    published_revision: tuple[int, ...] | None = None
    try:
        project_descriptor, project_directory = _open_project_directory(
            base,
            base_descriptor,
            selected,
        )
        temporary_name, temporary_descriptor = _temporary_file(project_descriptor)
        _write_all(temporary_descriptor, data)
        published_revision = _publication_revision(
            os.fstat(temporary_descriptor)
        )
        if (
            project_directory is None
            or not project_directory_unchanged(project_directory)
        ):
            raise LessonInvalid("project lessons link changed during write")
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=project_descriptor,
                dst_dir_fd=project_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise LessonExists(
                (Path(base.name) / selected / name).as_posix()
            ) from error
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise LessonExists(
                    (Path(base.name) / selected / name).as_posix()
                ) from error
            raise LessonError("unable to publish lesson") from error
        project_unchanged = (
            project_directory is not None
            and project_directory_unchanged(project_directory)
        )
        publication_unchanged = (
            published_revision is not None
            and _published_lesson_unchanged(
                project_descriptor,
                name,
                data,
                published_revision,
            )
        )
        if not project_unchanged:
            rollback_succeeded = False
            if published_revision is not None:
                try:
                    rollback_succeeded = _rollback_lesson_publication(
                        project_descriptor,
                        name,
                        data,
                        published_revision,
                    )
                except OSError:
                    pass
            if rollback_succeeded:
                try:
                    os.fsync(project_descriptor)
                except OSError:
                    pass
            raise LessonInvalid("project lessons link changed during write")
        if not publication_unchanged:
            raise LessonInvalid("project lessons link changed during write")
        os.close(temporary_descriptor)
        temporary_descriptor = None
        os.unlink(temporary_name, dir_fd=project_descriptor)
        temporary_name = None
        os.fsync(project_descriptor)
    except (LessonInvalid, LessonExists, LessonError):
        raise
    except OSError as error:
        raise LessonError("unable to store lesson") from error
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None and project_descriptor is not None:
            try:
                os.unlink(temporary_name, dir_fd=project_descriptor)
            except OSError:
                pass
        if project_descriptor is not None:
            os.close(project_descriptor)
        os.close(base_descriptor)

    _refresh_index(base)
    return Path(base.name) / selected / name


def publish_lesson(slug, text, project=None, root=None, cwd=None) -> Path:
    """Validate and atomically create one lesson and its derived index."""
    if not isinstance(slug, str) or SLUG.fullmatch(slug) is None:
        raise LessonInvalid("slug must use letters, digits, and hyphens")
    try:
        selected = resolve_project(project, cwd=cwd, allow_global=True)
    except ValueError as error:
        raise LessonInvalid(str(error)) from error

    normalized = text.replace("\r\n", "\n").replace("\r", "\n") if isinstance(text, str) else text
    data = _encode_input(normalized)
    _reject_secrets(data)
    name = slug + ".md"
    if parse_lesson_text(name, normalized) is None:
        raise LessonInvalid("lesson does not satisfy the frontmatter contract")

    base = Path(lessons_dir() if root is None else root)
    lock = path_lock(base.parent)
    try:
        lock.__enter__()
    except OSError as error:
        raise LessonInvalid(
            "lessons parent must be a real directory"
        ) from error
    try:
        return _publish_lesson_locked(base, selected, name, data)
    finally:
        lock.__exit__(None, None, None)


def _read_stdin() -> str:
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise LessonInvalid("lesson exceeds {} bytes".format(MAX_INPUT_BYTES))
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise LessonInvalid("lesson must be valid UTF-8 text") from error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="검증된 lesson을 프로젝트별로 저장한다"
    )
    parser.add_argument("slug")
    parser.add_argument("--project")
    arguments = parser.parse_args(argv)
    try:
        relative = publish_lesson(
            arguments.slug,
            _read_stdin(),
            project=arguments.project,
        )
    except LessonSecret as error:
        print("LESSON_SECRET: {}".format(error), file=sys.stderr)
        return 5
    except LessonInvalid as error:
        print("LESSON_INVALID: {}".format(error), file=sys.stderr)
        return 2
    except LessonExists as error:
        print("LESSON_EXISTS: {}".format(error), file=sys.stderr)
        return 3
    except LessonError as error:
        print("LESSON_ERROR: {}".format(error), file=sys.stderr)
        return 4
    print(relative.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
