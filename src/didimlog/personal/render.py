"""Render a project book Markdown source as self-contained HTML."""

from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import html
from html.parser import HTMLParser

from importlib import resources
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
import sys

import markdown
from markdown.extensions.fenced_code import FencedBlockPreprocessor
from markdown.inlinepatterns import AUTOLINK_RE, AUTOMAIL_RE, BACKTICK_RE, HTML_RE
from markdown.preprocessors import NormalizeWhitespace

from didimlog import file_io

from .lesson import parse_frontmatter_text
from .paths import (
    data_home,
    project_directory_unchanged,
    resolve_project_directory,
    validate_project,
)


_SLUG = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
_IMAGE = re.compile(r'<img\s+([^>]*?)src="([^"]+)"([^>]*)>', re.IGNORECASE)
_MERMAID_BLOCK = re.compile(
    r'<pre><code class="language-mermaid">(.*?)</code></pre>', re.DOTALL
)
_INLINE_CODE = re.compile(BACKTICK_RE, re.DOTALL | re.UNICODE)
_AUTOLINK = re.compile(AUTOLINK_RE, re.DOTALL | re.UNICODE)
_AUTOMAIL = re.compile(AUTOMAIL_RE, re.DOTALL | re.UNICODE)
_RAW_HTML = re.compile(HTML_RE, re.DOTALL | re.UNICODE)
_EXTERNAL_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_ALLOWED_LINK_SCHEMES = frozenset(("http", "https", "mailto"))
_MERMAID_SHA256 = "70137e77bb273bb2ef972b86e8b0400cca8be53cb25bfc45911a186dc98665de"


_STYLE = r"""
:root{color-scheme:light dark;--bg:#fbfaf7;--fg:#1d2523;--muted:#64706d;--line:#d8dedb;--card:#fff;--accent:#176b5b;--code:#eff3f1;--max:52rem}
*{box-sizing:border-box}html,body{max-width:100%;overflow-x:hidden}body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.75 system-ui,-apple-system,BlinkMacSystemFont,"Noto Sans KR",sans-serif}main{max-width:var(--max);margin:auto;padding:4rem 1.4rem 7rem}h1{font-size:clamp(2rem,6vw,3.6rem);line-height:1.12;letter-spacing:-.035em;margin:0 0 2rem}h2{font-size:1.65rem;line-height:1.3;margin:4rem 0 1rem;border-top:1px solid var(--line);padding-top:1.5rem}h3{margin-top:2.2rem}p,li,blockquote{overflow-wrap:anywhere}p,li{max-width:46rem}a{color:var(--accent)}blockquote{margin:1.6rem 0;padding:.7rem 1.2rem;border-left:4px solid var(--accent);background:color-mix(in srgb,var(--card) 86%,var(--accent));border-radius:0 .5rem .5rem 0}pre{max-width:100%;overflow:auto;padding:1rem;border-radius:.65rem;background:var(--code)}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}table{border-collapse:collapse;width:100%;max-width:100%;margin:1.5rem 0;display:block;overflow-x:auto}th,td{border:1px solid var(--line);padding:.6rem .8rem;text-align:left}th{background:var(--code)}img,svg{max-width:100%;height:auto}.mermaid{max-width:100%;margin:2rem 0;overflow-x:auto;text-align:center}.source{color:var(--muted);font-size:.82rem;margin-top:5rem;border-top:1px solid var(--line);padding-top:1rem}
@media(prefers-color-scheme:dark){:root{--bg:#101513;--fg:#e4ebe8;--muted:#a1ada8;--line:#34403b;--card:#18201d;--accent:#68c9b3;--code:#19231f}}
:root[data-theme="light"]{--bg:#fbfaf7;--fg:#1d2523;--muted:#64706d;--line:#d8dedb;--card:#fff;--accent:#176b5b;--code:#eff3f1}:root[data-theme="dark"]{--bg:#101513;--fg:#e4ebe8;--muted:#a1ada8;--line:#34403b;--card:#18201d;--accent:#68c9b3;--code:#19231f}
@media print{body{background:#fff;color:#000}main{padding:1cm;max-width:none}a::after{content:" (" attr(href) ")"}.source{color:#333}}
"""


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _validate_topic(topic: str) -> str:
    if _SLUG.fullmatch(topic) is None:
        raise ValueError("topic must use letters, digits, and hyphens")
    return topic


