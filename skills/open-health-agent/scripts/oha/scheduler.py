from __future__ import annotations

import json
import os
import plistlib
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import Config, save_config

SYNC_LABEL = "io.github.open-health-agent.sync"
AWAKE_LABEL = "io.github.open-health-agent.keep-awake-on-ac"


def _run(command: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command, check=False, capture_output=True, text=True
        )
    except FileNotFoundError:
        return 127, "scheduler backend command is unavailable"
    return completed.returncode, (completed.stdout or completed.stderr).strip()


def _entrypoint() -> Path:
    return Path(sys.argv[0]).expanduser().resolve()


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


def _persist_resolved_ghealth(config: Config) -> Path:
    executable = resolve_ghealth_executable(config.ghealth_command)
    if config.ghealth_command != str(executable):
        config.ghealth_command = str(executable)
        save_config(config)
    return executable


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


def _resolve_active_ghealth_profile(executable: Path) -> str:
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
    for entry in profiles:
        if not isinstance(entry, dict) or entry.get("active") is not True:
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            return _validate_profile_name(name)
    # ghealth itself falls back to the default profile when an override names a
    # profile that is absent, so pin the same deterministic fallback.
    return "default"


def _rollback_launchd_definition(
    path: Path,
    snapshot: dict[Path, tuple[bytes, int] | None],
    domain: str,
    label: str,
    was_loaded: bool,
) -> str | None:
    # Best effort unload in case bootstrap partially registered the new job.
    _run(["launchctl", "bootout", domain, str(path)])
    _restore_definitions(snapshot)
    if not was_loaded or snapshot[path] is None:
        return None
    code, output = _run(["launchctl", "bootstrap", domain, str(path)])
    if code != 0:
        return _diagnostic(output)
    code, output = _run(["launchctl", "kickstart", "-k", f"{domain}/{label}"])
    return _diagnostic(output) if code != 0 else None


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
    return code in {1, 3, 4} and any(
        marker in normalized
        for marker in (
            "inactive",
            "disabled",
            "unknown",
            "not-found",
            "not found",
            "does not exist",
        )
    )


def install_macos(
    config: Config,
    interval_seconds: int = 3600,
    ghealth_profile: str = "default",
) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("macOS scheduler requested on a non-macOS host")
    if interval_seconds < 900:
        raise ValueError("interval must be at least 900 seconds")
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    path = launch_agents / f"{SYNC_LABEL}.plist"
    logs = config.home_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": SYNC_LABEL,
        "ProgramArguments": [
            sys.executable,
            str(_entrypoint()),
            "--home",
            str(config.home_path),
            "sync",
            "--quiet",
        ],
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "GHEALTH_PROFILE": _validate_profile_name(ghealth_profile),
            "GHEALTH_FORMAT": "json",
        },
        "StandardOutPath": str(logs / "scheduler.out.log"),
        "StandardErrorPath": str(logs / "scheduler.err.log"),
    }
    domain = f"gui/{os.getuid()}"
    snapshot = _snapshot_definitions([path])
    loaded_code, _ = _run(["launchctl", "print", f"{domain}/{SYNC_LABEL}"])
    was_loaded = loaded_code == 0
    if was_loaded:
        code, output = _run(["launchctl", "bootout", domain, str(path)])
        if code != 0:
            raise RuntimeError(
                "launchctl could not unload the existing health scheduler; its definition was not changed. "
                f"Fix the launchd error and retry install. Diagnostic: {_diagnostic(output)}"
            )
    try:
        _atomic_write(path, plistlib.dumps(payload, sort_keys=True))
    except Exception:
        _restore_definitions(snapshot)
        if was_loaded and snapshot[path] is not None:
            _run(["launchctl", "bootstrap", domain, str(path)])
        raise
    code, output = _run(["launchctl", "bootstrap", domain, str(path)])
    if code != 0:
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, SYNC_LABEL, was_loaded
        )
        detail = (
            f" Previous job restore also failed: {rollback_error}."
            if rollback_error
            else " The previous definition and loaded state were restored."
        )
        raise RuntimeError(
            "launchctl bootstrap failed; the new scheduler was not kept. "
            f"Diagnostic: {_diagnostic(output)}.{detail}"
        )
    code, output = _run(["launchctl", "kickstart", "-k", f"{domain}/{SYNC_LABEL}"])
    if code != 0:
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, SYNC_LABEL, was_loaded
        )
        detail = (
            f" Previous job restore also failed: {rollback_error}."
            if rollback_error
            else " The previous definition and loaded state were restored."
        )
        raise RuntimeError(
            "launchctl kickstart failed; the new scheduler was rolled back. "
            f"Diagnostic: {_diagnostic(output)}.{detail}"
        )
    return {"backend": "launchd", "path": str(path), "interval_seconds": interval_seconds}


