"""Bounded, read-only startup readiness checks for non-Claude clients."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import sys
import time

from didimlog.connections import _INTEGRATION_REVISION, startup_ready


_INPUT_LIMIT = 16 * 1024
_OUTPUT_LIMIT = 1024
_OPERATION_SECONDS = 1.8
_WARNING = "Didimlog 시작 상태를 확인하지 못했습니다. didim status를 실행하세요."


class _DeadlineExpired(Exception):
    pass


def _deadline_handler(_signal, _frame):
    raise _DeadlineExpired()


def _bounded_fd_input(stream, deadline: float) -> bytes:
    descriptor = stream.fileno()
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    content = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise _DeadlineExpired()
            chunk = os.read(descriptor, min(4096, _INPUT_LIMIT + 1 - len(content)))
            if not chunk:
                return bytes(content)
            content.extend(chunk)
            if len(content) > _INPUT_LIMIT:
                raise ValueError("startup input is too large")
    finally:
        selector.close()


def _bounded_input(stream, deadline: float) -> bytes:
    try:
        stream.fileno()
    except (AttributeError, OSError):
        raw = stream.read(_INPUT_LIMIT + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes) or len(raw) > _INPUT_LIMIT:
            raise ValueError("startup input is invalid")
        return raw
    return _bounded_fd_input(stream, deadline)


def _cwd_from_codex(stream, deadline: float) -> Path | None:
    raw = _bounded_input(stream, deadline)
    try:
        value = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("startup input is invalid") from None
    if not isinstance(value, dict):
        raise ValueError("startup input is invalid")
    cwd = value.get("cwd")
    if cwd is None:
        return None
    if not isinstance(cwd, str) or "\x00" in cwd:
        raise ValueError("startup cwd is invalid")
    return Path(os.path.abspath(cwd))


def _write_bounded(stdout, text: str) -> None:
    encoded = text.encode("utf-8")
    if len(encoded) > _OUTPUT_LIMIT:
        raise ValueError("startup output is too large")
    try:
        stdout.write(text)
    except TypeError:
        stdout.write(encoded)


def startup_check(
    *,
    client: str,
    root: Path,
    revision: str,
    cwd: str | None,
    stdin,
    stdout,
    home: Path | None = None,
) -> int:
    """Run one fail-open readiness advisory with no source traversal or writes."""
    deadline = time.monotonic() + _OPERATION_SECONDS
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, _deadline_handler)
    signal.setitimer(signal.ITIMER_REAL, _OPERATION_SECONDS)
    warning = False
    try:
        if client not in ("omp", "codex") or revision != _INTEGRATION_REVISION:
            warning = True
        else:
            selected_cwd = (
                _cwd_from_codex(stdin, deadline)
                if client == "codex"
                else None if cwd is None else Path(os.path.abspath(cwd))
            )
            selected_home = Path.home() if home is None else Path(home)
            warning = not startup_ready(
                client,
                root=root,
                home=Path(os.path.abspath(selected_home)),
                cwd=selected_cwd,
            )
    except BaseException:
        warning = True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)

    if warning:
        if client == "codex":
            _write_bounded(
                stdout,
                json.dumps({"systemMessage": _WARNING}, ensure_ascii=False) + "\n",
            )
        else:
            _write_bounded(stdout, "DIDIMLOG_STARTUP_WARNING\n")
    return 0