def _read_source(
    project_descriptor: int,
    name: str,
    logical_path: str,
) -> str:
    try:
        linked = os.stat(
            name,
            dir_fd=project_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        raise ValueError("book markdown missing: " + logical_path) from None
    except OSError:
        raise ValueError("book markdown must be a regular file") from None
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
        raise ValueError(
            "book markdown must be a regular file; symlinks are not allowed"
        )
    try:
        data = file_io.read_regular_file_at(
            project_descriptor,
            name,
            sys.maxsize,
        )
    except OSError:
        raise ValueError("book markdown must be a regular file") from None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("book markdown must be UTF-8") from None


def _image_relative_path(source_path: Path, image_source: str) -> Path:
    candidate = Path(os.path.normpath(os.fspath(source_path.parent / image_source)))
    if (
        candidate.is_absolute()
        or not candidate.parts
        or candidate.parts[0] == ".."
    ):
        raise ValueError("image escapes book directory: " + image_source)
    return candidate


def _read_image_at(
    project_descriptor: int,
    relative_path: Path,
    image_source: str,
) -> bytes:
    descriptor = os.dup(project_descriptor)
    try:
        for component in relative_path.parts[:-1]:
            child = file_io.open_child_directory(descriptor, component)
            os.close(descriptor)
            descriptor = child
        try:
            linked = os.stat(
                relative_path.name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            raise ValueError("image missing: " + image_source) from None
        except OSError:
            raise ValueError("image missing: " + image_source) from None
        if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
            raise ValueError(
                "image must be a regular file; symlinks are not allowed: "
                + image_source
            )
        try:
            return file_io.read_regular_file_at(
                descriptor,
                relative_path.name,
                sys.maxsize,
            )
        except OSError:
            raise ValueError("image missing: " + image_source) from None
    except ValueError:
        raise
    except OSError:
        raise ValueError(
            "image must be a regular file; symlinks are not allowed: "
            + image_source
        ) from None
    finally:
        os.close(descriptor)


def _remove_inline_code(source: str) -> str:
    return _INLINE_CODE.sub(
        lambda match: "" if match.group(3) is not None else match.group(0),
        source,
    )


def _reject_raw_html(source: str) -> None:
    parser = markdown.Markdown()
    normalized = "\n".join(NormalizeWhitespace(parser).run(source.split("\n")))
    visible = FencedBlockPreprocessor.FENCED_BLOCK_RE.sub("", normalized)
    visible = _remove_inline_code(visible)
    visible = _AUTOLINK.sub("", visible)
    visible = _AUTOMAIL.sub("", visible)
    visible = re.sub(r"(?<!\\)\\[<>]", "", visible)
    if _RAW_HTML.search(visible):
        raise ValueError("raw HTML is not allowed in book Markdown")


def _inline_images(
    rendered: str,
    source_path: Path,
    project_descriptor: int,
) -> str:
    def replace(match: re.Match[str]) -> str:
        before, rendered_source, after = match.groups()
        image_source = html.unescape(rendered_source)
        if _EXTERNAL_URI.match(image_source) or image_source.startswith("//"):
            raise ValueError("external image URL is not allowed: " + image_source)

        relative_path = _image_relative_path(source_path, image_source)
        data = _read_image_at(
            project_descriptor,
            relative_path,
            image_source,
        )
        mime = (
            mimetypes.guess_type(relative_path.name)[0]
            or "application/octet-stream"
        )
        encoded = base64.b64encode(data).decode("ascii")
        alt = "" if re.search(r"\balt=", before + after, re.IGNORECASE) else ' alt=""'
        return '<img {}src="data:{};base64,{}"{}{}>'.format(
            before,
            mime,
            encoded,
            alt,
            after,
        )

    return _IMAGE.sub(replace, rendered)


class _LinkValidator(HTMLParser):
    def handle_starttag(self, tag: str, attributes) -> None:
        if tag.lower() != "a":
            return
        for name, value in attributes:
            if name.lower() == "href" and value is not None:
                self._validate_href(value)

    def handle_startendtag(self, tag: str, attributes) -> None:
        self.handle_starttag(tag, attributes)

    @staticmethod
    def _validate_href(href: str) -> None:
        compact = re.sub(r"[\x00-\x20\x7f]+", "", href)
        if compact.startswith("//"):
            raise ValueError("unsafe link destination is not allowed: " + href)
        scheme = _EXTERNAL_URI.match(compact)
        if scheme is not None:
            name = compact[: compact.index(":")].lower()
            if name not in _ALLOWED_LINK_SCHEMES:
                raise ValueError("unsafe link destination is not allowed: " + href)


def _reject_unsafe_links(rendered: str) -> None:
    parser = _LinkValidator(convert_charrefs=True)
    parser.feed(rendered)
    parser.close()


def _load_mermaid() -> str:
    try:
        package = resources.files("didimlog.resources.personal")
        runtime = (package / "mermaid.min.js").read_bytes()
    except OSError as error:
        raise ValueError("vendored Mermaid runtime missing") from error
    if hashlib.sha256(runtime).hexdigest() != _MERMAID_SHA256:
        raise ValueError("vendored Mermaid runtime failed integrity check")
    try:
        return runtime.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("vendored Mermaid runtime missing") from error


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _entry_revision(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
    )

@dataclass(frozen=True)
class _EntrySnapshot:
    revision: tuple[int, ...]
    digest: bytes | None = None
    link_target: bytes | None = None

def _snapshot_regular_descriptor(descriptor: int) -> _EntrySnapshot:
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        raise ValueError("book output changed during render")
    revision = _entry_revision(opened)
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 64 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    if _entry_revision(os.fstat(descriptor)) != revision:
        raise ValueError("book output changed during render")
    return _EntrySnapshot(
        revision=revision,
        digest=digest.digest(),
    )


def _snapshot_output_entry(
    output_descriptor: int,
    name: str,
) -> _EntrySnapshot:
    linked = os.stat(
        name,
        dir_fd=output_descriptor,
        follow_symlinks=False,
    )
    revision = _entry_revision(linked)
    if stat.S_ISREG(linked.st_mode):
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(
            name,
            flags,
            dir_fd=output_descriptor,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _entry_revision(opened) != revision
            ):
                raise ValueError("book output changed during render")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            if _entry_revision(os.fstat(descriptor)) != revision:
                raise ValueError("book output changed during render")
            return _EntrySnapshot(
                revision=revision,
                digest=digest.digest(),
            )
        finally:
            os.close(descriptor)
    if stat.S_ISLNK(linked.st_mode):
        target = os.readlink(
            os.fsencode(name),
            dir_fd=output_descriptor,
        )
        if not isinstance(target, bytes):
            raise ValueError("book output changed during render")
        if (
            _entry_revision(
                os.stat(
                    name,
                    dir_fd=output_descriptor,
                    follow_symlinks=False,
                )
            )
            != revision
        ):
            raise ValueError("book output changed during render")
        return _EntrySnapshot(
            revision=revision,
            link_target=target,
        )
    return _EntrySnapshot(revision=revision)


def _project_directory_is_current(directory, descriptor: int) -> bool:
    try:
        opened_identity = _directory_identity(os.fstat(descriptor))
    except OSError:
        return False
    return (
        opened_identity == directory.target_identity
        and project_directory_unchanged(directory)
    )


def _prepare_output_directory(project_descriptor: int) -> int:
    try:
        linked = os.stat(
            "html",
            dir_fd=project_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        try:
            os.mkdir("html", 0o700, dir_fd=project_descriptor)
        except FileExistsError:
            pass
        except OSError as error:
            raise ValueError("book output directory could not be created") from error
        try:
            linked = os.stat(
                "html",
                dir_fd=project_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ValueError("book output directory must be a real directory") from error
    except OSError as error:
        raise ValueError("book output directory must be a real directory") from error

    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
        raise ValueError("book output directory must be a real directory")
    try:
        return file_io.open_child_directory(project_descriptor, "html")
    except OSError as error:
        raise ValueError("book output directory must be a real directory") from error


def _temporary_output(
    output_descriptor: int,
    suffix: str,
) -> tuple[str, int]:
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _ in range(32):
        name = ".render-{}{}".format(secrets.token_hex(12), suffix)
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=output_descriptor,
            )
        except FileExistsError:
            continue
        try:
            os.fchmod(descriptor, 0o600)
        except OSError as error:
            os.close(descriptor)
            try:
                os.unlink(name, dir_fd=output_descriptor)
            except OSError:
                pass
            raise ValueError("book output temporary file could not be created") from error
        return name, descriptor
    raise ValueError("book output temporary file could not be created")


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written
    os.fsync(descriptor)


def _entry_name_bytes(name: str) -> bytes:
    if (
        not isinstance(name, str)
        or name in ("", ".", "..")
        or "/" in name
        or "\x00" in name
    ):
        raise ValueError("unsafe render output entry")
    return os.fsencode(name)


def _rename_entry_no_replace(
    directory_descriptor: int,
    source_name: str,
    destination_name: str,
) -> None:
    source = _entry_name_bytes(source_name)
    destination = _entry_name_bytes(destination_name)
    if sys.platform == "darwin":
        symbol_name = "renameatx_np"
        flags = 0x4
    elif sys.platform.startswith("linux"):
        symbol_name = "renameat2"
        flags = 1
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace directory rename unavailable",
        )

    library = ctypes.CDLL(None, use_errno=True)
    try:
        rename = getattr(library, symbol_name)
    except AttributeError as error:
        raise OSError(
            errno.ENOSYS,
            "atomic no-replace directory rename unavailable",
        ) from error
    rename.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = rename(
        directory_descriptor,
        source,
        directory_descriptor,
        destination,
        flags,
    )
    if result == 0:
        return

    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination_name,
        )
    unavailable = {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EINVAL),
    }
    if error_number in unavailable:
        raise OSError(
            error_number,
            "atomic no-replace directory rename unavailable",
        )
    raise OSError(
        error_number,
        os.strerror(error_number),
        destination_name,
    )

