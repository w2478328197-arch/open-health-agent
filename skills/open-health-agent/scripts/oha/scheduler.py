from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import json
import os
import plistlib
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import Config, save_config

SYNC_LABEL = "io.github.open-health-agent.sync"
AWAKE_LABEL = "io.github.open-health-agent.keep-awake-on-ac"
_FILESYSTEM_GETUID = getattr(os, "getuid", None)
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
SAFE_RUNTIME_ENVIRONMENT = {
    "PYTHONPATH": "",
    "PYTHONHOME": "",
    "PYTHONINSPECT": "",
    "PYTHONSTARTUP": "",
    "PYTHONUSERBASE": "",
    "LD_PRELOAD": "",
    "LD_AUDIT": "",
    "LD_LIBRARY_PATH": "",
    "DYLD_INSERT_LIBRARIES": "",
    "DYLD_LIBRARY_PATH": "",
    "DYLD_FRAMEWORK_PATH": "",
}


def _filesystem_uid() -> int | None:
    if _FILESYSTEM_GETUID is None:
        return None
    try:
        return _FILESYSTEM_GETUID()
    except OSError:
        return None


def validate_proxy_environment(values: dict[str, str]) -> dict[str, str]:
    """Validate a minimal loopback-proxy environment without embedded credentials."""

    output: dict[str, str] = {}
    folded_keys: set[str] = set()
    for key, raw_value in values.items():
        if key not in PROXY_ENV_KEYS:
            raise ValueError("unsupported scheduler proxy environment key")
        folded = key.casefold()
        if folded in folded_keys:
            raise ValueError("scheduler proxy environment contains conflicting key variants")
        folded_keys.add(folded)
        if not isinstance(raw_value, str):
            raise ValueError("scheduler proxy environment values must be text")
        value = raw_value.strip()
        if not value:
            continue
        if len(value) > 2048 or any(character.isspace() for character in value) or "\x00" in value:
            raise ValueError("scheduler proxy environment contains an unsafe value")
        if not re.fullmatch(r"[A-Za-z0-9:/._,\-\[\]*]+", value):
            raise ValueError("scheduler proxy environment contains unsupported characters")
        if key.casefold() != "no_proxy":
            parsed = urlsplit(value)
            if (
                parsed.scheme.casefold() not in {"http", "https", "socks5", "socks5h"}
                or not parsed.netloc
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("scheduler proxy URL must use http, https, socks5, or socks5h")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("scheduler proxy URLs must not embed credentials")
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("scheduler proxy URL has an invalid port") from exc
            if port is None or not 1 <= port <= 65535:
                raise ValueError("scheduler proxy URL must include a valid port")
            hostname = parsed.hostname.casefold()
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname == "localhost"
            if not loopback:
                raise ValueError("scheduler proxy must be a local loopback endpoint")
        output[key] = value
    return output


def proxy_environment_from_process() -> dict[str, str]:
    selected = validate_proxy_environment(
        {key: os.environ[key] for key in PROXY_ENV_KEYS if os.environ.get(key)}
    )
    if not any(key.casefold() != "no_proxy" for key in selected):
        raise ValueError("the current process contains no scheduler proxy URL")
    return selected


def proxy_environment_from_file(path: Path) -> dict[str, str]:
    selected = path.expanduser().resolve()
    if not selected.is_file():
        raise FileNotFoundError("scheduler proxy environment file is unavailable")
    metadata = selected.stat()
    if metadata.st_size > 64 * 1024:
        raise ValueError("scheduler proxy environment file is too large")
    if os.name != "nt":
        if metadata.st_mode & 0o077:
            raise PermissionError("scheduler proxy environment file must be owner-only")
        filesystem_uid = _filesystem_uid()
        if filesystem_uid is not None and metadata.st_uid != filesystem_uid:
            raise PermissionError("scheduler proxy environment file must be owned by the current user")
    values: dict[str, str] = {}
    for raw_line in selected.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError("scheduler proxy environment file contains a malformed line")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in PROXY_ENV_KEYS:
            raise ValueError("scheduler proxy environment file contains an unsupported key")
        if key in values:
            raise ValueError("scheduler proxy environment file contains a duplicate key")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    selected_values = validate_proxy_environment(values)
    if not any(key.casefold() != "no_proxy" for key in selected_values):
        raise ValueError("scheduler proxy environment file contains no proxy URL")
    return selected_values


def _run(command: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return 127, "scheduler backend command is unavailable"
    except subprocess.TimeoutExpired:
        return 124, "scheduler backend command timed out"
    return completed.returncode, (completed.stdout or completed.stderr).strip()


def _entrypoint() -> Path:
    return Path(sys.argv[0]).expanduser().resolve()


def _valid_runtime_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _scheduler_program_arguments(
    config: Config,
    static_runtime_fingerprint: str,
    runtime_fingerprint: str,
) -> list[str]:
    """Return the one exact command shape accepted for a managed sync job."""

    if not _valid_runtime_fingerprint(static_runtime_fingerprint) or not _valid_runtime_fingerprint(
        runtime_fingerprint
    ):
        raise ValueError("scheduler runtime fingerprint is invalid")
    return [
        sys.executable,
        "-E",
        "-s",
        str(_entrypoint()),
        "--home",
        str(config.home_path),
        "sync",
        "--scheduled",
        "--static-runtime-fingerprint",
        static_runtime_fingerprint,
        "--runtime-fingerprint",
        runtime_fingerprint,
        "--quiet",
    ]


def _macos_schedule_payload(
    config: Config,
    interval_seconds: int,
    profile: str,
    proxy_environment: dict[str, str],
    static_runtime_fingerprint: str,
    runtime_fingerprint: str,
) -> dict[str, Any]:
    logs = config.home_path / "logs"
    return {
        "Label": SYNC_LABEL,
        "ProgramArguments": _scheduler_program_arguments(
            config, static_runtime_fingerprint, runtime_fingerprint
        ),
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "GHEALTH_PROFILE": _validate_profile_name(profile),
            "GHEALTH_FORMAT": "json",
        }
        | SAFE_RUNTIME_ENVIRONMENT
        | proxy_environment,
        "StandardOutPath": str(logs / "scheduler.out.log"),
        "StandardErrorPath": str(logs / "scheduler.err.log"),
    }


def _systemd_service_content(
    config: Config,
    profile: str,
    proxy_environment: dict[str, str],
    static_runtime_fingerprint: str,
    runtime_fingerprint: str,
) -> bytes:
    command = _systemd_exec_command(
        _scheduler_program_arguments(
            config, static_runtime_fingerprint, runtime_fingerprint
        )
    )
    return "\n".join(
        [
            "[Unit]",
            "Description=Open Health Agent deterministic health sync",
            "",
            "[Service]",
            "Type=oneshot",
            f"Environment=GHEALTH_PROFILE={_validate_profile_name(profile)}",
            "Environment=GHEALTH_FORMAT=json",
            *[
                f"Environment={key}="
                for key in sorted(SAFE_RUNTIME_ENVIRONMENT)
            ],
            *[
                f"Environment={key}={value}"
                for key, value in sorted(proxy_environment.items())
            ],
            f"ExecStart=:{command}",
            "",
        ]
    ).encode("utf-8")


def _systemd_escape_argument(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise ValueError("scheduler paths may not contain control characters")
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _systemd_exec_command(arguments: list[str]) -> str:
    return " ".join(_systemd_escape_argument(argument) for argument in arguments)


def _systemd_timer_content(interval_seconds: int) -> bytes:
    return "\n".join(
        [
            "[Unit]",
            "Description=Run Open Health Agent sync hourly",
            "",
            "[Timer]",
            "OnBootSec=5m",
            f"OnUnitActiveSec={interval_seconds}s",
            "Persistent=true",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    ).encode("utf-8")


def _atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _private_log_directory(metadata: os.stat_result) -> bool:
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    if os.name == "nt":
        return True
    return bool(
        not metadata.st_mode & 0o077
        and metadata.st_mode & stat.S_IWUSR
        and metadata.st_mode & stat.S_IXUSR
        and (_filesystem_uid() is None or metadata.st_uid == _filesystem_uid())
    )


def _private_log_file(metadata: os.stat_result) -> bool:
    if not stat.S_ISREG(metadata.st_mode):
        return False
    if os.name == "nt":
        return True
    return bool(
        not metadata.st_mode & 0o077
        and metadata.st_mode & stat.S_IWUSR
        and (_filesystem_uid() is None or metadata.st_uid == _filesystem_uid())
    )


def _prepare_macos_log_files(config: Config) -> None:
    """Create launchd logs without following attacker-controlled links."""

    logs = config.home_path / "logs"
    if os.name == "nt":
        logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        if logs.is_symlink() or not logs.is_dir():
            raise PermissionError(
                "scheduler logs directory must be a private, non-symlink directory owned by the current user"
            )
        for name in ("scheduler.out.log", "scheduler.err.log"):
            path = logs / name
            if path.exists() and (path.is_symlink() or not path.is_file()):
                raise PermissionError(
                    "scheduler log files must be private, non-symlink regular files owned by the current user"
                )
            path.touch(exist_ok=True)
        return
    try:
        logs.mkdir(mode=0o700, parents=True)
        directory_created = True
    except FileExistsError:
        directory_created = False
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_descriptor = os.open(logs, directory_flags)
    except OSError as exc:
        raise PermissionError(
            "scheduler logs directory must be a private, non-symlink directory owned by the current user"
        ) from exc
    try:
        if directory_created and hasattr(os, "fchmod"):
            os.fchmod(directory_descriptor, 0o700)
        if not _private_log_directory(os.fstat(directory_descriptor)):
            raise PermissionError(
                "scheduler logs directory must be owner-only and writable by the current user"
            )
        # O_NONBLOCK prevents a pre-existing FIFO from hanging installation
        # before fstat can reject it as a non-regular file.
        file_flags = (
            os.O_WRONLY
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        for name in ("scheduler.out.log", "scheduler.err.log"):
            descriptor: int | None = None
            file_created = False
            try:
                try:
                    descriptor = os.open(
                        name,
                        file_flags,
                        dir_fd=directory_descriptor,
                    )
                except FileNotFoundError:
                    descriptor = os.open(
                        name,
                        file_flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=directory_descriptor,
                    )
                    file_created = True
                if file_created and hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                if not _private_log_file(os.fstat(descriptor)):
                    raise PermissionError(
                        "scheduler log files must be private regular files owned by the current user"
                    )
            except OSError as exc:
                raise PermissionError(
                    "scheduler log files must be private, non-symlink regular files owned by the current user"
                ) from exc
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    finally:
        os.close(directory_descriptor)


def _scheduler_log_files_safe(config: Config) -> bool:
    logs = config.home_path / "logs"
    try:
        directory = logs.lstat()
        if logs.is_symlink() or not _private_log_directory(directory):
            return False
        for name in ("scheduler.out.log", "scheduler.err.log"):
            path = logs / name
            metadata = path.lstat()
            if path.is_symlink() or not _private_log_file(metadata):
                return False
    except OSError:
        return False
    return True


def resolve_ghealth_executable(command: str) -> Path:
    expanded = Path(command).expanduser()
    if os.sep in command or (os.altsep and os.altsep in command):
        candidate = expanded.resolve()
    else:
        found = shutil.which(command)
        candidate = Path(found).resolve() if found else (Path.home() / ".local" / "bin" / command).resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise FileNotFoundError(
            f"ghealth executable is unavailable: {candidate}; install/authenticate it before enabling hourly sync"
        )
    return candidate


def _diagnostic(output: str) -> str:
    """Keep scheduler errors bounded and useful without dumping arbitrary output."""
    cleaned = " ".join(output.split())
    return cleaned[:500] if cleaned else "no diagnostic was returned"


def _snapshot_definitions(paths: list[Path]) -> dict[Path, tuple[bytes, int] | None]:
    snapshots: dict[Path, tuple[bytes, int] | None] = {}
    for path in paths:
        if path.exists():
            snapshots[path] = (path.read_bytes(), path.stat().st_mode & 0o777)
        else:
            snapshots[path] = None
    return snapshots


def _restore_definitions(snapshots: dict[Path, tuple[bytes, int] | None]) -> None:
    for path, snapshot in snapshots.items():
        if snapshot is None:
            path.unlink(missing_ok=True)
        else:
            content, mode = snapshot
            _atomic_write(path, content, mode=mode)


def _snapshot_symlink(path: Path) -> str | None:
    if not os.path.lexists(path):
        return None
    if not path.is_symlink():
        raise RuntimeError(
            f"refusing to replace unexpected non-symlink systemd enablement path: {path}"
        )
    return os.readlink(path)


def _restore_symlink(path: Path, target: str | None) -> None:
    if os.path.lexists(path):
        if not path.is_symlink():
            raise RuntimeError(
                f"cannot restore systemd state over unexpected non-symlink path: {path}"
            )
        path.unlink()
    if target is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)


def _validate_profile_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
        raise RuntimeError(
            "the active ghealth profile name is not scheduler-safe; use a 1-128 character name "
            "containing only letters, numbers, dot, underscore, or hyphen"
        )
    return name


def _ghealth_profile_entries(executable: Path) -> list[dict[str, Any]]:
    code, output = _run(
        [str(executable), "config", "profiles", "list", "--format", "json"]
    )
    if code != 0:
        raise RuntimeError(
            "could not identify the active ghealth profile before installing the scheduler; "
            f"run ghealth config profiles list locally and retry. Diagnostic: {_diagnostic(output)}"
        )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "ghealth profile output was not JSON; reinstall the pinned ghealth version and retry scheduler install"
        ) from exc
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list):
        raise RuntimeError(
            "ghealth did not return a profile list; reinstall the pinned ghealth version and retry scheduler install"
        )
    return [entry for entry in profiles if isinstance(entry, dict)]


def _resolve_active_ghealth_profile(executable: Path) -> str:
    profiles = _ghealth_profile_entries(executable)
    for entry in profiles:
        if entry.get("active") is not True:
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            return _validate_profile_name(name)
    # ghealth itself falls back to the default profile when an override names a
    # profile that is absent, so pin the same deterministic fallback.
    return "default"


def resolve_active_ghealth_profile(config: Config) -> str:
    """Resolve one foreground profile name for pinning an entire sync batch."""

    return _resolve_active_ghealth_profile(
        resolve_ghealth_executable(config.ghealth_command)
    )


def _resolve_named_ghealth_profile(executable: Path, profile: str) -> str:
    selected = _validate_profile_name(profile)
    if selected == "default":
        return selected
    available = {
        entry.get("name")
        for entry in _ghealth_profile_entries(executable)
        if isinstance(entry.get("name"), str)
    }
    if selected not in available:
        raise RuntimeError("the scheduler-pinned ghealth profile is no longer available")
    return selected


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _path_identity(path: Path, *, content: bool = True) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    metadata = resolved.stat()
    identity: dict[str, Any] = {
        "path": str(resolved),
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
    }
    if content:
        identity["sha256"] = _sha256_file(resolved)
    return identity


def _oha_python_tree_digest(entrypoint: Path) -> str:
    root = entrypoint.expanduser().resolve().parent
    candidates = sorted(
        path
        for path in root.rglob("*.py")
        if path.is_file() and not path.is_symlink()
    )
    if not candidates:
        raise FileNotFoundError("the Open Health Agent Python runtime is unavailable")
    digest = hashlib.sha256()
    for path in candidates:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def static_runtime_fingerprint(config: Config) -> str:
    """Hash the pinned files and day boundary without executing ghealth."""

    executable = resolve_ghealth_executable(config.ghealth_command)
    entrypoint = _entrypoint().expanduser().resolve()
    skill_root = entrypoint.parent.parent
    template = skill_root / "assets" / "health-ledger.xlsx"
    requirements = entrypoint.parent / "requirements.txt"
    dependency_versions: dict[str, str | None] = {}
    for distribution in ("openpyxl", "et-xmlfile", "tzdata"):
        try:
            dependency_versions[distribution] = importlib.metadata.version(
                distribution
            )
        except importlib.metadata.PackageNotFoundError:
            dependency_versions[distribution] = None
    material = json.dumps(
        {
            "ghealth": _path_identity(executable),
            "python": _path_identity(Path(sys.executable)),
            "python_version": sys.version,
            "python_prefix": str(Path(sys.prefix).expanduser().resolve()),
            "oha_entrypoint": _path_identity(entrypoint),
            "oha_python_tree_sha256": _oha_python_tree_digest(entrypoint),
            "workbook_template": _path_identity(template),
            "requirements": _path_identity(requirements),
            "dependency_versions": dependency_versions,
            "scheduler_config": {
                "home": str(config.home_path),
                "database": str(config.database),
                "workbook": str(config.workbook),
                "timezone": config.timezone,
                "lookback_days": config.lookback_days,
                "sleep_source_priority": config.sleep_source_priority,
                "activity_energy_semantics": config.activity_energy_semantics,
                "planning_tef_fraction": config.planning_tef_fraction,
                "backup_retention": config.backup_retention,
                "audit_event_retention": config.audit_event_retention,
                "sync_run_retention": config.sync_run_retention,
                "raw_summary_retention": config.raw_summary_retention,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _ghealth_profile_identity_fingerprint(
    executable: Path, profile: str
) -> str:
    """Hash a minimal stable account/project projection without exposing it."""

    selected = _validate_profile_name(profile)
    matching_entries = [
        entry
        for entry in _ghealth_profile_entries(executable)
        if entry.get("name") == selected
    ]
    if len(matching_entries) != 1:
        raise RuntimeError(
            "the selected ghealth profile cannot be bound to one configured project"
        )
    project_id = matching_entries[0].get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise RuntimeError(
            "the selected ghealth profile does not expose a stable project identity"
        )
    code, output = _run(
        [
            str(executable),
            "auth",
            "status",
            "--profile",
            selected,
            "--format",
            "json",
        ]
    )
    if code != 0:
        raise RuntimeError(
            "ghealth local authentication identity is unavailable for scheduler binding"
        )
    try:
        auth = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "ghealth authentication identity output was not valid JSON"
        ) from exc
    if not isinstance(auth, dict):
        raise RuntimeError("ghealth authentication identity output was invalid")
    email = auth.get("email")
    auth_method = auth.get("auth_method")
    scopes = auth.get("scopes")
    if (
        auth.get("authenticated") is not True
        or auth.get("expired") is True
        or not isinstance(email, str)
        or not email.strip()
        or not isinstance(auth_method, str)
        or not auth_method.strip()
        or not isinstance(scopes, list)
        or not all(isinstance(scope, str) and scope for scope in scopes)
    ):
        raise RuntimeError(
            "ghealth does not expose a stable authenticated account identity; background sync remains disabled"
        )
    projection = json.dumps(
        {
            "profile": selected,
            "project_id": project_id.strip(),
            "email": email.strip().casefold(),
            "auth_method": auth_method.strip(),
            "scopes": sorted(set(scopes)),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(projection).hexdigest()


def _compose_runtime_fingerprint(
    static_fingerprint: str,
    profile: str,
    profile_identity_fingerprint: str,
) -> str:
    if not _valid_runtime_fingerprint(static_fingerprint) or not _valid_runtime_fingerprint(
        profile_identity_fingerprint
    ):
        raise ValueError("runtime fingerprint material is invalid")
    material = json.dumps(
        {
            "static_runtime": static_fingerprint,
            "profile": _validate_profile_name(profile),
            "profile_identity": profile_identity_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def ghealth_runtime_fingerprint(
    config: Config, profile: str | None = None
) -> str:
    """Return an opaque fingerprint for the complete pinned sync runtime."""

    executable = resolve_ghealth_executable(config.ghealth_command)
    selected_profile = (
        _resolve_active_ghealth_profile(executable)
        if profile is None
        else _resolve_named_ghealth_profile(executable, profile)
    )
    profile_identity = _ghealth_profile_identity_fingerprint(
        executable, selected_profile
    )
    return _compose_runtime_fingerprint(
        static_runtime_fingerprint(config),
        selected_profile,
        profile_identity,
    )


def _rollback_launchd_definition(
    path: Path,
    snapshot: dict[Path, tuple[bytes, int] | None],
    domain: str,
    label: str,
    was_loaded: bool,
) -> str | None:
    # A failed bootstrap can still register part of a job. Do not replace its
    # on-disk definition with an older snapshot until launchd has proved the new
    # runtime absent; otherwise the user loses the only reliable removal handle.
    try:
        currently_loaded = _launchd_job_loaded(domain, label)
    except RuntimeError as exc:
        return f"could not verify the partially installed launchd job: {_diagnostic(str(exc))}"
    if currently_loaded:
        code, output = _run(["launchctl", "bootout", domain, str(path)])
        if code != 0:
            return (
                "could not unload the partially installed launchd job; its managed "
                f"definition was kept for remediation: {_diagnostic(output)}"
            )
    try:
        _restore_definitions(snapshot)
    except Exception as exc:
        return f"could not restore the previous launchd definition: {_diagnostic(str(exc))}"
    if not was_loaded or snapshot[path] is None:
        return None
    code, output = _run(["launchctl", "bootstrap", domain, str(path)])
    if code != 0:
        return f"previous job bootstrap failed: {_diagnostic(output)}"
    code, output = _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"])
    return f"previous job kickstart failed: {_diagnostic(output)}" if code != 0 else None


def _launchd_rollback_detail(rollback_error: str | None) -> str:
    if rollback_error:
        return (
            " Rollback was incomplete: "
            f"{rollback_error}. Inspect and remove the owner-only managed definition before retrying."
        )
    return " The previous definition and loaded state were restored."


def _stage_removal(paths: list[Path]) -> list[tuple[Path, Path]]:
    """Move definitions aside so a failed systemd reload can be rolled back."""
    staged: list[tuple[Path, Path]] = []
    try:
        for path in paths:
            if not path.exists():
                continue
            descriptor, raw_temporary = tempfile.mkstemp(
                prefix=f".{path.name}.removing.", dir=path.parent
            )
            os.close(descriptor)
            temporary = Path(raw_temporary)
            temporary.unlink()
            os.replace(path, temporary)
            staged.append((path, temporary))
    except Exception as exc:
        for original, temporary in reversed(staged):
            if temporary.exists():
                os.replace(temporary, original)
        raise RuntimeError(
            "could not stage scheduler definitions for removal; existing definitions were restored"
        ) from exc
    return staged


def _restore_staged(staged: list[tuple[Path, Path]]) -> None:
    for original, temporary in reversed(staged):
        if temporary.exists():
            os.replace(temporary, original)


def _launchd_job_loaded(domain: str, label: str) -> bool:
    code, output = _run(["launchctl", "print", f"{domain}/{label}"])
    if code == 0:
        return True
    normalized = output.casefold()
    if any(
        marker in normalized
        for marker in (
            "could not find service",
            "service not found",
            "no such process",
        )
    ):
        return False
    raise RuntimeError(
        "launchctl could not verify whether the background job is loaded; no definition was removed. "
        f"Fix the launchd error and retry uninstall. Diagnostic: {_diagnostic(output)}"
    )


def _uninstall_launchd_job(label: str, path: Path, description: str) -> dict[str, Any]:
    """Stop runtime state independently from the on-disk plist."""

    domain = f"gui/{os.getuid()}"
    loaded = _launchd_job_loaded(domain, label)
    definition_existed = path.exists()
    if not loaded and not definition_existed:
        return {
            "backend": "launchd",
            "removed": False,
            "runtime_stopped": False,
            "definition_removed": False,
            "path": str(path),
        }
    if loaded:
        code, output = _run(["launchctl", "bootout", f"{domain}/{label}"])
        if code != 0:
            raise RuntimeError(
                f"launchctl could not unload {description}; its definition was kept if present. "
                f"Fix the launchd error and retry uninstall. Diagnostic: {_diagnostic(output)}"
            )
    if definition_existed:
        try:
            path.unlink()
        except OSError as exc:
            state = "was unloaded" if loaded else "was not loaded"
            raise RuntimeError(
                f"{description} {state}, but its definition could not be removed; "
                "check file permissions and retry uninstall"
            ) from exc
    return {
        "backend": "launchd",
        "removed": True,
        "runtime_stopped": loaded,
        "definition_removed": definition_existed,
        "path": str(path),
    }


def _systemd_state_is_absent(code: int, output: str) -> bool:
    if code == 0:
        return False
    normalized = output.casefold()
    ordinary_absence = code in {1, 3, 4} and any(
        marker in normalized
        for marker in (
            "inactive",
            "disabled",
            "not-found",
            "not found",
            "does not exist",
        )
    )
    return ordinary_absence or (code == 4 and normalized.strip() == "unknown")


def _systemd_unit_state(unit: str, query: str) -> bool:
    if query not in {"is-active", "is-enabled"}:
        raise ValueError("unsupported systemd state query")
    code, output = _run(["systemctl", "--user", query, unit])
    if code == 0:
        return True
    if _systemd_state_is_absent(code, output):
        return False
    raise RuntimeError(
        f"systemctl could not verify whether {unit} is {query.removeprefix('is-')}; "
        f"no definition was removed or changed. Diagnostic: {_diagnostic(output)}"
    )


def _restore_systemd_timer_state(
    timer_name: str, *, enabled: bool, active: bool
) -> str | None:
    diagnostics: list[str] = []
    enable_action = "enable" if enabled else "disable"
    code, output = _run(["systemctl", "--user", enable_action, timer_name])
    if code != 0:
        diagnostics.append(f"{enable_action}={_diagnostic(output)}")
    active_action = "start" if active else "stop"
    code, output = _run(["systemctl", "--user", active_action, timer_name])
    if code != 0:
        diagnostics.append(f"{active_action}={_diagnostic(output)}")
    try:
        enabled_after = _systemd_unit_state(timer_name, "is-enabled")
        active_after = _systemd_unit_state(timer_name, "is-active")
        if enabled_after != enabled:
            diagnostics.append("enablement verification mismatch")
        if active_after != active:
            diagnostics.append("activity verification mismatch")
    except RuntimeError as exc:
        diagnostics.append(_diagnostic(str(exc)))
    return "; ".join(diagnostics) if diagnostics else None


def _rollback_systemd_install(
    snapshots: dict[Path, tuple[bytes, int] | None],
    wants_link: Path,
    previous_wants_target: str | None,
    timer_name: str,
    service_name: str,
    *,
    previous_enabled: bool,
    previous_active: bool,
    previous_service_active: bool,
) -> str | None:
    diagnostics: list[str] = []
    try:
        code, output = _run(
            ["systemctl", "--user", "disable", "--now", timer_name]
        )
        if code != 0 and not _systemd_state_is_absent(code, output):
            diagnostics.append(f"new timer cleanup={_diagnostic(output)}")
    except Exception as exc:
        diagnostics.append(f"new timer cleanup={_diagnostic(str(exc))}")
    if not previous_service_active:
        try:
            service_active = _systemd_unit_state(service_name, "is-active")
        except RuntimeError as exc:
            diagnostics.append(f"new service state={_diagnostic(str(exc))}")
        else:
            if service_active:
                code, output = _run(
                    ["systemctl", "--user", "stop", service_name]
                )
                if code != 0:
                    diagnostics.append(f"new service cleanup={_diagnostic(output)}")
                else:
                    try:
                        if _systemd_unit_state(service_name, "is-active"):
                            diagnostics.append("new service remained active after stop")
                    except RuntimeError as exc:
                        diagnostics.append(
                            f"new service cleanup verification={_diagnostic(str(exc))}"
                        )
    try:
        _restore_definitions(snapshots)
    except Exception as exc:
        diagnostics.append(f"definition restore={_diagnostic(str(exc))}")
    try:
        _restore_symlink(wants_link, previous_wants_target)
    except Exception as exc:
        diagnostics.append(f"enablement link restore={_diagnostic(str(exc))}")
    try:
        code, output = _run(["systemctl", "--user", "daemon-reload"])
    except Exception as exc:
        code, output = 1, str(exc)
    if code != 0:
        diagnostics.append(f"daemon reload={_diagnostic(output)}")
    else:
        state_error = _restore_systemd_timer_state(
            timer_name,
            enabled=previous_enabled,
            active=previous_active,
        )
        if state_error:
            diagnostics.append(f"runtime state restore={state_error}")
    return "; ".join(diagnostics) if diagnostics else None


def install_macos(
    config: Config,
    interval_seconds: int = 3600,
    ghealth_profile: str = "default",
    proxy_environment: dict[str, str] | None = None,
    static_runtime_fingerprint: str | None = None,
    runtime_fingerprint: str | None = None,
) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("macOS scheduler requested on a non-macOS host")
    if interval_seconds < 900:
        raise ValueError("interval must be at least 900 seconds")
    selected_proxy = validate_proxy_environment(proxy_environment or {})
    if not _valid_runtime_fingerprint(
        static_runtime_fingerprint
    ) or not _valid_runtime_fingerprint(runtime_fingerprint):
        raise ValueError("scheduler runtime fingerprint is required")
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    path = launch_agents / f"{SYNC_LABEL}.plist"
    # Validate and safely open both output targets before unloading or replacing
    # any launchd job. Existing log contents are intentionally preserved.
    _prepare_macos_log_files(config)
    payload = _macos_schedule_payload(
        config,
        interval_seconds,
        ghealth_profile,
        selected_proxy,
        static_runtime_fingerprint,
        runtime_fingerprint,
    )
    domain = f"gui/{os.getuid()}"
    snapshot = _snapshot_definitions([path])
    was_loaded = _launchd_job_loaded(domain, SYNC_LABEL)
    if was_loaded:
        code, output = _run(["launchctl", "bootout", domain, str(path)])
        if code != 0:
            raise RuntimeError(
                "launchctl could not unload the existing health scheduler; its definition was not changed. "
                f"Fix the launchd error and retry install. Diagnostic: {_diagnostic(output)}"
            )
    try:
        _atomic_write(path, plistlib.dumps(payload, sort_keys=True))
    except Exception as exc:
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, SYNC_LABEL, was_loaded
        )
        if rollback_error:
            raise RuntimeError(
                "writing the new launchd scheduler definition failed and rollback was incomplete: "
                f"{rollback_error}"
            ) from exc
        raise
    code, output = _run(["launchctl", "bootstrap", domain, str(path)])
    if code != 0:
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, SYNC_LABEL, was_loaded
        )
        raise RuntimeError(
            "launchctl bootstrap failed; the new scheduler was not kept. "
            f"Diagnostic: {_diagnostic(output)}."
            f"{_launchd_rollback_detail(rollback_error)}"
        )
    code, output = _run(["launchctl", "kickstart", "-k", f"{domain}/{SYNC_LABEL}"])
    if code != 0:
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, SYNC_LABEL, was_loaded
        )
        raise RuntimeError(
            "launchctl kickstart failed; the scheduler could not be activated. "
            f"Diagnostic: {_diagnostic(output)}."
            f"{_launchd_rollback_detail(rollback_error)}"
        )
    verify_code, verify_output = _run(
        ["launchctl", "print", f"{domain}/{SYNC_LABEL}"]
    )
    if verify_code != 0 or not _launchd_effective_payload_matches(
        verify_output,
        label=SYNC_LABEL,
        definition_path=path,
        payload=payload,
    ):
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, SYNC_LABEL, was_loaded
        )
        raise RuntimeError(
            "launchctl could not prove that the activated scheduler matches its managed definition."
            f"{_launchd_rollback_detail(rollback_error)}"
        )
    return {
        "backend": "launchd",
        "path": str(path),
        "interval_seconds": interval_seconds,
        "proxy_environment_keys": sorted(selected_proxy),
    }


def install_linux(
    config: Config,
    interval_seconds: int = 3600,
    ghealth_profile: str = "default",
    proxy_environment: dict[str, str] | None = None,
    static_runtime_fingerprint: str | None = None,
    runtime_fingerprint: str | None = None,
) -> dict[str, Any]:
    if platform.system() != "Linux":
        raise RuntimeError("systemd scheduler requested on a non-Linux host")
    if interval_seconds < 900:
        raise ValueError("interval must be at least 900 seconds")
    user_dir = Path.home() / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True, exist_ok=True)
    service = user_dir / "open-health-agent-sync.service"
    timer = user_dir / "open-health-agent-sync.timer"
    profile = _validate_profile_name(ghealth_profile)
    selected_proxy = validate_proxy_environment(proxy_environment or {})
    if not _valid_runtime_fingerprint(
        static_runtime_fingerprint
    ) or not _valid_runtime_fingerprint(runtime_fingerprint):
        raise ValueError("scheduler runtime fingerprint is required")
    service_content = _systemd_service_content(
        config,
        profile,
        selected_proxy,
        static_runtime_fingerprint,
        runtime_fingerprint,
    )
    timer_content = _systemd_timer_content(interval_seconds)
    snapshots = _snapshot_definitions([service, timer])
    wants_link = user_dir / "timers.target.wants" / timer.name
    previous_wants_target = _snapshot_symlink(wants_link)
    had_previous = any(snapshot is not None for snapshot in snapshots.values())
    previous_enabled = _systemd_unit_state(timer.name, "is-enabled")
    previous_active = _systemd_unit_state(timer.name, "is-active")
    previous_service_active = _systemd_unit_state(service.name, "is-active")

    def rollback_note() -> str:
        rollback_error = _rollback_systemd_install(
            snapshots,
            wants_link,
            previous_wants_target,
            timer.name,
            service.name,
            previous_enabled=previous_enabled,
            previous_active=previous_active,
            previous_service_active=previous_service_active,
        )
        if rollback_error:
            return f"rollback was incomplete: {rollback_error}"
        if had_previous or previous_enabled or previous_active:
            return (
                "the previous definitions and enabled state were restored, "
                "including the prior active/inactive state"
            )
        return "the new scheduler was rolled back and no prior timer state remained"

    if previous_active:
        code, output = _run(["systemctl", "--user", "stop", timer.name])
        if code != 0:
            raise RuntimeError(
                "systemctl could not stop the existing health timer; no definition was changed. "
                f"Diagnostic: {_diagnostic(output)}"
            )
        try:
            remained_active = _systemd_unit_state(timer.name, "is-active")
        except RuntimeError as exc:
            restore_code, restore_output = _run(
                ["systemctl", "--user", "start", timer.name]
            )
            restore_note = (
                "the prior active state was restored"
                if restore_code == 0
                else f"restoring the prior active state also failed: {_diagnostic(restore_output)}"
            )
            raise RuntimeError(
                "systemctl could not verify that the existing health timer stopped; "
                f"{restore_note}; no definition was changed"
            ) from exc
        if remained_active:
            raise RuntimeError(
                "systemctl reported success but the existing health timer remained active; "
                "no definition was changed"
            )
    try:
        _atomic_write(service, service_content)
        _atomic_write(timer, timer_content)
    except Exception as exc:
        note = rollback_note()
        if note.startswith("rollback was incomplete"):
            raise RuntimeError(
                f"writing the systemd scheduler definition failed and {note}"
            ) from exc
        raise
    try:
        code, output = _run(["systemctl", "--user", "daemon-reload"])
    except Exception as exc:
        note = rollback_note()
        raise RuntimeError(
            "systemctl daemon-reload could not run; the new scheduler was not kept and "
            f"{note}"
        ) from exc
    if code != 0:
        note = rollback_note()
        raise RuntimeError(
            "systemctl daemon-reload failed; the new scheduler was not kept and "
            f"{note}. Diagnostic: {_diagnostic(output)}"
        )
    if not (
        _systemd_effective_unit_matches(service.name, service)
        and _systemd_effective_unit_matches(timer.name, timer)
    ):
        note = rollback_note()
        raise RuntimeError(
            "systemd loaded a fragment, drop-in, or pending definition that does not match the managed scheduler; "
            f"{note}"
        )
    try:
        code, output = _run(
            ["systemctl", "--user", "enable", "--now", timer.name]
        )
    except Exception as exc:
        note = rollback_note()
        raise RuntimeError(
            "systemctl enable could not run; the new scheduler was rolled back and "
            f"{note}"
        ) from exc
    if code != 0:
        note = rollback_note()
        raise RuntimeError(
            "systemctl enable failed; the new scheduler was rolled back and "
            f"{note}. Diagnostic: {_diagnostic(output)}"
        )
    try:
        enabled_after = _systemd_unit_state(timer.name, "is-enabled")
        active_after = _systemd_unit_state(timer.name, "is-active")
    except RuntimeError as exc:
        note = rollback_note()
        raise RuntimeError(
            "systemctl could not verify the newly installed scheduler; "
            f"{note}"
        ) from exc
    if not enabled_after or not active_after:
        note = rollback_note()
        raise RuntimeError(
            "systemctl did not activate and enable the new scheduler; "
            f"{note}"
        )
    return {
        "backend": "systemd-user",
        "service": str(service),
        "timer": str(timer),
        "proxy_environment_keys": sorted(selected_proxy),
    }


def install_scheduler(
    config: Config,
    interval_seconds: int = 3600,
    proxy_environment: dict[str, str] | None = None,
    expected_static_runtime_fingerprint: str | None = None,
    expected_runtime_fingerprint: str | None = None,
) -> dict[str, Any]:
    if not _valid_runtime_fingerprint(
        expected_static_runtime_fingerprint
    ) or not _valid_runtime_fingerprint(expected_runtime_fingerprint):
        raise ValueError("a verified scheduler runtime fingerprint is required")
    system = platform.system()
    if system not in {"Darwin", "Linux"}:
        raise RuntimeError("automatic scheduling currently supports macOS and systemd Linux; use the CLI from your OS scheduler")
    current_static_fingerprint = static_runtime_fingerprint(config)
    if current_static_fingerprint != expected_static_runtime_fingerprint:
        raise RuntimeError(
            "the current ghealth executable, Python runtime, Open Health Agent code, or timezone "
            "differs from the last successful manual sync"
        )
    # Only execute ghealth after the no-exec file/runtime gate has matched.
    executable = resolve_ghealth_executable(config.ghealth_command)
    profile = _resolve_active_ghealth_profile(executable)
    profile_identity = _ghealth_profile_identity_fingerprint(executable, profile)
    current_runtime_fingerprint = _compose_runtime_fingerprint(
        current_static_fingerprint,
        profile,
        profile_identity,
    )
    if current_runtime_fingerprint != expected_runtime_fingerprint:
        raise RuntimeError(
            "the current ghealth profile, project, account, scopes, or authentication method "
            "differs from the last successful manual sync"
        )
    if config.ghealth_command != str(executable):
        config.ghealth_command = str(executable)
        save_config(config)
    if system == "Darwin":
        result = install_macos(
            config,
            interval_seconds,
            profile,
            proxy_environment,
            static_runtime_fingerprint=expected_static_runtime_fingerprint,
            runtime_fingerprint=expected_runtime_fingerprint,
        )
        return result | {"ghealth_configured": True}
    if system == "Linux":
        result = install_linux(
            config,
            interval_seconds,
            profile,
            proxy_environment,
            static_runtime_fingerprint=expected_static_runtime_fingerprint,
            runtime_fingerprint=expected_runtime_fingerprint,
        )
        return result | {"ghealth_configured": True}
    raise AssertionError("unreachable scheduler platform")


def _macos_keepawake_payload() -> dict[str, Any]:
    return {
        "Label": AWAKE_LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-s"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
    }


def install_macos_ac_keepawake(config: Config) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("AC-only keep-awake is available on macOS only")
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    path = launch_agents / f"{AWAKE_LABEL}.plist"
    payload = _macos_keepawake_payload()
    domain = f"gui/{os.getuid()}"
    snapshot = _snapshot_definitions([path])
    was_loaded = _launchd_job_loaded(domain, AWAKE_LABEL)
    if was_loaded:
        code, output = _run(["launchctl", "bootout", domain, str(path)])
        if code != 0:
            raise RuntimeError(
                "launchctl could not unload the existing AC-only keep-awake job; its definition was not changed. "
                f"Fix the launchd error and retry install. Diagnostic: {_diagnostic(output)}"
            )
    try:
        _atomic_write(path, plistlib.dumps(payload, sort_keys=True))
    except Exception as exc:
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, AWAKE_LABEL, was_loaded
        )
        if rollback_error:
            raise RuntimeError(
                "writing the AC-only keep-awake definition failed and rollback was incomplete: "
                f"{rollback_error}"
            ) from exc
        raise
    code, output = _run(["launchctl", "bootstrap", domain, str(path)])
    if code != 0:
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, AWAKE_LABEL, was_loaded
        )
        raise RuntimeError(
            "launchctl bootstrap failed; the new AC-only keep-awake job was not kept. "
            f"Diagnostic: {_diagnostic(output)}."
            f"{_launchd_rollback_detail(rollback_error)}"
        )
    verify_code, verify_output = _run(
        ["launchctl", "print", f"{domain}/{AWAKE_LABEL}"]
    )
    if verify_code != 0 or not _launchd_effective_payload_matches(
        verify_output,
        label=AWAKE_LABEL,
        definition_path=path,
        payload=payload,
    ):
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, AWAKE_LABEL, was_loaded
        )
        raise RuntimeError(
            "launchctl could not prove that the AC-only keep-awake job matches its managed definition."
            f"{_launchd_rollback_detail(rollback_error)}"
        )
    return {
        "backend": "launchd",
        "path": str(path),
        "behavior": "caffeinate -s prevents idle system sleep only while on AC power; closing a MacBook lid still sleeps",
    }


