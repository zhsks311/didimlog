"""lesson frontmatter의 단일 결정론 계약."""

import datetime
import os
import re
from pathlib import Path

from didimlog.file_io import UnsafePathError, read_regular_file_beneath


SLUG = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BLOCK_VALUE = re.compile(r"^[|>][+-]?$")
REQUIRED = ("topic", "title", "summary", "date")
TITLE_MAX_SCALARS = 120
INDEX_TERM_MAX_SCALARS = 32
INDEX_TERM_MAX_BYTES = 96
INDEX_TERMS_MAX_ITEMS = 20
LESSON_MAX_BYTES = 64 * 1024


def valid_index_title(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= TITLE_MAX_SCALARS
        and not BLOCK_VALUE.fullmatch(value)
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def valid_index_term(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= INDEX_TERM_MAX_SCALARS
        and len(value.encode("utf-8")) <= INDEX_TERM_MAX_BYTES
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def parse_inline_list(raw, canonical=False, unique=True):
    raw = (raw or "").strip()
    if not raw.startswith("[") or not raw.endswith("]"):
        return None
    body = raw[1:-1].strip()
    if not body:
        return []
    values = []
    for item in body.split(","):
        value = item.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not value or any(char in value for char in "\t\r\n[]"):
            return None
        values.append(value)
    if len(values) > INDEX_TERMS_MAX_ITEMS:
        return None
    if any(not valid_index_term(value) for value in values):
        return None
    if canonical and values != sorted(values, key=lambda value: value.encode("utf-8")):
        return None
    if unique and len(values) != len(set(values)):
        return None
    return values


def parse_booked(raw):
    values = parse_inline_list(raw)
    if values is None or not all(SLUG.fullmatch(value) for value in values):
        return []
    return values


def _valid_date(value):
    if not DATE.fullmatch(value or ""):
        return False
    try:
        datetime.date.fromisoformat(value)
        return True
    except ValueError:
        return False


def parse_frontmatter_text(name, text, required):
    if name != os.path.basename(name) or not name.endswith(".md") or "\r" in text:
        return None
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return None
    fields, closing = {}, None
    for index, line in enumerate(lines[1:], start=1):
        if line == "---":
            closing = index
            break
        if not line or line[0].isspace() or ":" not in line:
            return None
        key, separator, value = line.partition(": ")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key)
            or not value
            or key in fields
        ):
            return None
        fields[key] = value
    if closing is None or any(not fields.get(key) for key in required):
        return None
    return fields, lines, closing


def parse_lesson_text(name, text):
    """파일명과 문자열이 계약을 만족하면 파싱 결과를, 아니면 None을 반환한다."""
    if not name.endswith(".md") or not SLUG.fullmatch(name[:-3]):
        return None
    parsed = parse_frontmatter_text(name, text, REQUIRED)
    if parsed is None:
        return None
    fields, lines, closing = parsed
    if not SLUG.fullmatch(fields["topic"]):
        return None
    if not valid_index_term(fields["topic"]):
        return None
    if not valid_index_title(fields["title"]):
        return None
    if BLOCK_VALUE.fullmatch(fields["summary"]) or any(
        ord(char) < 32 or ord(char) == 127 for char in fields["summary"]
    ):
        return None
    if not _valid_date(fields["date"]):
        return None
    if fields.get("review_by") and not _valid_date(fields["review_by"]):
        return None
    if fields.get("tags") is not None and parse_inline_list(
        fields["tags"], unique=False
    ) is None:
        return None
    if fields.get("booked") is not None:
        values = parse_inline_list(fields["booked"])
        if values is None or not all(SLUG.fullmatch(value) for value in values):
            return None
    return fields, lines, closing


def parse_lesson(path, root=None):
    candidate = Path(path)
    base = Path(root) if root is not None else candidate.parent
    try:
        relative = candidate.relative_to(base)
        data = read_regular_file_beneath(base, relative, LESSON_MAX_BYTES)
        if len(data) > LESSON_MAX_BYTES:
            return None
        text = data.decode("utf-8")
    except (UnsafePathError, UnicodeDecodeError, ValueError):
        return None
    return parse_lesson_text(candidate.name, text)