def _probe_atomic_no_replace(output_descriptor: int) -> None:
    for _ in range(32):
        source_name, source_descriptor = _temporary_output(
            output_descriptor,
            ".probe",
        )
        os.close(source_descriptor)
        destination_name = ".render-{}.probe-target".format(
            secrets.token_hex(12)
        )
        try:
            _rename_entry_no_replace(
                output_descriptor,
                source_name,
                destination_name,
            )
        except FileExistsError:
            os.unlink(source_name, dir_fd=output_descriptor)
            continue
        except OSError:
            try:
                os.unlink(source_name, dir_fd=output_descriptor)
            except OSError:
                pass
            try:
                os.unlink(destination_name, dir_fd=output_descriptor)
            except OSError:
                pass
            raise
        os.unlink(destination_name, dir_fd=output_descriptor)
        return
    raise OSError(
        errno.EEXIST,
        "atomic no-replace probe name collision",
    )


def _restore_entry_no_clobber(
    output_descriptor: int,
    source_name: str,
    destination_name: str,
) -> bool:
    try:
        _rename_entry_no_replace(
            output_descriptor,
            source_name,
            destination_name,
        )
    except FileExistsError:
        return False
    return True


@dataclass
class _OutputPublication:
    name: str
    descriptor: int
    snapshot: _EntrySnapshot
    backup_name: str | None