def uninstall_scheduler(config: Config) -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{SYNC_LABEL}.plist"
        return _uninstall_launchd_job(SYNC_LABEL, path, "the health scheduler")
    if system == "Linux":
        user_dir = Path.home() / ".config" / "systemd" / "user"
        service = user_dir / "open-health-agent-sync.service"
        timer = user_dir / "open-health-agent-sync.timer"
        existed = service.exists() or timer.exists()
        timer_active = _systemd_unit_state(timer.name, "is-active")
        timer_enabled = _systemd_unit_state(timer.name, "is-enabled")
        service_active = _systemd_unit_state(service.name, "is-active")
        runtime_present = timer_active or timer_enabled or service_active
        if not existed and not runtime_present:
            return {
                "backend": "systemd-user",
                "removed": False,
                "runtime_stopped": False,
                "timer_runtime_stopped": False,
                "service_runtime_stopped": False,
                "definition_removed": False,
                "service": str(service),
                "timer": str(timer),
            }
        if timer_active:
            code, output = _run(["systemctl", "--user", "stop", timer.name])
            if code != 0:
                raise RuntimeError(
                    "systemctl could not stop the active health timer; its definitions were kept. "
                    f"Fix the user systemd session and retry uninstall. Diagnostic: {_diagnostic(output)}"
                )
            if _systemd_unit_state(timer.name, "is-active"):
                raise RuntimeError(
                    "systemctl reported success but the health timer remained active; its definitions were kept"
                )
        if service_active:
            code, output = _run(["systemctl", "--user", "stop", service.name])
            if code != 0:
                raise RuntimeError(
                    "systemctl could not stop the running health sync service; its definitions were kept. "
                    f"Fix the user systemd session and retry uninstall. Diagnostic: {_diagnostic(output)}"
                )
            if _systemd_unit_state(service.name, "is-active"):
                raise RuntimeError(
                    "systemctl reported success but the health sync service remained active; its definitions were kept"
                )
        if timer_enabled:
            code, output = _run(["systemctl", "--user", "disable", timer.name])
            if code != 0:
                raise RuntimeError(
                    "systemctl stopped the health timer and service if active but could not disable the timer; "
                    "its definitions were kept. Fix the user systemd session and retry uninstall. "
                    f"Diagnostic: {_diagnostic(output)}"
                )
            if _systemd_unit_state(timer.name, "is-enabled"):
                raise RuntimeError(
                    "systemctl reported success but the health timer remained enabled; its definitions were kept"
                )
        staged = _stage_removal([service, timer])
        try:
            code, output = _run(["systemctl", "--user", "daemon-reload"])
        except Exception as exc:
            _restore_staged(staged)
            raise RuntimeError(
                "systemctl daemon-reload could not run; scheduler definitions were restored, but the timer "
                "remains disabled and any active timer/service remains stopped. Fix the user systemd session "
                "and retry uninstall"
            ) from exc
        if code != 0:
            _restore_staged(staged)
            restore_note = (
                "scheduler definitions were restored, but the timer remains disabled and prior runtime remains stopped"
                if staged
                else "the orphan timer/service was stopped and the timer disabled, but daemon reload did not complete"
            )
            raise RuntimeError(
                f"systemctl daemon-reload failed; {restore_note}. Fix the user systemd session and "
                f"retry uninstall. Diagnostic: {_diagnostic(output)}"
            )
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)
        return {
            "backend": "systemd-user",
            "removed": True,
            "runtime_stopped": timer_active or service_active,
            "timer_runtime_stopped": timer_active,
            "service_runtime_stopped": service_active,
            "enablement_removed": timer_enabled,
            "definition_removed": existed,
            "service": str(service),
            "timer": str(timer),
        }
    raise RuntimeError("automatic scheduler removal currently supports macOS and systemd Linux")


