#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from oha.config import (
    atomic_write_json,
    config_path,
    create_config,
    initialize_local_files,
    load_config,
    save_config,
)
from oha.context import build_context
from oha.database import HealthDatabase
from oha.ghealth_adapter import (
    CommandRunner,
    DAILY_FIELDS_BY_QUERY,
    FixtureRunner,
    GHealthAdapter,
    GHealthError,
    merge_partial_daily,
)
from oha.locking import FileLock
from oha.profile import (
    GoalProjectionError,
    ProfileProjectionError,
    goal_views_status,
    normalize_goal,
    profile_invalid_fields,
    rebuild_goal_views,
    retire_goal,
    set_goal,
    set_profile_value,
    validate_profile_value,
)
from oha.recording import SUPPORTED_KINDS, normalize_record
from oha.scheduler import (
    ghealth_runtime_fingerprint,
    install_macos_ac_keepawake,
    install_scheduler,
    keepawake_status,
    proxy_environment_from_file,
    proxy_environment_from_process,
    resolve_active_ghealth_profile,
    resolve_ghealth_executable,
    scheduler_status,
    static_runtime_fingerprint,
    uninstall_macos_ac_keepawake,
    uninstall_scheduler,
)
from oha.state import state_validation_error
from oha.workbook_store import export_workbook, inspect_workbook_schema


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ASSETS_DIR = SKILL_DIR / "assets"


class CLIUsageError(ValueError):
    """An argparse failure whose original message may contain private input."""


class CanonicalDatabaseMissingError(RuntimeError):
    """The configured SQLite source of truth is absent and must not be recreated."""


class InitializationConflictError(RuntimeError):
    """An unconfigured private home or workbook already contains managed data."""


class StateInvalidError(RuntimeError):
    """The private operational state is missing its required object shape."""


CONSENT_SCOPES = {
    "google-health",
    "scheduler",
    "keep-awake",
    "weixin",
    "vision",
    "speech",
    "cloud-workbook",
    "cloud-private-home",
}

EXTERNAL_REVOCATION_ACTIONS = {
    "google-health": (
        "Local health access is disabled. Revoke the provider authorization separately if its token "
        "must also be invalidated."
    ),
    "weixin": (
        "Local consent is withdrawn. Disable or re-pair the Weixin channel in Hermes separately."
    ),
    "vision": (
        "Local consent is withdrawn. Disable the configured vision provider in Hermes separately."
    ),
    "speech": (
        "Local consent is withdrawn. Disable the configured speech provider in Hermes separately."
    ),
    "cloud-workbook": (
        "Local consent is withdrawn. Remove cloud sharing or synchronization separately; no workbook was deleted."
    ),
    "cloud-private-home": (
        "Local consent is withdrawn. The existing synchronized private home was not moved or deleted; "
        "health writes remain paused until it is migrated to local storage or explicitly re-authorized."
    ),
}


class PrivateArgumentParser(argparse.ArgumentParser):
    """Keep malformed command lines from echoing health text or local paths."""

    def error(self, message: str) -> None:
        del message
        raise CLIUsageError("command-line parsing failed")


def emit(value: Any, *, quiet: bool = False) -> None:
    if quiet:
        return
    # Keep the command's JSON byte stream portable even when a Windows console
    # is still using a legacy code page. JSON consumers recover the exact
    # Unicode text from escapes instead of the CLI failing after a safe write.
    print(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True, default=str))


def local_timezone_name() -> str:
    tzinfo = datetime.now().astimezone().tzinfo
    key = getattr(tzinfo, "key", None)
    return key or os.environ.get("TZ") or "UTC"


def configured_home(arguments: argparse.Namespace) -> Path | None:
    return Path(arguments.home).expanduser().resolve() if arguments.home else None


def open_config(arguments: argparse.Namespace):
    return load_config(configured_home(arguments))


def workbook_template() -> Path:
    return ASSETS_DIR / "health-ledger.xlsx"


def lock_for(config) -> FileLock:
    return FileLock(config.home_path / "locks" / "health-agent.lock", timeout_seconds=90)


def sync_lock_for(config) -> FileLock:
    """Serialize real upstream snapshots without blocking local ledger writes."""

    return FileLock(config.home_path / "locks" / "health-sync.lock", timeout_seconds=90)


def open_existing_database(config) -> HealthDatabase:
    """Open the canonical database only after proving it already exists.

    ``HealthDatabase`` intentionally creates a new database for initialization
    and unit-level use. Every operational CLI command goes through this guard so
    a missing SQLite truth can never be replaced by an empty database and then
    exported over an existing workbook.
    """

    if not config.database.is_file():
        raise CanonicalDatabaseMissingError(
            "the canonical SQLite database is missing; restore it from a trusted backup before recording, exporting, or building context"
        )
    try:
        return HealthDatabase(config.database, create=False)
    except (sqlite3.DatabaseError, OSError) as exc:
        raise CanonicalDatabaseMissingError(
            "the canonical SQLite database is invalid; restore it from a trusted backup before recording, exporting, or building context"
        ) from exc


def command_init(arguments: argparse.Namespace) -> dict[str, Any]:
    home = configured_home(arguments) or Path.home() / ".open-health-agent"
    if arguments.workbook and Path(arguments.workbook).suffix.lower() != ".xlsx":
        raise ValueError("workbook must use the .xlsx format")
    if arguments.timezone:
        ZoneInfo(arguments.timezone)
    if _sync_storage_provider(home) is not None:
        if not config_path(home).is_file():
            raise ValueError(
                "a new private home must use local storage; sync only the workbook or migrate an explicitly authorized legacy home"
            )
        existing_cloud_config = load_config(home)
        _require_private_home_storage_consent(existing_cloud_config)
    home.mkdir(parents=True, exist_ok=True)
    try:
        home.chmod(0o700)
    except OSError:
        pass
    init_lock = FileLock(home / "locks" / "health-agent.lock", timeout_seconds=90)
    with init_lock:
        existing_path = config_path(home)
        existing = os.path.lexists(existing_path)
        config_backup = None
        new_workbook_preexisting: bool | None = None
        initial_workbook = (
            Path(arguments.workbook).expanduser().resolve()
            if arguments.workbook
            else (home / "健康档案.xlsx").resolve()
        )
        managed_paths = (
            existing_path,
            home / "AGENTS.md",
            home / "profile.json",
            home / "state.json",
            home / "health.sqlite3",
            home / "health.sqlite3-wal",
            home / "health.sqlite3-shm",
        )
        existed_before = {
            path: os.path.lexists(path) for path in (*managed_paths, initial_workbook)
        }
        if not existing and any(existed_before[path] for path in managed_paths[1:]):
            raise InitializationConflictError(
                "managed private files exist without config.json; recover the missing configuration or migrate into a new empty private home"
            )
        if not existing and existed_before[initial_workbook]:
            raise InitializationConflictError(
                "the selected workbook already exists; preserve it and use the migration workflow or choose a new workbook path"
            )
        try:
            if existing:
                config = load_config(home)
                _require_private_home_storage_consent(config)
                # ``init --force`` may update configuration, but it is never a
                # recovery command. Refuse to manufacture a replacement truth
                # when an existing installation has lost its SQLite ledger.
                open_existing_database(config).close()
                if arguments.force:
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    config_backup = existing_path.with_name(
                        f"{existing_path.name}.backup-{stamp}"
                    )
                    shutil.copy2(existing_path, config_backup)
                    try:
                        config_backup.chmod(0o600)
                    except OSError:
                        pass
                    if arguments.workbook:
                        config.workbook_path = str(
                            Path(arguments.workbook).expanduser().resolve()
                        )
                    if arguments.timezone:
                        config.timezone = arguments.timezone
                    if arguments.ghealth_command:
                        config.ghealth_command = arguments.ghealth_command
                    save_config(config)
            else:
                workbook = (
                    Path(arguments.workbook).expanduser()
                    if arguments.workbook
                    else None
                )
                selected_timezone = arguments.timezone or local_timezone_name()
                ZoneInfo(selected_timezone)
                config = create_config(
                    home,
                    workbook=workbook,
                    timezone=selected_timezone,
                    ghealth_command=arguments.ghealth_command or "ghealth",
                )
                new_workbook_preexisting = config.workbook.exists()
                save_config(config)

            local_files = initialize_local_files(
                config,
                ASSETS_DIR / "AGENTS.md.template",
                ASSETS_DIR / "profile.example.json",
                force=False,
            )
            projection_requested = not (
                existing and not arguments.force and config.workbook.is_file()
            )
            # Reopen after local-file initialization so an existing ledger that
            # disappears or is replaced during init is never recreated empty.
            database_handle = (
                open_existing_database(config)
                if existing
                else HealthDatabase(config.database)
            )
            database_handle.close()
            workbook_exported = False
            workbook_export_status = "unchanged"
            exported = config.workbook
            if projection_requested and not _cloud_workbook_export_allowed(config):
                _begin_workbook_projection(
                    config,
                    operation_id="init:workbook",
                    blocked_by_consent=True,
                )
                workbook_export_status = "blocked_by_consent"
            elif projection_requested:
                _begin_workbook_projection(
                    config,
                    operation_id="init:workbook",
                )
                with open_existing_database(config) as database:
                    exported = export_workbook(
                        config, database, workbook_template()
                    )
                workbook_exported = True
                workbook_export_status = "succeeded"
                try:
                    _update_state(
                        config,
                        workbook_export_pending=False,
                        workbook_export_pending_since=None,
                        workbook_export_pending_operation=None,
                        workbook_export_blocked_by_consent=False,
                    )
                except (OSError, StateInvalidError):
                    # SQLite and the atomically replaced workbook already agree;
                    # retain a conservative pending journal if clearing it fails.
                    pass
        except Exception:
            if config_backup is not None:
                shutil.copy2(config_backup, existing_path)
                try:
                    existing_path.chmod(0o600)
                except OSError:
                    pass
            elif not existing:
                # A failed first initialization must remain retryable. Remove
                # only artifacts owned by this attempt; keep an already-existing
                # workbook or unrelated files in the chosen directory.
                for created in managed_paths:
                    if existed_before[created]:
                        continue
                    try:
                        created.unlink(missing_ok=True)
                    except OSError:
                        pass
                if (
                    new_workbook_preexisting is False
                    and not existed_before[initial_workbook]
                ):
                    try:
                        config.workbook.unlink(missing_ok=True)
                    except OSError:
                        pass
            raise
    result: dict[str, Any] = {
        "status": "ready",
        "existing_configuration_reused": existing and not arguments.force,
        "workbook_exported": workbook_exported,
        "workbook_export": workbook_export_status,
        "configuration_backed_up": config_backup is not None,
        "local_file_kinds": sorted(local_files),
        "next": [
            "Have the agent explain the Skill, privacy processors, and non-medical limits before consent.",
            "Run doctor, authenticate ghealth if desired, then run sync.",
            "Set user goals with goal set; exact wording is written to the private AGENTS.md.",
        ],
    }
    if arguments.verbose_paths:
        result.update(
            {
                "home": str(config.home_path),
                "workbook": str(exported),
                "database": str(config.database),
                "local_files": local_files,
                "configuration_backup": str(config_backup) if config_backup else None,
            }
        )
    if workbook_export_status == "blocked_by_consent":
        result["required_consent_scope"] = "cloud-workbook"
    return result


def date_range(
    config,
    from_date: str | None,
    to_date: str | None,
    lookback_days: int | None = None,
) -> tuple[str, str]:
    try:
        today = datetime.now(ZoneInfo(config.timezone)).date()
    except Exception:
        today = datetime.now(timezone.utc).date()
    end = date.fromisoformat(to_date) if to_date else today
    selected_lookback = lookback_days if lookback_days is not None else config.lookback_days
    if isinstance(selected_lookback, bool) or not 1 <= selected_lookback <= 91:
        raise ValueError("lookback-days must be between 1 and 91")
    start = date.fromisoformat(from_date) if from_date else end - timedelta(days=selected_lookback - 1)
    if start > end:
        raise ValueError("from-date cannot be after to-date")
    if (end - start).days > 90:
        raise ValueError("a single sync range cannot exceed 91 days")
    return start.isoformat(), end.isoformat()


def _update_state(config, **changes: Any) -> None:
    state_path = config.home_path / "state.json"
    state = _load_state(config)
    state.update(changes)
    atomic_write_json(state_path, state)