def _replace_output(
    output_descriptor: int,
    output_name: str,
    document: str,
) -> _OutputPublication:
    existing: _EntrySnapshot | None
    try:
        existing = _snapshot_output_entry(
            output_descriptor,
            output_name,
        )
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise ValueError("book output could not be inspected") from error
    if existing is not None and stat.S_ISDIR(existing.revision[2]):
        raise ValueError("book output must not be a directory")

    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    backup_name: str | None = None
    backup_moved = False
    publication: _OutputPublication | None = None
    completed = False
    restore_error: OSError | None = None
    try:
        temporary_name, temporary_descriptor = _temporary_output(
            output_descriptor,
            ".tmp",
        )
        _write_all(temporary_descriptor, document.encode("utf-8"))
        publication_snapshot = _snapshot_regular_descriptor(
            temporary_descriptor
        )

        if existing is not None:
            _probe_atomic_no_replace(output_descriptor)
            backup_name, backup_descriptor = _temporary_output(
                output_descriptor,
                ".bak",
            )
            os.close(backup_descriptor)
            current = _snapshot_output_entry(
                output_descriptor,
                output_name,
            )
            if current != existing:
                raise ValueError("book output changed during render")
            try:
                os.rename(
                    output_name,
                    backup_name,
                    src_dir_fd=output_descriptor,
                    dst_dir_fd=output_descriptor,
                )
            except FileNotFoundError:
                raise ValueError("book output changed during render") from None
            backup_moved = True
            moved = _snapshot_output_entry(
                output_descriptor,
                backup_name,
            )
            if moved != existing:
                raise ValueError("book output changed during render")

        if _snapshot_regular_descriptor(temporary_descriptor) != publication_snapshot:
            raise ValueError("book output changed during render")
        try:
            _rename_entry_no_replace(
                output_descriptor,
                temporary_name,
                output_name,
            )
        except FileExistsError:
            raise ValueError("book output changed during render") from None
        temporary_name = None

        publication = _OutputPublication(
            name=output_name,
            descriptor=temporary_descriptor,
            snapshot=publication_snapshot,
            backup_name=backup_name if backup_moved else None,
        )
        temporary_descriptor = None
        backup_name = None
        backup_moved = False
        linked = _snapshot_output_entry(
            output_descriptor,
            output_name,
        )
        if linked != publication_snapshot:
            raise ValueError("book output changed during render")
        os.fsync(output_descriptor)
        completed = True
        return publication
    except OSError as error:
        raise ValueError("book output could not be replaced") from error
    finally:
        if publication is not None and not completed:
            try:
                _finish_output(
                    output_descriptor,
                    publication,
                    rollback=True,
                )
            except (OSError, ValueError):
                pass
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=output_descriptor)
            except OSError:
                pass
        if backup_name is not None:
            if backup_moved:
                try:
                    restored = _restore_entry_no_clobber(
                        output_descriptor,
                        backup_name,
                        output_name,
                    )
                except OSError as error:
                    restore_error = error
                else:
                    if restored:
                        backup_name = None
                    else:
                        restore_error = FileExistsError(
                            errno.EEXIST,
                            "book output backup destination exists",
                            output_name,
                        )
            elif backup_name is not None:
                try:
                    os.unlink(backup_name, dir_fd=output_descriptor)
                    backup_name = None
                except OSError:
                    pass
        try:
            os.fsync(output_descriptor)
        except OSError:
            pass
        if restore_error is not None:
            raise ValueError("book output backup could not be restored") from restore_error