def _launchd_print_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = shlex.split(value)
        except ValueError:
            return value
        if len(parsed) == 1:
            return parsed[0]
    return value


def _launchd_print_block(lines: list[str], name: str) -> list[str] | None:
    for index, line in enumerate(lines):
        if line.strip() != f"{name} = {{":
            continue
        indentation = len(line) - len(line.lstrip())
        values: list[str] = []
        for nested in lines[index + 1 :]:
            if nested.strip() == "}" and len(nested) - len(nested.lstrip()) == indentation:
                return values
            if nested.strip():
                values.append(nested.strip())
        return None
    return None


def _launchd_effective_payload_matches(
    output: str,
    *,
    label: str,
    definition_path: Path,
    payload: dict[str, Any],
) -> bool:
    """Conservatively project launchctl's effective job without exposing it."""

    lines = output.splitlines()
    if not lines or not any(f"/{label} = {{" in line for line in lines[:2]):
        return False
    scalars: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if " = " not in stripped or stripped.endswith("{"):
            continue
        key, raw_value = stripped.split(" = ", 1)
        if key in {"path", "program", "stdout path", "stderr path"} and key not in scalars:
            scalars[key] = _launchd_print_value(raw_value)
    try:
        if Path(scalars.get("path", "")).expanduser().resolve() != definition_path.resolve():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    arguments = payload.get("ProgramArguments")
    if not isinstance(arguments, list) or not all(
        isinstance(argument, str) for argument in arguments
    ):
        return False
    raw_arguments = _launchd_print_block(lines, "arguments")
    if raw_arguments is None:
        return False
    effective_arguments = [_launchd_print_value(value) for value in raw_arguments]
    if effective_arguments != arguments or scalars.get("program") != arguments[0]:
        return False
    expected_environment = payload.get("EnvironmentVariables", {})
    if not isinstance(expected_environment, dict):
        return False
    raw_environment = _launchd_print_block(lines, "environment")
    if raw_environment is None:
        return False
    effective_environment: dict[str, str] = {}
    for entry in raw_environment:
        if " => " in entry:
            key, value = entry.split(" => ", 1)
        elif entry.endswith(" =>"):
            key, value = entry[:-3], ""
        else:
            continue
        if not key or key in effective_environment:
            return False
        effective_environment[key] = _launchd_print_value(value)
    if any(
        effective_environment.get(key) != value
        for key, value in expected_environment.items()
    ):
        return False
    if any(
        key in effective_environment and key not in expected_environment
        for key in PROXY_ENV_KEYS
    ):
        return False
    for payload_key, scalar_key in (
        ("StandardOutPath", "stdout path"),
        ("StandardErrorPath", "stderr path"),
    ):
        expected = payload.get(payload_key)
        if isinstance(expected, str) and scalars.get(scalar_key) != expected:
            return False
    interval = payload.get("StartInterval")
    if interval is not None:
        interval_markers = [
            int(match.group(1))
            for line in lines
            if (
                match := re.search(
                    r"(?:\"?Interval\"?|run interval)\s*(?:=>|=)\s*([0-9]+)",
                    line,
                    re.IGNORECASE,
                )
            )
        ]
        if interval_markers != [interval]:
            return False
    return True


