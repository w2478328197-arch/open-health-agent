from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .config import Config
from .legacy_migration import (
    LEGACY_DERIVED_OR_LOG_SHEETS,
    legacy_health_fingerprint,
    migration_audit_core_valid,
)

if TYPE_CHECKING:
    from .database import HealthDatabase


_LEGACY_SCRIPT_NAMES = {
    "sync_ghealth_hourly.py",
    "import_health_ledger.py",
}
_LEGACY_HERMES_JOB_NAME = "ghealth每小时同步到iCloud健康台账"


def cloud_home_warning(home: Path) -> dict[str, Any] | None:
    """Describe a cloud-synced private home without treating it as invalid."""

    normalized = "/".join(part.casefold() for part in home.expanduser().resolve().parts)
    provider = None
    if "mobile documents" in normalized or "icloud drive" in normalized:
        provider = "icloud"
    elif "onedrive" in normalized:
        provider = "onedrive"
    elif "dropbox" in normalized:
        provider = "dropbox"
    elif "google drive" in normalized:
        provider = "google_drive"
    if provider is None:
        return None
    return {
        "check": "cloud-synced private home",
        "ok": True,
        "required": False,
        "severity": "warning",
        "detail": {
            "provider": provider,
            "single_system_user_required": True,
            "single_writer_host_required": True,
        },
    }


def confirmed_profile_input_status(profile_path: Path) -> dict[str, Any]:
    """Report only missing calculation field names, never profile values."""

    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        profile = {}
    if not isinstance(profile, dict):
        profile = {}

    goals = profile.get("goals")
    has_active_goal = isinstance(goals, list) and any(
        isinstance(item, dict) and str(item.get("status", "")).startswith("active")
        for item in goals
    )
    provenance = profile.get("field_provenance")
    lean_provenance = (
        provenance.get("lean_mass_kg") if isinstance(provenance, dict) else None
    )
    lean_mass_confirmed = (
        isinstance(profile.get("lean_mass_kg"), (int, float))
        and not isinstance(profile.get("lean_mass_kg"), bool)
        and isinstance(lean_provenance, dict)
        and lean_provenance.get("source") == "user-confirmed"
    )

    missing: list[str] = []
    effects: list[str] = []
    if not has_active_goal:
        missing.append("active_goal")
        effects.append("goal-specific planning remains unavailable")
    if not lean_mass_confirmed:
        missing.append("lean_mass_kg")
        effects.append("FFM-based REE remains unavailable")
    return {
        "check": "confirmed calculation inputs",
        "ok": not missing,
        "required": False,
        "severity": "warning" if missing else "info",
        "detail": {"missing_fields": missing, "effects": effects},
    }


