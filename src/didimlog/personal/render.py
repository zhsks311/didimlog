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

from .lesson import parse_frontmatter_text, parse_inline_list, valid_index_title
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


class _RenderedHtmlValidator(HTMLParser):
    def handle_starttag(self, tag: str, attributes) -> None:
        normalized_tag = tag.lower()
        for name, value in attributes:
            normalized = name.lower()
            if normalized == "style":
                if (
                    normalized_tag in ("th", "td")
                    and value
                    in (
                        "text-align: center;",
                        "text-align: left;",
                        "text-align: right;",
                    )
                ):
                    continue
                raise ValueError(
                    "unsafe rendered attribute is not allowed: " + normalized
                )
            if normalized.startswith("on"):
                raise ValueError(
                    "unsafe rendered attribute is not allowed: " + normalized
                )
            if (
                normalized_tag == "a"
                and normalized == "href"
                and value is not None
            ):
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


def _reject_unsafe_rendered_html(rendered: str) -> None:
    parser = _RenderedHtmlValidator(convert_charrefs=True)
    parser.feed(rendered)
    parser.close()


def load_verified_mermaid() -> bytes:
    """Return the pinned vendored Mermaid runtime only after SHA-256 verification."""
    try:
        package = resources.files("didimlog.resources.personal")
        runtime = (package / "mermaid.min.js").read_bytes()
    except OSError as error:
        raise ValueError("vendored Mermaid runtime missing") from error
    if hashlib.sha256(runtime).hexdigest() != _MERMAID_SHA256:
        raise ValueError("vendored Mermaid runtime failed integrity check")
    return runtime


def _load_mermaid() -> str:
    try:
        return load_verified_mermaid().decode("utf-8")
    except UnicodeError as error:
        raise ValueError("vendored Mermaid runtime missing") from error


@dataclass(frozen=True)
class BookHeading:
    level: int
    anchor: str
    text: str


@dataclass(frozen=True)
class RenderedBook:
    title: str
    find_when: tuple[str, ...]
    logical_path: str
    body_html: str
    headings: tuple[BookHeading, ...]


def _heading_text(value: object) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", str(value)))


def _book_headings(tokens) -> tuple[BookHeading, ...]:
    headings = []

    def append(items) -> None:
        for item in items:
            headings.append(
                BookHeading(
                    level=int(item["level"]),
                    anchor=str(item["id"]),
                    text=_heading_text(item["name"]),
                )
            )
            append(item.get("children", ()))

    append(tokens)
    return tuple(headings)


def _render_book_view_at(
    project_descriptor: int,
    *,
    project: str,
    source_name: str,
    include_toc: bool,
) -> RenderedBook:
    logical_path = "book/{}/{}".format(project, source_name)
    source_text = _read_source(
        project_descriptor,
        source_name,
        logical_path,
    )
    parsed = parse_frontmatter_text(
        source_name,
        source_text,
        ("title", "find_when"),
    )
    if parsed is None:
        raise ValueError("book markdown metadata is invalid")
    fields, lines, closing = parsed
    title = fields["title"].strip()
    find_when = parse_inline_list(fields["find_when"], canonical=True)
    if not valid_index_title(title) or not find_when:
        raise ValueError("book markdown metadata is invalid")
    body_source = "\n".join(lines[closing + 1 :])

    _reject_raw_html(body_source)
    # ``extra`` includes attr_list, which would let source text add arbitrary
    # HTML attributes. Keep its documented safe features explicit.
    extensions = [
        "abbr",
        "def_list",
        "fenced_code",
        "footnotes",
        "sane_lists",
        "tables",
    ]
    if include_toc:
        extensions.append("toc")
    converter = markdown.Markdown(
        extensions=extensions,
        output_format="html5",
    )
    body = converter.convert(body_source)
    body = _MERMAID_BLOCK.sub(
        lambda match: '<pre class="mermaid">{}</pre>'.format(match.group(1)),
        body,
    )
    body = _inline_images(
        body,
        Path(source_name),
        project_descriptor,
    )
    _reject_unsafe_rendered_html(body)
    return RenderedBook(
        title=title,
        find_when=tuple(find_when),
        logical_path=logical_path,
        body_html=body,
        headings=(
            _book_headings(getattr(converter, "toc_tokens", ()))
            if include_toc
            else ()
        ),
    )