def _finish_output(
    output_descriptor: int,
    publication: _OutputPublication,
    *,
    rollback: bool,
) -> None:
    recovery_name: str | None = None
    current_uncertain = False
    try:
        try:
            current = _snapshot_output_entry(
                output_descriptor,
                publication.name,
            )
        except FileNotFoundError:
            current = None
        except ValueError:
            current = None
            current_uncertain = True

        try:
            publication_descriptor_unchanged = (
                _snapshot_regular_descriptor(publication.descriptor)
                == publication.snapshot
            )
        except ValueError:
            publication_descriptor_unchanged = False
        current_was_publication = (
            current == publication.snapshot
            and publication_descriptor_unchanged
        )
        if rollback and current_was_publication:
            candidate_name, recovery_descriptor = _temporary_output(
                output_descriptor,
                ".recover",
            )
            os.close(recovery_descriptor)
            os.unlink(candidate_name, dir_fd=output_descriptor)
            try:
                _rename_entry_no_replace(
                    output_descriptor,
                    publication.name,
                    candidate_name,
                )
            except FileNotFoundError:
                pass
            except OSError:
                try:
                    remaining = _snapshot_output_entry(
                        output_descriptor,
                        publication.name,
                    )
                except FileNotFoundError:
                    remaining = None
                if (
                    remaining == publication.snapshot
                    and _snapshot_regular_descriptor(publication.descriptor)
                    == publication.snapshot
                ):
                    raise
            else:
                recovery_name = candidate_name

        if recovery_name is not None:
            recovered = _snapshot_output_entry(
                output_descriptor,
                recovery_name,
            )
            publication_unchanged = (
                recovered == publication.snapshot
                and _snapshot_regular_descriptor(publication.descriptor)
                == publication.snapshot
            )
            if publication_unchanged:
                if publication.backup_name is not None:
                    restored = _restore_entry_no_clobber(
                        output_descriptor,
                        publication.backup_name,
                        publication.name,
                    )
                    if not restored:
                        raise FileExistsError(
                            errno.EEXIST,
                            "book output backup destination exists",
                            publication.name,
                        )
                    publication.backup_name = None
                os.unlink(recovery_name, dir_fd=output_descriptor)
                recovery_name = None
            else:
                restored = _restore_entry_no_clobber(
                    output_descriptor,
                    recovery_name,
                    publication.name,
                )
                if not restored:
                    raise FileExistsError(
                        errno.EEXIST,
                        "book output recovery destination exists",
                        publication.name,
                    )
                recovery_name = None
        elif (
            rollback
            and not current_uncertain
            and publication.backup_name is not None
        ):
            restored = _restore_entry_no_clobber(
                output_descriptor,
                publication.backup_name,
                publication.name,
            )
            if not restored:
                raise FileExistsError(
                    errno.EEXIST,
                    "book output backup destination exists",
                    publication.name,
                )
            publication.backup_name = None

        if (
            publication.backup_name is not None
            and not (rollback and current_uncertain)
        ):
            os.unlink(publication.backup_name, dir_fd=output_descriptor)
            publication.backup_name = None
        os.fsync(output_descriptor)
        if recovery_name is not None:
            raise ValueError("book output cleanup could not preserve concurrent output")
    except OSError as error:
        raise ValueError("book output cleanup failed") from error
    finally:
        os.close(publication.descriptor)


