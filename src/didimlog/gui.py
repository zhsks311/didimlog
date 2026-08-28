"""Loopback-only, read-only local web GUI for Didimlog."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sys
from urllib.parse import urlsplit
import webbrowser

from didimlog.claude.status import StatusSnapshot, status_snapshot
from didimlog.errors import DidimError, EXIT_POLICY
from didimlog.file_io import UnsafePathError
from didimlog.personal import index as personal_index
from didimlog.personal.paths import data_home
from didimlog.personal.render import (
    BookRenderLimits,
    BookRenderTooLarge,
    load_verified_mermaid,
    render_book_view,
)


LOOPBACK_HOST = "127.0.0.1"
_RESOURCE_ID = re.compile(r"^[0-9a-f]{64}$")
_PERSONAL_INDEX_TOKENS = {
    personal_index.IndexCheckState.CURRENT: "PERSONAL_INDEX_CURRENT",
    personal_index.IndexCheckState.EXTRA: "PERSONAL_INDEX_EXTRA",
    personal_index.IndexCheckState.MISSING: "PERSONAL_INDEX_MISSING",
    personal_index.IndexCheckState.STALE: "PERSONAL_INDEX_STALE",
}
_GUI_BOOK_RENDER_LIMITS = BookRenderLimits(
    source_bytes=personal_index.SOURCE_MAX_BYTES,
    image_bytes=16 * 1024 * 1024,
    aggregate_image_bytes=64 * 1024 * 1024,
    body_html_bytes=96 * 1024 * 1024,
)
_GUI_BOOK_RESPONSE_MAX_BYTES = 128 * 1024 * 1024

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'none'; frame-ancestors 'none'; "
        "img-src 'self' data:; object-src 'none'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=(), payment=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class GuiRequestError(ValueError):
    def __init__(self, token: str, status: HTTPStatus, message: str) -> None:
        self.token = token
        self.status = status
        self.message = message
        super().__init__(token)


def _resource_id(logical_path: str) -> str:
    return hashlib.sha256(logical_path.encode("utf-8")).hexdigest()


def _state(token: str) -> str:
    if token.endswith("_CURRENT") or token.endswith("_OK"):
        return "current"
    if token.endswith("_STALE"):
        return "stale"
    if token.endswith("_MISSING"):
        return "missing"
    if token.endswith("_EXTRA"):
        return "extra"
    if token.endswith("_INVALID_SOURCE") or token.endswith("_PROBLEMS"):
        return "problem"
    if token.endswith("_NOT_CONFIGURED"):
        return "unconfigured"
    return "unknown"


def _personal_index_payload(token: str) -> dict[str, object]:
    return {
        "token": token,
        "state": _state(token),
        "current": token == "PERSONAL_INDEX_CURRENT",
    }


def _health_payload(snapshot: StatusSnapshot) -> dict[str, object]:
    return {
        "version": snapshot.version,
        "personal_index": _personal_index_payload(snapshot.personal_token),
        "project": {
            "name": snapshot.project_name,
            "index": {
                "token": snapshot.project_token,
                "state": _state(snapshot.project_token),
                "current": snapshot.project_token == "PROJECT_INDEX_CURRENT",
            },
        },
        "claude": {
            "token": snapshot.claude_token,
            "state": _state(snapshot.claude_token),
        },
        "issues": [asdict(problem) for problem in snapshot.problems],
        "read_only": True,
    }


class GuiApplication:
    """Typed read-only application boundary used by the HTTP adapter."""

    def __init__(self, *, home=None, cwd=None, config=None) -> None:
        self.home = None if home is None else Path(home)
        self.cwd = None if cwd is None else Path(cwd)
        self.config = None if config is None else Path(config)

    @property
    def personal_root(self) -> Path:
        return data_home(self.home)

    def health(self) -> dict[str, object]:
        return _health_payload(
            status_snapshot(home=self.home, cwd=self.cwd, config=self.config)
        )

    def _collected(self) -> dict[str, list[dict[str, object]]]:
        root = self.personal_root
        if not root.exists():
            return {}
        return personal_index.collect(root, include_content=True)

    def _library_snapshot(
        self,
    ) -> tuple[dict[str, list[dict[str, object]]], str]:
        root = self.personal_root
        if not root.exists():
            return {}, "PERSONAL_INDEX_MISSING"
        collected, index_state = personal_index.collect_snapshot(
            root,
            include_content=True,
        )
        return collected, _PERSONAL_INDEX_TOKENS[index_state]

    def library(self) -> dict[str, object]:
        health = self.health()
        collected, personal_token = self._library_snapshot()
        health["personal_index"] = _personal_index_payload(personal_token)
        scopes = []
        current_project = health["project"]["name"]
        for scope, items in sorted(
            collected.items(),
            key=lambda pair: (
                0 if pair[0] == current_project else 1,
                pair[0].encode("utf-8"),
            ),
        ):
            books = []
            lessons = []
            for item in items:
                logical_path = str(item["path"])
                common = {
                    "id": _resource_id(logical_path),
                    "title": str(item["title"]),
                    "logical_path": logical_path,
                    "scope": scope,
                }
                if item["kind"] == "book":
                    books.append(
                        {
                            **common,
                            "find_when": list(item["find_when"]),
                        }
                    )
                elif item["kind"] == "lesson":
                    topic = str(item["topic"])
                    booked_topics = list(item["booked"])
                    lessons.append(
                        {
                            **common,
                            "slug": str(item["slug"]),
                            "summary": str(item["summary"]),
                            "topic": topic,
                            "tags": list(item["tags"]),
                            "date": str(item["date"]),
                            "review_by": item["review_by"],
                            "booked_state": (
                                "booked" if topic in booked_topics else "unbooked"
                            ),
                        }
                    )
            books.sort(
                key=lambda item: (
                    str(item["title"]).encode("utf-8"),
                    str(item["logical_path"]).encode("utf-8"),
                )
            )
            lessons.sort(
                key=lambda item: (
                    str(item["date"]),
                    str(item["logical_path"]),
                ),
                reverse=True,
            )
            scopes.append(
                {
                    "scope": scope,
                    "books": books,
                    "lessons": lessons,
                    "book_count": len(books),
                    "lesson_count": len(lessons),
                }
            )
        return {
            "health": health,
            "source_snapshot": {"state": "validated", "write_performed": False},
            "scopes": scopes,
        }

    def _find_item(self, identifier: str, kind: str) -> tuple[str, dict[str, object]]:
        if _RESOURCE_ID.fullmatch(identifier) is None:
            raise GuiRequestError(
                "GUI_RESOURCE_INVALID",
                HTTPStatus.BAD_REQUEST,
                "요청한 자료 식별자가 올바르지 않습니다.",
            )
        for scope, items in self._collected().items():
            for item in items:
                if item["kind"] != kind:
                    continue
                if _resource_id(str(item["path"])) == identifier:
                    return scope, item
        raise GuiRequestError(
            "GUI_RESOURCE_NOT_FOUND",
            HTTPStatus.NOT_FOUND,
            "요청한 자료가 현재 source snapshot에 없습니다.",
        )

    @staticmethod
    def _source_exceeded_index_limit(
        error: personal_index.KnowledgeSourceError,
        identifier: str,
    ) -> bool:
        # The index preserves this descriptor-safe limit failure as its cause.
        # Keep every other unreadable-source case on the existing conflict path.
        cause = error.__cause__
        return (
            error.logical_path.startswith("book/")
            and _resource_id(error.logical_path) == identifier
            and isinstance(cause, UnsafePathError)
            and str(cause) == "source exceeds read limit"
        )

    def book(self, identifier: str) -> dict[str, object]:
        try:
            scope, item = self._find_item(identifier, "book")
        except personal_index.KnowledgeSourceError as error:
            if self._source_exceeded_index_limit(error, identifier):
                raise GuiRequestError(
                    "BOOK_RENDER_TOO_LARGE",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "책 reader 결과가 local GUI의 안전한 크기 제한을 넘었습니다.",
                ) from error
            raise
        source_name = PurePosixPath(str(item["path"])).name
        try:
            view = render_book_view(
                project=scope,
                source_name=source_name,
                data_root=self.personal_root,
                limits=_GUI_BOOK_RENDER_LIMITS,
                namespace_headings=True,
            )
        except BookRenderTooLarge as error:
            raise GuiRequestError(
                "BOOK_RENDER_TOO_LARGE",
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "책 reader 결과가 local GUI의 안전한 크기 제한을 넘었습니다.",
            ) from error
        except (OSError, UnicodeError, ValueError) as error:
            raise GuiRequestError(
                "BOOK_RENDER_REJECTED",
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "책 원문이 안전한 reader 계약을 통과하지 못했습니다.",
            ) from error
        return {
            "id": identifier,
            "title": view.title,
            "find_when": list(view.find_when),
            "logical_path": view.logical_path,
            "scope": scope,
            "body_html": view.body_html,
            "headings": [asdict(heading) for heading in view.headings],
            "source_of_truth": "canonical_markdown",
            "view": "regenerable_in_memory_html",
            "write_performed": False,
        }

    def lesson(self, identifier: str) -> dict[str, object]:
        scope, item = self._find_item(identifier, "lesson")
        topic = str(item["topic"])
        booked_topics = list(item["booked"])
        return {
            "id": identifier,
            "slug": str(item["slug"]),
            "title": str(item["title"]),
            "summary": str(item["summary"]),
            "topic": topic,
            "tags": list(item["tags"]),
            "date": str(item["date"]),
            "review_by": item["review_by"],
            "booked_topics": booked_topics,
            "booked_state": "booked" if topic in booked_topics else "unbooked",
            "logical_path": str(item["path"]),
            "scope": scope,
            "markdown": str(item["body"]),
            "source_of_truth": "canonical_markdown",
            "write_performed": False,
        }


def _static_bytes(name: str) -> tuple[bytes, str]:
    if name == "mermaid.min.js":
        return load_verified_mermaid(), "text/javascript; charset=utf-8"
    package = resources.files("didimlog.resources.web")
    content_types = {
        "index.html": "text/html; charset=utf-8",
        "app.css": "text/css; charset=utf-8",
        "app.js": "text/javascript; charset=utf-8",
    }
    if name not in content_types:
        raise FileNotFoundError(name)
    return (package / name).read_bytes(), content_types[name]


def _handler(application: GuiApplication):
    class GuiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args) -> None:
            return

        def _allowed_origin(self) -> str | None:
            port = self.server.server_port
            host = self.headers.get("Host", "")
            allowed = {
                "127.0.0.1:{}".format(port),
                "localhost:{}".format(port),
            }
            if host not in allowed:
                return None
            try:
                client = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return None
            if not client.is_loopback:
                return None
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {
                "http://" + value for value in allowed
            }:
                return None
            return host

        @staticmethod
        def _api_path(path: str) -> bool:
            return path.startswith("/api/")

        def _authorized(self) -> bool:
            values = self.headers.get_all("Authorization", ())
            candidate = b""
            if len(values) == 1 and values[0].startswith("Bearer "):
                candidate = values[0].removeprefix("Bearer ").encode(
                    "utf-8",
                    "surrogatepass",
                )
            return hmac.compare_digest(
                candidate,
                self.server._capability.encode("ascii"),
            )

        def _require_capability(self, *, head_only: bool = False) -> bool:
            if self._authorized():
                return True
            self._error(
                GuiRequestError(
                    "GUI_CAPABILITY_REQUIRED",
                    HTTPStatus.UNAUTHORIZED,
                    "이 local GUI launch의 browser capability가 필요합니다.",
                ),
                head_only=head_only,
            )
            return False


        def _send(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
            *,
            head_only: bool = False,
            allow: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if allow is not None:
                self.send_header("Allow", allow)
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            if not head_only:
                self.wfile.write(body)

        def _json(
            self,
            status: HTTPStatus,
            payload: dict[str, object],
            *,
            head_only: bool = False,
            allow: str | None = None,
            maximum_bytes: int | None = None,
        ) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if maximum_bytes is not None and len(body) > maximum_bytes:
                raise GuiRequestError(
                    "BOOK_RENDER_TOO_LARGE",
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "책 reader 결과가 local GUI의 안전한 크기 제한을 넘었습니다.",
                )
            self._send(
                status,
                body,
                "application/json; charset=utf-8",
                head_only=head_only,
                allow=allow,
            )

        def _error(
            self,
            error: GuiRequestError,
            *,
            head_only: bool = False,
            allow: str | None = None,
        ) -> None:
            self._json(
                error.status,
                {"error": {"token": error.token, "message": error.message}},
                head_only=head_only,
                allow=allow,
            )

        def _dispatch(self, *, head_only: bool) -> None:
            if self._allowed_origin() is None:
                self._error(
                    GuiRequestError(
                        "GUI_LOCAL_REQUEST_REQUIRED",
                        HTTPStatus.FORBIDDEN,
                        "loopback origin 요청만 허용합니다.",
                    ),
                    head_only=head_only,
                )
                return
            path = urlsplit(self.path).path
            if self._api_path(path) and not self._require_capability(
                head_only=head_only
            ):
                return
            try:
                if path == "/api/v1/health":
                    self._json(HTTPStatus.OK, application.health(), head_only=head_only)
                    return
                if path == "/api/v1/library":
                    self._json(HTTPStatus.OK, application.library(), head_only=head_only)
                    return
                if path.startswith("/api/v1/books/"):
                    identifier = path.removeprefix("/api/v1/books/")
                    self._json(
                        HTTPStatus.OK,
                        application.book(identifier),
                        head_only=head_only,
                        maximum_bytes=_GUI_BOOK_RESPONSE_MAX_BYTES,
                    )
                    return
                if path.startswith("/api/v1/lessons/"):
                    identifier = path.removeprefix("/api/v1/lessons/")
                    self._json(
                        HTTPStatus.OK,
                        application.lesson(identifier),
                        head_only=head_only,
                    )
                    return
                static_name = {
                    "/": "index.html",
                    "/assets/app.css": "app.css",
                    "/assets/app.js": "app.js",
                    "/assets/mermaid.min.js": "mermaid.min.js",
                }.get(path)
                if static_name is not None:
                    body, content_type = _static_bytes(static_name)
                    self._send(
                        HTTPStatus.OK,
                        body,
                        content_type,
                        head_only=head_only,
                    )
                    return
                raise GuiRequestError(
                    "GUI_ROUTE_NOT_FOUND",
                    HTTPStatus.NOT_FOUND,
                    "요청한 local GUI 경로가 없습니다.",
                )
            except GuiRequestError as error:
                self._error(error, head_only=head_only)
            except personal_index.KnowledgeSourceError as error:
                self._json(
                    HTTPStatus.CONFLICT,
                    {
                        "error": {
                            "token": "PERSONAL_INDEX_INVALID_SOURCE",
                            "message": "개인 지식 원본을 안전하게 읽을 수 없습니다.",
                            "logical_path": error.logical_path,
                            "reason": error.reason,
                        }
                    },
                    head_only=head_only,
                )
            except (OSError, UnicodeError, ValueError):
                self._error(
                    GuiRequestError(
                        "GUI_READ_FAILED",
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "현재 local source snapshot을 안전하게 읽지 못했습니다.",
                    ),
                    head_only=head_only,
                )

        def do_GET(self) -> None:
            self._dispatch(head_only=False)

        def do_HEAD(self) -> None:
            self._dispatch(head_only=True)

        def do_POST(self) -> None:
            if self._allowed_origin() is None:
                self._error(
                    GuiRequestError(
                        "GUI_LOCAL_REQUEST_REQUIRED",
                        HTTPStatus.FORBIDDEN,
                        "loopback origin 요청만 허용합니다.",
                    )
                )
                return
            path = urlsplit(self.path).path
            if self._api_path(path) and not self._require_capability():
                return
            self._error(
                GuiRequestError(
                    "GUI_READ_ONLY",
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "Milestone A local GUI는 읽기 전용입니다.",
                ),
                allow="GET, HEAD",
            )

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    return GuiHandler


class GuiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, request_handler) -> None:
        self._capability = secrets.token_urlsafe(32)
        super().__init__(server_address, request_handler)


def create_server(
    application: GuiApplication | None = None,
    *,
    port: int = 0,
) -> GuiServer:
    """Bind a GUI server to IPv4 loopback only."""
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("GUI port must be between 0 and 65535")
    selected = GuiApplication() if application is None else application
    return GuiServer((LOOPBACK_HOST, port), _handler(selected))


def serve_gui(*, port: int = 0, open_browser: bool = False) -> int:
    """Serve until interrupted; opening a browser is explicit via ``--open``."""
    try:
        server = create_server(port=port)
    except OSError as error:
        raise DidimError(
            "GUI_PORT_UNAVAILABLE",
            exit_code=EXIT_POLICY,
            help_text="다른 --port 값을 선택하거나 사용 중인 local server를 종료하세요.",
        ) from error
    url = "http://{}:{}/".format(LOOPBACK_HOST, server.server_port)
    bootstrap_url = url + "#cap=" + server._capability
    try:
        print("Didimlog local GUI: " + url, flush=True)
        if open_browser:
            opened = False
            try:
                opened = webbrowser.open(bootstrap_url, new=2)
            except (OSError, webbrowser.Error):
                pass
            if not opened:
                print(
                    "GUI_BROWSER_OPEN_FAILED: --open 없이 다시 실행해 private handoff URL을 여세요.",
                    file=sys.stderr,
                    flush=True,
                )
        else:
            print(
                "Didimlog private GUI handoff (sensitive; do not share): "
                + bootstrap_url,
                flush=True,
            )
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0