def _systemd_effective_unit_matches(unit: str, expected_fragment: Path) -> bool:
    code, output = _run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=FragmentPath",
            "--property=DropInPaths",
            "--property=NeedDaemonReload",
            "--no-pager",
        ]
    )
    if code != 0:
        return False
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            return False
        key, value = line.split("=", 1)
        if key in values:
            return False
        values[key] = value
    if set(values) != {"FragmentPath", "DropInPaths", "NeedDaemonReload"}:
        return False
    try:
        fragment_matches = (
            Path(values["FragmentPath"]).expanduser().resolve()
            == expected_fragment.resolve()
        )
    except (OSError, RuntimeError, ValueError):
        fragment_matches = False
    return bool(
        fragment_matches
        and not values["DropInPaths"].strip()
        and values["NeedDaemonReload"].strip().casefold() == "no"
    )


def keepawake_status() -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"backend": "unsupported", "installed": False}
    path = Path.home() / "Library" / "LaunchAgents" / f"{AWAKE_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    code, output = _run(["launchctl", "print", f"{domain}/{AWAKE_LABEL}"])
    configuration_matches = False
    definition_owner_only = _owner_only_regular_file(path)
    effective_runtime_matches = False
    if path.exists():
        try:
            payload = plistlib.loads(path.read_bytes())
            expected = _macos_keepawake_payload()
            configuration_matches = payload == expected
            if configuration_matches and definition_owner_only and code == 0:
                effective_runtime_matches = _launchd_effective_payload_matches(
                    output,
                    label=AWAKE_LABEL,
                    definition_path=path,
                    payload=expected,
                )
        except (OSError, ValueError, plistlib.InvalidFileException):
            configuration_matches = False
    return {
        "backend": "launchd",
        "installed": bool(
            path.exists()
            and code == 0
            and configuration_matches
            and definition_owner_only
            and effective_runtime_matches
        ),
        "definition_exists": path.exists(),
        "loaded": code == 0,
        "configuration_matches": configuration_matches,
        "definition_owner_only": definition_owner_only,
        "effective_runtime_matches": effective_runtime_matches,
        "path": str(path),
    }


