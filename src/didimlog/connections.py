"""Small, explicit connection ownership for local coding clients."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import importlib.resources
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
from collections.abc import Mapping

from didimlog.claude.connect import (
    _backup_original,
    _validate_launcher,
    managed_connection_present,
)
from didimlog.claude.transaction import InstallJournal
from didimlog.claude.paths import config_dir as claude_config_dir
from didimlog.conditional_file import (
    read_optional_regular_file,
    write_regular_file_at_if_unchanged,
)
from didimlog.file_io import (
    open_child_directory,
    open_directory_path,
    read_regular_file_at_with_stat,
    read_regular_file_beneath,
)
from didimlog.errors import DidimError, EXIT_POLICY
from didimlog.locking import acquire_directory_lock
from didimlog.personal.paths import data_home

_MAX_SELECTIONS = 32
_MAX_ASSETS_PER_SELECTION = 8
_STARTUP_FILE_LIMIT = 32


_STATE_VERSION = 1
_INTEGRATION_REVISION = "1"
_CLIENTS = ("claude", "omp", "codex")
_INTENTS = ("connected", "disconnected")
_FILE_MAXIMUM_BYTES = 4 * 1024 * 1024
_STATE_MAXIMUM_BYTES = 256 * 1024
_SKILL_FILES = ("SKILL.md", "KNOWLEDGE_USAGE.md", "LESSON_WRITING_RULES.md")
_CODEX_SUFFIX = " hook startup-check --client codex --revision " + _INTEGRATION_REVISION


@dataclass(frozen=True)
class OwnedAsset:
    id: str
    scope: str
    target: str
    sha256: str
    kind: str = "file"


@dataclass(frozen=True)
class Selection:
    intent: str
    assets: tuple[OwnedAsset, ...]


@dataclass(frozen=True)
class ConnectionState:
    clients: dict[str, dict[str, Selection]]


@dataclass(frozen=True)
class ClientStatus:
    client: str
    root: Path
    intent: str
    token: str


@dataclass(frozen=True)
class _Mutation:
    name: str
    path: Path
    original: bytes | None
    intended: bytes | None


@dataclass(frozen=True)
class _Deleted:
    mutation: _Mutation
    parent_descriptor: int


@dataclass(frozen=True)
class _Dependency:
    path: Path
    original: bytes | None


@dataclass(frozen=True)
class ConnectionPlan:
    changes: tuple[str, ...]
    notices: tuple[str, ...]
    _home: Path = field(repr=False, compare=False)
    _home_identity: tuple[int, int] = field(repr=False, compare=False)
    _personal_identity: tuple[int, int] | None = field(repr=False, compare=False)
    _state_path: Path = field(repr=False, compare=False)
    _state_original: bytes | None = field(repr=False, compare=False)
    _state_intended: bytes = field(repr=False, compare=False)
    _files: tuple[_Mutation, ...] = field(repr=False, compare=False)
    _dependencies: tuple[_Dependency, ...] = field(repr=False, compare=False)
    _root_identities: tuple[tuple[Path, tuple[int, int]], ...] = field(
        repr=False, compare=False
    )


def state_path(home: Path) -> Path:
    return data_home(home) / ".didimlog" / "connections.json"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("connection state contains duplicate keys")
        result[key] = value
    return result


def _load_json(raw: bytes, *, label: str):
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError("invalid JSON constant")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is invalid") from error
    return value


def _read_optional(path: Path, maximum: int) -> bytes | None:
    try:
        parent = path.parent.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError("managed parent could not be inspected") from error
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise ValueError("managed parent must be a real directory")
    return read_optional_regular_file(path, maximum)


def _parse_asset(value) -> OwnedAsset:
    if not isinstance(value, dict) or set(value) not in (
        {"id", "scope", "target", "sha256"},
        {"id", "scope", "target", "sha256", "kind"},
    ):
        raise ValueError("connection asset is invalid")
    asset = OwnedAsset(
        id=value["id"],
        scope=value["scope"],
        target=value["target"],
        sha256=value["sha256"],
        kind=value.get("kind", "file"),
    )
    if (
        not isinstance(asset.id, str)
        or not asset.id
        or asset.scope not in ("root", "home")
        or not isinstance(asset.target, str)
        or not asset.target
        or not isinstance(asset.sha256, str)
        or Path(asset.target).is_absolute()
        or ".." in Path(asset.target).parts
        or not re.fullmatch(r"[0-9a-f]{64}", asset.sha256)
        or asset.kind not in ("file", "codex-hook")
    ):
        raise ValueError("connection asset is invalid")
    return asset


def _validate_client_assets(
    client: str,
    assets: tuple[OwnedAsset, ...],
    *,
    intent: str,
) -> None:
    allowed = set()
    if client == "omp":
        allowed.add(("omp-extension", "root", "extensions/didimlog.js", "file"))
        allowed.update(
            (
                "omp-skill:" + name,
                "root",
                "skills/didimlog/" + name,
                "file",
            )
            for name in _SKILL_FILES
        )
    elif client == "codex":
        allowed.add(("codex-hook", "root", "hooks.json", "codex-hook"))
        allowed.update(
            (
                "codex-skill:" + name,
                "home",
                ".agents/skills/didimlog/" + name,
                "file",
            )
            for name in _SKILL_FILES
        )
    elif assets:
        raise ValueError("Claude connection assets must be empty")
    if intent == "disconnected" and not assets:
        return
    actual = {
        (asset.id, asset.scope, asset.target, asset.kind)
        for asset in assets
    }
    if actual != allowed:
        raise ValueError("connection assets do not match the client allowlist")


def parse_state(raw: bytes) -> ConnectionState:
    value = _load_json(raw, label="connection state")
    if not isinstance(value, dict) or set(value) != {"version", "clients"}:
        raise ValueError("connection state root is invalid")
    if value["version"] != _STATE_VERSION or not isinstance(value["clients"], dict):
        raise ValueError("connection state version is unsupported")
    clients: dict[str, dict[str, Selection]] = {}
    for client, roots_value in value["clients"].items():
        if client not in _CLIENTS or not isinstance(roots_value, dict):
            raise ValueError("connection client is invalid")
        roots: dict[str, Selection] = {}
        for root, selection_value in roots_value.items():
            if (
                not isinstance(root, str)
                or not Path(root).is_absolute()
                or not isinstance(selection_value, dict)
                or set(selection_value) != {"intent", "assets"}
                or selection_value["intent"] not in _INTENTS
                or not isinstance(selection_value["assets"], list)
            ):
                raise ValueError("connection selection is invalid")
            if len(selection_value["assets"]) > _MAX_ASSETS_PER_SELECTION:
                raise ValueError("connection selection has too many assets")
            assets = tuple(_parse_asset(item) for item in selection_value["assets"])
            if len({asset.id for asset in assets}) != len(assets):
                raise ValueError("connection asset ids must be unique")
            _validate_client_assets(
                client,
                assets,
                intent=selection_value["intent"],
            )
            roots[root] = Selection(selection_value["intent"], assets)
        clients[client] = roots
    if sum(len(roots) for roots in clients.values()) > _MAX_SELECTIONS:
        raise ValueError("connection state has too many selections")
    return ConnectionState(clients)


def render_state(state: ConnectionState) -> bytes:
    clients = {}
    for client in _CLIENTS:
        roots = state.clients.get(client)
        if not roots:
            continue
        clients[client] = {
            root: {
                "intent": selection.intent,
                "assets": [
                    {
                        "id": asset.id,
                        "scope": asset.scope,
                        "target": asset.target,
                        "sha256": asset.sha256,
                        **({"kind": asset.kind} if asset.kind != "file" else {}),
                    }
                    for asset in selection.assets
                ],
            }
            for root, selection in sorted(roots.items())
        }
    return (
        json.dumps(
            {"version": _STATE_VERSION, "clients": clients},
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def load_state(home: Path) -> tuple[ConnectionState | None, bytes | None]:
    raw = _read_optional(state_path(home), _STATE_MAXIMUM_BYTES)
    return (None if raw is None else parse_state(raw), raw)


def _real_directory(path: Path, *, label: str) -> tuple[Path, tuple[int, int]]:
    try:
        candidate = Path(path).expanduser().absolute()
        linked = candidate.lstat()
        resolved = candidate.resolve(strict=True)
        opened = resolved.stat()
    except (OSError, RuntimeError, TypeError) as error:
        raise ValueError(f"{label} must be an existing directory") from error
    if (
        stat.S_ISLNK(linked.st_mode)
        or not stat.S_ISDIR(linked.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise ValueError(f"{label} must be a real directory")
    return resolved, (opened.st_dev, opened.st_ino)


def selected_root(
    client: str,
    explicit,
    *,
    home: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, tuple[int, int] | None]:
    environment = os.environ if environ is None else environ
    if client == "omp":
        value = explicit or environment.get("PI_CODING_AGENT_DIR") or home / ".omp/agent"
        return _real_directory(Path(value), label="OMP agent directory")
    if client == "codex":
        value = explicit or environment.get("CODEX_HOME") or home / ".codex"
        return _real_directory(Path(value), label="Codex home")
    if client == "claude":
        value = claude_config_dir(explicit, environ=environment, home=home)
        try:
            return _real_directory(value, label="Claude config directory")
        except ValueError:
            if not value.exists() and not value.is_symlink():
                return value.absolute(), None
            raise
    raise ValueError("unsupported connection client")




def _packaged_skill_files() -> tuple[tuple[str, bytes], ...]:
    clients = importlib.resources.files("didimlog.resources.clients")
    personal = importlib.resources.files("didimlog.resources.personal")
    return (
        ("SKILL.md", clients.joinpath("SKILL.md").read_bytes()),
        ("KNOWLEDGE_USAGE.md", personal.joinpath("KNOWLEDGE_USAGE.md").read_bytes()),
        (
            "LESSON_WRITING_RULES.md",
            personal.joinpath("LESSON_WRITING_RULES.md").read_bytes(),
        ),
    )


def _render_omp_extension(
    launcher: Path,
    env_launcher: Path,
    root: Path,
) -> bytes:
    template = importlib.resources.files("didimlog.resources.clients").joinpath(
        "didimlog-omp.js"
    ).read_text(encoding="utf-8")
    return (
        template.replace("__DIDIM_LAUNCHER_JSON__", json.dumps(str(launcher)))
        .replace("__ENV_LAUNCHER_JSON__", json.dumps(str(env_launcher)))
        .replace("__DIDIM_ROOT_JSON__", json.dumps(str(root)))
        .replace("__REVISION_JSON__", json.dumps(_INTEGRATION_REVISION))
        .encode("utf-8")
    )


def _env_launcher() -> Path:
    executable = shutil.which("env")
    if executable is None:
        raise ValueError("env launcher is unavailable")
    return Path(executable).resolve(strict=True)


def _codex_command(launcher: Path, env_launcher: Path, root: Path) -> str:
    arguments = (
        str(env_launcher),
        "PYTHONDONTWRITEBYTECODE=1",
        "DIDIM_NO_UPDATE_CHECK=1",
        str(launcher),
        "hook",
        "startup-check",
        "--client",
        "codex",
        "--root",
        str(root),
        "--revision",
        _INTEGRATION_REVISION,
    )
    return shlex.join(arguments)


def _codex_hook(
    launcher: Path,
    env_launcher: Path,
    root: Path,
) -> dict[str, object]:
    return {
        "type": "command",
        "command": _codex_command(launcher, env_launcher, root),
        "async": True,
        "timeout": 2,
    }


def _codex_startup_tail(value) -> list[str] | None:
    if not isinstance(value, dict) or value.get("type") != "command":
        return None
    command = value.get("command")
    if not isinstance(command, str):
        return None
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    for index in range(len(arguments) - 2):
        if (
            Path(arguments[index]).name == "didim"
            and arguments[index + 1 : index + 3] == ["hook", "startup-check"]
        ):
            return arguments[index + 1 :]
    return None


def _codex_startup_hook(value) -> bool:
    return _codex_startup_tail(value) is not None


def _managed_codex_hook(value) -> bool:
    command = value.get("command") if isinstance(value, dict) else None
    if not isinstance(command, str) or "\n" in command or "\r" in command:
        return False
    tail = _codex_startup_tail(value)
    return (
        tail is not None
        and len(tail) == 8
        and tail[:5]
        == ["hook", "startup-check", "--client", "codex", "--root"]
        and Path(tail[5]).is_absolute()
        and tail[6:] == ["--revision", _INTEGRATION_REVISION]
    )


def _plan_codex_hooks(original: bytes, hook: dict[str, object] | None) -> bytes:
    value = {} if not original else _load_json(original, label="hooks.json")
    if not isinstance(value, dict):
        raise ValueError("hooks.json root must be an object")
    hooks_value = value.get("hooks")
    if hooks_value is None:
        hooks = {}
        if hook is not None:
            value["hooks"] = hooks
    elif not isinstance(hooks_value, dict):
        raise ValueError("hooks.json hooks must be an object")
    else:
        hooks = hooks_value
    session_value = hooks.get("SessionStart")
    if session_value is None:
        session = []
    elif not isinstance(session_value, list):
        raise ValueError("hooks.json SessionStart must be an array")
    else:
        session = session_value
    planned = []
    for matcher in session:
        if not isinstance(matcher, dict):
            raise ValueError("hooks.json SessionStart entries must be objects")
        commands = matcher.get("hooks")
        if not isinstance(commands, list) or any(not isinstance(item, dict) for item in commands):
            raise ValueError("hooks.json SessionStart hooks must be objects")
        remaining = [item for item in commands if not _managed_codex_hook(item)]
        if len(remaining) == len(commands):
            planned.append(matcher)
        elif remaining or set(matcher) != {"hooks"}:
            replacement = dict(matcher)
            replacement["hooks"] = remaining
            planned.append(replacement)
    if hook is not None:
        planned.append({"matcher": "^(startup|resume)$", "hooks": [hook]})
    if planned:
        hooks["SessionStart"] = planned
        value["hooks"] = hooks
    else:
        hooks.pop("SessionStart", None)
        if not hooks:
            value.pop("hooks", None)
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _asset_path(asset: OwnedAsset, *, root: Path, home: Path) -> Path:
    base = root if asset.scope == "root" else home
    return base / Path(asset.target)


def _discovery_targets(
    client: str,
    *,
    root: Path,
    home: Path,
) -> tuple[tuple[Path, str], ...]:
    if client == "omp":
        return (
            (root / "extensions/didimlog.js", "file"),
            *tuple(
                (root / "skills/didimlog" / name, "file")
                for name in _SKILL_FILES
            ),
        )
    if client == "codex":
        return (
            (root / "hooks.json", "codex-hook"),
            *tuple(
                (home / ".agents/skills/didimlog" / name, "file")
                for name in _SKILL_FILES
            ),
        )
    return ()


def _file_asset(identifier: str, scope: str, target: str, content: bytes) -> OwnedAsset:
    return OwnedAsset(identifier, scope, target, _digest(content))


def _selection_assets(
    client: str,
    *,
    root: Path,
    home: Path,
    launcher: Path,
    env_launcher: Path,
) -> tuple[OwnedAsset, ...]:
    if client == "claude":
        return ()
    if client == "omp":
        assets = [
            _file_asset(
                "omp-extension",
                "root",
                "extensions/didimlog.js",
                _render_omp_extension(launcher, env_launcher, root),
            )
        ]
        assets.extend(
            _file_asset("omp-skill:" + name, "root", "skills/didimlog/" + name, data)
            for name, data in _packaged_skill_files()
        )
        return tuple(assets)
    hook_bytes = json.dumps(
        {
            "matcher": "^(startup|resume)$",
            "hooks": [_codex_hook(launcher, env_launcher, root)],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assets = [
        OwnedAsset(
            "codex-hook",
            "root",
            "hooks.json",
            _digest(hook_bytes),
            "codex-hook",
        )
    ]
    assets.extend(
        _file_asset(
            "codex-skill:" + name,
            "home",
            ".agents/skills/didimlog/" + name,
            data,
        )
        for name, data in _packaged_skill_files()
    )
    return tuple(assets)


def _copy_clients(state: ConnectionState | None) -> dict[str, dict[str, Selection]]:
    return {
        client: dict(roots)
        for client, roots in (() if state is None else state.clients.items())
    }


def _normal_asset_content(
    client: str,
    asset: OwnedAsset,
    launcher: Path,
    env_launcher: Path,
    root: Path,
) -> bytes:
    if asset.id == "omp-extension":
        return _render_omp_extension(launcher, env_launcher, root)
    name = Path(asset.target).name
    return dict(_packaged_skill_files())[name]


def _append_dependency(dependencies: list[_Dependency], path: Path, original: bytes | None) -> None:
    for existing in dependencies:
        if existing.path == path:
            if existing.original != original:
                raise ValueError("shared connection dependency changed during planning")
            return
    dependencies.append(_Dependency(path, original))


def _append_mutation(files: list[_Mutation], mutation: _Mutation) -> bool:
    for existing in files:
        if existing.path != mutation.path:
            continue
        if (
            existing.original != mutation.original
            or existing.intended != mutation.intended
        ):
            raise ValueError("shared connection target has conflicting plans")
        return False
    files.append(mutation)
    return True


def _owned_digests(
    state: ConnectionState | None,
    *,
    path: Path,
    home: Path,
) -> set[str]:
    digests = set()
    if state is None:
        return digests
    for roots in state.clients.values():
        for root_text, selection in roots.items():
            root = Path(root_text)
            for asset in selection.assets:
                if _asset_path(asset, root=root, home=home) == path:
                    digests.add(asset.sha256)
    return digests



def _other_connected_owner(
    clients: dict[str, dict[str, Selection]],
    *,
    current_client: str,
    current_root: Path,
    path: Path,
    home: Path,
) -> bool:
    for client, roots in clients.items():
        for root_text, selection in roots.items():
            if (
                client == current_client
                and root_text == str(current_root)
            ) or selection.intent != "connected":
                continue
            root = Path(root_text)
            if any(
                _asset_path(asset, root=root, home=home) == path
                for asset in selection.assets
            ):
                return True
    return False

def plan_connections(
    requested: tuple[tuple[str, object], ...],
    *,
    launcher: Path,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    connect: bool,
    require_storage: bool,
) -> ConnectionPlan:
    selected_home, home_identity = _real_directory(
        Path.home() if home is None else Path(home), label="home"
    )
    personal_root = data_home(selected_home)
    personal_identity = None
    try:
        personal = personal_root.lstat()
    except FileNotFoundError:
        personal = None
    except OSError as error:
        raise ValueError("personal knowledge root is unsafe") from error
    if personal is not None:
        if stat.S_ISLNK(personal.st_mode) or not stat.S_ISDIR(personal.st_mode):
            raise ValueError("personal knowledge root is unsafe")
        personal_identity = (personal.st_dev, personal.st_ino)
    elif require_storage:
        raise DidimError(
            "CONNECTION_STORAGE_NOT_PREPARED",
            exit_code=EXIT_POLICY,
            help_text="먼저 didim setup --skip-claude를 실행하세요.",
        )
    launcher_path = _validate_launcher(Path(launcher))
    env_launcher = _env_launcher()
    try:
        current_state, original_state = load_state(selected_home)
    except ValueError as error:
        raise DidimError(
            "CONNECTION_STATE_INVALID",
            exit_code=EXIT_POLICY,
        ) from error
    clients = _copy_clients(current_state)
    files: list[_Mutation] = []
    dependencies: list[_Dependency] = []
    changes: list[str] = []
    notices: list[str] = []
    identities: list[tuple[Path, tuple[int, int]]] = []
    if current_state is None and managed_connection_present(
        None,
        environ=environ,
        home=selected_home,
    ):
        claude_root, _claude_identity = selected_root(
            "claude",
            None,
            home=selected_home,
            environ=environ,
        )
        clients.setdefault("claude", {})[str(claude_root)] = Selection(
            "connected",
            (),
        )
        changes.append("기존 Claude 연결 선택 기록: " + str(claude_root))

    normalized: list[tuple[str, object]] = []
    seen = set()
    for client, explicit in requested:
        key = (client, str(explicit) if explicit is not None else "")
        if key in seen:
            continue
        seen.add(key)
        normalized.append((client, explicit))
    for client, explicit in normalized:
        root, identity = selected_root(
            client, explicit, home=selected_home, environ=environ
        )
        if identity is not None:
            identities.append((root, identity))
        if connect and client == "codex":
            notices.append(
                "Codex hook은 설치 뒤 /hooks에서 직접 검토하고 신뢰해야 합니다. 실행 상태는 아직 확인되지 않았습니다."
            )
        elif connect and client == "omp":
            notices.append(
                "OMP 연결 파일은 설치되지만 실제 새 세션 실행 상태는 아직 확인되지 않았습니다."
            )
        planned_assets = _selection_assets(
            client,
            root=root,
            home=selected_home,
            launcher=launcher_path,
            env_launcher=env_launcher,
        )
        roots = clients.setdefault(client, {})
        previous = roots.get(str(root))
        assets = (
            planned_assets
            if connect
            else () if previous is None
            else previous.assets
        )
        roots[str(root)] = Selection(
            "connected" if connect else "disconnected",
            assets,
        )

        if client == "claude":
            if previous != roots[str(root)]:
                changes.append(
                    ("Claude 연결 선택 기록: " if connect else "Claude 연결 해제 의도 기록: ")
                    + str(root)
                )
            continue

        if connect:
            for asset in assets:
                path = _asset_path(asset, root=root, home=selected_home)
                owned_digests = _owned_digests(
                    current_state,
                    path=path,
                    home=selected_home,
                )
                original = _read_optional(path, _FILE_MAXIMUM_BYTES)
                if asset.kind == "codex-hook":
                    managed_digests = (
                        () if original is None else _codex_hook_digests(original)
                    )
                    if (
                        len(managed_digests) > 1
                        or (
                            managed_digests
                            and managed_digests[0] not in owned_digests
                        )
                    ):
                        raise DidimError(
                            "CONNECTION_ASSET_CONFLICT",
                            exit_code=EXIT_POLICY,
                        )
                    intended = _plan_codex_hooks(
                        b"" if original is None else original,
                        _codex_hook(launcher_path, env_launcher, root),
                    )
                    label = "Codex SessionStart hook 연결"
                else:
                    if (
                        original is not None
                        and _digest(original) not in owned_digests
                    ):
                        raise DidimError(
                            "CONNECTION_ASSET_CONFLICT",
                            exit_code=EXIT_POLICY,
                        )
                    intended = _normal_asset_content(
                        client,
                        asset,
                        launcher_path,
                        env_launcher,
                        root,
                    )
                    label = f"{client.upper()} 관리 파일 설치"
                _append_dependency(dependencies, path, original)
                if original != intended and _append_mutation(
                    files,
                    _Mutation(f"{client}:{root}:{asset.id}", path, original, intended),
                ):
                    changes.append(f"{label}: {path}")
        else:
            for asset in assets:
                path = _asset_path(asset, root=root, home=selected_home)
                original = _read_optional(path, _FILE_MAXIMUM_BYTES)
                _append_dependency(dependencies, path, original)
                if asset.kind == "codex-hook":
                    if original is None:
                        continue
                    managed_digests = _codex_hook_digests(original)
                    if not managed_digests:
                        continue
                    if (
                        len(managed_digests) != 1
                        or previous is None
                        or managed_digests[0] != asset.sha256
                    ):
                        notices.append(
                            "Codex hook이 수정되었거나 소유 기록이 없어 보존합니다. 자동 실행이 남을 수 있습니다."
                        )
                        continue
                    intended = _plan_codex_hooks(original, None)
                    if intended != original and _append_mutation(
                        files,
                        _Mutation(
                            f"disconnect:{client}:{root}:{asset.id}",
                            path,
                            original,
                            intended,
                        ),
                    ):
                        changes.append(f"Codex SessionStart hook 연결 해제: {path}")
                    continue
                if _other_connected_owner(
                    clients,
                    current_client=client,
                    current_root=root,
                    path=path,
                    home=selected_home,
                ):
                    continue
                if (
                    previous is not None
                    and original is not None
                    and _digest(original) == asset.sha256
                ):
                    if _append_mutation(
                        files,
                        _Mutation(
                            f"disconnect:{client}:{root}:{asset.id}",
                            path,
                            original,
                            None,
                        ),
                    ):
                        changes.append(f"{client.upper()} 관리 파일 제거: {path}")
                elif original is not None:
                    notices.append(
                        f"{client.upper()} 관리 파일이 수정되었거나 소유 기록이 없어 보존합니다. 자동 발견이 남을 수 있습니다."
                    )
            if client == "codex":
                notices.append(
                    "공유 .agents skill이나 다른 설정의 지침은 이 연결 해제로 비활성화되지 않을 수 있습니다."
                )
            if client == "omp":
                notices.append(
                    "공유 .agents/.agent skill이나 외부 지침은 이 연결 해제로 비활성화되지 않을 수 있습니다."
                )

    intended_state = render_state(ConnectionState(clients))
    if len(intended_state) > _STATE_MAXIMUM_BYTES:
        raise DidimError("CONNECTION_STATE_TOO_LARGE", exit_code=EXIT_POLICY)
    try:
        parse_state(intended_state)
    except ValueError as error:
        raise DidimError(
            "CONNECTION_STATE_TOO_LARGE",
            exit_code=EXIT_POLICY,
        ) from error
    path = state_path(selected_home)
    _append_dependency(dependencies, path, original_state)
    if original_state != intended_state:
        changes.append("연결 선택 상태 기록")
    return ConnectionPlan(
        tuple(changes),
        tuple(dict.fromkeys(notices)),
        selected_home,
        home_identity,
        personal_identity,
        path,
        original_state,
        intended_state,
        tuple(files),
        tuple(dependencies),
        tuple(identities),
    )


def _opened_identity(descriptor: int) -> tuple[int, int]:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("connection directory is unsafe")
    return info.st_dev, info.st_ino


def _open_or_create_child(parent_descriptor: int, name: str) -> int:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
    return open_child_directory(parent_descriptor, name)


def _open_plan_parent(plan: ConnectionPlan, target: Path) -> int:
    base = _root_for_target(plan, target)
    descriptor: int | None = None
    try:
        if base == plan._home:
            descriptor = open_directory_path(plan._home)
            if _opened_identity(descriptor) != plan._home_identity:
                raise ValueError("home changed after planning")
        elif base == data_home(plan._home):
            descriptor = open_directory_path(plan._home)
            if _opened_identity(descriptor) != plan._home_identity:
                raise ValueError("home changed after planning")
            child = _open_or_create_child(descriptor, "knowledge")
            os.close(descriptor)
            descriptor = child
            if (
                plan._personal_identity is not None
                and _opened_identity(descriptor) != plan._personal_identity
            ):
                raise ValueError("personal knowledge root changed after planning")
        else:
            expected = dict(plan._root_identities).get(base)
            if expected is None:
                raise ValueError("connection root is not approved")
            descriptor = open_directory_path(base)
            if _opened_identity(descriptor) != expected:
                raise ValueError("connection root changed after planning")

        relative_parent = target.parent.relative_to(base)
        for part in relative_parent.parts:
            child = _open_or_create_child(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _root_for_target(plan: ConnectionPlan, target: Path) -> Path:
    candidates = [plan._home, data_home(plan._home)]
    candidates.extend(root for root, _ in plan._root_identities)
    contained = [base for base in candidates if target == base or target.is_relative_to(base)]
    if not contained:
        raise ValueError("managed target escaped approved roots")
    return max(contained, key=lambda path: len(path.parts))


def _directory_identity(path: Path) -> tuple[int, int]:
    linked = path.lstat()
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
        raise ValueError("connection directory is unsafe")
    return linked.st_dev, linked.st_ino


def _recheck_plan(plan: ConnectionPlan) -> None:
    if _directory_identity(plan._home) != plan._home_identity:
        raise ValueError("home changed after planning")
    personal = data_home(plan._home)
    current_personal = _directory_identity(personal)
    if (
        plan._personal_identity is not None
        and current_personal != plan._personal_identity
    ):
        raise ValueError("personal knowledge root changed after planning")
    if personal.parent != plan._home:
        raise ValueError("personal knowledge root escaped home")
    for root, identity in plan._root_identities:
        if _directory_identity(root) != identity:
            raise ValueError("connection root changed after planning")
    for dependency in plan._dependencies:
        maximum = (
            _STATE_MAXIMUM_BYTES
            if dependency.path == plan._state_path
            else _FILE_MAXIMUM_BYTES
        )
        if _read_optional(dependency.path, maximum) != dependency.original:
            raise ValueError("connection dependency changed after planning")


def _read_optional_at(
    parent_descriptor: int,
    name: str,
    maximum_bytes: int,
) -> bytes | None:
    try:
        linked = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISREG(linked.st_mode):
        raise ValueError("managed target is unsafe")
    raw, opened = read_regular_file_at_with_stat(
        parent_descriptor,
        name,
        maximum_bytes,
    )
    if (
        len(raw) > maximum_bytes
        or linked.st_dev != opened.st_dev
        or linked.st_ino != opened.st_ino
    ):
        raise ValueError("managed target changed during read")
    return raw


def _restore_deleted(deleted: list[_Deleted]) -> list[str]:
    failed = []
    for record in reversed(deleted):
        mutation = record.mutation
        try:
            if (
                _read_optional_at(
                    record.parent_descriptor,
                    mutation.path.name,
                    _FILE_MAXIMUM_BYTES,
                )
                is not None
            ):
                failed.append(mutation.name)
                continue
            ownership = write_regular_file_at_if_unchanged(
                record.parent_descriptor,
                mutation.path.name,
                None,
                mutation.original,
            )
            if ownership is not None:
                os.close(ownership)
        except (OSError, RuntimeError, ValueError):
            failed.append(mutation.name)
        finally:
            os.close(record.parent_descriptor)
    deleted.clear()
    return failed


def _journaled_write(
    journal: InstallJournal,
    *,
    name: str,
    path: Path,
    parent_descriptor: int,
    original: bytes | None,
    intended: bytes,
) -> None:
    backup = _backup_original(journal, name, original)
    journal.record_original(name, path, original, backup)
    ownership = write_regular_file_at_if_unchanged(
        parent_descriptor,
        path.name,
        original,
        intended,
    )
    try:
        journal.record_installed(
            name,
            intended,
            parent_descriptor=parent_descriptor,
        )
    finally:
        if ownership is not None:
            os.close(ownership)


def apply_connections(
    plan: ConnectionPlan,
    journal: InstallJournal,
    *,
    rollback_on_error: bool = True,
) -> tuple[str, ...]:
    if not isinstance(plan, ConnectionPlan):
        raise ValueError("invalid connection plan")
    try:
        _recheck_plan(plan)
    except (OSError, ValueError) as error:
        raise DidimError(
            "CONNECTION_PLAN_CHANGED",
            exit_code=EXIT_POLICY,
        ) from error
    deleted: list[_Deleted] = []
    try:
        for mutation in plan._files:
            parent_descriptor = _open_plan_parent(plan, mutation.path)
            if mutation.intended is None:
                recovery_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                try:
                    recovery_descriptor = os.open(
                        ".",
                        recovery_flags,
                        dir_fd=parent_descriptor,
                    )
                except BaseException:
                    os.close(parent_descriptor)
                    raise
                record = _Deleted(mutation, recovery_descriptor)
                lock_descriptor: int | None = None
                retained = False
                try:
                    lock_descriptor = acquire_directory_lock(parent_descriptor)
                    current = _read_optional_at(
                        parent_descriptor,
                        mutation.path.name,
                        _FILE_MAXIMUM_BYTES,
                    )
                    rechecked = _read_optional_at(
                        parent_descriptor,
                        mutation.path.name,
                        _FILE_MAXIMUM_BYTES,
                    )
                    if (
                        current != mutation.original
                        or rechecked != mutation.original
                    ):
                        raise ValueError("managed target changed before deletion")
                    os.unlink(mutation.path.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
                except BaseException:
                    try:
                        if (
                            _read_optional_at(
                                parent_descriptor,
                                mutation.path.name,
                                _FILE_MAXIMUM_BYTES,
                            )
                            is None
                        ):
                            deleted.append(record)
                            retained = True
                    except (OSError, RuntimeError, ValueError):
                        pass
                    if not retained:
                        os.close(recovery_descriptor)
                    raise
                finally:
                    if lock_descriptor is not None:
                        os.close(lock_descriptor)
                    os.close(parent_descriptor)
                deleted.append(record)
                continue
            try:
                _journaled_write(
                    journal,
                    name=mutation.name,
                    path=mutation.path,
                    parent_descriptor=parent_descriptor,
                    original=mutation.original,
                    intended=mutation.intended,
                )
            finally:
                os.close(parent_descriptor)

        if plan._state_original != plan._state_intended:
            parent_descriptor = _open_plan_parent(plan, plan._state_path)
            try:
                _journaled_write(
                    journal,
                    name="connection-state",
                    path=plan._state_path,
                    parent_descriptor=parent_descriptor,
                    original=plan._state_original,
                    intended=plan._state_intended,
                )
            finally:
                os.close(parent_descriptor)
        postcheck_connections(plan)
        for record in deleted:
            os.close(record.parent_descriptor)
        deleted.clear()
        return ()
    except BaseException:
        failed = _restore_deleted(deleted)
        if rollback_on_error:
            failed.extend(journal.rollback())
        if failed:
            raise DidimError(
                "CONNECTION_ROLLBACK_INCOMPLETE",
                exit_code=EXIT_POLICY,
                details=tuple("대상: " + name for name in sorted(set(failed))),
            )
        raise


def _codex_hook_digests(raw: bytes) -> tuple[str, ...]:
    try:
        value = _load_json(raw, label="hooks.json")
    except ValueError:
        return ()
    if not isinstance(value, dict):
        return ()
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        return ()
    session = hooks.get("SessionStart")
    if not isinstance(session, list):
        return ()
    found = []
    for matcher in session:
        if not isinstance(matcher, dict) or not isinstance(matcher.get("hooks"), list):
            return ()
        if any(_managed_codex_hook(hook) for hook in matcher["hooks"]):
            encoded = json.dumps(
                matcher,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            found.append(_digest(encoded))
    return tuple(found)


def _codex_hook_digest(raw: bytes) -> str | None:
    found = _codex_hook_digests(raw)
    return found[0] if len(found) == 1 else None


def _codex_hook_residual(raw: bytes) -> bool:
    try:
        value = _load_json(raw, label="hooks.json")
    except ValueError:
        return True
    if not isinstance(value, dict):
        return True
    hooks = value.get("hooks")
    if hooks is None:
        return False
    if not isinstance(hooks, dict):
        return True
    session = hooks.get("SessionStart")
    if session is None:
        return False
    if not isinstance(session, list):
        return True
    for matcher in session:
        if not isinstance(matcher, dict):
            return True
        commands = matcher.get("hooks")
        if not isinstance(commands, list) or any(
            not isinstance(command, dict) for command in commands
        ):
            return True
        if any(_codex_startup_hook(command) for command in commands):
            return True
    return False


def postcheck_connections(plan: ConnectionPlan) -> None:
    expected = {mutation.path: mutation.intended for mutation in plan._files}
    expected[plan._state_path] = plan._state_intended
    for dependency in plan._dependencies:
        maximum = (
            _STATE_MAXIMUM_BYTES
            if dependency.path == plan._state_path
            else _FILE_MAXIMUM_BYTES
        )
        intended = expected.get(dependency.path, dependency.original)
        if _read_optional(dependency.path, maximum) != intended:
            raise DidimError(
                "CONNECTION_POSTCHECK_FAILED",
                exit_code=EXIT_POLICY,
            )



def inspect_connections(
    state: ConnectionState,
    *,
    home: Path,
) -> tuple[tuple[ClientStatus, ...], tuple[tuple[str, str, str], ...]]:
    statuses: list[ClientStatus] = []
    problems: list[tuple[str, str, str]] = []
    for client in _CLIENTS:
        for root_text, selection in sorted(state.clients.get(client, {}).items()):
            root = Path(root_text)
            if selection.intent == "disconnected":
                token = f"{client.upper()}_DISABLED"
                residual = False
                for path, kind in _discovery_targets(
                    client,
                    root=root,
                    home=home,
                ):
                    try:
                        raw = _read_optional(path, _FILE_MAXIMUM_BYTES)
                    except (OSError, ValueError):
                        residual = True
                        break
                    if raw is None:
                        continue
                    residual = (
                        _codex_hook_residual(raw)
                        if kind == "codex-hook"
                        else True
                    )
                    if residual:
                        break
                if client == "omp" and any(
                    entry.intent == "connected"
                    for entry in state.clients.get("codex", {}).values()
                ):
                    residual = True
                if residual:
                    token = f"{client.upper()}_RESIDUAL_DISCOVERY"
                    problems.append(
                        (
                            token,
                            f"{client.upper()}에서 Didimlog 자동 발견이 남을 수 있습니다.",
                            f"didim disconnect {client} --dry-run",
                        )
                    )
                statuses.append(ClientStatus(client, root, selection.intent, token))
                continue

            broken = False
            for asset in selection.assets:
                path = _asset_path(asset, root=root, home=home)
                try:
                    raw = _read_optional(path, _FILE_MAXIMUM_BYTES)
                except (OSError, ValueError):
                    broken = True
                    break
                if raw is None:
                    broken = True
                    break
                actual = _codex_hook_digest(raw) if asset.kind == "codex-hook" else _digest(raw)
                if actual != asset.sha256:
                    broken = True
                    break
            token = (
                f"{client.upper()}_CONNECTION_BROKEN"
                if broken
                else f"{client.upper()}_INSTALLED_UNVERIFIED"
            )
            if broken:
                problems.append(
                    (
                        token,
                        f"선택한 {client.upper()} 연결 파일이 없거나 설치본과 다릅니다.",
                        f"didim connect {client} --dry-run",
                    )
                )
            statuses.append(ClientStatus(client, root, selection.intent, token))
    return tuple(statuses), tuple(problems)


def startup_ready(
    client: str,
    *,
    root: Path,
    home: Path,
    cwd: Path | None,
) -> bool:
    del cwd
    try:
        selected_home, _ = _real_directory(home, label="home")
        raw_state = read_regular_file_beneath(
            selected_home,
            "knowledge/.didimlog/connections.json",
            _STATE_MAXIMUM_BYTES,
        )
        state = parse_state(raw_state)
        supplied_root = Path(root).expanduser().absolute()
        selected_root, _ = _real_directory(supplied_root, label="connection root")
        if selected_root != supplied_root:
            return False
        root_key = str(selected_root)
        selection = state.clients.get(client, {}).get(root_key)
        if selection is None or selection.intent != "connected":
            return False
        if len(selection.assets) > _STARTUP_FILE_LIMIT:
            return False
        for asset in selection.assets:
            if asset.kind == "codex-hook":
                continue
            base = selected_root if asset.scope == "root" else selected_home
            raw = read_regular_file_beneath(
                base,
                asset.target,
                64 * 1024,
            )
            if _digest(raw) != asset.sha256:
                return False
        read_regular_file_beneath(
            selected_home,
            "knowledge/index/_global.md",
            64 * 1024,
        )
        return True
    except (OSError, RuntimeError, ValueError):
        return False