def render_book_view(
    *,
    project: str,
    source_name: str,
    data_root=None,
) -> RenderedBook:
    """Render a validated canonical book in memory without writing a derived file."""
    selected_project = validate_project(project, allow_global=True)
    if (
        source_name != os.path.basename(source_name)
        or not source_name.endswith(".md")
        or source_name in (".md", "..md")
    ):
        raise ValueError("book source name is invalid")
    root = _absolute(data_home() if data_root is None else Path(data_root))
    resolved = resolve_project_directory(root / "book", selected_project)
    if resolved is None:
        raise ValueError("project book directory missing")

    descriptor: int | None = None
    try:
        try:
            descriptor = file_io.open_directory_path(resolved.physical)
        except OSError as error:
            raise ValueError("project book link changed during render") from error
        if not _project_directory_is_current(resolved, descriptor):
            raise ValueError("project book link changed during render")
        view = _render_book_view_at(
            descriptor,
            project=selected_project,
            source_name=source_name,
            include_toc=True,
        )
        if not _project_directory_is_current(resolved, descriptor):
            raise ValueError("project book link changed during render")
        return view
    finally:
        if descriptor is not None:
            os.close(descriptor)


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
        return name, descriptor
    raise ValueError("book output temporary file could not be created")




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

def _move_entry_to_staging(
    output_descriptor: int,
    source_name: str,
    suffix: str,
) -> str:
    for _ in range(32):
        staging_name = ".render-{}{}".format(secrets.token_hex(12), suffix)
        try:
            _rename_entry_no_replace(
                output_descriptor,
                source_name,
                staging_name,
            )
        except FileExistsError:
            continue
        return staging_name
    raise OSError(errno.EEXIST, "render staging name collision")


def _read_regular_descriptor(
    descriptor: int,
    maximum_bytes: int,
) -> tuple[bytes, _EntrySnapshot]:
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_size < 0
        or opened.st_size > maximum_bytes
    ):
        raise ValueError("book output changed during render")
    revision = _entry_revision(opened)
    remaining = opened.st_size
    offset = 0
    chunks = bytearray()
    while remaining:
        chunk = os.pread(descriptor, remaining, offset)
        if not chunk:
            raise ValueError("book output changed during render")
        chunks.extend(chunk)
        offset += len(chunk)
        remaining -= len(chunk)
    if _entry_revision(os.fstat(descriptor)) != revision:
        raise ValueError("book output changed during render")
    data = bytes(chunks)
    return data, _EntrySnapshot(
        revision=revision,
        digest=hashlib.sha256(data).digest(),
    )


def _remove_entry_if_unchanged(
    output_descriptor: int,
    name: str,
    expected: _EntrySnapshot,
    *,
    descriptor: int | None = None,
    expected_data: bytes | None = None,
) -> bool:
    try:
        current = _snapshot_output_entry(output_descriptor, name)
    except FileNotFoundError:
        return False
    if current != expected:
        return False

    if descriptor is not None:
        if expected_data is None:
            raise ValueError("book output cleanup ownership is incomplete")
        actual_data, descriptor_snapshot = _read_regular_descriptor(
            descriptor,
            len(expected_data),
        )
        if (
            descriptor_snapshot != expected
            or not expected_data.startswith(actual_data)
        ):
            return False

    cleanup_name = _move_entry_to_staging(
        output_descriptor,
        name,
        ".cleanup",
    )
    moved = _snapshot_output_entry(output_descriptor, cleanup_name)
    descriptor_unchanged = True
    if descriptor is not None:
        actual_data, descriptor_snapshot = _read_regular_descriptor(
            descriptor,
            len(expected_data),
        )
        descriptor_unchanged = (
            descriptor_snapshot == expected
            and expected_data.startswith(actual_data)
        )
    if moved != expected or not descriptor_unchanged:
        try:
            _rename_entry_no_replace(
                output_descriptor,
                cleanup_name,
                name,
            )
        except FileExistsError:
            pass
        return False
    os.unlink(cleanup_name, dir_fd=output_descriptor)
    return True


