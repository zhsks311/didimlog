"""Didimlog v1 project record scalar and document contracts."""

import datetime
import json
import re
import unicodedata
from pathlib import PurePosixPath


from didimlog.errors import DidimError, EXIT_GIT, EXIT_POLICY, EXIT_USAGE


SCHEMA_VERSION = 1
STATUS_DRAFT = "draft"
INITIAL_VERSION = 1

TYPE_BY_PREFIX = {"OBS": "observation", "EXP": "experiment", "EVD": "evidence"}
PREFIX_BY_TYPE = {value: key for key, value in TYPE_BY_PREFIX.items()}
STAGE1_TYPES = ("observation", "experiment", "evidence")

SOURCE_PREFIXES = ("EVD", "EXP")
CONTRADICTS_PREFIXES = ("OBS", "EXP", "EVD")
RESULT_VALUES = ("success", "failure", "inconclusive")

STAGE1_ID_RE = re.compile(r"^(OBS|EXP|EVD)-[0-9]{8}-[0-9]{2}$")
SOURCE_ID_RE = re.compile(r"^(EVD|EXP)-[0-9]{8}-[0-9]{2}$")
CONTRADICTS_ID_RE = re.compile(r"^(OBS|EXP|EVD)-[0-9]{8}-[0-9]{2}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SCOPE_RE = re.compile(r"^(project|task:[A-Za-z0-9._-]{1,64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_OID_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
TITLE_FORBIDDEN_RE = re.compile(r"[\r\n\u0000-\u001F\u007F]")

TAG_MAX_SCALARS = 32
TAG_MAX_BYTES = 96
TAGS_MAX_ITEMS = 20
SOURCES_MAX_ITEMS = 50
TITLE_MAX_SCALARS = 120
ARTIFACT_PATH_MAX = 1024
RECORD_MAX_BYTES = 12_000
RECORD_MAX_LINES = 200
VERSION_MAX = 2_147_483_647

STATUS_BY_TYPE = {
    "observation": ("draft", "validated", "refuted", "superseded"),
    "evidence": ("draft", "validated", "refuted", "superseded"),
    "experiment": ("draft", "running", "validated", "refuted", "superseded"),
}

COMMON_KEY_ORDER = (
    "schema_version",
    "id",
    "type",
    "title",
    "status",
    "scope",
    "created",
    "updated",
    "version",
    "tags",
    "sources",
)
OPTIONAL_META_KEYS = ("review_by", "supersedes", "superseded_by")
EVD_ARTIFACT_KEYS = ("artifact_path", "artifact_sha256", "artifact_git")

BODY_HEADINGS = {
    "observation": ("Observation",),
    "experiment": ("Hypothesis", "Method", "Result", "Interpretation"),
    "evidence": ("Artifact", "Origin", "Collection"),
}


class UsageError(DidimError):
    """Command usage error with the shared usage exit code."""
    exit_code = EXIT_USAGE

    def __init__(self, token: str) -> None:
        super().__init__(token, exit_code=self.exit_code)


class SchemaError(DidimError):
    """Record schema error with the shared usage exit code."""
    exit_code = EXIT_USAGE

    def __init__(self, token: str) -> None:
        super().__init__(token, exit_code=self.exit_code)


class PolicyError(DidimError):
    """Record policy error with the shared policy exit code."""
    exit_code = EXIT_POLICY

    def __init__(self, token: str) -> None:
        super().__init__(token, exit_code=self.exit_code)


class GitUnavailable(DidimError):
    """Unavailable Git baseline with the shared Git exit code."""
    exit_code = EXIT_GIT

    def __init__(self, token: str) -> None:
        super().__init__(token, exit_code=self.exit_code)


def parse_date(value):
    """Return a real Gregorian date in exact ``YYYY-MM-DD`` form."""
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise SchemaError("INVALID_DATE {}".format(value))
    try:
        datetime.date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        raise SchemaError("INVALID_DATE {}".format(value)) from None
    return value


def parse_stage1_id(value):
    """Return a valid v1 OBS, EXP, or EVD identifier."""
    if not isinstance(value, str) or not STAGE1_ID_RE.fullmatch(value):
        raise SchemaError("INVALID_ID {}".format(value))
    return value


def _check_id_matches(record_id, record_type, created):
    if TYPE_BY_PREFIX.get(record_id[:3]) != record_type:
        raise SchemaError("ID_TYPE_MISMATCH {} {}".format(record_id, record_type))
    if record_id[4:12] != created.replace("-", ""):
        raise SchemaError("ID_DATE_MISMATCH {} {}".format(record_id, created))


def parse_scope(value):
    """Return a project or task scope, rejecting global scopes by policy."""
    if not isinstance(value, str):
        raise SchemaError("INVALID_SCOPE {}".format(value))
    if value == "global" or value.startswith("tech:"):
        raise PolicyError("FORBIDDEN_SCOPE {}".format(value))
    if not SCOPE_RE.fullmatch(value):
        raise SchemaError("INVALID_SCOPE {}".format(value))
    return value


def parse_title(value):
    """Return a one-line title of 1 through 120 Unicode scalars."""
    if not isinstance(value, str):
        raise SchemaError("INVALID_TITLE")
    if not 1 <= len(value) <= TITLE_MAX_SCALARS:
        raise SchemaError("INVALID_TITLE length")
    if TITLE_FORBIDDEN_RE.search(value):
        raise SchemaError("INVALID_TITLE control")
    return value


def _canonicalize_tag(raw):
    normalized = unicodedata.normalize("NFKC", raw)
    return "".join(char.lower() if "A" <= char <= "Z" else char for char in normalized)


def _validate_tag(tag):
    if not 1 <= len(tag) <= TAG_MAX_SCALARS:
        raise SchemaError("INVALID_TAG {}".format(tag))
    try:
        encoded_size = len(tag.encode("utf-8"))
    except UnicodeEncodeError:
        raise SchemaError("INVALID_TAG {}".format(tag)) from None
    if encoded_size > TAG_MAX_BYTES:
        raise SchemaError("INVALID_TAG {}".format(tag))
    first_category = unicodedata.category(tag[0])
    if not (first_category.startswith("L") or first_category.startswith("N")):
        raise SchemaError("INVALID_TAG {}".format(tag))
    for char in tag[1:]:
        category = unicodedata.category(char)
        if not (
            category.startswith("L")
            or category.startswith("N")
            or char in "._-"
        ):
            raise SchemaError("INVALID_TAG {}".format(tag))
    return tag


def _check_sorted_unique(items, field):
    if len(set(items)) != len(items):
        raise SchemaError("DUPLICATE {}".format(field))
    previous = None
    for item in items:
        encoded = item.encode("utf-8")
        if previous is not None and previous > encoded:
            raise SchemaError("UNSORTED {}".format(field))
        previous = encoded


def _validate_stored_tags(tags):
    if not isinstance(tags, list):
        raise SchemaError("INVALID_TAGS")
    if len(tags) > TAGS_MAX_ITEMS:
        raise SchemaError("INVALID_TAGS maxItems")
    for tag in tags:
        if not isinstance(tag, str):
            raise SchemaError("INVALID_TAG {}".format(tag))
        if _canonicalize_tag(tag) != tag:
            raise SchemaError("NONCANONICAL_TAG {}".format(tag))
        _validate_tag(tag)
    _check_sorted_unique(tags, "tags")
    return tags


def _validate_stored_sources(sources):
    if not isinstance(sources, list):
        raise SchemaError("INVALID_SOURCES")
    if len(sources) > SOURCES_MAX_ITEMS:
        raise SchemaError("INVALID_SOURCES maxItems")
    for source_id in sources:
        if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
            raise SchemaError("INVALID_SOURCE {}".format(source_id))
    _check_sorted_unique(sources, "sources")
    return sources


def _validate_supersession(value, record_id, field):
    if not isinstance(value, str):
        raise SchemaError("INVALID_{} {}".format(field.upper(), record_id))
    if value == "":
        return
    if not STAGE1_ID_RE.fullmatch(value):
        raise SchemaError("INVALID_{} {}".format(field.upper(), record_id))
    if value == record_id:
        raise PolicyError("SELF_SUPERSESSION {} {}".format(record_id, field))
    if value[:3] != record_id[:3]:
        raise PolicyError("CROSS_TYPE_SUPERSESSION {} {}".format(record_id, field))


def _validate_artifact_path(artifact_path, evidence_id):
    if not isinstance(artifact_path, str):
        raise SchemaError("INVALID_ARTIFACT_PATH {}".format(evidence_id))
    if not 1 <= len(artifact_path) <= ARTIFACT_PATH_MAX:
        raise SchemaError("INVALID_ARTIFACT_PATH {}".format(evidence_id))
    if "\\" in artifact_path or any(ord(char) < 0x20 for char in artifact_path):
        raise SchemaError("INVALID_ARTIFACT_PATH {}".format(evidence_id))
    path = PurePosixPath(artifact_path)
    parts = artifact_path.split("/")
    if (
        path.is_absolute()
        or artifact_path.endswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or path.as_posix() != artifact_path
    ):
        raise SchemaError("INVALID_ARTIFACT_PATH {}".format(evidence_id))
    return artifact_path


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_frontmatter(frontmatter):
    """Validate exact v1 frontmatter keys, order, scalars, and list forms."""
    keys = list(frontmatter.keys())
    record_type = frontmatter.get("type")
    if not isinstance(record_type, str) or record_type not in STAGE1_TYPES:
        raise SchemaError("FUTURE_TYPE {}".format(record_type))

    for key in COMMON_KEY_ORDER:
        if key not in frontmatter:
            raise SchemaError("MISSING_KEY {}".format(key))
    if record_type == "evidence" and "artifact_path" not in frontmatter:
        raise SchemaError("MISSING_KEY artifact_path")

    record_id_for_error = (
        frontmatter["id"] if isinstance(frontmatter.get("id"), str) else "?"
    )
    allowed = set(COMMON_KEY_ORDER) | set(OPTIONAL_META_KEYS)
    if record_type == "evidence":
        allowed.update(EVD_ARTIFACT_KEYS)
    for key in keys:
        if key not in allowed:
            raise SchemaError(
                "UNKNOWN_KEY {} {}".format(record_id_for_error, key)
            )

    full_order = COMMON_KEY_ORDER + OPTIONAL_META_KEYS
    if record_type == "evidence":
        full_order += EVD_ARTIFACT_KEYS
    expected_order = [key for key in full_order if key in frontmatter]
    if keys != expected_order:
        raise SchemaError("FRONTMATTER_ORDER {}".format(record_id_for_error))

    schema_version = frontmatter["schema_version"]
    if not _is_int(schema_version) or schema_version != SCHEMA_VERSION:
        raise SchemaError("INVALID_SCHEMA_VERSION {}".format(schema_version))

    record_id = parse_stage1_id(frontmatter["id"])
    if TYPE_BY_PREFIX.get(record_id[:3]) != record_type:
        raise SchemaError(
            "ID_TYPE_MISMATCH {} {}".format(record_id, record_type)
        )

    title = parse_title(frontmatter["title"])
    status = frontmatter["status"]
    if not isinstance(status, str) or status not in STATUS_BY_TYPE[record_type]:
        raise SchemaError("INVALID_STATUS {} {}".format(record_id, status))

    scope = parse_scope(frontmatter["scope"])
    created = parse_date(frontmatter["created"])
    _check_id_matches(record_id, record_type, created)
    updated = parse_date(frontmatter["updated"])
    if updated < created:
        raise SchemaError("UPDATED_BEFORE_CREATED {}".format(record_id))

    version = frontmatter["version"]
    if not _is_int(version) or not 1 <= version <= VERSION_MAX:
        raise SchemaError("INVALID_VERSION {}".format(record_id))

    tags = _validate_stored_tags(frontmatter["tags"])
    sources = _validate_stored_sources(frontmatter["sources"])

    review_by = None
    if "review_by" in frontmatter:
        review_by = parse_date(frontmatter["review_by"])
    for field in ("supersedes", "superseded_by"):
        if field in frontmatter:
            _validate_supersession(frontmatter[field], record_id, field)

    record = {
        "id": record_id,
        "type": record_type,
        "title": title,
        "status": status,
        "scope": scope,
        "created": created,
        "updated": updated,
        "version": version,
        "tags": tags,
        "sources": sources,
        "review_by": review_by,
        "result": None,
        "contradicts": [],
        "supersedes": frontmatter.get("supersedes") or None,
        "superseded_by": frontmatter.get("superseded_by") or None,
    }

    if record_type == "evidence":
        artifact_path = _validate_artifact_path(
            frontmatter["artifact_path"], record_id
        )
        has_sha256 = "artifact_sha256" in frontmatter
        has_git = "artifact_git" in frontmatter
        if has_sha256 == has_git:
            raise SchemaError("ARTIFACT_MODE {}".format(record_id))
        record["artifact_path"] = artifact_path
        if has_sha256:
            artifact_sha256 = frontmatter["artifact_sha256"]
            if not isinstance(artifact_sha256, str) or not SHA256_RE.fullmatch(
                artifact_sha256
            ):
                raise SchemaError("INVALID_ARTIFACT_SHA256 {}".format(record_id))
            record["artifact_mode"] = "local"
            record["artifact_sha256"] = artifact_sha256
        else:
            artifact_git = frontmatter["artifact_git"]
            if not isinstance(artifact_git, str) or not GIT_OID_RE.fullmatch(
                artifact_git
            ):
                raise SchemaError("INVALID_ARTIFACT_GIT {}".format(record_id))
            record["artifact_mode"] = "git"
            record["artifact_git"] = artifact_git
    return record


def _split_h2_sections(body):
    preamble = []
    sections = []
    title = None
    content = None
    for line in body.split("\n"):
        if line.startswith("## "):
            if title is not None:
                sections.append((title, content))
            title = line[3:]
            content = []
        elif title is None:
            preamble.append(line)
        else:
            content.append(line)
    if title is not None:
        sections.append((title, content))
    return preamble, sections


def _first_nonempty(lines):
    for line in lines:
        if line.strip() != "":
            return line
    return None


def _parse_contradicts(value):
    if value == "none":
        return []
    identifiers = []
    for part in value.split(","):
        identifier = part.strip(" ")
        if not CONTRADICTS_ID_RE.fullmatch(identifier):
            raise SchemaError("INVALID_CONTRADICTS {}".format(value))
        identifiers.append(identifier)
    _check_sorted_unique(identifiers, "contradicts")
    return identifiers


def _parse_contradiction_line(line, record_id):
    prefix = "Contradicts: "
    if line is None or not line.startswith(prefix):
        raise SchemaError("INVALID_CONTRADICTS {}".format(record_id))
    return _parse_contradicts(line[len(prefix) :])


def validate_body(record, body):
    """Validate the exact H2 sequence and required content for a v1 record."""
    record_type = record["type"]
    record_id = record["id"]
    preamble, sections = _split_h2_sections(body)
    if any(line.strip() != "" for line in preamble):
        raise SchemaError("INVALID_HEADINGS {}".format(record_id))
    titles = [title for title, _ in sections]
    if titles != list(BODY_HEADINGS[record_type]):
        raise SchemaError("INVALID_HEADINGS {}".format(record_id))
    for title, content in sections:
        if _first_nonempty(content) is None:
            raise SchemaError("EMPTY_SECTION {} {}".format(record_id, title))

    if record_type == "experiment":
        result_line = _first_nonempty(sections[2][1])
        if result_line not in RESULT_VALUES:
            raise SchemaError("INVALID_RESULT {}".format(record_id))
        record["result"] = result_line
        interpretation = sections[3][1]
        contradiction_line = _first_nonempty(interpretation)
        record["contradicts"] = _parse_contradiction_line(
            contradiction_line, record_id
        )
        remainder = interpretation[interpretation.index(contradiction_line) + 1 :]
        if _first_nonempty(remainder) is None:
            raise SchemaError("EMPTY_INTERPRETATION {}".format(record_id))
    elif record_type == "evidence":
        artifact_lines = [
            line for line in sections[0][1] if line.strip() != ""
        ]
        if artifact_lines != [record["artifact_path"]]:
            raise SchemaError("ARTIFACT_BODY_MISMATCH {}".format(record_id))
    return record


def _render_toml_value(value):
    if isinstance(value, bool):
        raise TypeError("boolean is not a valid record value")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(
            json.dumps(item, ensure_ascii=False) for item in value
        ) + "]"
    raise TypeError("unsupported value type: {!r}".format(type(value)))


def _render_frontmatter(fields):
    lines = ["+++"]
    lines.extend(
        "{} = {}".format(key, _render_toml_value(value))
        for key, value in fields
    )
    lines.append("+++")
    return "\n".join(lines) + "\n"


def serialize_record(
    record_id, record_type, title, scope, created, tags, sources, type_fields
):
    """Serialize one canonical v1 record and enforce its byte and LF caps."""
    fields = [
        ("schema_version", SCHEMA_VERSION),
        ("id", record_id),
        ("type", record_type),
        ("title", title),
        ("status", STATUS_DRAFT),
        ("scope", scope),
        ("created", created),
        ("updated", created),
        ("version", INITIAL_VERSION),
        ("tags", tags),
        ("sources", sources),
    ]
    if record_type == "evidence":
        fields.append(("artifact_path", type_fields["artifact_path"]))
        if type_fields["mode"] == "local":
            fields.append(("artifact_sha256", type_fields["artifact_sha256"]))
        else:
            fields.append(("artifact_git", type_fields["artifact_git"]))

    document = _render_frontmatter(fields) + "\n" + type_fields["body"]
    try:
        encoded = document.encode("utf-8")
    except UnicodeEncodeError:
        raise SchemaError("INVALID_UTF8 {}".format(record_id)) from None
    if len(encoded) > RECORD_MAX_BYTES:
        raise SchemaError("RECORD_TOO_LARGE {}".format(record_id))
    if encoded.count(b"\n") > RECORD_MAX_LINES:
        raise SchemaError("RECORD_TOO_MANY_LINES {}".format(record_id))
    return document

