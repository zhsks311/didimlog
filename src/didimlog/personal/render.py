"""Render a project book Markdown source as self-contained HTML."""

from __future__ import annotations

import base64
from dataclasses import dataclass
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


def _assert_no_symlink(path: Path, root: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("{} escapes book directory".format(label)) from None

    current = root
    if current.is_symlink():
        raise ValueError("{} contains a symlink".format(label))
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("{} contains a symlink".format(label))


def _assert_real_descendant(path: Path, root: Path, label: str) -> None:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        raise ValueError("{} escapes book directory".format(label)) from None


def _read_source(path: Path) -> str:
    try:
        path_status = path.lstat()
    except OSError as error:
        raise ValueError("book markdown missing: {}".format(path)) from error
    if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
        raise ValueError("book markdown must be a regular file; symlinks are not allowed")

    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("book markdown must be a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8", newline="") as handle:
            descriptor = None
            return handle.read()
    except UnicodeDecodeError as error:
        raise ValueError("book markdown must be UTF-8") from error
    except OSError as error:
        raise ValueError("book markdown must be a regular file") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_image(path: Path, source: str) -> bytes:
    try:
        path_status = path.lstat()
    except OSError as error:
        raise ValueError("image missing: {} ({})".format(source, error)) from error
    if stat.S_ISLNK(path_status.st_mode) or not stat.S_ISREG(path_status.st_mode):
        raise ValueError("image must be a regular file; symlinks are not allowed: " + source)

    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("image must be a regular file: " + source)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read()
    except OSError as error:
        raise ValueError("image missing: {} ({})".format(source, error)) from error
    finally:
        if descriptor is not None:
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
    project_book: Path,
) -> str:
    def replace(match: re.Match[str]) -> str:
        before, rendered_source, after = match.groups()
        image_source = html.unescape(rendered_source)
        if _EXTERNAL_URI.match(image_source) or image_source.startswith("//"):
            raise ValueError("external image URL is not allowed: " + image_source)

        image_path = _absolute(source_path.parent / image_source)
        try:
            image_path.relative_to(project_book)
        except ValueError:
            raise ValueError("image escapes book directory: " + image_source) from None
        _assert_no_symlink(image_path, project_book, "image")
        if image_path.exists():
            _assert_real_descendant(image_path, project_book, "image")
        data = _read_image(image_path, image_source)

        mime = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
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
        os.O_WRONLY
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


@dataclass
class _OutputPublication:
    name: str
    descriptor: int
    revision: tuple[int, ...]
    backup_name: str | None


def _replace_output(
    output_descriptor: int,
    output_name: str,
    document: str,
) -> _OutputPublication:
    existing: os.stat_result | None
    try:
        existing = os.stat(
            output_name,
            dir_fd=output_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise ValueError("book output could not be inspected") from error
    if existing is not None and stat.S_ISDIR(existing.st_mode):
        raise ValueError("book output must not be a directory")

    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    backup_name: str | None = None
    publication: _OutputPublication | None = None
    published = False
    completed = False
    try:
        temporary_name, temporary_descriptor = _temporary_output(
            output_descriptor,
            ".tmp",
        )
        _write_all(temporary_descriptor, document.encode("utf-8"))

        if existing is not None:
            backup_name, backup_descriptor = _temporary_output(
                output_descriptor,
                ".bak",
            )
            os.close(backup_descriptor)
            os.rename(
                output_name,
                backup_name,
                src_dir_fd=output_descriptor,
                dst_dir_fd=output_descriptor,
            )
            moved = os.stat(
                backup_name,
                dir_fd=output_descriptor,
                follow_symlinks=False,
            )
            if _entry_revision(moved) != _entry_revision(existing):
                raise ValueError("book output changed during render")

        os.rename(
            temporary_name,
            output_name,
            src_dir_fd=output_descriptor,
            dst_dir_fd=output_descriptor,
        )
        temporary_name = None
        published = True
        revision = _entry_revision(os.fstat(temporary_descriptor))
        publication = _OutputPublication(
            name=output_name,
            descriptor=temporary_descriptor,
            revision=revision,
            backup_name=backup_name,
        )
        temporary_descriptor = None
        linked = os.stat(
            output_name,
            dir_fd=output_descriptor,
            follow_symlinks=False,
        )
        if _entry_revision(linked) != revision:
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
        if not published and backup_name is not None:
            try:
                os.rename(
                    backup_name,
                    output_name,
                    src_dir_fd=output_descriptor,
                    dst_dir_fd=output_descriptor,
                )
                backup_name = None
            except OSError:
                pass
        if not published and backup_name is not None:
            try:
                os.unlink(backup_name, dir_fd=output_descriptor)
            except OSError:
                pass


def _finish_output(
    output_descriptor: int,
    publication: _OutputPublication,
    *,
    rollback: bool,
) -> None:
    try:
        current: os.stat_result | None
        try:
            current = os.stat(
                publication.name,
                dir_fd=output_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            current = None

        published_is_current = (
            current is not None
            and _entry_revision(current) == publication.revision
            and _entry_revision(os.fstat(publication.descriptor))
            == publication.revision
        )
        if rollback and published_is_current:
            if publication.backup_name is None:
                os.unlink(publication.name, dir_fd=output_descriptor)
            else:
                os.rename(
                    publication.backup_name,
                    publication.name,
                    src_dir_fd=output_descriptor,
                    dst_dir_fd=output_descriptor,
                )
                publication.backup_name = None
        if publication.backup_name is not None:
            os.unlink(publication.backup_name, dir_fd=output_descriptor)
            publication.backup_name = None
        os.fsync(output_descriptor)
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
    physical_source = physical_project / source_path.relative_to(resolved.logical)
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

        _assert_no_symlink(physical_source, physical_project, "book markdown")
        if not physical_source.exists():
            raise ValueError("book markdown missing: {}".format(source_path))
        _assert_real_descendant(physical_source, physical_project, "book markdown")
        source_text = _read_source(physical_source)

        source_title = topic
        body_source = source_text
        if source_text.startswith("---"):
            parsed = parse_frontmatter_text(
                physical_source.name,
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
        body = _inline_images(body, physical_source, physical_project)
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