def _migration_audit_record(
    database: "HealthDatabase",
) -> tuple[dict[str, Any] | None, bool, bool] | None:
    row = database.connection.execute(
        """
        SELECT record_id, payload_json FROM audit_events
        WHERE action='legacy_workbook_migration'
        ORDER BY event_id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except (TypeError, json.JSONDecodeError):
        return None, False, False
    if not isinstance(payload, dict):
        return None, True, False

    valid = migration_audit_core_valid(
        payload,
        row["record_id"],
        completion="complete",
    )
    return payload, True, valid


def latest_migration_audit(database: "HealthDatabase") -> dict[str, Any] | None:
    record = _migration_audit_record(database)
    if record is None:
        return None
    payload, _readable, valid = record
    return payload if valid and payload is not None else {"_invalid": True}


def pending_migration_status(
    database: "HealthDatabase",
) -> dict[str, Any] | None:
    count = database.connection.execute(
        """
        SELECT COUNT(*) FROM audit_events
        WHERE action='legacy_workbook_migration_pending'
        """
    ).fetchone()[0]
    if count == 0:
        return None
    return {
        "check": "migration pending postconditions",
        "ok": False,
        "required": True,
        "detail": {"present": True, "count": count},
    }


def migration_audit_integrity_status(
    database: "HealthDatabase",
) -> dict[str, Any] | None:
    record = _migration_audit_record(database)
    if record is None:
        return None
    _payload, readable, valid = record
    return {
        "check": "migration audit integrity",
        "ok": valid,
        "required": True,
        "detail": {"present": True, "readable": readable, "valid": valid},
    }


def workbook_convergence_status(
    config: Config, database: "HealthDatabase"
) -> dict[str, Any] | None:
    audit = latest_migration_audit(database)
    if audit is None:
        return None
    migration_target = audit.get("target_workbook")
    migration_database = audit.get("target_database")
    source = audit.get("source")
    legacy_source = source.get("path") if isinstance(source, dict) else None
    if (
        not isinstance(migration_target, str)
        or not migration_target
        or not isinstance(migration_database, str)
        or not migration_database
    ):
        return None
    expected = Path(migration_target).expanduser().resolve()
    expected_database = Path(migration_database).expanduser().resolve()
    workbook_matches = config.workbook == expected
    database_matches = config.database == expected_database
    matches = workbook_matches and database_matches
    return {
        "check": "workbook convergence",
        "ok": matches,
        "required": True,
        "detail": {
            "configured_workbook": str(config.workbook),
            "migration_target_workbook": str(expected),
            "configured_database": str(config.database),
            "migration_target_database": str(expected_database),
            "legacy_source": legacy_source,
            "matches_migration_target": workbook_matches,
            "matches_migration_database": database_matches,
        },
    }


def legacy_workbook_freshness_status(
    database: "HealthDatabase",
) -> dict[str, Any] | None:
    audit = latest_migration_audit(database)
    source = audit.get("source") if isinstance(audit, dict) else None
    if not isinstance(source, dict) or not isinstance(source.get("path"), str):
        return None
    path = Path(source["path"]).expanduser().resolve()
    detail: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "mtime_ns": None,
        "size": None,
        "health_sheet_rows": {},
        "unknown_sheet_count": 0,
        "matches_completed_migration": False,
        "has_unimported_changes": True,
    }
    if path.is_file():
        try:
            stat = path.stat()
            current_health = legacy_health_fingerprint(path)
            completed_health = source.get("health")
            completed_health_sha = (
                completed_health.get("sha256")
                if isinstance(completed_health, dict)
                else None
            )
            matches = (
                isinstance(completed_health_sha, str)
                and current_health["sha256"] == completed_health_sha
            )
            detail.update(
                {
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "health_sheet_rows": current_health["sheet_rows"],
                    "unknown_sheet_count": current_health["unknown_sheet_count"],
                    "health_fingerprint_available": isinstance(
                        completed_health_sha, str
                    ),
                    "matches_completed_migration": matches,
                    "has_unimported_changes": not matches,
                }
            )
        except Exception as exc:
            detail["error_type"] = type(exc).__name__
    return {
        "check": "legacy workbook freshness",
        "ok": not detail["has_unimported_changes"],
        "required": True,
        "detail": detail,
    }


def canonical_sync_coverage_status(
    config: Config,
    database: "HealthDatabase",
    specified_legacy: Path | None = None,
) -> dict[str, Any] | None:
    audit = latest_migration_audit(database)
    if specified_legacy is not None:
        path = specified_legacy.expanduser().resolve()
    else:
        source = audit.get("source") if isinstance(audit, dict) else None
        raw_path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(raw_path, str) or not raw_path:
            return None
        path = Path(raw_path).expanduser().resolve()

    detail: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "mtime_ns": None,
        "size": None,
        "health_sheet_rows": {},
        "unknown_sheet_count": 0,
        "configured_workbook": str(config.workbook),
        "distinct_from_configured_workbook": True,
        "database_sync_run_count": 0,
        "successful_health_sync_run_count": 0,
    }
    ok = False
    try:
        if not path.is_file() or path.suffix.lower() != ".xlsx":
            raise ValueError("specified legacy workbook is unavailable")
        stat = path.stat()
        fingerprint = legacy_health_fingerprint(path)
        sync_counts = database.connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(
                    CASE
                        WHEN status='success'
                         AND daily_count + workout_count + measurement_count > 0
                        THEN 1 ELSE 0
                    END
                ) AS successful_health
            FROM sync_runs
            """
        ).fetchone()
        sync_count = sync_counts["total"]
        successful_health_sync_count = sync_counts["successful_health"] or 0
        distinct = not path.samefile(config.workbook) if config.workbook.exists() else path != config.workbook
        imported_rows = {
            name: count
            for name, count in fingerprint["sheet_rows"].items()
            if name not in LEGACY_DERIVED_OR_LOG_SHEETS
        }
        has_health_rows = any(count > 0 for count in imported_rows.values())
        ok = distinct and (not has_health_rows or successful_health_sync_count > 0)
        detail.update(
            {
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "health_sheet_rows": imported_rows,
                "unknown_sheet_count": fingerprint["unknown_sheet_count"],
                "distinct_from_configured_workbook": distinct,
                "database_sync_run_count": sync_count,
                "successful_health_sync_run_count": successful_health_sync_count,
            }
        )
    except Exception as exc:
        detail["error_type"] = type(exc).__name__
    return {
        "check": "canonical health sync coverage",
        "ok": ok,
        "required": True,
        "detail": detail,
    }


def _job_matches_legacy_writer(job: dict[str, Any]) -> bool:
    name = str(job.get("name") or "")
    if name == _LEGACY_HERMES_JOB_NAME:
        return True
    searchable = " ".join(
        str(job.get(field) or "") for field in ("script", "prompt", "command")
    )
    return any(script_name in searchable for script_name in _LEGACY_SCRIPT_NAMES)


