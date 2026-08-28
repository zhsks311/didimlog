import contextlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from didimlog import cli
from didimlog.gui import GuiApplication, LOOPBACK_HOST, create_server
from didimlog.personal import index as personal_index


BOOK = """---
title: 안전한 로컬 책
find_when: [safe, test]
---
# 안전한 로컬 책

## 원칙

원문은 바뀌지 않는다.
"""

LESSON = """---
topic: safe-reader
title: 읽기 전용 계약
summary: browse는 canonical source를 바꾸지 않는다
tags: [gui, safe]
date: 2026-08-28
review_by: 2026-09-28
booked: [safe-reader]
---
## 상황

브라우저에서 원문을 읽는다.

## 교훈

<script>이 문자열은 실행되지 않고 Markdown 원문으로만 반환된다.</script>
"""


class GuiBehaviorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.cwd = self.root / "workspace"
        self.config = self.root / "claude"
        self.knowledge = self.home / "knowledge"
        self.cwd.mkdir()
        self.config.mkdir()
        for relative in ("lessons/demo", "docs", "book/demo"):
            (self.knowledge / relative).mkdir(parents=True, exist_ok=True)
        (self.knowledge / "book/demo/reader.md").write_text(BOOK, encoding="utf-8")
        (self.knowledge / "lessons/demo/safe-reader.md").write_text(
            LESSON,
            encoding="utf-8",
        )
        personal_index.write_all(
            data_root=self.knowledge,
            target=self.knowledge / "index",
        )
        self.application = GuiApplication(
            home=self.home,
            cwd=self.cwd,
            config=self.config,
        )
        self.server = create_server(self.application, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, *, headers=None):
        connection = http.client.HTTPConnection(
            LOOPBACK_HOST,
            self.server.server_port,
            timeout=5,
        )
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            content_type = response.getheader("Content-Type", "")
            payload = json.loads(body) if "application/json" in content_type else body
            return response.status, response.getheaders(), payload
        finally:
            connection.close()

    def snapshot(self):
        return {
            path.relative_to(self.root).as_posix(): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def library(self):
        status, _, payload = self.request("GET", "/api/v1/library")
        self.assertEqual(status, 200)
        return payload

    def test_cli_registers_gui_and_browser_opening_is_explicit(self):
        for argv, expected in (
            (["gui"], mock.call(port=0, open_browser=False)),
            (["gui", "--port", "8123", "--open"], mock.call(port=8123, open_browser=True)),
        ):
            with self.subTest(argv=argv), mock.patch(
                "didimlog.cli.serve_gui",
                return_value=0,
            ) as serve:
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = cli.main(argv)
                self.assertEqual(code, 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(serve.call_args, expected)

        with mock.patch("didimlog.cli._stderr_is_tty", return_value=True):
            parsed = cli.build_parser().parse_args(["gui"])
            self.assertFalse(
                cli._automatic_update_eligible(parsed, real_invocation=True)
            )

    def test_server_binds_ipv4_loopback_and_rejects_nonlocal_host(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        content_security_policy = dict(headers)["Content-Security-Policy"]
        self.assertIn("default-src 'self'", content_security_policy)
        self.assertIn("script-src 'self'", content_security_policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", content_security_policy)
        self.assertIn("style-src 'self' 'unsafe-inline'", content_security_policy)

        status, _, payload = self.request(
            "GET",
            "/api/v1/health",
            headers={"Host": "attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["token"], "GUI_LOCAL_REQUEST_REQUIRED")

    def test_closed_server_port_can_be_reused_immediately(self):
        first = create_server(self.application, port=0)
        port = first.server_port
        thread = threading.Thread(target=first.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(LOOPBACK_HOST, port, timeout=5)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        finally:
            connection.close()
            first.shutdown()
            first.server_close()
            thread.join(timeout=5)

        second = create_server(self.application, port=port)
        second.server_close()

    def test_library_labels_the_exact_locked_source_snapshot(self):
        collected = personal_index.collect(
            self.knowledge,
            include_content=True,
        )
        with mock.patch(
            "didimlog.gui.personal_index.collect_snapshot",
            return_value=(
                collected,
                personal_index.IndexCheckState.STALE,
            ),
        ) as collect_snapshot:
            library = self.application.library()

        self.assertEqual(
            library["health"]["personal_index"]["token"],
            "PERSONAL_INDEX_STALE",
        )
        self.assertFalse(
            library["health"]["personal_index"]["current"],
        )
        collect_snapshot.assert_called_once_with(
            self.knowledge,
            include_content=True,
        )

    def test_mermaid_asset_uses_verified_loader_and_fails_closed(self):
        with mock.patch(
            "didimlog.gui.load_verified_mermaid",
            return_value=b"verified-runtime",
        ) as verified:
            status, _, body = self.request(
                "GET",
                "/assets/mermaid.min.js",
            )

        self.assertEqual(status, 200)
        self.assertEqual(body, b"verified-runtime")
        verified.assert_called_once_with()

        with mock.patch(
            "didimlog.gui.load_verified_mermaid",
            side_effect=ValueError("integrity failure"),
        ):
            status, _, payload = self.request(
                "GET",
                "/assets/mermaid.min.js",
            )

        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["token"], "GUI_READ_FAILED")
        self.assertNotIn("integrity failure", json.dumps(payload))

    def test_resource_ids_reject_traversal_and_do_not_accept_paths(self):
        for path in (
            "/api/v1/books/../../private",
            "/api/v1/books/%2e%2e%2fprivate",
            "/api/v1/lessons/book%2Fdemo%2Freader.md",
        ):
            with self.subTest(path=path):
                status, _, payload = self.request("GET", path)
                self.assertIn(status, (400, 404))
                self.assertIn(
                    payload["error"]["token"],
                    ("GUI_RESOURCE_INVALID", "GUI_ROUTE_NOT_FOUND"),
                )
                self.assertNotIn(str(self.root), json.dumps(payload))

    def test_books_lessons_and_health_are_zero_write_and_path_redacted(self):
        before = self.snapshot()
        library = self.library()
        book_id = library["scopes"][0]["books"][0]["id"]
        lesson_id = library["scopes"][0]["lessons"][0]["id"]

        book_status, _, book = self.request("GET", "/api/v1/books/" + book_id)
        lesson_status, _, lesson = self.request(
            "GET",
            "/api/v1/lessons/" + lesson_id,
        )
        health_status, _, health = self.request("GET", "/api/v1/health")
        write_status, _, write_error = self.request("POST", "/api/v1/library")

        self.assertEqual((book_status, lesson_status, health_status), (200, 200, 200))
        self.assertEqual(write_status, 405)
        self.assertEqual(write_error["error"]["token"], "GUI_READ_ONLY")
        self.assertEqual(before, self.snapshot())
        payload = json.dumps(
            [library, book, lesson, health],
            ensure_ascii=False,
        )
        self.assertNotIn(str(self.root), payload)
        self.assertEqual(book["logical_path"], "book/demo/reader.md")
        self.assertEqual(lesson["logical_path"], "lessons/demo/safe-reader.md")
        self.assertIn("<script>", lesson["markdown"])

    def test_stale_index_is_explicit_while_validated_source_is_current(self):
        changed = BOOK.replace("안전한 로컬 책", "바뀐 원문 책")
        (self.knowledge / "book/demo/reader.md").write_text(changed, encoding="utf-8")

        library = self.library()

        self.assertEqual(
            library["health"]["personal_index"]["token"],
            "PERSONAL_INDEX_STALE",
        )
        self.assertFalse(library["health"]["personal_index"]["current"])
        self.assertEqual(library["source_snapshot"]["state"], "validated")
        self.assertEqual(library["scopes"][0]["books"][0]["title"], "바뀐 원문 책")

    def test_book_reader_rejects_raw_html_and_unsafe_link_schemes(self):
        unsafe_bodies = (
            "# unsafe\n\n<script>alert(1)</script>\n",
            "# unsafe\n\n[run](javascript:alert(1))\n",
        )
        path = self.knowledge / "book/demo/reader.md"
        identifier = self.library()["scopes"][0]["books"][0]["id"]
        for body in unsafe_bodies:
            with self.subTest(body=body):
                path.write_text(
                    "---\ntitle: 안전하지 않은 책\nfind_when: [safe, test]\n---\n" + body,
                    encoding="utf-8",
                )
                status, _, payload = self.request(
                    "GET",
                    "/api/v1/books/" + identifier,
                )
                self.assertEqual(status, 422)
                self.assertEqual(payload["error"]["token"], "BOOK_RENDER_REJECTED")
                self.assertNotIn("alert(1)", json.dumps(payload))
                self.assertNotIn(str(self.root), json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