def install_linux(
    config: Config,
    interval_seconds: int = 3600,
    ghealth_profile: str = "default",
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
    command = shlex.join(
        [sys.executable, str(_entrypoint()), "--home", str(config.home_path), "sync", "--quiet"]
    )
    service_content = "\n".join(
        [
            "[Unit]",
            "Description=Open Health Agent deterministic health sync",
            "",
            "[Service]",
            "Type=oneshot",
            f"Environment=GHEALTH_PROFILE={profile}",
            "Environment=GHEALTH_FORMAT=json",
            f"ExecStart={command}",
            "",
        ]
    ).encode("utf-8")
    timer_content = "\n".join(
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
    snapshots = _snapshot_definitions([service, timer])
    wants_link = user_dir / "timers.target.wants" / timer.name
    previous_wants_target = _snapshot_symlink(wants_link)
    had_previous = any(snapshot is not None for snapshot in snapshots.values())
    previous_enabled = False
    if had_previous:
        enabled_code, _ = _run(["systemctl", "--user", "is-enabled", timer.name])
        previous_enabled = enabled_code == 0
    try:
        _atomic_write(service, service_content)
        _atomic_write(timer, timer_content)
    except Exception:
        _restore_definitions(snapshots)
        raise
    try:
        code, output = _run(["systemctl", "--user", "daemon-reload"])
    except Exception as exc:
        _restore_definitions(snapshots)
        raise RuntimeError(
            "systemctl daemon-reload could not run; the new scheduler definitions were rolled back"
        ) from exc
    if code != 0:
        _restore_definitions(snapshots)
        rollback_code, rollback_output = _run(["systemctl", "--user", "daemon-reload"])
        rollback_note = (
            "previous definitions were restored"
            if rollback_code == 0
            else f"definitions were restored on disk but reload also failed: {_diagnostic(rollback_output)}"
        )
        raise RuntimeError(
            "systemctl daemon-reload failed; the new scheduler was not kept and "
            f"{rollback_note}. Diagnostic: {_diagnostic(output)}"
        )
    code, output = _run(["systemctl", "--user", "enable", "--now", timer.name])
    if code != 0:
        # Remove any enablement created before the failing command returned,
        # then restore the exact prior definitions and enabled state.
        cleanup_code, cleanup_output = _run(
            ["systemctl", "--user", "disable", "--now", timer.name]
        )
        _restore_definitions(snapshots)
        _restore_symlink(wants_link, previous_wants_target)
        reload_code, reload_output = _run(["systemctl", "--user", "daemon-reload"])
        state_code = 0
        state_output = ""
        if previous_enabled:
            state_code, state_output = _run(
                ["systemctl", "--user", "enable", "--now", timer.name]
            )
        rollback_ok = (
            reload_code == 0
            and (not previous_enabled or state_code == 0)
            and (cleanup_code == 0 or previous_wants_target is not None or not os.path.lexists(wants_link))
        )
        rollback_note = (
            "the previous definitions and enabled state were restored"
            if rollback_ok
            else "definitions were restored on disk, but restoring the prior systemd state failed; "
            f"cleanup={_diagnostic(cleanup_output)}, reload={_diagnostic(reload_output)}, "
            f"state={_diagnostic(state_output)}"
        )
        raise RuntimeError(
            "systemctl enable failed; the new scheduler was rolled back and "
            f"{rollback_note}. Diagnostic: {_diagnostic(output)}"
        )
    return {"backend": "systemd-user", "service": str(service), "timer": str(timer)}


def install_scheduler(config: Config, interval_seconds: int = 3600) -> dict[str, Any]:
    system = platform.system()
    if system not in {"Darwin", "Linux"}:
        raise RuntimeError("automatic scheduling currently supports macOS and systemd Linux; use the CLI from your OS scheduler")
    executable = _persist_resolved_ghealth(config)
    profile = _resolve_active_ghealth_profile(executable)
    if system == "Darwin":
        result = install_macos(config, interval_seconds, profile)
        return result | {"ghealth_command": str(executable)}
    if system == "Linux":
        result = install_linux(config, interval_seconds, profile)
        return result | {"ghealth_command": str(executable)}
    raise AssertionError("unreachable scheduler platform")


def install_macos_ac_keepawake(config: Config) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("AC-only keep-awake is available on macOS only")
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    path = launch_agents / f"{AWAKE_LABEL}.plist"
    payload = {
        "Label": AWAKE_LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-s"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
    }
    domain = f"gui/{os.getuid()}"
    snapshot = _snapshot_definitions([path])
    loaded_code, _ = _run(["launchctl", "print", f"{domain}/{AWAKE_LABEL}"])
    was_loaded = loaded_code == 0
    if was_loaded:
        code, output = _run(["launchctl", "bootout", domain, str(path)])
        if code != 0:
            raise RuntimeError(
                "launchctl could not unload the existing AC-only keep-awake job; its definition was not changed. "
                f"Fix the launchd error and retry install. Diagnostic: {_diagnostic(output)}"
            )
    try:
        _atomic_write(path, plistlib.dumps(payload, sort_keys=True))
    except Exception:
        _restore_definitions(snapshot)
        if was_loaded and snapshot[path] is not None:
            _run(["launchctl", "bootstrap", domain, str(path)])
        raise
    code, output = _run(["launchctl", "bootstrap", domain, str(path)])
    if code != 0:
        rollback_error = _rollback_launchd_definition(
            path, snapshot, domain, AWAKE_LABEL, was_loaded
        )
        detail = (
            f" Previous job restore also failed: {rollback_error}."
            if rollback_error
            else " The previous definition and loaded state were restored."
        )
        raise RuntimeError(
            "launchctl bootstrap failed; the new AC-only keep-awake job was not kept. "
            f"Diagnostic: {_diagnostic(output)}.{detail}"
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
        active_code, active_output = _run(
            ["systemctl", "--user", "is-active", timer.name]
        )
        enabled_code, enabled_output = _run(
            ["systemctl", "--user", "is-enabled", timer.name]
        )
        active = active_code == 0
        enabled = enabled_code == 0
        active_known = active or _systemd_state_is_absent(active_code, active_output)
        enabled_known = enabled or _systemd_state_is_absent(
            enabled_code, enabled_output
        )
        if not active_known or not enabled_known:
            raise RuntimeError(
                "systemctl could not verify whether the health timer is active or enabled; "
                "no definition was removed. Fix the user systemd session and retry"
            )
        runtime_present = active or enabled
        if not existed and not runtime_present:
            return {
                "backend": "systemd-user",
                "removed": False,
                "runtime_stopped": False,
                "definition_removed": False,
                "service": str(service),
                "timer": str(timer),
            }
        if active:
            code, output = _run(["systemctl", "--user", "stop", timer.name])
            if code != 0:
                raise RuntimeError(
                    "systemctl could not stop the active health timer; its definitions were kept. "
                    f"Fix the user systemd session and retry uninstall. Diagnostic: {_diagnostic(output)}"
                )
        if enabled:
            code, output = _run(["systemctl", "--user", "disable", timer.name])
            if code != 0:
                raise RuntimeError(
                    "systemctl stopped the health timer if it was active but could not disable it; "
                    "its definitions were kept. Fix the user systemd session and retry uninstall. "
                    f"Diagnostic: {_diagnostic(output)}"
                )
        staged = _stage_removal([service, timer])
        try:
            code, output = _run(["systemctl", "--user", "daemon-reload"])
        except Exception as exc:
            _restore_staged(staged)
            raise RuntimeError(
                "systemctl daemon-reload could not run; scheduler definitions were restored, but the timer "
                "remains disabled. Fix the user systemd session and retry uninstall"
            ) from exc
        if code != 0:
            _restore_staged(staged)
            restore_note = (
                "scheduler definitions were restored, but the timer remains disabled"
                if staged
                else "the orphan timer was stopped/disabled, but daemon reload did not complete"
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
            "runtime_stopped": active,
            "enablement_removed": enabled,
            "definition_removed": existed,
            "service": str(service),
            "timer": str(timer),
        }
    raise RuntimeError("automatic scheduler removal currently supports macOS and systemd Linux")


def keepawake_status() -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"backend": "unsupported", "installed": False}
    path = Path.home() / "Library" / "LaunchAgents" / f"{AWAKE_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    code, _ = _run(["launchctl", "print", f"{domain}/{AWAKE_LABEL}"])
    configuration_matches = False
    if path.exists():
        try:
            payload = plistlib.loads(path.read_bytes())
            configuration_matches = payload.get("ProgramArguments") == ["/usr/bin/caffeinate", "-s"]
        except (OSError, ValueError, plistlib.InvalidFileException):
            configuration_matches = False
    return {
        "backend": "launchd",
        "installed": path.exists() and code == 0 and configuration_matches,
        "definition_exists": path.exists(),
        "loaded": code == 0,
        "configuration_matches": configuration_matches,
        "path": str(path),
    }


def uninstall_macos_ac_keepawake() -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise RuntimeError("AC-only keep-awake removal is available on macOS only")
    path = Path.home() / "Library" / "LaunchAgents" / f"{AWAKE_LABEL}.plist"
    return _uninstall_launchd_job(AWAKE_LABEL, path, "AC-only keep-awake")


def scheduler_status(config: Config) -> dict[str, Any]:
    system = platform.system()
    command_path = Path(config.ghealth_command).expanduser()
    command_ok = command_path.is_file() and os.access(command_path, os.X_OK)
    if system == "Darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{SYNC_LABEL}.plist"
        domain = f"gui/{os.getuid()}"
        code, _ = _run(["launchctl", "print", f"{domain}/{SYNC_LABEL}"])
        definition_matches = False
        profile_pinned = False
        json_format_forced = False
        if path.exists():
            try:
                payload = plistlib.loads(path.read_bytes())
                arguments = payload.get("ProgramArguments") or []
                environment = payload.get("EnvironmentVariables") or {}
                pinned_profile = environment.get("GHEALTH_PROFILE")
                profile_pinned = isinstance(pinned_profile, str) and bool(
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", pinned_profile)
                )
                json_format_forced = environment.get("GHEALTH_FORMAT") == "json"
                definition_matches = (
                    str(config.home_path) in arguments
                    and str(_entrypoint()) in arguments
                    and profile_pinned
                    and json_format_forced
                )
            except (OSError, ValueError, plistlib.InvalidFileException):
                definition_matches = False
        installed = path.exists() and code == 0 and definition_matches and command_ok
        return {
            "backend": "launchd",
            "installed": installed,
            "definition_exists": path.exists(),
            "loaded": code == 0,
            "runtime_orphaned": code == 0 and not path.exists(),
            "configuration_matches": definition_matches,
            "ghealth_profile_pinned": profile_pinned,
            "ghealth_json_forced": json_format_forced,
            "ghealth_available": command_ok,
            "path": str(path),
        }
    if system == "Linux":
        user_dir = Path.home() / ".config" / "systemd" / "user"
        service = user_dir / "open-health-agent-sync.service"
        timer = user_dir / "open-health-agent-sync.timer"
        code, _ = _run(["systemctl", "--user", "is-enabled", timer.name])
        active_code, _ = _run(["systemctl", "--user", "is-active", timer.name])
        try:
            service_text = service.read_text(encoding="utf-8")
        except OSError:
            service_text = ""
        definition_exists = service.exists() or timer.exists()
        definition_complete = service.exists() and timer.exists()
        profile_pinned = bool(
            re.search(
                r"(?m)^Environment=GHEALTH_PROFILE=[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
                service_text,
            )
        )
        json_format_forced = bool(
            re.search(r"(?m)^Environment=GHEALTH_FORMAT=json$", service_text)
        )
        definition_matches = (
            str(config.home_path) in service_text
            and str(_entrypoint()) in service_text
            and profile_pinned
            and json_format_forced
        )
        linger_code, linger_output = _run(
            ["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"]
        )
        linger = linger_output.strip().lower() == "yes" if linger_code == 0 else None
        installed = (
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
            "ghealth_available": command_ok,
            "linger": linger,
            "logout_warning": linger is not True,
            "service": str(service),
            "timer": str(timer),
        }
    return {"backend": "unsupported", "installed": False}