def render_book(
    source: Path,
    output: Path,
    *,
    project: str,
    data_root=None,
) -> Path:
    """Render one canonical ``book/<project>/<topic>.md`` into its HTML view."""
    project = validate_project(project)
    root = _absolute(data_home() if data_root is None else Path(data_root))
    logical_project = root / "book" / project
    source_path = _absolute(Path(source))
    output_path = _absolute(Path(output))

    if source_path.suffix != ".md":
        raise ValueError("book source must be a Markdown file")
    topic = _validate_topic(source_path.stem)
    expected_source = logical_project / (topic + ".md")
    expected_output = logical_project / "html" / (topic + ".html")
    if source_path != expected_source:
        raise ValueError("book markdown escapes selected project")
    if output_path != expected_output:
        raise ValueError("book output must match the selected source")

    resolved = resolve_project_directory(root / "book", project)
    if resolved is None:
        raise ValueError("project book directory missing")
    physical_project = resolved.physical
    physical_output = physical_project / output_path.relative_to(resolved.logical)

    project_descriptor: int | None = None
    output_descriptor: int | None = None
    publication: _OutputPublication | None = None
    try:
        try:
            project_descriptor = file_io.open_directory_path(physical_project)
        except OSError as error:
            raise ValueError("project book link changed during render") from error
        if not _project_directory_is_current(resolved, project_descriptor):
            raise ValueError("project book link changed during render")

        logical_source_display = "book/{}/{}.md".format(project, topic)
        source_text = _read_source(
            project_descriptor,
            source_path.name,
            logical_source_display,
        )

        source_title = topic
        body_source = source_text
        if source_text.startswith("---"):
            parsed = parse_frontmatter_text(
                source_path.name,
                source_text,
                ("title", "find_when"),
            )
            if parsed is None:
                raise ValueError("book markdown metadata is invalid")
            fields, lines, closing = parsed
            source_title = fields["title"].strip()
            if not source_title or not fields["find_when"].strip():
                raise ValueError("book markdown metadata is invalid")
            body_source = "\n".join(lines[closing + 1 :])

        _reject_raw_html(body_source)
        body = markdown.markdown(
            body_source,
            extensions=("extra", "fenced_code", "tables", "sane_lists"),
            output_format="html5",
        )
        body = _MERMAID_BLOCK.sub(
            lambda match: '<pre class="mermaid">{}</pre>'.format(match.group(1)),
            body,
        )
        body = _inline_images(
            body,
            Path(source_path.name),
            project_descriptor,
        )
        _reject_unsafe_links(body)

        heading = re.search(r"^#\s+(.+)$", body_source, re.MULTILINE)
        title = heading.group(1).strip() if heading else source_title
        mermaid = _load_mermaid()
        document = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{style}</style></head><body><main>{body}
