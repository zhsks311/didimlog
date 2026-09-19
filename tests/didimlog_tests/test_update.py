import contextlib
import errno
import io
import json
import os
import pty
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from didimlog import cli, update


class TerminalBuffer(io.StringIO):
    def __init__(self, *, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False

    def read(self, size):
        self.read_sizes.append(size)
        return self.payload[:size]


class RecordingOpener:
    def __init__(self, payload):
        self.response = FakeResponse(payload)
        self.calls = []

    def __call__(self, url, *, timeout):
        self.calls.append((url, timeout))
        return self.response


def pypi_payload(version):
    return json.dumps({"info": {"version": version}}).encode("utf-8")


class StableVersionTests(unittest.TestCase):
    def test_only_strict_numeric_stable_versions_are_comparable(self):
        self.assertTrue(update.is_newer_stable("0.0.5", "0.0.6"))
        self.assertFalse(update.is_newer_stable("0.0.5", "0.0.5"))
        self.assertFalse(update.is_newer_stable("0.0.6", "0.0.5"))
        for value in ("0.0.6rc1", "v0.0.6", "0.0", "0.0.6.1", "01.0.6"):
            with self.subTest(value=value):
                self.assertFalse(update.is_newer_stable("0.0.5", value))
        self.assertFalse(update.is_newer_stable("0.0.5.dev1", "0.0.6"))


class AutomaticUpdateNoticeTests(unittest.TestCase):
    def run_notice(
        self,
        home,
        *,
        installed="0.0.5",
        latest="0.0.6",
        now=100_000,
        environ=None,
        opener=None,
    ):
        stderr = io.StringIO()
        selected_opener = opener or RecordingOpener(pypi_payload(latest))
        update.automatic_update_notice(
            installed,
            stderr=stderr,
            environ={} if environ is None else environ,
            home=home,
            now=now,
            opener=selected_opener,
        )
        return stderr.getvalue(), selected_opener

    def test_newer_release_is_noticed_and_success_cache_suppresses_one_day(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            home.mkdir()
            first_output, first_opener = self.run_notice(home)
            second_output, second_opener = self.run_notice(home, now=100_060)

            cache_path = home / ".cache" / "didimlog" / "update.json"
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            cache_mode = stat.S_IMODE(cache_path.stat().st_mode)
            directory_mode = stat.S_IMODE(cache_path.parent.stat().st_mode)

        self.assertEqual(
            first_output,
            "Didimlog 0.0.6 업데이트 가능 — uv tool upgrade didimlog\n",
        )
        self.assertEqual(second_output, "")
        self.assertEqual(first_opener.calls, [(update.PYPI_URL, update.REQUEST_TIMEOUT)])
        self.assertEqual(second_opener.calls, [])
        self.assertEqual(
            first_opener.response.read_sizes,
            [update.RESPONSE_MAX_BYTES + 1],
        )
        self.assertEqual(cache, {"checked_at": 100_000, "latest": "0.0.6"})
        self.assertEqual(cache_mode, 0o600)
        self.assertEqual(directory_mode, 0o700)

    def test_growing_project_metadata_above_64_kib_still_allows_notice(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            home.mkdir()
            payload = json.dumps(
                {
                    "info": {
                        "version": "0.0.6",
                        "description": "x" * (96 * 1024),
                    }
                }
            ).encode("utf-8")
            opener = RecordingOpener(payload)

            output, _ = self.run_notice(home, opener=opener)

        self.assertGreater(len(payload), 64 * 1024)
        self.assertLess(len(payload), update.RESPONSE_MAX_BYTES)
        self.assertIn("Didimlog 0.0.6 업데이트 가능", output)

    def test_slow_response_stops_at_total_request_deadline(self):
        release = threading.Event()
        finished = threading.Event()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        def blocked_read(_size):
            try:
                release.wait(timeout=2)
                return pypi_payload("0.0.6")
            finally:
                finished.set()

        response.read.side_effect = blocked_read
        opener = mock.Mock(return_value=response)
        timer = threading.Timer(1.0, release.set)
        timer.start()
        try:
            with mock.patch.object(update, "REQUEST_TIMEOUT", 0.02):
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    update._fetch_latest(opener)
                elapsed = time.monotonic() - started
        finally:
            release.set()
            timer.cancel()
            self.assertTrue(finished.wait(timeout=1))

        self.assertLess(elapsed, 0.5)
        self.assertTrue(update._FETCH_GUARD.acquire(timeout=1))
        update._FETCH_GUARD.release()

    def test_absolute_xdg_cache_home_selects_the_cache_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir()
            cache_root = root / "selected-cache"

            output, _ = self.run_notice(
                home,
                environ={"XDG_CACHE_HOME": str(cache_root)},
            )

            self.assertIn("업데이트 가능", output)
            self.assertTrue((cache_root / "didimlog" / "update.json").is_file())
            self.assertFalse((home / ".cache").exists())

    def test_current_newer_installed_and_local_versions_do_not_emit_false_notice(self):
        scenarios = (
            ("0.0.5", "0.0.5"),
            ("0.0.6", "0.0.5"),
            ("0.0.5.dev1", "0.0.6"),
        )
        for installed, latest in scenarios:
            with self.subTest(installed=installed, latest=latest), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary).resolve() / "home"
                home.mkdir()
                output, opener = self.run_notice(
                    home,
                    installed=installed,
                    latest=latest,
                )

            self.assertEqual(output, "")
            self.assertEqual(len(opener.calls), 1)

    def test_disable_environment_prevents_request_and_cache_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            home.mkdir()
            output, opener = self.run_notice(
                home,
                environ={"DIDIM_NO_UPDATE_CHECK": "1"},
            )

            self.assertEqual(output, "")
            self.assertEqual(opener.calls, [])
            self.assertFalse((home / ".cache").exists())

    def test_network_and_response_failures_are_silent_and_not_cached(self):
        def offline(url, *, timeout):
            raise OSError("offline")

        oversized = RecordingOpener(b"x" * (update.RESPONSE_MAX_BYTES + 1))
        malformed = RecordingOpener(b"not json")
        invalid_version = RecordingOpener(pypi_payload("0.0.6rc1"))
        scenarios = (offline, oversized, malformed, invalid_version)
        for opener in scenarios:
            with self.subTest(opener=opener), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary).resolve() / "home"
                home.mkdir()
                output, _ = self.run_notice(home, opener=opener)

                self.assertEqual(output, "")
                self.assertFalse(
                    (home / ".cache" / "didimlog" / "update.json").exists()
                )

    def test_corrupt_cache_is_replaced_after_a_successful_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            cache = home / ".cache" / "didimlog" / "update.json"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"not-json")
            cache.chmod(0o600)

            output, _ = self.run_notice(home)

            self.assertIn("업데이트 가능", output)
            self.assertEqual(json.loads(cache.read_text()), {
                "checked_at": 100_000,
                "latest": "0.0.6",
            })

    def test_non_private_cache_aborts_before_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            cache = home / ".cache" / "didimlog" / "update.json"
            cache.parent.mkdir(parents=True)
            cache.write_text(
                '{"checked_at":100000,"latest":"0.0.6"}\n',
                encoding="utf-8",
            )
            cache.chmod(0o644)

            output, opener = self.run_notice(home, now=200_000)

            self.assertEqual(output, "")
            self.assertEqual(opener.calls, [])
            self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o644)

    def test_symlink_cache_aborts_before_request_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            cache = home / ".cache" / "didimlog" / "update.json"
            cache.parent.mkdir(parents=True)
            target = root / "outside.json"
            target.write_text("outside", encoding="utf-8")
            cache.symlink_to(target)

            output, opener = self.run_notice(home)

            self.assertEqual(output, "")
            self.assertEqual(opener.calls, [])
            self.assertEqual(target.read_text(encoding="utf-8"), "outside")

    def test_symlink_cache_parent_aborts_before_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            outside = root / "outside"
            home.mkdir()
            outside.mkdir()
            (home / ".cache").symlink_to(outside, target_is_directory=True)

            output, opener = self.run_notice(home)

            self.assertEqual(output, "")
            self.assertEqual(opener.calls, [])
            self.assertFalse((outside / "didimlog").exists())

    def test_concurrent_cache_change_suppresses_notice(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve() / "home"
            home.mkdir()
            with mock.patch(
                "didimlog.update.write_regular_file_if_unchanged",
                side_effect=ValueError("changed"),
            ):
                output, opener = self.run_notice(home)

        self.assertEqual(output, "")
        self.assertEqual(len(opener.calls), 1)


class AutomaticCliIntegrationTests(unittest.TestCase):
    @staticmethod
    def _tree_bytes(root):
        return {
            path.relative_to(root).as_posix(): (
                None if path.is_dir() else path.read_bytes()
            )
            for path in sorted(root.rglob("*"))
        }

    def run_real_tty(self, root, argv, *, stdin=b"", home_directories=()):
        home = root / "home"
        workspace = root / "workspace"
        cache = root / "cache"
        bin_directory = root / "bin"
        home.mkdir()
        workspace.mkdir()
        bin_directory.mkdir()
        for relative_directory in home_directories:
            (home / relative_directory).mkdir(parents=True)
        launcher = bin_directory / "didim"
        launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        original_home = self._tree_bytes(home)

        proxy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        proxy.bind(("127.0.0.1", 0))
        proxy.listen()
        proxy.setblocking(False)
        proxy_port = proxy.getsockname()[1]

        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "XDG_CACHE_HOME": str(cache),
                "PATH": str(bin_directory)
                + os.pathsep
                + environment.get("PATH", ""),
                "PYTHONDONTWRITEBYTECODE": "1",
                "HTTPS_PROXY": "http://127.0.0.1:{}".format(proxy_port),
                "https_proxy": "http://127.0.0.1:{}".format(proxy_port),
                "http_proxy": "http://127.0.0.1:{}".format(proxy_port),
                "no_proxy": "",
                "HTTP_PROXY": "http://127.0.0.1:{}".format(proxy_port),
                "NO_PROXY": "",
            }
        )
        environment.pop("DIDIM_NO_UPDATE_CHECK", None)
        source_root = Path(__file__).resolve().parents[2] / "src"
        environment["PYTHONPATH"] = str(source_root) + os.pathsep + environment.get(
            "PYTHONPATH", ""
        )

        master, slave = pty.openpty()
        process = None
        output = bytearray()
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "didimlog.cli", *argv],
                cwd=workspace,
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
            )
            os.close(slave)
            slave = -1
            os.set_blocking(master, False)
            if stdin:
                os.write(master, stdin)
            deadline = time.monotonic() + 15
            while process.poll() is None:
                try:
                    output.extend(os.read(master, 64 * 1024))
                except BlockingIOError:
                    pass
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                if time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    self.fail("real TTY CLI process timed out")
                time.sleep(0.01)
            os.set_blocking(master, True)
            while True:
                try:
                    chunk = os.read(master, 64 * 1024)
                except BlockingIOError:
                    break
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)

            try:
                connection, _ = proxy.accept()
            except BlockingIOError:
                update_requested = False
            else:
                update_requested = True
                connection.close()
            return (
                process.returncode,
                output.decode("utf-8", errors="replace"),
                home,
                cache,
                update_requested,
                original_home,
            )
        finally:
            if slave >= 0:
                os.close(slave)
            os.close(master)
            proxy.close()
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()

    def test_declined_setup_variants_and_claude_connect_leave_no_writes_or_update_request(
        self,
    ):
        scenarios = (
            (
                "default Claude setup",
                ["setup", "--project-knowledge", "local"],
                (".claude",),
            ),
            (
                "setup without Claude",
                ["setup", "--skip-claude", "--project-knowledge", "local"],
                (),
            ),
            (
                "explicit client setup",
                [
                    "setup",
                    "--client",
                    "omp",
                    "--omp-agent-dir",
                    "{home}/.omp/agent",
                    "--project-knowledge",
                    "local",
                ],
                (".omp/agent",),
            ),
            (
                "Claude connect",
                ["connect", "claude", "--config-dir", "{home}/.claude"],
                (".claude",),
            ),
        )
        for name, unformatted_argv, home_directories in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                expected_home = root / "home"
                argv = [
                    argument.format(home=expected_home)
                    for argument in unformatted_argv
                ]
                (
                    code,
                    output,
                    home,
                    cache,
                    update_requested,
                    original_home,
                ) = self.run_real_tty(
                    root,
                    argv,
                    stdin=b"n\n",
                    home_directories=home_directories,
                )

                self.assertEqual(code, 0, output)
                self.assertIn("변경하지 않았습니다.", output)
                self.assertEqual(self._tree_bytes(root / "workspace"), {})
                self.assertEqual(self._tree_bytes(home), original_home)
                self.assertFalse(cache.exists())
                self.assertFalse(update_requested)

    def test_real_tty_setup_dry_run_leaves_no_writes_or_update_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (
                code,
                output,
                home,
                cache,
                update_requested,
                original_home,
            ) = self.run_real_tty(
                root,
                [
                    "setup",
                    "--dry-run",
                    "--skip-claude",
                    "--project-knowledge",
                    "local",
                ],
            )

            self.assertEqual(code, 0, output)
            self.assertEqual(self._tree_bytes(root / "workspace"), {})
            self.assertEqual(self._tree_bytes(home), original_home)
            self.assertFalse(cache.exists())
            self.assertFalse(update_requested)

    def invoke_process(self, argv, *, tty=True, handler_result=0):
        stdout = TerminalBuffer(tty=False)
        stderr = TerminalBuffer(tty=tty)
        with mock.patch.object(sys, "argv", ["didim", *argv]), mock.patch(
            "didimlog.cli._status",
            return_value=handler_result,
        ), mock.patch(
            "didimlog.cli.automatic_update_notice"
        ) as notice, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()
        return code, stdout.getvalue(), stderr.getvalue(), notice


    def test_approved_real_tty_setup_remains_update_eligible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (
                code,
                output,
                home,
                cache,
                update_requested,
                original_home,
            ) = self.run_real_tty(
                root,
                [
                    "setup",
                    "--yes",
                    "--skip-claude",
                    "--project-knowledge",
                    "local",
                ],
            )

            self.assertEqual(code, 0, output)
            self.assertIn("Didimlog 준비를 마쳤습니다.", output)
            self.assertNotEqual(self._tree_bytes(home), original_home)
            self.assertTrue((home / "knowledge/MY-RULES.md").is_file())
            self.assertTrue(cache.is_dir())
            self.assertTrue(update_requested)

    def test_unexpected_checker_failure_preserves_successful_command(self):
        stdout = TerminalBuffer(tty=False)
        stderr = TerminalBuffer(tty=True)
        with mock.patch.object(
            sys,
            "argv",
            ["didim", "status"],
        ), mock.patch(
            "didimlog.cli._status",
            return_value=0,
        ), mock.patch(
            "didimlog.cli.automatic_update_notice",
            side_effect=RuntimeError("unexpected"),
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main()

        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_non_tty_and_failed_commands_do_not_check(self):
        for tty, result in ((False, 0), (True, 3)):
            with self.subTest(tty=tty, result=result):
                code, _, _, notice = self.invoke_process(
                    ["status"],
                    tty=tty,
                    handler_result=result,
                )
                self.assertEqual(code, result)
                notice.assert_not_called()

    def test_internal_hook_and_setup_dry_run_do_not_check(self):
        stdout = TerminalBuffer(tty=False)
        stderr = TerminalBuffer(tty=True)
        scenarios = (
            (["hook", "session-start"], "didimlog.cli._session_start"),
            (["setup", "--dry-run"], "didimlog.cli._setup"),
        )
        for argv, handler in scenarios:
            with self.subTest(argv=argv), mock.patch.object(
                sys, "argv", ["didim", *argv]
            ), mock.patch(handler, return_value=0), mock.patch(
                "didimlog.cli.automatic_update_notice"
            ) as notice, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(cli.main(), 0)
                notice.assert_not_called()

    def test_help_and_version_do_not_check(self):
        for argv in (["--help"], ["--version"]):
            with self.subTest(argv=argv), mock.patch.object(
                sys, "argv", ["didim", *argv]
            ), mock.patch(
                "didimlog.cli.automatic_update_notice"
            ) as notice, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                TerminalBuffer(tty=True)
            ):
                self.assertEqual(cli.main(), 0)
                notice.assert_not_called()


if __name__ == "__main__":
    unittest.main()
