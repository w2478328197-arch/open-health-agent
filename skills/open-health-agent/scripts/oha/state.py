from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


_BOOLEAN_FIELDS = {
    "workbook_export_pending",
    "workbook_export_blocked_by_consent",
    "profile_projection_pending",
    "scheduler_job_removal_pending",
    "keep_awake_job_removal_pending",
}
_OPTIONAL_TEXT_SUFFIXES = (
    "_status",
    "_batch_id",
    "_error_code",
    "_operation",
    "_field",
)


def _aware_timestamp(value: Any, now: datetime) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return parsed.astimezone(timezone.utc) <= now + timedelta(seconds=60)


def _fingerprint(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _consents_valid(value: Any, now: datetime) -> bool:
    if not isinstance(value, dict):
        return False
    for scope, raw_entry in value.items():
        if not isinstance(scope, str) or not scope or not isinstance(raw_entry, dict):
            return False
        policy_version = raw_entry.get("policy_version")
        if policy_version is not None and (
            isinstance(policy_version, bool)
            or not isinstance(policy_version, int)
            or policy_version != 1
        ):
            return False
        for field in ("granted_at", "consumed_at", "revoked_at"):
            timestamp = raw_entry.get(field)
            if timestamp is not None and not _aware_timestamp(timestamp, now):
                return False
        binding = raw_entry.get("resource_binding")
        if binding is not None and (
            not isinstance(binding, dict)
            or binding.get("version") != 1
            or not isinstance(binding.get("resource_type"), str)
            or not binding.get("resource_type")
            or not isinstance(binding.get("provider"), str)
            or not binding.get("provider")
            or not _fingerprint(binding.get("target_fingerprint"))
        ):
            return False
        authorization = raw_entry.get("installed_authorization")
        if authorization is None:
            continue
        if not isinstance(authorization, dict) or authorization.get("version") != 1:
            return False
        if not _aware_timestamp(authorization.get("authorized_at"), now):
            return False
        if not _fingerprint(authorization.get("static_runtime_fingerprint")):
            return False
        if not _fingerprint(authorization.get("runtime_fingerprint")):
            return False
    return True


def _manual_sync_evidence_valid(value: Any, now: datetime) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("version") != 2:
        return False
    for field in ("batch_id", "status"):
        if not isinstance(value.get(field), str) or not value[field]:
            return False
    for field in ("started_at", "finished_at"):
        timestamp = value.get(field)
        if timestamp is not None and not _aware_timestamp(timestamp, now):
            return False
    timezone_name = value.get("timezone")
    if timezone_name is not None and (
        not isinstance(timezone_name, str) or not timezone_name
    ):
        return False
    for field in ("static_runtime_fingerprint", "runtime_fingerprint"):
        fingerprint = value.get(field)
        if fingerprint is not None and not _fingerprint(fingerprint):
            return False
    return True


def state_validation_error(
    value: Any, *, now: datetime | None = None
) -> str | None:
    """Return a stable schema reason without exposing any private state value."""

    if not isinstance(value, dict):
        return "not_an_object"
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for key, field_value in value.items():
        if not isinstance(key, str) or not key:
            return "invalid_field_name"
        if key in _BOOLEAN_FIELDS and type(field_value) is not bool:
            return "invalid_boolean"
        if key.endswith(("_at", "_since")) and field_value is not None:
            if not _aware_timestamp(field_value, current):
                return "invalid_timestamp"
        if key.endswith(_OPTIONAL_TEXT_SUFFIXES) and field_value is not None:
            if not isinstance(field_value, str) or not field_value:
                return "invalid_text_field"
    if "onboarding_consents" in value and not _consents_valid(
        value["onboarding_consents"], current
    ):
        return "invalid_consents"
    if "last_manual_ghealth_sync" in value and not _manual_sync_evidence_valid(
        value["last_manual_ghealth_sync"], current
    ):
        return "invalid_manual_sync_evidence"
    return None