def _replace_output(
    output_descriptor: int,
    output_name: str,
    document: str,
) -> None:
    document_bytes = document.encode("utf-8")
    try:
        existing = _snapshot_output_entry(
            output_descriptor,
            output_name,
        )
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise ValueError("book output could not be inspected") from error
    if existing is not None and not (
        stat.S_ISREG(existing.revision[2])
        or stat.S_ISLNK(existing.revision[2])
    ):
        raise ValueError("book output must be a regular file or symlink")

    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    temporary_snapshot: _EntrySnapshot | None = None
    old_staging_name: str | None = None
    published = False
    cleanup_failed = False
    staging_restore_failed = False
    try:
        temporary_name, temporary_descriptor = _temporary_output(
            output_descriptor,
            ".tmp",
        )
        file_io.write_all_and_sync(temporary_descriptor, document_bytes)
        temporary_snapshot = _snapshot_regular_descriptor(
            temporary_descriptor
        )

        if existing is not None:
            current = _snapshot_output_entry(
                output_descriptor,
                output_name,
            )
            if current != existing:
                raise ValueError("book output changed during render")
            old_staging_name = _move_entry_to_staging(
                output_descriptor,
                output_name,
                ".staging",
            )
            moved = _snapshot_output_entry(
                output_descriptor,
                old_staging_name,
            )
            if moved != existing:
                try:
                    _rename_entry_no_replace(
                        output_descriptor,
                        old_staging_name,
                        output_name,
                    )
                except FileExistsError:
                    pass
                else:
                    old_staging_name = None
                raise ValueError("book output changed during render")

        if (
            _snapshot_regular_descriptor(temporary_descriptor)
            != temporary_snapshot
        ):
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
        published = True

        linked = _snapshot_output_entry(
            output_descriptor,
            output_name,
        )
        if linked != temporary_snapshot:
            raise ValueError("book output changed during render")
        os.fsync(output_descriptor)

        if old_staging_name is not None:
            if not _remove_entry_if_unchanged(
                output_descriptor,
                old_staging_name,
                existing,
            ):
                raise ValueError(
                    "book output staging changed during cleanup"
                )
            old_staging_name = None
            os.fsync(output_descriptor)
    except ValueError:
        raise
    except OSError as error:
        raise ValueError("book output could not be replaced") from error
    finally:
        if not published and old_staging_name is not None:
            try:
                _rename_entry_no_replace(
                    output_descriptor,
                    old_staging_name,
                    output_name,
                )
            except FileExistsError:
                pass
            except OSError:
                staging_restore_failed = True
            else:
                old_staging_name = None
                try:
                    os.fsync(output_descriptor)
                except OSError:
                    staging_restore_failed = True
        if (
            not published
            and temporary_name is not None
            and temporary_descriptor is not None
        ):
            try:
                if temporary_snapshot is None:
                    actual_data, observed = _read_regular_descriptor(
                        temporary_descriptor,
                        len(document_bytes),
                    )
                    if document_bytes.startswith(actual_data):
                        temporary_snapshot = observed
                if temporary_snapshot is None or not _remove_entry_if_unchanged(
                    output_descriptor,
                    temporary_name,
                    temporary_snapshot,
                    descriptor=temporary_descriptor,
                    expected_data=document_bytes,
                ):
                    cleanup_failed = True
            except (OSError, ValueError):
                cleanup_failed = True
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if staging_restore_failed:
            raise ValueError(
                "book output staging could not be restored"
            )
        if cleanup_failed:
            raise ValueError(
                "book output temporary cleanup could not be verified"
            )

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
    try:
        try:
            project_descriptor = file_io.open_directory_path(physical_project)
        except OSError as error:
            raise ValueError("project book link changed during render") from error
        if not _project_directory_is_current(resolved, project_descriptor):
            raise ValueError("project book link changed during render")

        view = _render_book_view_at(
            project_descriptor,
            project=project,
            source_name=source_path.name,
            include_toc=False,
        )
        mermaid = _load_mermaid()
        document = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{style}</style></head><body><main>{body}
<p class="source">원천: {logical_path} · 이 HTML은 재생성 가능한 파생 뷰입니다.</p></main>
<script>{mermaid}</script><script>mermaid.initialize({{startOnLoad:true,securityLevel:'strict',theme:'default'}});</script>
</body></html>""".format(
            title=html.escape(view.title),
            style=_STYLE,
            body=view.body_html,
            logical_path=html.escape(view.logical_path),
            mermaid=mermaid,
        )

        output_descriptor = _prepare_output_directory(project_descriptor)
        if not _project_directory_is_current(resolved, project_descriptor):
            raise ValueError("project book link changed during render")
        if physical_output.parent.name != "html":
            raise ValueError("book output must match the selected source")
        if not _project_directory_is_current(resolved, project_descriptor):
            raise ValueError("project book link changed during render")

        _replace_output(
            output_descriptor,
            physical_output.name,
            document,
        )
        if not _project_directory_is_current(resolved, project_descriptor):
            raise ValueError("project book link changed during render")
        return output_path
    finally:
        if output_descriptor is not None:
            os.close(output_descriptor)
        if project_descriptor is not None:
            os.close(project_descriptor)