def uninstall_macos_ac_keepawake() -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("AC-only keep-awake removal is available on macOS only")
    path = Path.home() / "Library" / "LaunchAgents" / f"{AWAKE_LABEL}.plist"
    return _uninstall_launchd_job(AWAKE_LABEL, path, "AC-only keep-awake")


def _owner_only_regular_file(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    if os.name == "nt":
        return True
    try:
        metadata = path.stat()
    except OSError:
        return False
    if metadata.st_mode & 0o077:
        return False
    filesystem_uid = _filesystem_uid()
    return filesystem_uid is None or metadata.st_uid == filesystem_uid


def _proxy_definition_status(environment: Any) -> tuple[list[str], bool]:
    if not isinstance(environment, dict):
        return [], False
    raw_proxy = {key: environment[key] for key in PROXY_ENV_KEYS if key in environment}
    keys = sorted(raw_proxy)
    try:
        validated = validate_proxy_environment(raw_proxy)
    except (TypeError, ValueError):
        return keys, False
    return keys, set(validated) == set(raw_proxy)


def _pinned_runtime_from_arguments(
    config: Config, arguments: Any
) -> tuple[str, str] | None:
    if not isinstance(arguments, list) or len(arguments) != 13:
        return None
    static_candidate = arguments[9]
    runtime_candidate = arguments[11]
    if not _valid_runtime_fingerprint(
        static_candidate
    ) or not _valid_runtime_fingerprint(runtime_candidate):
        return None
    return (
        (static_candidate, runtime_candidate)
        if arguments
        == _scheduler_program_arguments(
            config, static_candidate, runtime_candidate
        )
        else None
    )


def _pinned_runtime_from_systemd_exec(
    config: Config, raw_exec: str
) -> tuple[str, str] | None:
    if not raw_exec.startswith(":"):
        return None
    try:
        arguments = [
            value.replace("%%", "%")
            for value in shlex.split(raw_exec[1:])
        ]
    except ValueError:
        return None
    return _pinned_runtime_from_arguments(config, arguments)


def _systemd_environment(lines: list[str]) -> tuple[dict[str, str], bool]:
    environment: dict[str, str] = {}
    valid = True
    for line in lines:
        if not line.startswith("Environment="):
            continue
        raw = line[len("Environment=") :]
        if "=" not in raw:
            valid = False
            continue
        key, value = raw.split("=", 1)
        if not key or key in environment:
            valid = False
            continue
        environment[key] = value
    return environment, valid


def _assignment_values(lines: list[str]) -> tuple[dict[str, list[str]], bool]:
    values: dict[str, list[str]] = {}
    valid = True
    for line in lines:
        if not line or line.startswith("["):
            continue
        if "=" not in line:
            valid = False
            continue
        key, value = line.split("=", 1)
        values.setdefault(key, []).append(value)
    return values, valid


def scheduler_status(config: Config) -> dict[str, Any]:
    system = platform.system()
    try:
        resolve_ghealth_executable(config.ghealth_command)
        command_ok = True
    except FileNotFoundError:
        command_ok = False

    if system == "Darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{SYNC_LABEL}.plist"
        domain = f"gui/{os.getuid()}"
        code, launchd_output = _run(
            ["launchctl", "print", f"{domain}/{SYNC_LABEL}"]
        )
        definition_owner_only = _owner_only_regular_file(path)
        definition_matches = False
        profile_pinned = False
        json_format_forced = False
        scheduled_trigger_pinned = False
        static_runtime_fingerprint_matches = False
        runtime_fingerprint_matches = False
        effective_runtime_matches = False
        log_files_safe = _scheduler_log_files_safe(config)
        proxy_configuration_valid = False
        schedule_invariants_valid = False
        proxy_environment_keys: list[str] = []
        if path.exists():
            try:
                payload = plistlib.loads(path.read_bytes())
                if not isinstance(payload, dict):
                    raise ValueError("scheduler definition is invalid")
                environment = payload.get("EnvironmentVariables")
                if not isinstance(environment, dict):
                    raise ValueError("scheduler environment is invalid")
                proxy_environment_keys, proxy_configuration_valid = (
                    _proxy_definition_status(environment)
                )
                raw_proxy = {
                    key: environment[key]
                    for key in proxy_environment_keys
                    if key in environment
                }
                selected_proxy = (
                    validate_proxy_environment(raw_proxy)
                    if proxy_configuration_valid
                    else {}
                )
                pinned_profile = environment.get("GHEALTH_PROFILE")
                profile_pinned = isinstance(pinned_profile, str) and bool(
                    re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", pinned_profile
                    )
                )
                json_format_forced = environment.get("GHEALTH_FORMAT") == "json"
                arguments = payload.get("ProgramArguments")
                pinned = _pinned_runtime_from_arguments(config, arguments)
                scheduled_trigger_pinned = pinned is not None
                interval = payload.get("StartInterval")
                interval_valid = bool(
                    isinstance(interval, int)
                    and not isinstance(interval, bool)
                    and interval >= 900
                )
                environment_allowlisted = bool(
                    set(environment)
                    == {
                        "GHEALTH_PROFILE",
                        "GHEALTH_FORMAT",
                        *SAFE_RUNTIME_ENVIRONMENT,
                        *proxy_environment_keys,
                    }
                    and all(
                        environment.get(key) == value
                        for key, value in SAFE_RUNTIME_ENVIRONMENT.items()
                    )
                )
                if (
                    interval_valid
                    and profile_pinned
                    and json_format_forced
                    and scheduled_trigger_pinned
                    and proxy_configuration_valid
                    and environment_allowlisted
                    and pinned is not None
                ):
                    pinned_static, pinned_runtime = pinned
                    expected = _macos_schedule_payload(
                        config,
                        interval,
                        pinned_profile,
                        selected_proxy,
                        pinned_static,
                        pinned_runtime,
                    )
                    schedule_invariants_valid = payload == expected
                if schedule_invariants_valid and definition_owner_only and command_ok:
                    try:
                        current_static_runtime = static_runtime_fingerprint(config)
                    except (OSError, RuntimeError, ValueError):
                        current_static_runtime = None
                    static_runtime_fingerprint_matches = bool(
                        current_static_runtime
                        and current_static_runtime == pinned_static
                    )
                    if static_runtime_fingerprint_matches:
                        try:
                            current_runtime = ghealth_runtime_fingerprint(
                                config, pinned_profile
                            )
                        except (OSError, RuntimeError, ValueError):
                            current_runtime = None
                    else:
                        current_runtime = None
                    runtime_fingerprint_matches = bool(
                        current_runtime and current_runtime == pinned_runtime
                    )
                    if runtime_fingerprint_matches and code == 0:
                        effective_runtime_matches = (
                            _launchd_effective_payload_matches(
                                launchd_output,
                                label=SYNC_LABEL,
                                definition_path=path,
                                payload=expected,
                            )
                        )
                definition_matches = bool(
                    schedule_invariants_valid
                    and definition_owner_only
                    and static_runtime_fingerprint_matches
                    and runtime_fingerprint_matches
                )
            except (
                OSError,
                TypeError,
                ValueError,
                plistlib.InvalidFileException,
            ):
                definition_matches = False
        installed = bool(
            path.exists()
            and code == 0
            and definition_matches
            and effective_runtime_matches
            and command_ok
            and log_files_safe
        )
        return {
            "backend": "launchd",
            "installed": installed,
            "definition_exists": path.exists(),
            "loaded": code == 0,
            "runtime_orphaned": code == 0 and not path.exists(),
            "configuration_matches": definition_matches,
            "ghealth_profile_pinned": profile_pinned,
            "ghealth_json_forced": json_format_forced,
            "scheduled_trigger_pinned": scheduled_trigger_pinned,
            "static_runtime_fingerprint_matches": static_runtime_fingerprint_matches,
            "runtime_fingerprint_matches": runtime_fingerprint_matches,
            "effective_runtime_matches": effective_runtime_matches,
            "proxy_configuration_valid": proxy_configuration_valid,
            "schedule_invariants_valid": schedule_invariants_valid,
            "definition_owner_only": definition_owner_only,
            "log_files_safe": log_files_safe,
            "ghealth_available": command_ok,
            "proxy_environment_keys": proxy_environment_keys,
            "proxy_configured": bool(proxy_environment_keys),
            "path": str(path),
        }

    if system == "Linux":
        user_dir = Path.home() / ".config" / "systemd" / "user"
        service = user_dir / "open-health-agent-sync.service"
        timer = user_dir / "open-health-agent-sync.timer"
        code, _ = _run(["systemctl", "--user", "is-enabled", timer.name])
        active_code, _ = _run(["systemctl", "--user", "is-active", timer.name])
        try:
            service_bytes = service.read_bytes()
            service_text = service_bytes.decode("utf-8")
        except (OSError, UnicodeError):
            service_bytes = b""
            service_text = ""
        try:
            timer_bytes = timer.read_bytes()
            timer_text = timer_bytes.decode("utf-8")
        except (OSError, UnicodeError):
            timer_bytes = b""
            timer_text = ""
        service_lines = service_text.splitlines()
        timer_lines = timer_text.splitlines()
        definition_exists = service.exists() or timer.exists()
        definition_complete = service.exists() and timer.exists()
        definition_owner_only = bool(
            definition_complete
            and _owner_only_regular_file(service)
            and _owner_only_regular_file(timer)
        )
        environment, environment_parse_valid = _systemd_environment(service_lines)
        proxy_environment_keys, proxy_configuration_valid = _proxy_definition_status(
            environment
        )
        raw_proxy = {
            key: environment[key]
            for key in proxy_environment_keys
            if key in environment
        }
        selected_proxy = (
            validate_proxy_environment(raw_proxy)
            if proxy_configuration_valid
            else {}
        )
        pinned_profile = environment.get("GHEALTH_PROFILE")
        profile_pinned = isinstance(pinned_profile, str) and bool(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", pinned_profile)
        )
        json_format_forced = environment.get("GHEALTH_FORMAT") == "json"
        environment_allowlisted = bool(
            environment_parse_valid
            and set(environment)
            == {
                "GHEALTH_PROFILE",
                "GHEALTH_FORMAT",
                *SAFE_RUNTIME_ENVIRONMENT,
                *proxy_environment_keys,
            }
            and all(
                environment.get(key) == value
                for key, value in SAFE_RUNTIME_ENVIRONMENT.items()
            )
        )
        exec_lines = [
            line[len("ExecStart=") :]
            for line in service_lines
            if line.startswith("ExecStart=")
        ]
        pinned: tuple[str, str] | None = None
        if len(exec_lines) == 1:
            pinned = _pinned_runtime_from_systemd_exec(config, exec_lines[0])
        scheduled_trigger_pinned = pinned is not None
        interval_lines = [
            line[len("OnUnitActiveSec=") :]
            for line in timer_lines
            if line.startswith("OnUnitActiveSec=")
        ]
        interval: int | None = None
        if len(interval_lines) == 1:
            match = re.fullmatch(r"([0-9]+)s", interval_lines[0])
            if match and int(match.group(1)) >= 900:
                interval = int(match.group(1))
        schedule_invariants_valid = False
        if (
            definition_complete
            and interval is not None
            and profile_pinned
            and json_format_forced
            and environment_allowlisted
            and proxy_configuration_valid
            and scheduled_trigger_pinned
            and pinned is not None
        ):
            pinned_static, pinned_runtime = pinned
            expected_service = _systemd_service_content(
                config,
                pinned_profile,
                selected_proxy,
                pinned_static,
                pinned_runtime,
            )
            expected_timer = _systemd_timer_content(interval)
            schedule_invariants_valid = bool(
                service_bytes == expected_service and timer_bytes == expected_timer
            )
        effective_service_matches = False
        effective_timer_matches = False
        effective_runtime_matches = False
        if schedule_invariants_valid and definition_owner_only:
            effective_service_matches = _systemd_effective_unit_matches(
                service.name, service
            )
            effective_timer_matches = _systemd_effective_unit_matches(
                timer.name, timer
            )
            effective_runtime_matches = bool(
                effective_service_matches and effective_timer_matches
            )
        static_runtime_fingerprint_matches = False
        runtime_fingerprint_matches = False
        if (
            schedule_invariants_valid
            and definition_owner_only
            and effective_runtime_matches
            and command_ok
        ):
            try:
                current_static_runtime = static_runtime_fingerprint(config)
            except (OSError, RuntimeError, ValueError):
                current_static_runtime = None
            static_runtime_fingerprint_matches = bool(
                current_static_runtime
                and current_static_runtime == pinned_static
            )
            if static_runtime_fingerprint_matches:
                try:
                    current_runtime = ghealth_runtime_fingerprint(
                        config, pinned_profile
                    )
                except (OSError, RuntimeError, ValueError):
                    current_runtime = None
            else:
                current_runtime = None
            runtime_fingerprint_matches = bool(
                current_runtime and current_runtime == pinned_runtime
            )
        definition_matches = bool(
            schedule_invariants_valid
            and definition_owner_only
            and effective_runtime_matches
            and static_runtime_fingerprint_matches
            and runtime_fingerprint_matches
        )
        linger_code, linger_output = _run(
            ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"]
        )
        linger = linger_output.strip().lower() == "yes" if linger_code == 0 else None
        installed = bool(
            definition_complete
            and code == 0
            and active_code == 0
            and definition_matches
            and command_ok
        )
        return {
            "backend": "systemd-user",
            "installed": installed,
            "definition_exists": definition_exists,
            "definition_complete": definition_complete,
            "enabled": code == 0,
            "active": active_code == 0,
            "runtime_orphaned": active_code == 0 and not definition_exists,
            "definition_incomplete": definition_exists and not definition_complete,
            "configuration_matches": definition_matches,
            "ghealth_profile_pinned": profile_pinned,
            "ghealth_json_forced": json_format_forced,
            "scheduled_trigger_pinned": scheduled_trigger_pinned,
            "static_runtime_fingerprint_matches": static_runtime_fingerprint_matches,
            "runtime_fingerprint_matches": runtime_fingerprint_matches,
            "effective_service_matches": effective_service_matches,
            "effective_timer_matches": effective_timer_matches,
            "effective_runtime_matches": effective_runtime_matches,
            "proxy_configuration_valid": proxy_configuration_valid,
            "schedule_invariants_valid": schedule_invariants_valid,
            "definition_owner_only": definition_owner_only,
            "ghealth_available": command_ok,
            "proxy_environment_keys": proxy_environment_keys,
            "proxy_configured": bool(proxy_environment_keys),
            "linger": linger,
            "logout_warning": linger is not True,
            "service": str(service),
            "timer": str(timer),
        }

    return {"backend": "unsupported", "installed": False}
