#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
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
    goal_views_status,
    rebuild_goal_views,
    retire_goal,
    set_goal,
    set_profile_value,
)
from oha.recording import SUPPORTED_KINDS, record
from oha.scheduler import (
    install_macos_ac_keepawake,
    install_scheduler,
    keepawake_status,
    resolve_ghealth_executable,
    scheduler_status,
    uninstall_macos_ac_keepawake,
    uninstall_scheduler,
)
from oha.workbook_store import export_workbook, inspect_workbook_schema


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
ASSETS_DIR = SKILL_DIR / "assets"


class CLIUsageError(ValueError):
    """An argparse failure whose original message may contain private input."""


class PrivateArgumentParser(argparse.ArgumentParser):
    """Keep malformed command lines from echoing health text or local paths."""

    def error(self, message: str) -> None:
        del message
        raise CLIUsageError("command-line parsing failed")


def emit(value: Any, *, quiet: bool = False) -> None:
    if quiet:
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


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


def command_init(arguments: argparse.Namespace) -> dict[str, Any]:
    home = configured_home(arguments) or Path.home() / ".open-health-agent"
    if arguments.workbook and Path(arguments.workbook).suffix.lower() != ".xlsx":
        raise ValueError("workbook must use the .xlsx format")
    if arguments.timezone:
        ZoneInfo(arguments.timezone)
    existing_path = config_path(home)
    existing = existing_path.exists()
    config_backup = None
    if existing:
        config = load_config(home)
        if arguments.force:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            config_backup = existing_path.with_name(f"{existing_path.name}.backup-{stamp}")
            shutil.copy2(existing_path, config_backup)
            try:
                config_backup.chmod(0o600)
            except OSError:
                pass
            if arguments.workbook:
                config.workbook_path = str(Path(arguments.workbook).expanduser().resolve())
            if arguments.timezone:
                ZoneInfo(arguments.timezone)
                config.timezone = arguments.timezone
            if arguments.ghealth_command:
                config.ghealth_command = arguments.ghealth_command
            save_config(config)
    else:
        workbook = Path(arguments.workbook).expanduser() if arguments.workbook else None
        selected_timezone = arguments.timezone or local_timezone_name()
        ZoneInfo(selected_timezone)
        config = create_config(
            home,
            workbook=workbook,
            timezone=selected_timezone,
            ghealth_command=arguments.ghealth_command or "ghealth",
        )
        save_config(config)
    try:
        local_files = initialize_local_files(
            config,
            ASSETS_DIR / "AGENTS.md.template",
            ASSETS_DIR / "profile.example.json",
            force=False,
        )
        workbook_exported = not (
            existing and not arguments.force and config.workbook.is_file()
        )
        if not workbook_exported and not config.database.is_file():
            raise RuntimeError(
                "the canonical database is missing; refusing to regenerate an existing workbook during init"
            )
        if workbook_exported:
            with lock_for(config):
                with HealthDatabase(config.database) as database:
                    exported = export_workbook(config, database, workbook_template())
        else:
            exported = config.workbook
    except Exception:
        if config_backup is not None:
            shutil.copy2(config_backup, existing_path)
            try:
                existing_path.chmod(0o600)
            except OSError:
                pass
        raise
    return {
        "status": "ready",
        "existing_configuration_reused": existing and not arguments.force,
        "workbook_exported": workbook_exported,
        "home": str(config.home_path),
        "workbook": str(exported),
        "database": str(config.database),
        "local_files": local_files,
        "configuration_backup": str(config_backup) if config_backup else None,
        "next": [
            "Have the agent explain the Skill, privacy processors, and non-medical limits before consent.",
            "Run doctor, authenticate ghealth if desired, then run sync.",
            "Set user goals with goal set; exact wording is written to the private AGENTS.md.",
        ],
    }


def date_range(config, from_date: str | None, to_date: str | None) -> tuple[str, str]:
    try:
        today = datetime.now(ZoneInfo(config.timezone)).date()
    except Exception:
        today = datetime.now(timezone.utc).date()
    end = date.fromisoformat(to_date) if to_date else today
    start = date.fromisoformat(from_date) if from_date else end - timedelta(days=config.lookback_days - 1)
    if start > end:
        raise ValueError("from-date cannot be after to-date")
    if (end - start).days > 90:
        raise ValueError("a single sync range cannot exceed 91 days")
    return start.isoformat(), end.isoformat()