def _hermes_jobs(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.is_file():
        return [], True
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], False
    if isinstance(raw, list):
        jobs = raw
    elif isinstance(raw, dict) and isinstance(raw.get("jobs"), list):
        jobs = raw["jobs"]
    else:
        return [], False
    if any(not isinstance(item, dict) for item in jobs):
        return [], False
    return jobs, True


def _legacy_launchagent_status(host_home: Path) -> tuple[int, int]:
    directory = host_home / "Library" / "LaunchAgents"
    count = 0
    unknown = 0
    if not directory.exists():
        return count, unknown
    try:
        candidates = list(directory.glob("*.plist"))
    except OSError:
        return 0, 1
    markers = [name.encode("utf-8") for name in _LEGACY_SCRIPT_NAMES]
    for path in candidates:
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                unknown += 1
                continue
            payload = path.read_bytes()
        except OSError:
            unknown += 1
            continue
        if any(marker in payload for marker in markers):
            count += 1
    return count, unknown


def legacy_writer_status(host_home: Path) -> dict[str, Any]:
    """Inspect documented Hermes cron state without returning job contents."""

    host_home = host_home.expanduser().resolve()
    scripts = host_home / ".hermes" / "scripts"
    artifact_count = sum((scripts / name).is_file() for name in _LEGACY_SCRIPT_NAMES)
    jobs, jobs_readable = _hermes_jobs(host_home / ".hermes" / "cron" / "jobs.json")
    matching = [job for job in jobs if _job_matches_legacy_writer(job)]
    active = sum(job.get("enabled") is True for job in matching)
    paused = sum(job.get("enabled") is False for job in matching)
    unknown = sum(type(job.get("enabled")) is not bool for job in matching)
    if not jobs_readable:
        unknown += 1
    launchagents, unknown_launchagents = _legacy_launchagent_status(host_home)
    unverified_artifacts = artifact_count > 0 and not matching
    unsafe = (
        active > 0
        or unknown > 0
        or launchagents > 0
        or unknown_launchagents > 0
        or unverified_artifacts
    )
    if active:
        state = "active"
    elif unknown or launchagents or unknown_launchagents:
        state = "unknown"
    elif unverified_artifacts:
        state = "unverified_artifacts"
    elif paused:
        state = "paused"
    else:
        state = "absent"
    return {
        "check": "legacy health writer",
        "ok": not unsafe,
        "required": unsafe,
        "severity": "error" if unsafe else "info",
        "detail": {
            "script_artifact_count": artifact_count,
            "matching_hermes_job_count": len(matching),
            "active_hermes_job_count": active,
            "paused_hermes_job_count": paused,
            "unknown_hermes_job_count": unknown,
            "legacy_launchagent_definition_count": launchagents,
            "unknown_legacy_launchagent_definition_count": unknown_launchagents,
            "active_writer_detected": active > 0 or launchagents > 0,
            "state": state,
        },
    }


def require_scheduler_install_convergence(
    config: Config,
    database: "HealthDatabase",
    writer_status: dict[str, Any],
) -> None:
    if pending_migration_status(database) is not None:
        raise RuntimeError(
            "scheduler install is blocked by a pending legacy migration; resume and verify its postconditions first"
        )
    audit_integrity = migration_audit_integrity_status(database)
    if audit_integrity is not None and audit_integrity.get("ok") is not True:
        raise RuntimeError(
            "scheduler install is blocked because the completed migration audit is invalid"
        )
    latest = database.list_sync_runs(limit=1)
    today = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    if (
        not latest
        or latest[0].get("status") != "success"
        or latest[0].get("trigger") != "manual"
        or latest[0].get("to_date") != today
    ):
        raise RuntimeError(
            "scheduler install requires the latest foreground run to be a full successful manual sync"
        )
    audit = latest_migration_audit(database)
    if audit is not None:
        if latest[0].get("migration_id") != audit.get("migration_id"):
            raise RuntimeError(
                "scheduler install requires a full successful manual sync after the latest completed migration"
            )
        workbook_status = workbook_convergence_status(config, database)
        freshness_status = legacy_workbook_freshness_status(database)
        if (
            workbook_status is None
            or workbook_status.get("ok") is not True
            or freshness_status is None
            or freshness_status.get("ok") is not True
        ):
            raise RuntimeError(
                "scheduler install requires verified workbook convergence and unchanged legacy health sheets"
            )
    detail = writer_status.get("detail")
    state = detail.get("state") if isinstance(detail, dict) else None
    if writer_status.get("ok") is not True or state not in {"absent", "paused"}:
        raise RuntimeError(
            "scheduler install requires the legacy health writer to be verified inactive"
        )
