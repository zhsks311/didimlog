#!/usr/bin/env python3
"""Scan staged Git blobs for potential secrets without exposing their values."""

from __future__ import annotations

from collections.abc import Sequence
import re
import subprocess
import sys

_GIT_TIMEOUT_SECONDS = 5


PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    # Stripe publishable keys (pk_) are intentionally excluded.
    (
        "API secret key",
        re.compile(
            rb"(?:sk-[A-Za-z0-9_-]{16,}|(?:sk_(?:live|test|org)|rk_(?:live|test))_[A-Za-z0-9]{16,})"
        ),
    ),
    ("GitHub token", re.compile(rb"\b(?:ghp_|gho_|github_pat_)[A-Za-z0-9_]{20,}\b")),
    ("Slack token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("AWS access key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    (
        "JWT",
        re.compile(
            rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        ),
    ),
)


def is_binary(data: bytes) -> bool:
    """Return whether the initial content contains a NUL byte."""
    return b"\0" in data[:8192]


def scan_bytes(data: bytes) -> list[str]:
    """Return secret kinds found in text-like data, never the matched values."""
    if is_binary(data):
        return []
    return [kind for kind, pattern in PATTERNS if pattern.search(data)]


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    """Run a Git read operation and capture its byte streams."""
    return subprocess.run(
        ("git",) + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def staged_paths() -> list[bytes]:
    """Return only added, copied, modified, renamed, and type-changed index paths."""
    result = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT")
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or "git diff failed")
    return [path for path in result.stdout.split(b"\0") if path]


def staged_blob(raw_path: bytes) -> bytes:
    """Read the exact stage-zero blob for a repository-relative path."""
    path = raw_path.decode("utf-8", "surrogateescape")
    # The explicit relative prefix prevents names such as ``0:notes.md`` from
    # being parsed as Git's stage-selection syntax.
    result = git("show", ":./" + path)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(message or "git show failed: " + path)
    return result.stdout


def main(argv: Sequence[str] | None = None) -> int:
    """Scan staged blobs and return zero, blocked, or scan-error status."""
    del argv  # Reserved for a stable callable CLI interface.
    findings: list[tuple[str, str]] = []
    try:
        for raw_path in staged_paths():
            path = raw_path.decode("utf-8", "surrogateescape")
            for kind in scan_bytes(staged_blob(raw_path)):
                findings.append((path, kind))
    except Exception as exc:
        print("SECRET_SCAN_ERROR: " + str(exc), file=sys.stderr)
        return 2

    if not findings:
        return 0

    print("SECRET_SCAN_BLOCKED: staged content may contain secrets", file=sys.stderr)
    for path, kind in findings:
        print(f"- {path}: {kind}", file=sys.stderr)
    print(
        "Remove or replace the value, then stage the safe content again.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