def _update_state(config, **changes: Any) -> None:
    state_path = config.home_path / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {}
    state.update(changes)
    atomic_write_json(state_path, state)


def _cap_scheduler_logs(config, maximum_bytes: int = 2 * 1024 * 1024) -> None:
    for name in ("scheduler.out.log", "scheduler.err.log"):
        path = config.home_path / "logs" / name
        try:
            if path.stat().st_size > maximum_bytes:
                path.write_bytes(b"")
                path.chmod(0o600)
        except FileNotFoundError:
            continue
        except OSError:
            continue


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

    if isinstance(exc, CLIUsageError):
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
        code = "invalid_input"
        if str(exc) == "mark the explanation complete before recording consent":
            message = "Mark the Skill explanation complete before recording consent."
        else:
            message = "Input validation failed; check the command fields and accepted ranges."
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
    }


def _safe_exception_summary(exc: Exception) -> str:
    payload = _public_exception_payload(exc)
    return f"code={payload['code']}; type={payload['type']}; private details withheld"


def command_sync(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    _cap_scheduler_logs(config)
    start, end = date_range(config, arguments.from_date, arguments.to_date)
    batch_id = f"sync_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    runner = (
        FixtureRunner(Path(arguments.fixture_dir).resolve())
        if arguments.fixture_dir
        else CommandRunner(str(resolve_ghealth_executable(config.ghealth_command)))
    )
    adapter = GHealthAdapter(runner, config.sleep_source_priority, config.timezone)
    # Fixtures are repository-only synthetic inputs. Real imports must prove that
    # ghealth and OHA use the same local-day boundary before any sync row is opened.
    if not arguments.fixture_dir:
        adapter.require_matching_timezone()
    with lock_for(config):
        with HealthDatabase(config.database) as database:
            database.begin_sync(batch_id, start, end)
            try:
                result = adapter.fetch(start, end, batch_id)
                errors = list(result.get("errors") or [])
                failed_query_keys = list(result.get("failed_query_keys") or [])
                refreshed_daily_ids: set[str] = set()
                for payload in result["daily"]:
                    # Retain only fields owned by the queries that actually
                    # failed. A successful-but-empty query is allowed to clear
                    # its old daily value on this recomputed composite row.
                    previous = database.get("daily", payload["record_id"])
                    selected_payload = merge_partial_daily(
                        payload, previous, failed_query_keys
                    )
                    database.upsert(
                        "daily",
                        selected_payload,
                        selected_payload["record_id"],
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
                        database.upsert(
                            "daily",
                            selected_payload,
                            selected_payload["record_id"],
                        )
                for payload in result["measurements"]:
                    database.upsert("measurement", payload, payload["record_id"])
                for payload in result["workouts"]:
                    database.upsert("workout", payload, payload["record_id"])
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
                database.finish_sync(
                    batch_id,
                    status,
                    counts["daily"],
                    counts["workouts"],
                    counts["measurements"],
                    result.get("data_until"),
                    errors,
                )
                raw_summary = {
                    "batch_id": batch_id,
                    "from_date": start,
                    "to_date": end,
                    "status": status,
                    "counts": counts,
                    "data_until": result.get("data_until"),
                    "errors": errors,
                }
                summary_path = config.home_path / "raw" / f"{batch_id}.summary.json"
                atomic_write_json(summary_path, raw_summary)
                _prune_raw_summaries(config)
                try:
                    workbook = export_workbook(config, database, workbook_template())
                except Exception as exc:
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
                    raise
                database.prune_history(
                    audit_limit=config.audit_event_retention,
                    sync_limit=config.sync_run_retention,
                )
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                changes: dict[str, Any] = {
                    "last_sync_at": now,
                    "last_sync_status": status,
                    "last_sync_batch_id": batch_id,
                }
                if status == "success":
                    changes["last_successful_sync_at"] = now
                _update_state(config, **changes)
            except Exception as exc:
                current = next((run for run in database.list_sync_runs(1) if run["batch_id"] == batch_id), None)
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
                _update_state(
                    config,
                    last_sync_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    last_sync_status="failed",
                    last_sync_batch_id=batch_id,
                )
                raise
    return raw_summary | {"workbook": str(workbook)}


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
    payload = _load_json_argument(arguments)
    with lock_for(config):
        with HealthDatabase(config.database) as database:
            normalized = record(database, arguments.kind, payload)
            workbook = export_workbook(config, database, workbook_template())
    return {"status": "recorded", "kind": arguments.kind, "record": normalized, "workbook": str(workbook)}


def command_delete(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    with lock_for(config):
        with HealthDatabase(config.database) as database:
            deleted = database.delete(arguments.kind, arguments.record_id, arguments.reason)
            workbook = export_workbook(config, database, workbook_template()) if deleted else config.workbook
    return {"status": "deleted" if deleted else "not_found", "kind": arguments.kind, "record_id": arguments.record_id, "workbook": str(workbook)}


def command_goal_set(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    if arguments.text is not None:
        text = arguments.text
    elif arguments.file:
        text = Path(arguments.file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        raise ValueError("provide goal text on stdin, with --file, or with --text")
    with lock_for(config):
        with HealthDatabase(config.database) as database:
            goal = set_goal(config, database, text, arguments.effective_date, arguments.priority, arguments.safety_constraint)
            workbook = export_workbook(config, database, workbook_template())
    return {"status": "goal_saved", "goal": goal, "private_agents_file": str(config.home_path / "AGENTS.md"), "workbook": str(workbook)}


def command_goal_retire(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    with lock_for(config):
        with HealthDatabase(config.database) as database:
            goal = retire_goal(config, database, arguments.record_id, arguments.reason)
            workbook = export_workbook(config, database, workbook_template())
    return {"status": "goal_retired", "goal": goal, "workbook": str(workbook)}


def command_goal_repair(arguments: argparse.Namespace) -> dict[str, Any]:
    """Rebuild private goal projections from the canonical SQLite records."""

    config = open_config(arguments)
    with lock_for(config):
        with HealthDatabase(config.database) as database:
            projection = rebuild_goal_views(config, database)
            workbook = export_workbook(config, database, workbook_template())
    return {
        "status": "goal_views_rebuilt",
        "projection": projection,
        "workbook": str(workbook),
    }


def command_profile_set(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
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
        profile = set_profile_value(config, arguments.key, value, arguments.source)
    return {"status": "profile_updated", "field": arguments.key, "value": profile.get(arguments.key)}


def command_context(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    with HealthDatabase(config.database) as database:
        return build_context(config, database, arguments.date)


def command_export(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    with lock_for(config):
        with HealthDatabase(config.database) as database:
            workbook = export_workbook(config, database, workbook_template())
    return {"status": "exported", "workbook": str(workbook)}


def _profile_schema_status(path: Path) -> dict[str, Any]:
    """Validate profile structure without returning any private values."""

    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": "unreadable", "error_type": type(exc).__name__}
    if not isinstance(profile, dict):
        return {"ok": False, "reason": "not_an_object", "invalid_fields": []}

    invalid: list[str] = []
    goals = profile.get("goals", [])
    if not isinstance(goals, list) or any(not isinstance(item, dict) for item in goals):
        invalid.append("goals")
    lean_mass = profile.get("lean_mass_kg")
    if lean_mass is not None and (
        isinstance(lean_mass, bool)
        or not isinstance(lean_mass, (int, float))
        or not math.isfinite(float(lean_mass))
        or not 20 <= float(lean_mass) <= 200
    ):
        invalid.append("lean_mass_kg")
    for field in ("health_constraints", "medications_affecting_exercise"):
        value = profile.get(field, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            invalid.append(field)
    for field in ("age_group", "life_stage"):
        value = profile.get(field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            invalid.append(field)
    portion_mode = profile.get("portion_mode", "range")
    if portion_mode not in {"range", "weighed", "exact"}:
        invalid.append("portion_mode")
    return {
        "ok": not invalid,
        "reason": "valid" if not invalid else "invalid_fields",
        "invalid_fields": sorted(invalid),
    }


def command_doctor(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
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
        checks.append({"check": label, "ok": path.exists(), "path": str(path)})
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
    checks.append(
        {
            "check": "ghealth command",
            "ok": bool(command and Path(command).exists()),
            "required": ghealth_required,
            "detail": "optional unless an hourly wearable-sync definition exists",
            "path": command,
        }
    )
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
            with HealthDatabase(config.database) as database:
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
            CommandRunner(str(command)),
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
    if not arguments.offline and command and Path(command).exists():
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
    state_path = config.home_path / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    schedule["last_sync_status"] = state.get("last_sync_status")
    schedule["last_successful_sync_at"] = state.get("last_successful_sync_at")
    checks.append(
        {
            "check": "hourly scheduler",
            "ok": schedule.get("installed", False),
            "required": False,
            "detail": schedule,
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
        state_path = config.home_path / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        if arguments.action == "status":
            return state
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if arguments.action == "mark-explained":
            state["onboarding_explained_at"] = now
        elif arguments.action == "grant-consent":
            if not state.get("onboarding_explained_at"):
                raise ValueError("mark the explanation complete before recording consent")
            state["onboarding_consent_at"] = now
        atomic_write_json(state_path, state)
        return state


def command_scheduler(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    if arguments.action == "status":
        status = scheduler_status(config)
        try:
            state = json.loads((config.home_path / "state.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        status["last_sync_status"] = state.get("last_sync_status")
        status["last_successful_sync_at"] = state.get("last_successful_sync_at")
        return status
    if arguments.action == "uninstall":
        return uninstall_scheduler(config)
    # Pinning a profile into a background definition is only safe after the
    # foreground profile has proved it uses OHA's same IANA day boundary.
    resolved_ghealth = resolve_ghealth_executable(config.ghealth_command)
    GHealthAdapter(
        CommandRunner(str(resolved_ghealth)),
        config.sleep_source_priority,
        config.timezone,
    ).require_matching_timezone()
    return install_scheduler(config, arguments.interval_seconds)


def command_power(arguments: argparse.Namespace) -> dict[str, Any]:
    config = open_config(arguments)
    if arguments.action == "status":
        return keepawake_status()
    if arguments.action == "uninstall":
        return uninstall_macos_ac_keepawake()
    return install_macos_ac_keepawake(config)


def build_parser() -> argparse.ArgumentParser:
    parser = PrivateArgumentParser(description="Private health ledger for agent workflows")
    parser.add_argument("--home", help="Private data directory (default: ~/.open-health-agent)")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create private local files and workbook")
    init.add_argument("--workbook", help="Workbook path; may be inside iCloud Drive")
    init.add_argument("--timezone", help="IANA timezone such as Asia/Shanghai")
    init.add_argument("--ghealth-command")
    init.add_argument(
        "--force",
        action="store_true",
        help="Back up and update explicitly supplied config fields; private AGENTS/profile/state are preserved",
    )
    init.set_defaults(handler=command_init)

    doctor = sub.add_parser("doctor", help="Check local setup without exposing health rows")
    doctor.add_argument("--offline", action="store_true", help="Skip live token validation")
    doctor.set_defaults(handler=command_doctor)

    sync = sub.add_parser("sync", help="Fetch a bounded ghealth range and atomically export Excel")
    sync.add_argument("--from-date")
    sync.add_argument("--to-date")
    sync.add_argument("--fixture-dir", help=argparse.SUPPRESS)
    sync.add_argument("--quiet", action="store_true")
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
    export.set_defaults(handler=command_export)

    onboarding = sub.add_parser("onboarding", help="Track explanation and explicit consent locally")
    onboarding.add_argument("action", choices=["status", "mark-explained", "grant-consent"])
    onboarding.set_defaults(handler=command_onboarding)

    scheduler = sub.add_parser("scheduler", help="Install or inspect hourly background sync")
    scheduler.add_argument("action", choices=["install", "status", "uninstall"])
    scheduler.add_argument("--interval-seconds", type=int, default=3600)
    scheduler.set_defaults(handler=command_scheduler)

    power = sub.add_parser("keep-awake-on-ac", help="macOS: prevent idle sleep only while plugged in")
    power.add_argument("action", nargs="?", choices=["install", "status", "uninstall"], default="install")
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
        print(json.dumps(_public_exception_payload(exc), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