<p class="source">원천: book/{project}/{topic}.md · 이 HTML은 재생성 가능한 파생 뷰입니다.</p></main>
<script>{mermaid}</script><script>mermaid.initialize({{startOnLoad:true,securityLevel:'strict',theme:'default'}});</script>
</body></html>""".format(
            title=html.escape(title),
            style=_STYLE,
            body=body,
            project=html.escape(project),
            topic=html.escape(topic),
            mermaid=mermaid,
        )

        output_descriptor = _prepare_output_directory(project_descriptor)
        if not _project_directory_is_current(resolved, project_descriptor):
            raise ValueError("project book link changed during render")
        if physical_output.parent.name != "html":
            raise ValueError("book output must match the selected source")
        if not _project_directory_is_current(resolved, project_descriptor):
            raise ValueError("project book link changed during render")

        publication = _replace_output(
            output_descriptor,
            physical_output.name,
            document,
        )
        if not _project_directory_is_current(resolved, project_descriptor):
            try:
                _finish_output(
                    output_descriptor,
                    publication,
                    rollback=True,
                )
            finally:
                publication = None
            raise ValueError("project book link changed during render")
        try:
            _finish_output(
                output_descriptor,
                publication,
                rollback=False,
            )
        finally:
            publication = None
        return output_path
    finally:
        if publication is not None and output_descriptor is not None:
            try:
                _finish_output(
                    output_descriptor,
                    publication,
                    rollback=True,
                )
            except (OSError, ValueError):
                pass
        if output_descriptor is not None:
            os.close(output_descriptor)
        if project_descriptor is not None:
            os.close(project_descriptor)