def _load_state(config) -> dict[str, Any]:
    path = config.home_path / "state.json"
    if not path.exists():
        raise StateInvalidError("private state JSON is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateInvalidError("private state JSON is unreadable") from exc
    if state_validation_error(value) is not None:
        raise StateInvalidError("private state JSON does not match the operational schema")
    return value


def _aware_state_timestamp(
    value: Any, *, reject_future: bool = True
) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    normalized = parsed.astimezone(timezone.utc)
    if reject_future and normalized > datetime.now(timezone.utc) + timedelta(seconds=60):
        return None
    return normalized


def _backup_database(config, database: HealthDatabase, label: str) -> Path | None:
    """Create an owner-only, bounded SQLite snapshot while the ledger lock is held."""

    if config.backup_retention == 0:
        return None
    safe_label = "".join(character for character in label if character.isalnum() or character in "-_")
    if not safe_label:
        safe_label = "snapshot"
    directory = config.home_path / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"health-ledger-db-{stamp}-{safe_label}-{uuid.uuid4().hex[:8]}.sqlite3"
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        with sqlite3.connect(temporary) as backup_connection:
            database.connection.backup(backup_connection)
            integrity = backup_connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError("SQLite backup integrity check failed")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    candidates = sorted(
        directory.glob("health-ledger-db-*.sqlite3"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for expired in candidates[config.backup_retention :]:
        expired.unlink(missing_ok=True)
    return destination


def _export_after_durable_write(
    config,
    database: HealthDatabase,
    *,
    success_status: str,
    pending_status: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Export the readable view while preserving truthful partial-success semantics.

    The caller must hold the shared ledger lock. A database mutation is already
    durable by the time this function runs, so an Excel failure is reported as
    export-pending instead of pretending the underlying write failed.
    """

    if not _cloud_workbook_export_allowed(config):
        try:
            _update_state(
                config,
                workbook_export_pending=True,
                workbook_export_pending_since=datetime.now(
                    timezone.utc
                ).isoformat(timespec="seconds"),
                workbook_export_blocked_by_consent=True,
            )
        except Exception:
            pass
        _prune_database_history_best_effort(config, database)
        return payload | {
            "status": pending_status,
            "database_write": "succeeded",
            "workbook_export": "blocked_by_consent",
            "required_consent_scope": "cloud-workbook",
        }

    try:
        export_workbook(config, database, workbook_template())
    except Exception as exc:
        try:
            _update_state(
                config,
                workbook_export_pending=True,
                workbook_export_pending_since=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                workbook_export_blocked_by_consent=False,
            )
        except Exception:
            pass
        _prune_database_history_best_effort(config, database)
        return payload | {
            "status": pending_status,
            "database_write": "succeeded",
            "workbook_export": "failed",
            "export_error": _public_exception_payload(exc),
        }

    try:
        _update_state(
            config,
            workbook_export_pending=False,
            workbook_export_pending_since=None,
            workbook_export_pending_operation=None,
            workbook_export_blocked_by_consent=False,
        )
    except Exception:
        pass
    _prune_database_history_best_effort(config, database)
    return payload | {
        "status": success_status,
        "database_write": "succeeded",
        "workbook_export": "succeeded",
    }


def _begin_workbook_projection(
    config, *, operation_id: str, blocked_by_consent: bool = False
) -> None:
    """Persist an outbox marker before a SQLite mutation can become durable."""

    _update_state(
        config,
        workbook_export_pending=True,
        workbook_export_pending_since=datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        workbook_export_pending_operation=operation_id,
        workbook_export_blocked_by_consent=blocked_by_consent,
    )


def _prune_database_history_best_effort(config, database: HealthDatabase) -> None:
    try:
        database.prune_history(
            audit_limit=config.audit_event_retention,
            sync_limit=config.sync_run_retention,
        )
    except sqlite3.Error:
        pass


def _cloud_workbook_export_allowed(config) -> bool:
    return bool(
        _sync_storage_provider(config.workbook) is None
        or _has_scoped_consent(config, "cloud-workbook")
    )


def _cloud_path_binding(path: Path, resource_type: str) -> dict[str, Any] | None:
    provider = _sync_storage_provider(path)
    if provider is None:
        return None
    material = json.dumps(
        {
            "resource_type": resource_type,
            "provider": provider,
            "resolved_path": str(path.expanduser().resolve()),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "version": 1,
        "resource_type": resource_type,
        "provider": provider,
        "target_fingerprint": hashlib.sha256(material).hexdigest(),
    }


def _private_home_binding(config) -> dict[str, Any] | None:
    bindings = [
        binding
        for binding in (
            _cloud_path_binding(config.home_path, "private-home"),
            _cloud_path_binding(config.database, "canonical-database"),
        )
        if binding is not None
    ]
    if not bindings:
        return None
    material = json.dumps(bindings, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "version": 1,
        "resource_type": "private-home",
        "provider": "+".join(sorted({str(item["provider"]) for item in bindings})),
        "target_fingerprint": hashlib.sha256(material).hexdigest(),
    }


def _resource_binding_for_scope(config, scope: str) -> dict[str, Any] | None:
    if scope == "cloud-workbook":
        return _cloud_path_binding(config.workbook, "workbook")
    if scope == "cloud-private-home":
        return _private_home_binding(config)
    return None


def _require_private_home_storage_consent(config) -> None:
    if _private_home_binding(config) is None:
        return
    if not _has_scoped_consent(config, "cloud-private-home"):
        raise ValueError(
            "cloud-private-home consent bound to this synchronized private home is required before health writes"
        )


def _cap_scheduler_logs(config, maximum_bytes: int = 2 * 1024 * 1024) -> None:
    for name in ("scheduler.out.log", "scheduler.err.log"):
        path = config.home_path / "logs" / name
        descriptor: int | None = None
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                continue
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                continue
            flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                continue
            if hasattr(os, "getuid") and opened.st_uid != os.getuid():
                continue
            if opened.st_size > maximum_bytes:
                os.ftruncate(descriptor, 0)
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                pass
        except FileNotFoundError:
            continue
        except OSError:
            continue
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _prune_raw_summaries(config) -> None:
    directory = config.home_path / "raw"
    try:
        candidates = sorted(
            directory.glob("sync_*.summary.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for path in candidates[config.raw_summary_retention :]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _public_exception_payload(exc: Exception) -> dict[str, str]:
    """Return an actionable error classification without exception-controlled text.

    Exception messages can contain a user's exact health wording, OAuth material,
    workbook names, or database paths.  This function deliberately uses messages
    only for classification and never copies them into CLI output.
    """

    exception_type = type(exc).__name__
    detail = str(exc).casefold()
    code = "internal_error"
    message = "The operation failed; run doctor and retry. Private error details were withheld."
    public_details: dict[str, str] = {}

    if isinstance(exc, StateInvalidError):
        code = "state_invalid"
        message = (
            "The private operational state is invalid. No consent or background authorization was inferred; "
            "restore state.json from a trusted backup or reinitialize it deliberately."
        )
    elif isinstance(exc, InitializationConflictError):
        code = "initialization_conflict"
        message = (
            "Existing managed private files or a workbook were found without a usable configuration. "
            "No replacement was created; recover the configuration or use a new empty destination and the migration checklist."
        )
    elif isinstance(exc, CanonicalDatabaseMissingError):
        code = "canonical_database_missing"
        message = (
            "The canonical SQLite health ledger is missing or invalid. No replacement was created; "
            "restore the database from a trusted backup before recording, exporting, or building context."
        )
    elif isinstance(exc, GoalProjectionError):
        code = "goal_projection_pending"
        message = (
            "The canonical SQLite goal is safe, but one or more private goal views are pending. "
            "Repair the private projection document if needed, then run goal repair."
        )
        public_details = {
            "database_write": "succeeded"
            if exc.database_state == "committed"
            else "unchanged",
            "goal_projection": "pending",
            "workbook_export": "pending",
        }
    elif isinstance(exc, ProfileProjectionError):
        code = "profile_projection_pending"
        message = (
            "The operational setting was saved, but its private profile view is pending. "
            "Retry the same profile update after repairing profile.json."
        )
        public_details = {
            "operational_config": "succeeded",
            "profile_projection": "pending",
        }
    elif isinstance(exc, CLIUsageError):
        code = "invalid_command"
        message = "Invalid command or option; run --help and check the command structure."
    elif isinstance(exc, GHealthError):
        if "timezone" in detail:
            code = "ghealth_timezone_mismatch"
            message = (
                "ghealth and Open Health Agent use different day boundaries; run doctor, set the active "
                "ghealth profile to the same IANA timezone, and retry."
            )
        elif "command not found" in detail:
            code = "ghealth_not_found"
            message = "ghealth was not found; run doctor and install or configure its executable."
        elif "authentication" in detail:
            code = "ghealth_authentication_required"
            message = "ghealth authentication is unavailable; run ghealth setup or ghealth auth login."
        elif "timed out" in detail:
            code = "ghealth_timeout"
            message = "ghealth timed out; retry after checking the local connection and authorization."
        elif "size limit" in detail:
            code = "ghealth_response_too_large"
            message = "ghealth returned too much data; retry with a shorter date range."
        elif "invalid json" in detail or "non-object json" in detail:
            code = "ghealth_invalid_response"
            message = "ghealth returned an invalid response; update ghealth and retry."
        else:
            code = "ghealth_error"
            message = "ghealth failed; run doctor, verify authorization, and retry."
    elif isinstance(exc, json.JSONDecodeError):
        code = "invalid_json"
        message = "Invalid JSON input or local configuration; correct the JSON and retry."
    elif isinstance(exc, sqlite3.Error):
        code = "database_error"
        message = "The private health database could not be used; run doctor before retrying."
    elif isinstance(exc, BlockingIOError):
        code = "operation_busy"
        message = "Another Open Health Agent operation is still running; retry shortly."
    elif isinstance(exc, PermissionError):
        code = "permission_denied"
        message = "Permission was denied; check access to the private data directory and workbook."
    elif isinstance(exc, FileNotFoundError):
        code = "missing_resource"
        message = "A required local file is missing; run init or doctor and retry."
    elif isinstance(exc, ZoneInfoNotFoundError):
        code = "invalid_timezone"
        message = "The timezone is invalid; use an IANA timezone name and retry."
    elif isinstance(exc, UnicodeError):
        code = "invalid_text_encoding"
        message = "Text encoding is invalid; use UTF-8 input and retry."
    elif exception_type == "IllegalCharacterError":
        code = "workbook_invalid_text"
        message = "The workbook contains text Excel cannot store; export-safe text cleanup is required."
    elif isinstance(exc, RuntimeError) and any(
        marker in detail
        for marker in (
            "stable authenticated account identity",
            "stable project identity",
            "bound to one configured project",
            "authentication identity",
        )
    ):
        code = "ghealth_profile_identity_unavailable"
        message = (
            "The selected ghealth profile cannot be bound to one stable authenticated account and project. "
            "Verify the profile locally before enabling background sync."
        )
    elif isinstance(exc, RuntimeError) and any(
        marker in detail
        for marker in (
            "differs from the last successful manual sync",
            "scheduler-pinned ghealth profile is no longer available",
        )
    ):
        code = "recent_manual_ghealth_sync_required"
        message = (
            "Run a real manual ghealth sync successfully, then retry scheduler installation within "
            "30 minutes. Fixture and scheduled runs do not qualify."
        )
    elif isinstance(exc, RuntimeError) and any(
        marker in detail
        for marker in ("launchctl", "systemctl", "scheduler definition", "keep-awake")
    ):
        code = "scheduler_operation_failed"
        message = (
            "The background-job change failed safely; its prior definition was kept or restored. "
            "Check the launchd/systemd user service, then retry the same install or uninstall command."
        )
    elif isinstance(exc, RuntimeError) and any(
        marker in detail
        for marker in (
            "incompatible headers",
            "unmanaged columns",
            "header validation failed",
            "non-health sheet changed unexpectedly",
            "temporary workbook is missing sheets",
        )
    ):
        code = "workbook_incompatible"
        message = "The workbook's managed sheets are incompatible; restore their standard columns or use a new workbook."
    elif isinstance(exc, ValueError):
        if "quiet sync without the managed scheduled marker" in detail:
            code = "legacy_scheduler_reinstall_required"
            message = (
                "A legacy background definition was rejected. Uninstall it, complete a real manual sync, "
                "and reinstall the scheduler with fresh consent."
            )
        elif "synthetic fixture sync" in detail:
            code = "fixture_sync_disabled"
            message = "Synthetic fixture ingestion is disabled in production."
        elif (
            "scheduled" in detail
            and ("runtime fingerprint" in detail or "profile pin" in detail)
        ) or "scheduler runtime fingerprint" in detail:
            code = "scheduler_runtime_mismatch"
            message = (
                "The pinned background runtime no longer matches. Complete a real manual sync and "
                "reinstall the scheduler with fresh consent."
            )
        elif "must be hh:mm" in detail:
            code = "invalid_time_format"
            message = "Time must use HH:MM, HH:MM:SS, or a full ISO-8601 datetime."
        elif "method and entry_method must match" in detail:
            code = "conflicting_entry_method"
            message = "Use method or entry_method; if both are present, they must match."
        elif "source_event_id conflicts" in detail:
            code = "conflicting_source_event_id"
            message = "The correction cannot be attached to a different source event."
        elif "source_event_item_id" in detail:
            code = "invalid_source_event_item_id"
            message = "source_event_item_id must be a short opaque per-item key used with source_event_id."
        elif "source_event_id" in detail:
            code = "invalid_source_event_id"
            message = "source_event_id must be an opaque host event identifier no longer than 512 characters."
        elif "blood pressure" in detail:
            code = "invalid_blood_pressure"
            message = "Blood pressure requires a valid systolic value and diastolic second_value in mmHg."
        elif "food records require consumed=true" in detail:
            code = "consumption_not_confirmed"
            message = "Food was not recorded because confirmed consumption requires consumed=true."
        elif "record-id conflicts" in detail:
            code = "conflicting_record_id"
            message = "The correction target conflicts with the JSON record_id."
        elif "lookback-days" in detail or "from-date" in detail:
            code = "invalid_sync_range"
            message = "The sync date range is invalid; use either from-date or lookback-days within the accepted range."
        elif "delivery-confirmed" in detail:
            code = "explanation_delivery_unconfirmed"
            message = "Confirm that the user-visible explanation was delivered before marking it explained."
        elif "cloud-private-home" in detail or "private home must use local storage" in detail:
            code = "cloud_private_home_consent_required"
            message = (
                "Health writes are paused because the private home or canonical database is synchronized. "
                "Migrate SQLite and private files to local storage, or explicitly authorize this exact legacy cloud target."
            )
        elif "consent scope" in detail:
            code = "consent_scope_required"
            message = "Record consent for one explicit provider or background-action scope."
        elif "scheduler consent" in detail:
            code = "scheduler_consent_required"
            message = "Record explicit scheduler consent before installing background sync."
        elif "successful real manual ghealth sync" in detail:
            code = "recent_manual_ghealth_sync_required"
            message = (
                "Run a real manual ghealth sync successfully, then retry scheduler installation within "
                "30 minutes. Fixture and scheduled runs do not qualify."
            )
        elif "keep-awake consent" in detail:
            code = "keep_awake_consent_required"
            message = "Record explicit keep-awake consent before installing the AC power helper."
        elif "google-health consent" in detail:
            code = "google_health_consent_required"
            message = "Record explicit Google Health consent before a real sync or scheduler install."
        elif "proxy" in detail:
            code = "invalid_scheduler_proxy"
            message = "The scheduler proxy configuration is missing or unsafe; use an owner-only file with a credential-free loopback URL."
        else:
            code = "invalid_input"
            message = "Input validation failed; check the command fields and accepted ranges."
        if str(exc) == "mark the explanation complete before recording consent":
            code = "invalid_input"
            message = "Mark the Skill explanation complete before recording consent."
    elif isinstance(exc, KeyError):
        code = "record_not_found"
        message = "The requested local record was not found; refresh identifiers and retry."
    elif isinstance(exc, OSError):
        code = "local_io_error"
        message = "A local file operation failed; check storage, permissions, and workbook availability."
    elif isinstance(exc, RuntimeError):
        code = "operation_failed"
        message = "The operation could not complete; run doctor and retry."

    return {
        "status": "error",
        "code": code,
        "type": exception_type,
        "error": message,
    } | public_details


def _safe_exception_summary(exc: Exception) -> str:
    payload = _public_exception_payload(exc)
    return f"code={payload['code']}; type={payload['type']}; private details withheld"


def _fixture_sync_test_mode_enabled() -> bool:
    return (
        os.environ.get("OPEN_HEALTH_AGENT_TEST_MODE") == "1"
        and bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _valid_runtime_fingerprint_input(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _update_sync_preflight_failure_locked(
    config,
    *,
    scheduled: bool,
    manual_evidence_started: bool,
    batch_id: str,
    exc: BaseException,
) -> None:
    failure_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if scheduled:
        _update_state(
            config,
            last_scheduled_sync_at=failure_at,
            last_scheduled_sync_status="failed_preflight",
            last_scheduled_sync_batch_id=batch_id,
            last_scheduled_sync_error_code=_public_exception_payload(exc)["code"],
        )
    elif manual_evidence_started:
        _update_state(
            config,
            last_manual_ghealth_sync={
                "version": 2,
                "batch_id": batch_id,
                "status": "failed_preflight",
                "finished_at": failure_at,
            },
        )


def _update_sync_postfetch_validation_failure_locked(
    config,
    *,
    scheduled: bool,
    manual_evidence_started: bool,
    batch_id: str,
    exc: BaseException,
) -> None:
    failure_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    validation_changes: dict[str, Any] = {
        "last_sync_at": failure_at,
        "last_sync_status": "failed_postfetch_validation",
        "last_sync_batch_id": batch_id,
    }
    if scheduled:
        validation_changes |= {
            "last_scheduled_sync_at": failure_at,
            "last_scheduled_sync_status": "failed_postfetch_validation",
            "last_scheduled_sync_batch_id": batch_id,
            "last_scheduled_sync_error_code": _public_exception_payload(exc)["code"],
        }
    elif manual_evidence_started:
        validation_changes["last_manual_ghealth_sync"] = {
            "version": 2,
            "batch_id": batch_id,
            "status": "failed_postfetch_validation",
            "finished_at": failure_at,
        }
    _update_state(config, **validation_changes)


def _sync_configuration_binding(config, start: str, end: str) -> str:
    """Bind fetched rows to the exact validated configuration and date range.

    The digest is process-local evidence used only across the unlocked external
    fetch window. It deliberately covers every persisted config field so even a
    seemingly unrelated administrative edit forces the caller to retry from a
    newly validated snapshot instead of committing rows under mixed settings.
    """

    payload = {
        "config": vars(config),
        "from_date": start,
        "to_date": end,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def command_sync(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    _require_private_home_storage_consent(config)
    if arguments.fixture_dir:
        return _command_sync_impl(arguments, config)
    with sync_lock_for(config):
        return _command_sync_impl(arguments, config)


def _command_sync_impl(arguments: argparse.Namespace, config) -> dict[str, Any]:
    lookback_days = getattr(arguments, "lookback_days", None)
    if lookback_days is not None and arguments.from_date:
        raise ValueError("use either from-date or lookback-days, not both")
    scheduled = bool(getattr(arguments, "scheduled", False))
    quiet = bool(getattr(arguments, "quiet", False))
    static_runtime_fingerprint_argument = getattr(
        arguments, "static_runtime_fingerprint", None
    )
    runtime_fingerprint_argument = getattr(arguments, "runtime_fingerprint", None)
    if arguments.fixture_dir and scheduled:
        raise ValueError("fixture sync cannot be marked as scheduled")
    if arguments.fixture_dir and not _fixture_sync_test_mode_enabled():
        raise ValueError("synthetic fixture sync is disabled outside repository tests")
    if quiet and not scheduled:
        rejected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rejected_batch = (
            "sync_legacy_scheduler_rejected_"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"
            f"{uuid.uuid4().hex[:8]}"
        )
        with lock_for(config):
            config = load_config(config.home_path)
            _require_private_home_storage_consent(config)
            _update_state(
                config,
                last_scheduled_sync_at=rejected_at,
                last_scheduled_sync_status="legacy_definition_rejected",
                last_scheduled_sync_batch_id=rejected_batch,
                last_scheduled_sync_error_code="legacy_scheduler_reinstall_required",
            )
        raise ValueError(
            "quiet sync without the managed scheduled marker is disabled; reinstall the scheduler"
        )
    if (static_runtime_fingerprint_argument or runtime_fingerprint_argument) and not scheduled:
        raise ValueError("a scheduler runtime fingerprint is valid only for a scheduled sync")
    run_kind = (
        f"{'fixture' if arguments.fixture_dir else 'ghealth'}_"
        f"{'scheduler' if scheduled else 'manual'}"
    )
    batch_id = (
        f"sync_{run_kind}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"
        f"{uuid.uuid4().hex[:8]}"
    )
    # Fixtures are repository-only synthetic inputs. Real imports must prove that
    # ghealth and OHA use the same local-day boundary before any sync row is opened.
    manual_static_runtime_fingerprint = None
    manual_runtime_fingerprint = None
    pinned_ghealth_profile: str | None = None
    manual_evidence_started = False
    sync_configuration_binding: str | None = None
    fetched_result: dict[str, Any] | None = None
    runner: FixtureRunner | CommandRunner | None = None
    adapter: GHealthAdapter | None = None
    resolved_ghealth: Path | None = None
    current_static_runtime: str | None = None
    with lock_for(config):
        # Another locked operation may have replaced configuration while this
        # command waited. Reload it before calculating day boundaries, choosing
        # a workbook, checking consent, or resolving any executable.
        config = load_config(config.home_path)
        try:
            _require_private_home_storage_consent(config)
            # Fail before any external query when the local source of truth is absent.
            open_existing_database(config).close()
            _cap_scheduler_logs(config)
            start, end = date_range(
                config,
                arguments.from_date,
                arguments.to_date,
                lookback_days,
            )
            sync_configuration_binding = _sync_configuration_binding(
                config, start, end
            )
            if arguments.fixture_dir:
                runner = FixtureRunner(Path(arguments.fixture_dir).resolve())
                adapter = GHealthAdapter(
                    runner, config.sleep_source_priority, config.timezone
                )
            else:
                # Consent is checked under the same lock used by revocation. A
                # waiting sync cannot use a stale pre-lock authorization result.
                if not _has_scoped_consent(config, "google-health"):
                    raise ValueError(
                        "google-health consent must be recorded before a real sync"
                    )
                if scheduled and not _has_scheduled_runtime_authorization(
                    config,
                    static_runtime_fingerprint_argument,
                    runtime_fingerprint_argument,
                ):
                    raise ValueError(
                        "scheduler consent must remain active and bound to a successful installed runtime"
                    )
                if not scheduled:
                    _update_state(
                        config,
                        last_manual_ghealth_sync={
                            "version": 2,
                            "batch_id": batch_id,
                            "status": "running",
                            "started_at": datetime.now(timezone.utc).isoformat(
                                timespec="seconds"
                            ),
                        },
                    )
                    manual_evidence_started = True
                resolved_ghealth = resolve_ghealth_executable(
                    config.ghealth_command
                )
                current_static_runtime = static_runtime_fingerprint(config)
                if scheduled:
                    profile = os.environ.get("GHEALTH_PROFILE")
                    if not isinstance(profile, str) or not profile:
                        raise ValueError(
                            "the scheduled ghealth profile pin is missing"
                        )
                    pinned_ghealth_profile = profile
                    if not _valid_runtime_fingerprint_input(
                        static_runtime_fingerprint_argument
                    ) or not _valid_runtime_fingerprint_input(
                        runtime_fingerprint_argument
                    ):
                        raise ValueError(
                            "the scheduled runtime fingerprint is missing or invalid"
                        )
                    # This first comparison only hashes local files/configuration and
                    # cannot execute a replaced ghealth binary.
                    if current_static_runtime != static_runtime_fingerprint_argument:
                        raise ValueError(
                            "the scheduled static runtime fingerprint no longer matches"
                        )
                else:
                    manual_static_runtime_fingerprint = current_static_runtime
        except Exception as exc:
            _update_sync_preflight_failure_locked(
                config,
                scheduled=scheduled,
                manual_evidence_started=manual_evidence_started,
                batch_id=batch_id,
                exc=exc,
            )
            raise

    if not arguments.fixture_dir:
        try:
            assert resolved_ghealth is not None
            if scheduled:
                assert pinned_ghealth_profile is not None
            else:
                pinned_ghealth_profile = resolve_active_ghealth_profile(config)
            # Freeze the selected profile in every foreground subprocess. A
            # concurrent `ghealth profile activate` can no longer mix accounts
            # or projects across queries in this batch.
            runner = CommandRunner(str(resolved_ghealth), profile=pinned_ghealth_profile)
            adapter = GHealthAdapter(
                runner, config.sleep_source_priority, config.timezone
            )
            if scheduled:
                current_runtime = ghealth_runtime_fingerprint(
                    config, pinned_ghealth_profile
                )
                if current_runtime != runtime_fingerprint_argument:
                    raise ValueError(
                        "the scheduled account-bound runtime fingerprint no longer matches"
                    )
            else:
                assert pinned_ghealth_profile is not None
                manual_runtime_fingerprint = ghealth_runtime_fingerprint(
                    config, pinned_ghealth_profile
                )
            adapter.require_matching_timezone()
        except Exception as exc:
            with lock_for(config):
                config = load_config(config.home_path)
                _update_sync_preflight_failure_locked(
                    config,
                    scheduled=scheduled,
                    manual_evidence_started=manual_evidence_started,
                    batch_id=batch_id,
                    exc=exc,
                )
            raise

        try:
            with lock_for(config):
                config = load_config(config.home_path)
                _require_private_home_storage_consent(config)
                open_existing_database(config).close()
                current_start, current_end = date_range(
                    config,
                    arguments.from_date,
                    arguments.to_date,
                    lookback_days,
                )
                current_configuration_binding = _sync_configuration_binding(
                    config, current_start, current_end
                )
                if (
                    (current_start, current_end) != (start, end)
                    or current_configuration_binding != sync_configuration_binding
                ):
                    raise RuntimeError(
                        "sync configuration or date range changed before the external fetch"
                    )
                if not _has_scoped_consent(config, "google-health"):
                    raise ValueError(
                        "google-health consent was withdrawn before the external fetch"
                    )
                if scheduled and not _has_scheduled_runtime_authorization(
                    config,
                    static_runtime_fingerprint_argument,
                    runtime_fingerprint_argument,
                ):
                    raise ValueError(
                        "scheduler consent or installed runtime authorization changed before the external fetch"
                    )
        except Exception as exc:
            with lock_for(config):
                config = load_config(config.home_path)
                _update_sync_preflight_failure_locked(
                    config,
                    scheduled=scheduled,
                    manual_evidence_started=manual_evidence_started,
                    batch_id=batch_id,
                    exc=exc,
                )
            raise

    # Network-bound ghealth reads must not monopolize the single ledger writer
    # lock. All authority and identity inputs were pinned above; fetched rows
    # remain process-local until the second locked validation phase accepts the
    # exact same authority/configuration snapshot.
    if not arguments.fixture_dir:
        try:
            fetched_result = adapter.fetch(start, end, batch_id)
        except Exception as exc:
            with lock_for(config):
                config = load_config(config.home_path)
                failure_at = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                fetch_changes: dict[str, Any] = {
                    "last_sync_at": failure_at,
                    "last_sync_status": "failed_fetch",
                    "last_sync_batch_id": batch_id,
                }
                if scheduled:
                    fetch_changes |= {
                        "last_scheduled_sync_at": failure_at,
                        "last_scheduled_sync_status": "failed_fetch",
                        "last_scheduled_sync_batch_id": batch_id,
                        "last_scheduled_sync_error_code": _public_exception_payload(
                            exc
                        )["code"],
                    }
                elif manual_evidence_started:
                    fetch_changes["last_manual_ghealth_sync"] = {
                        "version": 2,
                        "batch_id": batch_id,
                        "status": "failed_fetch",
                        "finished_at": failure_at,
                    }
                _update_state(config, **fetch_changes)
            raise

    if not arguments.fixture_dir:
        try:
            expected_static = (
                static_runtime_fingerprint_argument
                if scheduled
                else manual_static_runtime_fingerprint
            )
            expected_runtime = (
                runtime_fingerprint_argument
                if scheduled
                else manual_runtime_fingerprint
            )
            # Hash local runtime files before any profile/auth subprocess is
            # allowed to execute in the second phase.
            current_static = static_runtime_fingerprint(config)
            if current_static != expected_static:
                if scheduled:
                    raise ValueError(
                        "the scheduled static runtime fingerprint changed during sync"
                    )
                raise RuntimeError(
                    "the verified foreground runtime changed during sync"
                )
            assert pinned_ghealth_profile is not None
            if scheduled:
                if os.environ.get("GHEALTH_PROFILE") != pinned_ghealth_profile:
                    raise ValueError(
                        "the scheduled ghealth profile pin changed during sync"
                    )
            elif resolve_active_ghealth_profile(config) != pinned_ghealth_profile:
                raise RuntimeError(
                    "the verified foreground active profile changed during sync"
                )
            current_runtime = ghealth_runtime_fingerprint(
                config, pinned_ghealth_profile
            )
            if current_runtime != expected_runtime:
                if scheduled:
                    raise ValueError(
                        "the scheduled account-bound runtime fingerprint changed during sync"
                    )
                raise RuntimeError(
                    "the verified foreground profile identity changed during sync"
                )
            assert adapter is not None
            adapter.require_matching_timezone()
        except Exception as exc:
            with lock_for(config):
                config = load_config(config.home_path)
                _update_sync_postfetch_validation_failure_locked(
                    config,
                    scheduled=scheduled,
                    manual_evidence_started=manual_evidence_started,
                    batch_id=batch_id,
                    exc=exc,
                )
            raise

    with lock_for(config):
        # Phase two must reject every authority, identity, date, or config drift
        # before opening a sync run or applying a single fetched health row.
        try:
            config = load_config(config.home_path)
            _require_private_home_storage_consent(config)
            open_existing_database(config).close()
            current_start, current_end = date_range(
                config,
                arguments.from_date,
                arguments.to_date,
                lookback_days,
            )
            current_configuration_binding = _sync_configuration_binding(
                config, current_start, current_end
            )
            if (
                (current_start, current_end) != (start, end)
                or current_configuration_binding != sync_configuration_binding
            ):
                raise RuntimeError(
                    "sync configuration or date range changed during the external fetch"
                )
            if not arguments.fixture_dir:
                if not _has_scoped_consent(config, "google-health"):
                    raise ValueError(
                        "google-health consent was withdrawn during the external fetch"
                    )
                if scheduled and not _has_scheduled_runtime_authorization(
                    config,
                    static_runtime_fingerprint_argument,
                    runtime_fingerprint_argument,
                ):
                    raise ValueError(
                        "scheduler consent or installed runtime authorization changed during the external fetch"
                    )
                expected_static = (
                    static_runtime_fingerprint_argument
                    if scheduled
                    else manual_static_runtime_fingerprint
                )
                current_static = static_runtime_fingerprint(config)
                if current_static != expected_static:
                    if scheduled:
                        raise ValueError(
                            "the scheduled static runtime fingerprint changed during sync"
                        )
                    raise RuntimeError(
                        "the verified foreground runtime changed during sync"
                    )
                if scheduled:
                    if os.environ.get("GHEALTH_PROFILE") != pinned_ghealth_profile:
                        raise ValueError(
                            "the scheduled ghealth profile pin changed during sync"
                        )
        except Exception as exc:
            _update_sync_postfetch_validation_failure_locked(
                config,
                scheduled=scheduled,
                manual_evidence_started=manual_evidence_started,
                batch_id=batch_id,
                exc=exc,
            )
            raise

        with open_existing_database(config) as database:
            try:
                _backup_database(config, database, "before-sync")
                database.begin_sync(batch_id, start, end)
            except Exception as exc:
                failure_at = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                startup_changes: dict[str, Any] = {
                    "last_sync_at": failure_at,
                    "last_sync_status": "failed_startup",
                    "last_sync_batch_id": batch_id,
                }
                if scheduled:
                    startup_changes |= {
                        "last_scheduled_sync_at": failure_at,
                        "last_scheduled_sync_status": "failed_startup",
                        "last_scheduled_sync_batch_id": batch_id,
                        "last_scheduled_sync_error_code": _public_exception_payload(
                            exc
                        )["code"],
                    }
                elif manual_evidence_started:
                    startup_changes["last_manual_ghealth_sync"] = {
                        "version": 2,
                        "batch_id": batch_id,
                        "status": "failed_startup",
                        "finished_at": failure_at,
                    }
                try:
                    _update_state(config, **startup_changes)
                except (OSError, StateInvalidError):
                    pass
                raise
            try:
                workbook_export_status = "not_started"
                if arguments.fixture_dir:
                    result = adapter.fetch(start, end, batch_id)
                else:
                    assert fetched_result is not None
                    result = fetched_result
                errors = list(result.get("errors") or [])
                failed_query_keys = list(result.get("failed_query_keys") or [])
                pending_records: list[tuple[str, dict[str, Any], str]] = []
                refreshed_daily_ids: set[str] = set()
                for payload in result["daily"]:
                    # Retain only fields owned by the queries that actually
                    # failed. A successful-but-empty query is allowed to clear
                    # its old daily value on this recomputed composite row.
                    previous = database.get("daily", payload["record_id"])
                    selected_payload = merge_partial_daily(
                        payload, previous, failed_query_keys
                    )
                    pending_records.append(
                        (
                            "daily",
                            selected_payload,
                            str(selected_payload["record_id"]),
                        )
                    )
                    refreshed_daily_ids.add(str(selected_payload["record_id"]))
                failed_daily_fields = {
                    field
                    for query_key in failed_query_keys
                    for field in DAILY_FIELDS_BY_QUERY.get(query_key, ())
                }
                if failed_daily_fields:
                    # A range can contain an existing day whose only source was
                    # the query that failed, so normalization emits no current
                    # composite row. Keep that row and explicitly mark the
                    # affected values stale instead of silently leaving an
                    # apparently fresh prior quality label.
                    for previous in database.list_records("daily", start, end):
                        record_id = str(previous.get("record_id") or "")
                        if not record_id or record_id in refreshed_daily_ids:
                            continue
                        if not any(
                            previous.get(field) not in (None, "")
                            for field in failed_daily_fields
                        ):
                            continue
                        selected_payload = merge_partial_daily(
                            previous
                            | {
                                "batch_id": batch_id,
                                "imported_at": datetime.now(timezone.utc).isoformat(
                                    timespec="seconds"
                                ),
                            },
                            previous,
                            failed_query_keys,
                        )
                        pending_records.append(
                            (
                                "daily",
                                selected_payload,
                                str(selected_payload["record_id"]),
                            )
                        )
                for payload in result["measurements"]:
                    pending_records.append(
                        ("measurement", payload, str(payload["record_id"]))
                    )
                for payload in result["workouts"]:
                    pending_records.append(
                        ("workout", payload, str(payload["record_id"]))
                    )
                counts = {
                    "daily": len(result["daily"]),
                    "measurements": len(result["measurements"]),
                    "workouts": len(result["workouts"]),
                }
                total = sum(counts.values())
                if errors and total:
                    status = "partial"
                elif errors:
                    status = "failed"
                elif total == 0:
                    status = "empty"
                    errors = ["ghealth returned no usable records for the selected range"]
                else:
                    status = "success"
                export_allowed = _cloud_workbook_export_allowed(config)
                _begin_workbook_projection(
                    config,
                    operation_id=batch_id,
                    blocked_by_consent=not export_allowed,
                )
                database.apply_sync_result(
                    batch_id,
                    pending_records,
                    status=status,
                    daily_count=counts["daily"],
                    workout_count=counts["workouts"],
                    measurement_count=counts["measurements"],
                    data_until=result.get("data_until"),
                    errors=errors,
                )
                raw_summary = {
                    "batch_id": batch_id,
                    "source_kind": "fixture" if arguments.fixture_dir else "ghealth",
                    "trigger": "scheduler" if scheduled else "manual",
                    "from_date": start,
                    "to_date": end,
                    "status": status,
                    "counts": counts,
                    "data_until": result.get("data_until"),
                    "errors": errors,
                }
                summary_path = config.home_path / "raw" / f"{batch_id}.summary.json"
                workbook: Path | None = None
                workbook_export_status = "blocked_by_consent"
                if export_allowed:
                    try:
                        workbook = export_workbook(
                            config, database, workbook_template()
                        )
                        workbook_export_status = "succeeded"
                    except Exception as exc:
                        workbook_export_status = "failed"
                        export_errors = [
                            *errors,
                            f"workbook export: {_safe_exception_summary(exc)}",
                        ]
                        database.finish_sync(
                            batch_id,
                            "failed_export",
                            counts["daily"],
                            counts["workouts"],
                            counts["measurements"],
                            result.get("data_until"),
                            export_errors,
                        )
                        failed_summary = raw_summary | {
                            "status": "failed_export",
                            "workbook_export": workbook_export_status,
                            "errors": export_errors,
                        }
                        try:
                            atomic_write_json(summary_path, failed_summary)
                        except OSError:
                            # Never leave a success/partial summary behind when the
                            # readable export did not complete. The database sync
                            # run remains the durable source of failure details.
                            try:
                                summary_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                        _prune_raw_summaries(config)
                        try:
                            _update_state(
                                config,
                                workbook_export_pending=True,
                                workbook_export_pending_since=datetime.now(
                                    timezone.utc
                                ).isoformat(timespec="seconds"),
                                workbook_export_blocked_by_consent=False,
                            )
                        except OSError:
                            pass
                        raise
                raw_summary["workbook_export"] = workbook_export_status
                # Raw summaries are bounded private diagnostics, not a source
                # of truth. Write them only after the database and readable
                # workbook agree, and never turn a successful durable sync into
                # a failure solely because this optional diagnostic is unwritable.
                try:
                    atomic_write_json(summary_path, raw_summary)
                except OSError:
                    pass
                _prune_raw_summaries(config)
                try:
                    database.prune_history(
                        audit_limit=config.audit_event_retention,
                        sync_limit=config.sync_run_retention,
                    )
                except sqlite3.Error:
                    # Retention is maintenance after the canonical rows and
                    # workbook projection are durable. Never rewrite a real
                    # success as a failed import solely because pruning waits.
                    raw_summary["maintenance"] = "history_prune_pending"
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                persisted_run = database.get_sync_run(batch_id)
                persisted_finished_at = (
                    persisted_run.get("finished_at") if persisted_run else None
                )
                projected = workbook_export_status == "succeeded"
                reported_status = (
                    status
                    if projected
                    else f"{status}_export_blocked_by_consent"
                )
                changes: dict[str, Any] = {
                    "last_sync_at": now,
                    "last_sync_status": reported_status,
                    "last_sync_batch_id": batch_id,
                    "workbook_export_pending": not projected,
                    "workbook_export_pending_since": None if projected else now,
                    "workbook_export_pending_operation": None
                    if projected
                    else batch_id,
                    "workbook_export_blocked_by_consent": not projected,
                }
                if scheduled:
                    changes |= {
                        "last_scheduled_sync_at": now,
                        "last_scheduled_sync_status": reported_status,
                        "last_scheduled_sync_batch_id": batch_id,
                        "last_scheduled_sync_error_code": (
                            None
                            if status == "success" and projected
                            else (
                                "cloud_workbook_consent_required"
                                if not projected
                                else f"sync_{status}"
                            )
                        ),
                    }
                if status == "success" and projected:
                    changes["last_successful_sync_at"] = now
                    if scheduled:
                        changes["last_successful_scheduled_sync_at"] = now
                    if manual_runtime_fingerprint is not None:
                        changes["last_manual_ghealth_sync"] = {
                            "version": 2,
                            "batch_id": batch_id,
                            "status": "success",
                            "finished_at": persisted_finished_at,
                            "timezone": config.timezone,
                            "static_runtime_fingerprint": manual_static_runtime_fingerprint,
                            "runtime_fingerprint": manual_runtime_fingerprint,
                        }
                elif manual_runtime_fingerprint is not None:
                    changes["last_manual_ghealth_sync"] = {
                        "version": 2,
                        "batch_id": batch_id,
                        "status": reported_status,
                        "finished_at": persisted_finished_at,
                        "timezone": config.timezone,
                        "static_runtime_fingerprint": manual_static_runtime_fingerprint,
                        "runtime_fingerprint": manual_runtime_fingerprint,
                    }
                try:
                    _update_state(config, **changes)
                except (OSError, StateInvalidError):
                    raw_summary["state_projection"] = "pending"
                    try:
                        atomic_write_json(summary_path, raw_summary)
                    except OSError:
                        pass
            except Exception as exc:
                current = database.get_sync_run(batch_id)
                final_failure_status = "failed"
                if current and current["status"] == "running":
                    database.finish_sync(
                        batch_id,
                        "failed",
                        0,
                        0,
                        0,
                        None,
                        [_safe_exception_summary(exc)],
                    )
                elif current and current.get("status") == "failed_export":
                    final_failure_status = "failed_export"
                elif current and workbook_export_status == "succeeded":
                    final_failure_status = str(current.get("status") or "success")
                elif current:
                    final_failure_status = (
                        f"{current.get('status') or 'sync'}_export_pending"
                    )
                failure_changes: dict[str, Any] = {
                    "last_sync_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "last_sync_status": final_failure_status,
                    "last_sync_batch_id": batch_id,
                }
                if scheduled:
                    failure_changes |= {
                        "last_scheduled_sync_at": failure_changes["last_sync_at"],
                        "last_scheduled_sync_status": final_failure_status,
                        "last_scheduled_sync_batch_id": batch_id,
                        "last_scheduled_sync_error_code": _public_exception_payload(
                            exc
                        )["code"],
                    }
                if manual_runtime_fingerprint is not None:
                    failure_changes["last_manual_ghealth_sync"] = {
                        "version": 2,
                        "batch_id": batch_id,
                        "status": final_failure_status,
                        "finished_at": failure_changes["last_sync_at"],
                        "timezone": config.timezone,
                        "static_runtime_fingerprint": manual_static_runtime_fingerprint,
                        "runtime_fingerprint": manual_runtime_fingerprint,
                    }
                _update_state(config, **failure_changes)
                raise
    return raw_summary | (
        {"workbook": str(workbook)}
        if getattr(arguments, "verbose_path", False) and workbook is not None
        else {}
    )


def _load_json_argument(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.json_payload:
        raw = arguments.json_payload
    elif arguments.file:
        raw = Path(arguments.file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        raise ValueError("provide --json, --file, or JSON on stdin")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON payload must be an object")
    return value


def command_record(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    _require_private_home_storage_consent(config)
    supplied_payload = _load_json_argument(arguments)
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        with open_existing_database(config) as database:
            if arguments.record_id:
                target = database.get(arguments.kind, arguments.record_id)
                if not target:
                    raise KeyError(arguments.record_id)
                supplied_id = supplied_payload.get("record_id")
                if supplied_id and supplied_id != arguments.record_id:
                    raise ValueError("record-id conflicts with the JSON payload")
                supplied_event_id = supplied_payload.get("source_event_id")
                if (
                    "source_event_id" in supplied_payload
                    and target.get("source_event_id")
                    and supplied_event_id != target.get("source_event_id")
                ):
                    raise ValueError("source_event_id conflicts with the correction target")
                supplied_item_id = supplied_payload.get("source_event_item_id")
                if (
                    "source_event_item_id" in supplied_payload
                    and target.get("source_event_item_id")
                    and supplied_item_id != target.get("source_event_item_id")
                ):
                    raise ValueError(
                        "source_event_item_id conflicts with the correction target"
                    )
                payload = target | supplied_payload | {"record_id": arguments.record_id}
            else:
                payload = supplied_payload
            normalized = normalize_record(arguments.kind, payload)
            existing = database.get(arguments.kind, normalized["record_id"])
            if existing:
                timestamp_field = (
                    "recorded_at" if arguments.kind == "food" else "imported_at"
                )
                if timestamp_field not in supplied_payload and existing.get(timestamp_field):
                    normalized[timestamp_field] = existing[timestamp_field]
            changes_database = database.upsert_would_change(
                arguments.kind,
                normalized,
                normalized["record_id"],
            )
            if changes_database:
                _begin_workbook_projection(
                    config,
                    operation_id=f"record:{arguments.kind}:{normalized['record_id']}",
                    blocked_by_consent=not _cloud_workbook_export_allowed(config),
                )
                _backup_database(config, database, "before-record")
            database.upsert(arguments.kind, normalized, normalized["record_id"])
            return _export_after_durable_write(
                config,
                database,
                success_status="recorded",
                pending_status="recorded_export_pending",
                payload={
                    "kind": arguments.kind,
                    "record": {"record_id": normalized["record_id"]},
                },
            )


def command_delete(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    _require_private_home_storage_consent(config)
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        with open_existing_database(config) as database:
            if database.get(arguments.kind, arguments.record_id):
                _begin_workbook_projection(
                    config,
                    operation_id=f"delete:{arguments.kind}:{arguments.record_id}",
                    blocked_by_consent=not _cloud_workbook_export_allowed(config),
                )
                _backup_database(config, database, "before-delete")
            deleted = database.delete(arguments.kind, arguments.record_id, arguments.reason)
            if not deleted:
                return {
                    "status": "not_found",
                    "kind": arguments.kind,
                    "record_id": arguments.record_id,
                }
            return _export_after_durable_write(
                config,
                database,
                success_status="deleted",
                pending_status="deleted_export_pending",
                payload={"kind": arguments.kind, "record_id": arguments.record_id},
            )


def command_goal_set(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    _require_private_home_storage_consent(config)
    if arguments.text is not None:
        text = arguments.text
    elif arguments.file:
        text = Path(arguments.file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        raise ValueError("provide goal text on stdin, with --file, or with --text")
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        normalized = normalize_goal(
            config,
            text,
            arguments.effective_date,
            arguments.priority,
            arguments.safety_constraint,
        )
        with open_existing_database(config) as database:
            existing = database.get("goal", normalized["record_id"])
            if existing and existing.get("recorded_at"):
                normalized["recorded_at"] = existing["recorded_at"]
            changes_database = database.upsert_would_change(
                "goal", normalized, normalized["record_id"]
            )
            if changes_database:
                _begin_workbook_projection(
                    config,
                    operation_id=f"goal:set:{normalized['record_id']}",
                    blocked_by_consent=not _cloud_workbook_export_allowed(config),
                )
                _backup_database(config, database, "before-goal")
            views_current = goal_views_status(config, database)["ok"]
            if not changes_database and views_current:
                goal = existing
            else:
                if not changes_database:
                    _begin_workbook_projection(
                        config,
                        operation_id=f"goal:repair:{normalized['record_id']}",
                        blocked_by_consent=not _cloud_workbook_export_allowed(config),
                    )
                goal = set_goal(
                    config,
                    database,
                    text,
                    arguments.effective_date,
                    arguments.priority,
                    arguments.safety_constraint,
                    normalized_goal=normalized,
                )
            return _export_after_durable_write(
                config,
                database,
                success_status="goal_saved",
                pending_status="goal_saved_export_pending",
                payload={
                    "goal": {
                        "record_id": goal["record_id"],
                        "status": goal["status"],
                    },
                },
            )


def command_goal_retire(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    _require_private_home_storage_consent(config)
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        with open_existing_database(config) as database:
            existing = database.get("goal", arguments.record_id)
            if existing is None:
                raise KeyError(arguments.record_id)
            if str(existing.get("status") or "").startswith("retired"):
                return _export_after_durable_write(
                    config,
                    database,
                    success_status="goal_retired",
                    pending_status="goal_retired_export_pending",
                    payload={
                        "goal": {
                            "record_id": existing["record_id"],
                            "status": existing["status"],
                        }
                    },
                )
            _backup_database(config, database, "before-goal-retire")
            _begin_workbook_projection(
                config,
                operation_id=f"goal:retire:{arguments.record_id}",
                blocked_by_consent=not _cloud_workbook_export_allowed(config),
            )
            goal = retire_goal(config, database, arguments.record_id, arguments.reason)
            return _export_after_durable_write(
                config,
                database,
                success_status="goal_retired",
                pending_status="goal_retired_export_pending",
                payload={
                    "goal": {
                        "record_id": goal["record_id"],
                        "status": goal["status"],
                    }
                },
            )


def command_goal_repair(arguments: argparse.Namespace) -> dict[str, Any]:
    """Rebuild private goal projections from the canonical SQLite records."""

    config = open_config(arguments)
    _require_private_home_storage_consent(config)
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        with open_existing_database(config) as database:
            _begin_workbook_projection(
                config,
                operation_id="goal:repair",
                blocked_by_consent=not _cloud_workbook_export_allowed(config),
            )
            projection = rebuild_goal_views(config, database)
            return _export_after_durable_write(
                config,
                database,
                success_status="goal_views_rebuilt",
                pending_status="goal_views_rebuilt_export_pending",
                payload={"projection": projection},
            )


def command_profile_set(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    _require_private_home_storage_consent(config)
    if arguments.value is not None:
        raw_value = arguments.value
    elif arguments.file:
        raw_value = Path(arguments.file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        raw_value = sys.stdin.read()
    else:
        raise ValueError("provide a profile value on stdin, with --file, or with --value")
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        value = raw_value
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        value = validate_profile_value(arguments.key, value)
        operational_projection = arguments.key in {
            "timezone",
            "activity_energy_semantics",
        }
        if operational_projection:
            _update_state(
                config,
                profile_projection_pending=True,
                profile_projection_pending_field=arguments.key,
                profile_projection_pending_since=datetime.now(
                    timezone.utc
                ).isoformat(timespec="seconds"),
            )
        set_profile_value(config, arguments.key, value, arguments.source)
        if operational_projection:
            _update_state(
                config,
                profile_projection_pending=False,
                profile_projection_pending_field=None,
                profile_projection_pending_since=None,
            )
    return {"status": "profile_updated", "field": arguments.key}


def command_context(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    with lock_for(config):
        config = load_config(config.home_path)
        with open_existing_database(config) as database:
            return build_context(config, database, arguments.date)


def command_export(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    _require_private_home_storage_consent(config)
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        if not _cloud_workbook_export_allowed(config):
            _update_state(
                config,
                workbook_export_pending=True,
                workbook_export_pending_since=datetime.now(
                    timezone.utc
                ).isoformat(timespec="seconds"),
                workbook_export_blocked_by_consent=True,
            )
            return {
                "status": "export_blocked_by_consent",
                "workbook_export": "blocked_by_consent",
                "required_consent_scope": "cloud-workbook",
            }
        with open_existing_database(config) as database:
            workbook = export_workbook(config, database, workbook_template())
        try:
            _update_state(
                config,
                workbook_export_pending=False,
                workbook_export_pending_since=None,
                workbook_export_pending_operation=None,
                workbook_export_blocked_by_consent=False,
            )
        except Exception:
            pass
    return {
        "status": "exported",
        **({"workbook": str(workbook)} if arguments.verbose_path else {}),
    }


def command_backup(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    _require_private_home_storage_consent(config)
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        with open_existing_database(config) as database:
            backup = _backup_database(config, database, "manual")
    return {
        "status": "backup_created" if backup is not None else "backup_disabled",
        "backup_created": backup is not None,
        "retention": config.backup_retention,
        **({"path": str(backup)} if arguments.verbose_path and backup is not None else {}),
    }


def _profile_schema_status(path: Path) -> dict[str, Any]:
    """Validate profile structure without returning any private values."""

    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": "unreadable", "error_type": type(exc).__name__}
    if not isinstance(profile, dict):
        return {"ok": False, "reason": "not_an_object", "invalid_fields": []}

    invalid = profile_invalid_fields(profile)
    return {
        "ok": not invalid,
        "reason": "valid" if not invalid else "invalid_fields",
        "invalid_fields": sorted(invalid),
    }


def _sync_storage_provider(path: Path) -> str | None:
    candidates = {
        str(path.expanduser()).casefold(),
        str(path.expanduser().resolve()).casefold(),
    }
    markers = (
        ("icloud", ("library/mobile documents", "com~apple~clouddocs")),
        ("OneDrive", ("/onedrive", "\\onedrive")),
        ("Dropbox", ("/dropbox", "\\dropbox")),
        (
            "Google Drive",
            (
                "/google drive",
                "\\google drive",
                "/my drive",
                "/googledrive-",
                "\\googledrive-",
            ),
        ),
        (
            "Box",
            ("/box/", "\\box\\", "/box sync", "/box-", "\\box-"),
        ),
    )
    for provider, fragments in markers:
        if any(fragment in candidate for candidate in candidates for fragment in fragments):
            return provider
    return None


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _host_skill_prompt_checks(verbose_paths: bool) -> list[dict[str, Any]]:
    runtime_prompt = SKILL_DIR / "SKILL.md"
    expected = _file_sha256(runtime_prompt)
    home = Path.home()
    targets = (
        (
            "Hermes",
            Path(os.environ.get("HERMES_HOME", home / ".hermes"))
            / "skills"
            / "open-health-agent"
            / "SKILL.md",
        ),
        (
            "Codex",
            Path(os.environ.get("CODEX_HOME", home / ".codex"))
            / "skills"
            / "open-health-agent"
            / "SKILL.md",
        ),
    )
    checks: list[dict[str, Any]] = []
    for host, prompt in targets:
        discoverable_backups: list[Path] = []
        for candidate in prompt.parent.parent.rglob("SKILL.md"):
            if candidate == prompt or not candidate.is_file():
                continue
            try:
                prefix = candidate.read_text(encoding="utf-8")[:4096].casefold()
            except (OSError, UnicodeError):
                continue
            if "name: open-health-agent" in prefix:
                discoverable_backups.append(candidate)
        discoverable_backups.sort()
        duplicate_item: dict[str, Any] = {
            "check": f"{host} has one discoverable Open Health Agent Skill",
            "ok": not discoverable_backups,
            "required": False,
            "detail": {
                "host": host,
                "discoverable_backup_count": len(discoverable_backups),
            },
            "action": None
            if not discoverable_backups
            else "Re-run the repository installer for this host; it will quarantine adjacent legacy Skill backups.",
        }
        if verbose_paths and discoverable_backups:
            duplicate_item["paths"] = [str(path) for path in discoverable_backups]
        checks.append(duplicate_item)
        if not prompt.is_file():
            continue
        actual = _file_sha256(prompt)
        item: dict[str, Any] = {
            "check": f"{host} Skill prompt matches runtime",
            "ok": bool(expected and actual and expected == actual),
            "required": False,
            "detail": {
                "host": host,
                "matches_runtime": bool(expected and actual and expected == actual),
            },
            "action": None
            if expected and actual and expected == actual
            else "Re-run the repository installer for this host with --force after reviewing local Skill changes.",
        }
        if verbose_paths:
            item["path"] = str(prompt)
        checks.append(item)
    return checks


def _without_private_paths(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: nested
        for key, nested in value.items()
        if key not in {"path", "service", "timer", "home", "workbook", "database"}
    }


def command_doctor(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    verbose_paths = bool(getattr(arguments, "verbose_paths", False))
    requested_online = getattr(arguments, "online", None)
    online = (
        bool(requested_online)
        if requested_online is not None
        else not bool(getattr(arguments, "offline", False))
    )
    checks: list[dict[str, Any]] = []
    schedule = scheduler_status(config)
    ghealth_required = bool(
        schedule.get("installed")
        or schedule.get("definition_exists")
        or schedule.get("loaded")
        or schedule.get("active")
        or schedule.get("enabled")
    )
    for label, path in (
        ("private AGENTS.md", config.home_path / "AGENTS.md"),
        ("profile", config.home_path / "profile.json"),
        ("database", config.database),
        ("workbook", config.workbook),
    ):
        item: dict[str, Any] = {"check": label, "ok": path.exists()}
        if verbose_paths:
            item["path"] = str(path)
        checks.append(item)
    if os.name != "nt":
        try:
            home_mode = config.home_path.stat().st_mode & 0o777
            checks.append(
                {
                    "check": "private home permissions",
                    "ok": home_mode & 0o077 == 0,
                    "detail": oct(home_mode),
                }
            )
        except OSError as exc:
            checks.append(
                {
                    "check": "private home permissions",
                    "ok": False,
                    "detail": _safe_exception_summary(exc),
                }
            )
    try:
        command = str(resolve_ghealth_executable(config.ghealth_command))
    except FileNotFoundError:
        command = None
    ghealth_check: dict[str, Any] = {
        "check": "ghealth command",
        "ok": bool(command and Path(command).exists()),
        "required": ghealth_required,
        "detail": "optional unless an hourly wearable-sync definition exists",
    }
    if verbose_paths:
        ghealth_check["path"] = command
    checks.append(ghealth_check)
    home_provider = _sync_storage_provider(config.home_path)
    database_provider = _sync_storage_provider(config.database)
    workbook_provider = _sync_storage_provider(config.workbook)
    checks.append(
        {
            "check": "storage topology",
            "ok": database_provider is None,
            "required": False,
            "detail": {
                "canonical_database_local_only": database_provider is None,
                "canonical_database_sync_provider": database_provider,
                "private_home_sync_provider": home_provider,
                "workbook_sync_provider": workbook_provider,
            },
            "action": None
            if database_provider is None
            else (
                "Keep the live SQLite database and lock files on local storage; sync only the Excel view, "
                "or create a consistent backup while the shared writer lock is held."
            ),
        }
    )
    checks.extend(_host_skill_prompt_checks(verbose_paths))
    profile_status = _profile_schema_status(config.home_path / "profile.json")
    checks.append(
        {"check": "profile JSON schema", "ok": profile_status["ok"], "detail": profile_status}
    )
    workbook_status = inspect_workbook_schema(config.workbook)
    checks.append(
        {"check": "workbook managed schema", "ok": workbook_status["ok"], "detail": workbook_status}
    )
    if config.database.exists():
        try:
            with HealthDatabase(config.database, create=False, read_only=True) as database:
                integrity = database.connection.execute("PRAGMA quick_check").fetchone()[0]
                projections = goal_views_status(config, database)
            checks.append({"check": "SQLite integrity", "ok": integrity == "ok", "detail": integrity})
            checks.append(
                {
                    "check": "goal projections",
                    "ok": projections["ok"],
                    "detail": projections,
                    "action": None
                    if projections["ok"]
                    else (
                        "Run open-health-agent goal repair to rebuild profile and AGENTS goal views from SQLite."
                        if projections["agents_markers_valid"]
                        else "Restore the standard OPEN_HEALTH_AGENT_GOALS marker block from a private backup or template, then run goal repair."
                    ),
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "check": "SQLite integrity",
                    "ok": False,
                    "detail": _safe_exception_summary(exc),
                }
            )
            checks.append(
                {
                    "check": "goal projections",
                    "ok": False,
                    "detail": _safe_exception_summary(exc),
                }
            )
    else:
        checks.append(
            {
                "check": "goal projections",
                "ok": False,
                "detail": "canonical database is missing",
            }
        )
    if command and Path(command).exists():
        adapter = GHealthAdapter(
            CommandRunner(str(command), timeout=15),
            config.sleep_source_priority,
            config.timezone,
        )
        try:
            timezone_status = adapter.timezone_status()
            checks.append(
                {
                    "check": "ghealth timezone",
                    "ok": bool(timezone_status["matches"]),
                    "required": ghealth_required,
                    "detail": timezone_status,
                    "action": None
                    if timezone_status["matches"]
                    else f"Run ghealth config set timezone {config.timezone} for the active profile.",
                }
            )
        except GHealthError as exc:
            detail = _public_exception_payload(exc)
            detail.pop("status", None)
            checks.append(
                {
                    "check": "ghealth timezone",
                    "ok": False,
                    "required": ghealth_required,
                    "detail": detail,
                    "action": f"Run ghealth config set timezone {config.timezone} for the active profile, then rerun doctor.",
                }
            )
    if online and command and Path(command).exists():
        try:
            status = adapter.auth_status()
            checks.append(
                {
                    "check": "ghealth authentication",
                    "ok": status.get("authenticated") is True,
                    "required": ghealth_required,
                    "detail": status,
                }
            )
        except GHealthError as exc:
            detail = _public_exception_payload(exc)
            detail.pop("status", None)
            checks.append(
                {
                    "check": "ghealth authentication",
                    "ok": False,
                    "required": ghealth_required,
                    "detail": detail,
                }
            )
    try:
        state = _load_state(config)
        state_valid = True
    except StateInvalidError:
        state = {}
        state_valid = False
    checks.append(
        {
            "check": "state JSON schema",
            "ok": state_valid,
            "detail": "valid object" if state_valid else "invalid or unreadable",
            "action": None
            if state_valid
            else "Restore state.json from a trusted backup; consent and scheduler authorization remain disabled.",
        }
    )
    profile_projection_pending = state.get("profile_projection_pending") is True
    checks.append(
        {
            "check": "profile projection current",
            "ok": not profile_projection_pending,
            "detail": {
                "pending": profile_projection_pending,
                "field": state.get("profile_projection_pending_field")
                if profile_projection_pending
                else None,
            },
            "action": "Retry the same profile update after repairing profile.json."
            if profile_projection_pending
            else None,
        }
    )
    for scope, prefix, label in (
        ("scheduler", "scheduler_job_removal", "scheduler job removal complete"),
        ("keep-awake", "keep_awake_job_removal", "keep-awake job removal complete"),
    ):
        removal_pending = state.get(f"{prefix}_pending") is True
        checks.append(
            {
                "check": label,
                "ok": not removal_pending,
                "detail": {
                    "scope": scope,
                    "pending": removal_pending,
                    "last_attempt_at": state.get(f"{prefix}_last_attempt_at")
                    if removal_pending
                    else None,
                    "error_code": state.get(f"{prefix}_error_code")
                    if removal_pending
                    else None,
                },
                "action": (
                    f"Retry open-health-agent {'scheduler' if scope == 'scheduler' else 'keep-awake-on-ac'} uninstall."
                    if removal_pending
                    else None
                ),
            }
        )
    schedule["last_scheduled_sync_status"] = state.get(
        "last_scheduled_sync_status"
    )
    schedule["last_scheduled_sync_at"] = state.get("last_scheduled_sync_at")
    schedule["last_successful_scheduled_sync_at"] = state.get(
        "last_successful_scheduled_sync_at"
    )
    schedule["last_scheduled_sync_error_code"] = state.get(
        "last_scheduled_sync_error_code"
    )
    checks.append(
        {
            "check": "hourly scheduler",
            "ok": schedule.get("installed", False),
            "required": False,
            "detail": schedule if verbose_paths else _without_private_paths(schedule),
        }
    )
    export_pending = state.get("workbook_export_pending") is True
    checks.append(
        {
            "check": "workbook export current",
            "ok": not export_pending,
            "detail": {
                "pending": export_pending,
                "blocked_by_consent": state.get(
                    "workbook_export_blocked_by_consent"
                )
                is True,
                "pending_since": state.get("workbook_export_pending_since")
                if export_pending
                else None,
            },
            "action": "Run open-health-agent export after resolving the workbook problem."
            if export_pending
            else None,
        }
    )
    required_checks = [item for item in checks if item.get("required", True)]
    return {
        "status": "ok" if all(item["ok"] for item in required_checks) else "needs_attention",
        "checks": checks,
    }


def command_onboarding(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    with lock_for(config):
        config = load_config(config.home_path)
        state_path = config.home_path / "state.json"
        state = _load_state(config)
        delivery_confirmed = bool(getattr(arguments, "delivery_confirmed", False))
        scope = getattr(arguments, "scope", None)
        if arguments.action == "status":
            if delivery_confirmed or scope:
                raise ValueError("onboarding status does not accept action-specific options")
            return _onboarding_public_view(state)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if arguments.action == "mark-explained":
            if scope:
                raise ValueError("mark-explained does not accept a consent scope")
            if not delivery_confirmed:
                raise ValueError("mark-explained requires --delivery-confirmed")
            state["onboarding_explained_at"] = now
            state["onboarding_explanation_delivery_confirmed_at"] = now
        elif arguments.action == "grant-consent":
            if delivery_confirmed:
                raise ValueError("grant-consent does not accept --delivery-confirmed")
            if _aware_state_timestamp(
                state.get("onboarding_explanation_delivery_confirmed_at")
            ) is None:
                raise ValueError("mark the explanation complete before recording consent")
            if scope not in CONSENT_SCOPES:
                raise ValueError("a valid consent scope is required")
            consents = state.get("onboarding_consents")
            if not isinstance(consents, dict):
                consents = {}
            prior = consents.get(scope)
            entry: dict[str, Any] = {"granted_at": now, "policy_version": 1}
            if scope in {"cloud-workbook", "cloud-private-home"}:
                resource_binding = _resource_binding_for_scope(config, scope)
                if resource_binding is None:
                    raise ValueError(
                        f"{scope} consent requires a currently configured synchronized target"
                    )
                entry["resource_binding"] = resource_binding
            # Preparing a reinstall must not interrupt an already-authorized
            # healthy job. A grant after revocation deliberately does *not*
            # resurrect that authorization; only a successful install can.
            if (
                scope == "scheduler"
                and isinstance(prior, dict)
                and not prior.get("revoked_at")
                and isinstance(prior.get("installed_authorization"), dict)
            ):
                entry["installed_authorization"] = dict(
                    prior["installed_authorization"]
                )
            consents[scope] = entry
            state["onboarding_consents"] = consents
            state["onboarding_consent_at"] = now
        elif arguments.action == "revoke-consent":
            if delivery_confirmed:
                raise ValueError("revoke-consent does not accept --delivery-confirmed")
            if scope not in CONSENT_SCOPES:
                raise ValueError("a valid consent scope is required")
            consents = state.get("onboarding_consents")
            if not isinstance(consents, dict):
                consents = {}
            existing = consents.get(scope)
            entry = dict(existing) if isinstance(existing, dict) else {}
            entry["revoked_at"] = now
            entry.setdefault("policy_version", 1)
            consents[scope] = entry
            state["onboarding_consents"] = consents
            if scope in {"scheduler", "keep-awake"}:
                state.update(
                    _job_removal_state_changes(
                        scope,
                        pending=True,
                        attempted_at=now,
                    )
                )
            # Persist withdrawal first. A scheduled sync checks this active
            # consent on every run, so even an OS-service removal failure can no
            # longer fetch health data. Keep-awake has no callback into OHA, so
            # a failed removal is reported explicitly for immediate retry.
            atomic_write_json(state_path, state)
            removal_required = False
            try:
                if scope == "scheduler":
                    removal_required = (
                        scheduler_status(config).get("backend") != "unsupported"
                    )
                    if removal_required:
                        uninstall_scheduler(config)
                elif scope == "keep-awake":
                    removal_required = (
                        keepawake_status().get("backend") != "unsupported"
                    )
                    if removal_required:
                        uninstall_macos_ac_keepawake()
            except Exception as exc:
                removal_error = _public_exception_payload(exc)
                try:
                    _update_job_removal_state(
                        config,
                        scope,
                        pending=True,
                        error_code=removal_error["code"],
                    )
                except (OSError, StateInvalidError):
                    pass
                return {
                    "status": "consent_revoked_job_removal_pending",
                    "scope": scope,
                    "job_removal": "pending",
                    "removal_error": removal_error,
                }
            if scope in {"scheduler", "keep-awake"}:
                _update_job_removal_state(config, scope, pending=False)
            result = {
                "status": "consent_revoked",
                "scope": scope,
                "job_removal": "completed" if removal_required else "not_required",
            }
            if scope in EXTERNAL_REVOCATION_ACTIONS:
                result |= {
                    "status": "local_consent_revoked_external_disable_required",
                    "external_action_required": True,
                    "action": EXTERNAL_REVOCATION_ACTIONS[scope],
                }
            return result
        atomic_write_json(state_path, state)
        return _onboarding_public_view(state) | {
            "action": arguments.action,
            **({"scope": scope} if scope else {}),
        }


def _onboarding_public_view(state: dict[str, Any]) -> dict[str, Any]:
    """Project consent metadata without exposing operational sync state."""

    raw_consents = state.get("onboarding_consents")
    consents = raw_consents if isinstance(raw_consents, dict) else {}
    public_consents: dict[str, dict[str, Any]] = {}
    for scope in sorted(CONSENT_SCOPES):
        raw_entry = consents.get(scope)
        entry = raw_entry if isinstance(raw_entry, dict) else {}
        granted_at = entry.get("granted_at")
        timestamp_valid = _aware_state_timestamp(granted_at) is not None
        public_consents[scope] = {
            "granted": timestamp_valid,
            "active": bool(
                timestamp_valid
                and entry.get("policy_version") == 1
                and not entry.get("revoked_at")
            ),
            "consumed": bool(entry.get("consumed_at")),
            "revoked": bool(entry.get("revoked_at")),
            "policy_version": entry.get("policy_version")
            if entry.get("policy_version") == 1
            else None,
        }
    return {
        "status": "onboarding_status",
        "explanation_delivered": _aware_state_timestamp(
            state.get("onboarding_explanation_delivery_confirmed_at")
        )
        is not None,
        "consents": public_consents,
    }


def _job_removal_prefix(scope: str) -> str:
    if scope == "scheduler":
        return "scheduler_job_removal"
    if scope == "keep-awake":
        return "keep_awake_job_removal"
    raise ValueError("job removal state is unsupported for this consent scope")


def _job_removal_state_changes(
    scope: str,
    *,
    pending: bool,
    error_code: str | None = None,
    attempted_at: str | None = None,
) -> dict[str, Any]:
    prefix = _job_removal_prefix(scope)
    return {
        f"{prefix}_pending": pending,
        f"{prefix}_last_attempt_at": attempted_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        f"{prefix}_error_code": error_code if pending else None,
    }


def _update_job_removal_state(
    config,
    scope: str,
    *,
    pending: bool,
    error_code: str | None = None,
) -> None:
    _update_state(
        config,
        **_job_removal_state_changes(
            scope,
            pending=pending,
            error_code=error_code,
        ),
    )


def _has_scoped_consent(
    config,
    scope: str,
    *,
    one_shot: bool = False,
    not_before: str | None = None,
    max_age: timedelta | None = None,
) -> bool:
    try:
        state = _load_state(config)
    except StateInvalidError:
        return False
    consents = state.get("onboarding_consents")
    if not isinstance(consents, dict):
        return False
    entry = consents.get(scope)
    if not isinstance(entry, dict) or entry.get("policy_version") != 1:
        return False
    granted_at = entry.get("granted_at")
    granted_time = _aware_state_timestamp(granted_at)
    if granted_time is None:
        return False
    if entry.get("revoked_at"):
        return False
    if scope in {"cloud-workbook", "cloud-private-home"}:
        expected_binding = _resource_binding_for_scope(config, scope)
        if expected_binding is None or entry.get("resource_binding") != expected_binding:
            return False
    if one_shot and entry.get("consumed_at"):
        return False
    if max_age is not None and datetime.now(timezone.utc) - granted_time > max_age:
        return False
    if not_before is not None:
        boundary = _aware_state_timestamp(not_before)
        if boundary is None:
            return False
        # State timestamps are written with second precision. Treat equality as
        # a grant made immediately after the qualifying foreground sync rather
        # than rejecting a legitimate same-second approval.
        if granted_time < boundary:
            return False
    return True


def _consume_scoped_consent(config, scope: str) -> None:
    state_path = config.home_path / "state.json"
    state = _load_state(config)
    consents = state.get("onboarding_consents")
    if not isinstance(consents, dict) or not isinstance(consents.get(scope), dict):
        raise ValueError("scoped consent is unavailable")
    entry = dict(consents[scope])
    entry["consumed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    consents[scope] = entry
    state["onboarding_consents"] = consents
    atomic_write_json(state_path, state)


def _authorize_installed_scheduler(
    config, static_runtime: str, account_runtime: str
) -> None:
    if not _valid_runtime_fingerprint_input(
        static_runtime
    ) or not _valid_runtime_fingerprint_input(account_runtime):
        raise ValueError("scheduler authorization fingerprint is invalid")
    state_path = config.home_path / "state.json"
    state = _load_state(config)
    consents = state.get("onboarding_consents")
    if not isinstance(consents, dict):
        raise ValueError("scheduler consent state is unavailable")
    raw_entry = consents.get("scheduler")
    if not isinstance(raw_entry, dict) or not raw_entry.get("consumed_at"):
        raise ValueError("scheduler install consent was not consumed")
    entry = dict(raw_entry)
    if entry.get("revoked_at"):
        raise ValueError("scheduler consent was revoked during installation")
    entry["installed_authorization"] = {
        "version": 1,
        "authorized_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "static_runtime_fingerprint": static_runtime,
        "runtime_fingerprint": account_runtime,
    }
    consents["scheduler"] = entry
    state["onboarding_consents"] = consents
    atomic_write_json(state_path, state)


def _has_scheduled_runtime_authorization(
    config, static_runtime: Any, account_runtime: Any
) -> bool:
    if not _valid_runtime_fingerprint_input(
        static_runtime
    ) or not _valid_runtime_fingerprint_input(account_runtime):
        return False
    if not _has_scoped_consent(config, "scheduler"):
        return False
    try:
        state = _load_state(config)
    except StateInvalidError:
        return False
    consents = state.get("onboarding_consents")
    entry = consents.get("scheduler") if isinstance(consents, dict) else None
    authorization = (
        entry.get("installed_authorization") if isinstance(entry, dict) else None
    )
    if (
        not isinstance(entry, dict)
        or not entry.get("consumed_at")
        or not isinstance(authorization, dict)
        or authorization.get("version") != 1
    ):
        return False
    authorized_at = authorization.get("authorized_at")
    if not isinstance(authorized_at, str):
        return False
    if _aware_state_timestamp(authorized_at) is None:
        return False
    return bool(
        authorization.get("static_runtime_fingerprint") == static_runtime
        and authorization.get("runtime_fingerprint") == account_runtime
    )


def _revoke_scoped_consent(config, scope: str) -> None:
    state_path = config.home_path / "state.json"
    state = _load_state(config)
    consents = state.get("onboarding_consents")
    if not isinstance(consents, dict):
        consents = {}
    existing = consents.get(scope)
    entry = dict(existing) if isinstance(existing, dict) else {}
    entry["revoked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry.setdefault("policy_version", 1)
    consents[scope] = entry
    state["onboarding_consents"] = consents
    if scope in {"scheduler", "keep-awake"}:
        state.update(
            _job_removal_state_changes(
                scope,
                pending=True,
                attempted_at=entry["revoked_at"],
            )
        )
    atomic_write_json(state_path, state)


def _recent_manual_ghealth_sync_fingerprint(
    config,
) -> dict[str, str] | None:
    try:
        state = _load_state(config)
        evidence = state["last_manual_ghealth_sync"]
        if not isinstance(evidence, dict):
            return None
        if evidence.get("version") != 2 or evidence.get("status") != "success":
            return None
        batch_id = evidence.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.startswith(
            "sync_ghealth_manual_"
        ):
            return None
        raw_time = evidence["finished_at"]
        static_fingerprint = evidence["static_runtime_fingerprint"]
        fingerprint = evidence["runtime_fingerprint"]
        recorded_timezone = evidence["timezone"]
        completed_at = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
        if completed_at.tzinfo is None:
            return None
        age = datetime.now(timezone.utc) - completed_at.astimezone(timezone.utc)
        if age < -timedelta(seconds=60) or age > timedelta(minutes=30):
            return None
        if recorded_timezone != config.timezone:
            return None
        if not _valid_runtime_fingerprint_input(
            static_fingerprint
        ) or not _valid_runtime_fingerprint_input(fingerprint):
            return None
        # Hash/compare files before any ghealth subprocess can run. Only the
        # already-accepted executable is then allowed to expose profile/account
        # identity for the second, account-bound comparison.
        if static_runtime_fingerprint(config) != static_fingerprint:
            return None
        if ghealth_runtime_fingerprint(config) != fingerprint:
            return None
        with HealthDatabase(config.database, create=False, read_only=True) as database:
            sync_run = database.get_sync_run(batch_id)
        if (
            not sync_run
            or sync_run.get("status") != "success"
            or sync_run.get("finished_at") != raw_time
            or sum(
                int(sync_run.get(field) or 0)
                for field in (
                    "daily_count",
                    "workout_count",
                    "measurement_count",
                )
            )
            <= 0
        ):
            return None
        return {
            "static_runtime_fingerprint": static_fingerprint,
            "runtime_fingerprint": fingerprint,
            "finished_at": raw_time,
        }
    except (
        KeyError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RuntimeError,
        StateInvalidError,
        sqlite3.Error,
        ValueError,
    ):
        return None


def command_scheduler(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    proxy_env_file = getattr(arguments, "proxy_env_file", None)
    inherit_proxy_env = bool(getattr(arguments, "inherit_proxy_env", False))
    if arguments.action != "install" and (proxy_env_file or inherit_proxy_env):
        raise ValueError("scheduler proxy options are only valid during install")
    if arguments.action == "status":
        status = scheduler_status(config)
        with lock_for(config):
            config = load_config(config.home_path)
            try:
                state = _load_state(config)
                state_valid = True
            except StateInvalidError:
                state = {}
                state_valid = False
            status["state_valid"] = state_valid
            status["scheduler_consent_active"] = _has_scoped_consent(
                config, "scheduler"
            )
            status["google_health_consent_active"] = _has_scoped_consent(
                config, "google-health"
            )
            raw_consents = state.get("onboarding_consents")
            scheduler_entry = (
                raw_consents.get("scheduler")
                if isinstance(raw_consents, dict)
                else None
            )
            installed_authorization = (
                scheduler_entry.get("installed_authorization")
                if isinstance(scheduler_entry, dict)
                else None
            )
            status["installed_runtime_authorization_active"] = bool(
                isinstance(installed_authorization, dict)
                and _has_scheduled_runtime_authorization(
                    config,
                    installed_authorization.get("static_runtime_fingerprint"),
                    installed_authorization.get("runtime_fingerprint"),
                )
            )
            status["job_removal_pending"] = (
                state.get("scheduler_job_removal_pending") is True
            )
            status["job_removal_error_code"] = state.get(
                "scheduler_job_removal_error_code"
            )
            status["job_removal_last_attempt_at"] = state.get(
                "scheduler_job_removal_last_attempt_at"
            )
            status["last_scheduled_sync_status"] = state.get(
                "last_scheduled_sync_status"
            )
            status["last_scheduled_sync_at"] = state.get(
                "last_scheduled_sync_at"
            )
            status["last_successful_scheduled_sync_at"] = state.get(
                "last_successful_scheduled_sync_at"
            )
            status["last_scheduled_sync_error_code"] = state.get(
                "last_scheduled_sync_error_code"
            )
            status["last_any_sync_status"] = state.get("last_sync_status")
            status["last_any_successful_sync_at"] = state.get(
                "last_successful_sync_at"
            )
        status["recent_manual_ghealth_sync_eligible"] = (
            _recent_manual_ghealth_sync_fingerprint(config) is not None
        )
        return status if arguments.verbose_paths else _without_private_paths(status)
    if arguments.action == "uninstall":
        with lock_for(config):
            config = load_config(config.home_path)
            _revoke_scoped_consent(config, "scheduler")
            try:
                result = uninstall_scheduler(config)
            except Exception as exc:
                removal_error = _public_exception_payload(exc)
                try:
                    _update_job_removal_state(
                        config,
                        "scheduler",
                        pending=True,
                        error_code=removal_error["code"],
                    )
                except (OSError, StateInvalidError):
                    pass
                return {
                    "status": "consent_revoked_job_removal_pending",
                    "scope": "scheduler",
                    "job_removal": "pending",
                    "removal_error": removal_error,
                }
            _update_job_removal_state(config, "scheduler", pending=False)
        public = result if arguments.verbose_paths else _without_private_paths(result)
        return public | {"consent_revoked": True}
    _require_private_home_storage_consent(config)
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        if not _has_scoped_consent(
            config,
            "scheduler",
            one_shot=True,
            max_age=timedelta(minutes=30),
        ):
            raise ValueError("fresh scheduler consent must be recorded before install")
        if not _has_scoped_consent(config, "google-health"):
            raise ValueError("google-health consent must be active before scheduler install")
        # Preserve the canonical-database error instead of collapsing a missing
        # or damaged ledger into a generic manual-sync prerequisite failure.
        open_existing_database(config).close()
        manual_sync_fingerprints = _recent_manual_ghealth_sync_fingerprint(config)
        if manual_sync_fingerprints is None:
            raise ValueError(
                "a successful real manual ghealth sync with the current profile and timezone is required within 30 minutes before scheduler install"
            )
        if not _has_scoped_consent(
            config,
            "scheduler",
            one_shot=True,
            not_before=manual_sync_fingerprints["finished_at"],
            max_age=timedelta(minutes=30),
        ):
            raise ValueError(
                "fresh scheduler consent must be recorded after the qualifying manual sync before install"
            )
        # Pinning a profile into a background definition is only safe after the
        # foreground profile has proved it uses OHA's same IANA day boundary.
        resolved_ghealth = resolve_ghealth_executable(config.ghealth_command)
        GHealthAdapter(
            CommandRunner(str(resolved_ghealth)),
            config.sleep_source_priority,
            config.timezone,
        ).require_matching_timezone()
        if proxy_env_file:
            proxy_environment = proxy_environment_from_file(Path(proxy_env_file))
        elif inherit_proxy_env:
            proxy_environment = proxy_environment_from_process()
        else:
            proxy_environment = {}
        # Consume the one-shot action grant before mutating the live OS job. If
        # this state write fails, installation never starts. A later installer
        # failure leaves the grant consumed and requires fresh approval.
        _consume_scoped_consent(config, "scheduler")
        result = install_scheduler(
            config,
            arguments.interval_seconds,
            proxy_environment=proxy_environment,
            expected_static_runtime_fingerprint=manual_sync_fingerprints[
                "static_runtime_fingerprint"
            ],
            expected_runtime_fingerprint=manual_sync_fingerprints[
                "runtime_fingerprint"
            ],
        )
        # A one-shot grant is not ongoing runtime authorization. Bind the
        # successfully installed definition to the two opaque fingerprints;
        # an orphaned definition remains unable to run after revoke/re-grant.
        _authorize_installed_scheduler(
            config,
            manual_sync_fingerprints["static_runtime_fingerprint"],
            manual_sync_fingerprints["runtime_fingerprint"],
        )
    return result if arguments.verbose_paths else _without_private_paths(result)


def command_power(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    if arguments.action == "status":
        result = keepawake_status()
        with lock_for(config):
            config = load_config(config.home_path)
            try:
                state = _load_state(config)
                result["state_valid"] = True
            except StateInvalidError:
                state = {}
                result["state_valid"] = False
            result["keep_awake_consent_active"] = _has_scoped_consent(
                config, "keep-awake"
            )
            result["job_removal_pending"] = (
                state.get("keep_awake_job_removal_pending") is True
            )
            result["job_removal_error_code"] = state.get(
                "keep_awake_job_removal_error_code"
            )
            result["job_removal_last_attempt_at"] = state.get(
                "keep_awake_job_removal_last_attempt_at"
            )
        return result if arguments.verbose_paths else _without_private_paths(result)
    if arguments.action == "uninstall":
        with lock_for(config):
            config = load_config(config.home_path)
            _revoke_scoped_consent(config, "keep-awake")
            try:
                result = uninstall_macos_ac_keepawake()
            except Exception as exc:
                removal_error = _public_exception_payload(exc)
                try:
                    _update_job_removal_state(
                        config,
                        "keep-awake",
                        pending=True,
                        error_code=removal_error["code"],
                    )
                except (OSError, StateInvalidError):
                    pass
                return {
                    "status": "consent_revoked_job_removal_pending",
                    "scope": "keep-awake",
                    "job_removal": "pending",
                    "removal_error": removal_error,
                }
            _update_job_removal_state(config, "keep-awake", pending=False)
        public = result if arguments.verbose_paths else _without_private_paths(result)
        return public | {"consent_revoked": True}
    _require_private_home_storage_consent(config)
    with lock_for(config):
        config = load_config(config.home_path)
        _require_private_home_storage_consent(config)
        if not _has_scoped_consent(
            config,
            "keep-awake",
            one_shot=True,
            max_age=timedelta(minutes=30),
        ):
            raise ValueError("fresh keep-awake consent must be recorded before install")
        _consume_scoped_consent(config, "keep-awake")
        result = install_macos_ac_keepawake(config)
    return result if arguments.verbose_paths else _without_private_paths(result)


def build_parser() -> argparse.ArgumentParser:
    parser = PrivateArgumentParser(description="Private health ledger for agent workflows")
    parser.add_argument("--home", help="Private data directory (default: ~/.open-health-agent)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create private local files and workbook")
    init.add_argument("--workbook", help="Workbook path; may be inside iCloud Drive")
    init.add_argument("--timezone", help="IANA timezone such as Asia/Shanghai")
    init.add_argument("--ghealth-command")
    init.add_argument(
        "--verbose-paths",
        action="store_true",
        help="Include absolute local paths for terminal-only debugging",
    )
    init.add_argument(
        "--force",
        action="store_true",
        help="Back up and update explicitly supplied config fields; private AGENTS/profile/state are preserved",
    )
    init.set_defaults(handler=command_init)

    doctor = sub.add_parser("doctor", help="Check local setup without exposing health rows")
    doctor_mode = doctor.add_mutually_exclusive_group()
    doctor_mode.add_argument(
        "--online",
        action="store_true",
        help="Also validate live ghealth authentication (bounded to 15 seconds)",
    )
    doctor_mode.add_argument(
        "--offline",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    doctor.add_argument(
        "--verbose-paths",
        action="store_true",
        help="Include absolute local paths for terminal-only debugging",
    )
    doctor.set_defaults(handler=command_doctor)

    sync = sub.add_parser("sync", help="Fetch a bounded ghealth range and atomically export Excel")
    sync.add_argument("--from-date")
    sync.add_argument("--to-date")
    sync.add_argument(
        "--lookback-days",
        type=int,
        help="Explicit inclusive lookback for a manual backfill (1-91); the scheduler keeps the configured overlap",
    )
    sync.add_argument("--fixture-dir", help=argparse.SUPPRESS)
    sync.add_argument("--quiet", action="store_true")
    sync.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    sync.add_argument("--static-runtime-fingerprint", help=argparse.SUPPRESS)
    sync.add_argument("--runtime-fingerprint", help=argparse.SUPPRESS)
    sync.add_argument(
        "--verbose-path",
        action="store_true",
        help="Include the absolute workbook path for terminal-only debugging",
    )
    sync.set_defaults(handler=command_sync)

    record_parser = sub.add_parser(
        "record",
        help="Record a user-confirmed measurement, workout, or consumed food",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "stdin JSON fields:\n"
            "  measurement: date, metric, value, unit; blood pressure also requires second_value\n"
            "  workout: date, workout_type; optional duration_minutes, intensity_rpe (0-10), etc.\n"
            "  food: date, food_name, consumed=true; optional summary_nutrients and portion range\n"
            "Preserve original_text, source/method, and confidence. Prefer stdin or --file for private data."
        ),
    )
    record_parser.add_argument("--kind", choices=sorted(SUPPORTED_KINDS), required=True)
    record_parser.add_argument(
        "--record-id",
        help="Correct an existing record by stable ID; fails instead of creating a new ID when it is missing",
    )
    payload_group = record_parser.add_mutually_exclusive_group()
    payload_group.add_argument("--json", dest="json_payload")
    payload_group.add_argument("--file")
    record_parser.set_defaults(handler=command_record)

    delete = sub.add_parser("delete", help="Delete a mistaken record with an audit reason")
    delete.add_argument("--kind", choices=sorted(SUPPORTED_KINDS), required=True)
    delete.add_argument("--record-id", required=True)
    delete.add_argument("--reason", required=True)
    delete.set_defaults(handler=command_delete)

    goal = sub.add_parser("goal", help="Manage exact user goals in the private AGENTS.md")
    goal_sub = goal.add_subparsers(dest="goal_action", required=True)
    goal_set = goal_sub.add_parser("set")
    goal_input = goal_set.add_mutually_exclusive_group()
    goal_input.add_argument("--text", help="Goal text (may be visible in process history; prefer stdin or --file)")
    goal_input.add_argument("--file", help="Read exact goal wording from a private UTF-8 file")
    goal_set.add_argument("--effective-date")
    goal_set.add_argument("--priority", type=int, default=100)
    goal_set.add_argument("--safety-constraint", default="")
    goal_set.set_defaults(handler=command_goal_set)
    goal_retire = goal_sub.add_parser("retire")
    goal_retire.add_argument("--record-id", required=True)
    goal_retire.add_argument("--reason", default="")
    goal_retire.set_defaults(handler=command_goal_retire)
    goal_repair = goal_sub.add_parser(
        "repair", help="Rebuild profile/AGENTS goal views from the private SQLite ledger"
    )
    goal_repair.set_defaults(handler=command_goal_repair)

    profile = sub.add_parser("profile", help="Set a user-confirmed private profile field")
    profile_sub = profile.add_subparsers(dest="profile_action", required=True)
    profile_set = profile_sub.add_parser("set")
    profile_set.add_argument("--key", required=True)
    profile_input = profile_set.add_mutually_exclusive_group()
    profile_input.add_argument("--value", help="JSON value or plain text (prefer stdin or --file for sensitive values)")
    profile_input.add_argument("--file", help="Read JSON or text from a private UTF-8 file")
    profile_set.add_argument("--source", default="user-confirmed")
    profile_set.set_defaults(handler=command_profile_set)

    context = sub.add_parser("context", help="Return the ledger context required before advice")
    context.add_argument("--date")
    context.set_defaults(handler=command_context)

    export = sub.add_parser("export", help="Regenerate the readable workbook from the local database")
    export.add_argument(
        "--verbose-path",
        action="store_true",
        help="Include the absolute workbook path for terminal-only debugging",
    )
    export.set_defaults(handler=command_export)

    backup = sub.add_parser("backup", help="Create a bounded, consistent SQLite snapshot")
    backup.add_argument(
        "--verbose-path",
        action="store_true",
        help="Include the absolute backup path for terminal-only debugging",
    )
    backup.set_defaults(handler=command_backup)

    onboarding = sub.add_parser("onboarding", help="Track explanation and explicit consent locally")
    onboarding.add_argument(
        "action",
        choices=["status", "mark-explained", "grant-consent", "revoke-consent"],
    )
    onboarding.add_argument(
        "--delivery-confirmed",
        action="store_true",
        help="Assert that the user-visible explanation was successfully delivered",
    )
    onboarding.add_argument("--scope", choices=sorted(CONSENT_SCOPES))
    onboarding.set_defaults(handler=command_onboarding)

    scheduler = sub.add_parser("scheduler", help="Install or inspect hourly background sync")
    scheduler.add_argument("action", choices=["install", "status", "uninstall"])
    scheduler.add_argument("--interval-seconds", type=int, default=3600)
    scheduler.add_argument(
        "--verbose-paths",
        action="store_true",
        help="Include absolute background-definition paths for terminal-only debugging",
    )
    scheduler_proxy = scheduler.add_mutually_exclusive_group()
    scheduler_proxy.add_argument(
        "--inherit-proxy-env",
        action="store_true",
        help="Copy only validated proxy variables from this install process into the background definition",
    )
    scheduler_proxy.add_argument(
        "--proxy-env-file",
        help="Read validated proxy variables from an owner-only local file during install",
    )
    scheduler.set_defaults(handler=command_scheduler)

    power = sub.add_parser("keep-awake-on-ac", help="macOS: prevent idle sleep only while plugged in")
    power.add_argument("action", nargs="?", choices=["install", "status", "uninstall"], default="install")
    power.add_argument(
        "--verbose-paths",
        action="store_true",
        help="Include the absolute launchd definition path for terminal-only debugging",
    )
    power.set_defaults(handler=command_power)
    return parser


def main() -> int:
    parser = build_parser()
    try:
        arguments = parser.parse_args()
        result = arguments.handler(arguments)
        emit(result, quiet=getattr(arguments, "quiet", False))
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps(_public_exception_payload(exc), ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
