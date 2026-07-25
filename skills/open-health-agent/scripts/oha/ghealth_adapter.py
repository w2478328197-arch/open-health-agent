from __future__ import annotations

import json
import hashlib
import math
import os
import re
import subprocess
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .database import stable_record_id, utc_now

MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_PAGES = 20


class GHealthError(RuntimeError):
    pass


def redact(text: str) -> str:
    cleaned = re.sub(
        r"(?i)(authorization\s*[:=]\s*[\"']?bearer\s+)[A-Za-z0-9._~+/-]+",
        r"\1[REDACTED]",
        text,
    )
    cleaned = re.sub(r"ya29\.[A-Za-z0-9._-]+", "[REDACTED_TOKEN]", cleaned)
    cleaned = re.sub(
        r"(?i)([\"']?(?:access_token|refresh_token|id_token|client_secret|authorization)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)",
        r"\1[REDACTED]",
        cleaned,
    )
    cleaned = re.sub(r"(?i)(code=)[^&\s]+", r"\1[REDACTED]", cleaned)
    return cleaned[:2000]


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def aware_datetime(value: Any, timezone_name: str | None = None) -> datetime | None:
    """Parse a provider timestamp into a comparable, UTC-aware datetime.

    Offset-less timestamps are interpreted in OHA's configured timezone.  The
    adapter never compares raw ISO strings because different offsets can sort in
    the opposite order from their actual instants.
    """

    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name or "UTC"))
    return parsed.astimezone(timezone.utc)


def point_cutoff(point: dict[str, Any], timezone_name: str | None = None) -> datetime | None:
    """Return the latest real timestamp carried by one provider point."""

    candidates: list[datetime] = []
    for key in (
        "time",
        "end",
        "endTime",
        "endDateTime",
        "start",
        "startTime",
        "startDateTime",
    ):
        parsed = aware_datetime(point.get(key), timezone_name)
        if parsed is not None:
            candidates.append(parsed)
    for interval_key in ("timeInterval", "time_interval"):
        interval = point.get(interval_key)
        if isinstance(interval, dict):
            nested = point_cutoff(interval, timezone_name)
            if nested is not None:
                candidates.append(nested)
    return max(candidates) if candidates else None


def fallback_day_cutoff(
    day: str,
    imported_at: datetime,
    timezone_name: str | None = None,
) -> datetime:
    """Give date-only rollups a conservative, per-day cutoff.

    Completed local days end at their local 23:59:59; today's date-only rollup
    is only current through import time.  A future provider date is anchored to
    local midnight instead of pretending that future data was observed.
    """

    zone = ZoneInfo(timezone_name or "UTC")
    local_import = imported_at.astimezone(zone)
    selected_day = date.fromisoformat(day)
    if selected_day < local_import.date():
        local_cutoff = datetime.combine(
            selected_day + timedelta(days=1), time.min, tzinfo=zone
        ) - timedelta(seconds=1)
    elif selected_day == local_import.date():
        local_cutoff = local_import
    else:
        local_cutoff = datetime.combine(selected_day, time.min, tzinfo=zone)
    return local_cutoff.astimezone(timezone.utc)


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def direct_date(
    point: dict[str, Any],
    prefer_end: bool = False,
    timezone_name: str | None = None,
) -> str | None:
    for key in ("date", "day", "localDate"):
        value = point.get(key)
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", value):
            return value[:10]
    primary = ("end", "endTime", "endDateTime", "time") if prefer_end else ("start", "startTime", "startDateTime", "time")
    for key in (*primary, "end", "endTime", "start", "startTime"):
        parsed = parse_datetime(point.get(key))
        if parsed:
            if timezone_name and parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo(timezone_name))
            return parsed.date().isoformat()
    interval = point.get("timeInterval") or point.get("time_interval")
    if isinstance(interval, dict):
        return direct_date(interval, prefer_end=prefer_end, timezone_name=timezone_name)
    return None


def source_name(point: dict[str, Any]) -> str:
    for key in ("source", "sourceId", "dataSource", "application"):
        value = point.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for subkey in ("packageName", "package_name", "name", "id"):
                nested = value.get(subkey)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def explicit(point: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = point
        valid = True
        for segment in path.split("."):
            if not isinstance(current, dict) or segment not in current:
                valid = False
                break
            current = current[segment]
        if valid and current is not None:
            return current
    return None


def duration_seconds(value: Any) -> float | None:
    if isinstance(value, str) and value.endswith("s"):
        return number(value[:-1])
    return number(value)


def active_minutes_value(point: dict[str, Any]) -> float | None:
    """Read the explicit daily-rollup form without guessing at arbitrary numbers."""
    direct = number(explicit(point, "activeMinutesSum", "activeMinutes"))
    if direct is not None:
        return direct
    levels = explicit(point, "activeMinutesByActivityLevel")
    if not isinstance(levels, list):
        return None
    values: list[float] = []
    for item in levels:
        if not isinstance(item, dict):
            continue
        value = number(item.get("activeMinutes"))
        if value is not None:
            values.append(value)
    return sum(values) if values else None


def points(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("dataPoints", "rollupDataPoints"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


@dataclass(frozen=True)
class QuerySpec:
    key: str
    data_type: str
    operation: str
    detail: bool = False
    page_size: int | None = None
    max_days: int | None = None
    use_date_range: bool = True
    include_to: bool = True
    grain: str = "semantic"


QUERY_SPECS = (
    QuerySpec("steps", "steps", "daily-rollup", max_days=90),
    QuerySpec("distance", "distance", "daily-rollup", max_days=90),
    QuerySpec(
        "active_energy", "active-energy-burned", "daily-rollup", max_days=90
    ),
    QuerySpec("active_minutes", "active-minutes", "daily-rollup", max_days=14),
    QuerySpec("rhr", "daily-resting-heart-rate", "list", page_size=10_000),
    QuerySpec(
        "hrv", "daily-heart-rate-variability", "list", page_size=10_000
    ),
    QuerySpec("spo2", "daily-oxygen-saturation", "list", page_size=10_000),
    QuerySpec(
        "respiratory", "daily-respiratory-rate", "list", page_size=10_000
    ),
    QuerySpec("vo2", "daily-vo2-max", "list", page_size=10_000),
    QuerySpec("weight", "weight", "list", page_size=10_000),
    QuerySpec("body_fat", "body-fat", "list", page_size=10_000),
    QuerySpec("height", "height", "list", page_size=10_000),
    QuerySpec("sleep", "sleep", "list", True, page_size=25),
    QuerySpec("exercise", "exercise", "list", page_size=25),
)


# Full, lossless-at-the-ghealth-JSON-layer capture.  The 14 semantic queries
# above still feed the stable daily/measurement/workout views.  These specs
# independently preserve every personal or reference data type exposed by the
# pinned ghealth registry.  For interval types whose list response omits the
# metric value (steps, distance, swim lengths), retain both the interval stream
# and a daily value series. Active energy also retains both its granular stream
# and the daily series used by the semantic layer.
CAPTURE_SPECS = (
    QuerySpec("capture_steps_intervals", "steps", "list", page_size=10_000, grain="interval"),
    QuerySpec("capture_steps_daily", "steps", "daily-rollup", max_days=90, grain="daily"),
    QuerySpec("capture_heart_rate", "heart-rate", "list", page_size=10_000, grain="sample"),
    QuerySpec("capture_exercise", "exercise", "list", page_size=25, grain="session"),
    QuerySpec("capture_sleep", "sleep", "list", True, page_size=25, grain="session"),
    QuerySpec("capture_weight", "weight", "list", page_size=10_000, grain="sample"),
    QuerySpec("capture_body_fat", "body-fat", "list", page_size=10_000, grain="sample"),
    QuerySpec("capture_height", "height", "list", page_size=10_000, grain="sample"),
    QuerySpec("capture_distance_intervals", "distance", "list", page_size=10_000, grain="interval"),
    QuerySpec("capture_distance_daily", "distance", "daily-rollup", max_days=90, grain="daily"),
    QuerySpec(
        "capture_hrv",
        "heart-rate-variability",
        "list",
        page_size=10_000,
        grain="sample",
    ),
    QuerySpec(
        "capture_oxygen_saturation",
        "oxygen-saturation",
        "list",
        page_size=10_000,
        grain="sample",
    ),
    QuerySpec("capture_altitude", "altitude", "list", page_size=10_000, grain="interval"),
    QuerySpec(
        "capture_active_zone_minutes",
        "active-zone-minutes",
        "list",
        page_size=10_000,
        grain="interval",
    ),
    QuerySpec(
        "capture_activity_level",
        "activity-level",
        "list",
        page_size=10_000,
        grain="interval",
    ),
    QuerySpec(
        "capture_basal_energy",
        "basal-energy-burned",
        "list",
        page_size=10_000,
        grain="interval",
    ),
    QuerySpec(
        "capture_active_energy",
        "active-energy-burned",
        "list",
        page_size=10_000,
        grain="interval",
    ),
    QuerySpec(
        "capture_active_energy_daily",
        "active-energy-burned",
        "daily-rollup",
        max_days=90,
        grain="daily",
    ),
    QuerySpec("capture_vo2_max", "vo2-max", "list", page_size=10_000, grain="sample"),
    QuerySpec(
        "capture_total_calories",
        "total-calories",
        "daily-rollup",
        max_days=14,
        grain="daily",
    ),
    QuerySpec(
        "capture_sedentary_period",
        "sedentary-period",
        "list",
        page_size=10_000,
        grain="interval",
    ),
    QuerySpec(
        "capture_swim_intervals",
        "swim-lengths-data",
        "list",
        page_size=10_000,
        grain="interval",
    ),
    QuerySpec(
        "capture_swim_daily",
        "swim-lengths-data",
        "daily-rollup",
        max_days=90,
        grain="daily",
    ),
    QuerySpec(
        "capture_hydration",
        "hydration-log",
        "list",
        page_size=10_000,
        grain="event",
    ),
    QuerySpec(
        "capture_nutrition",
        "nutrition-log",
        "list",
        page_size=10_000,
        grain="event",
    ),
    QuerySpec(
        "capture_food_catalog",
        "food",
        "list",
        page_size=10_000,
        use_date_range=False,
        grain="reference",
    ),
    QuerySpec(
        "capture_food_units",
        "food-measurement-unit",
        "list",
        page_size=10_000,
        use_date_range=False,
        grain="reference",
    ),
    QuerySpec(
        "capture_blood_glucose",
        "blood-glucose",
        "list",
        page_size=10_000,
        grain="sample",
    ),
    QuerySpec(
        "capture_core_temperature",
        "core-body-temperature",
        "list",
        page_size=10_000,
        grain="sample",
    ),
    QuerySpec(
        "capture_ecg",
        "electrocardiogram",
        "list",
        page_size=25,
        include_to=False,
        grain="waveform",
    ),
    QuerySpec(
        "capture_irregular_rhythm",
        "irregular-rhythm-notification",
        "list",
        page_size=10_000,
        grain="alert",
    ),
    QuerySpec(
        "capture_daily_resting_hr",
        "daily-resting-heart-rate",
        "list",
        page_size=10_000,
        grain="daily",
    ),
    QuerySpec(
        "capture_daily_hrv",
        "daily-heart-rate-variability",
        "list",
        page_size=10_000,
        grain="daily",
    ),
    QuerySpec(
        "capture_daily_spo2",
        "daily-oxygen-saturation",
        "list",
        page_size=10_000,
        grain="daily",
    ),
    QuerySpec(
        "capture_daily_respiratory",
        "daily-respiratory-rate",
        "list",
        page_size=10_000,
        grain="daily",
    ),
    QuerySpec(
        "capture_daily_vo2",
        "daily-vo2-max",
        "list",
        page_size=10_000,
        grain="daily",
    ),
    QuerySpec(
        "capture_daily_sleep_temperature",
        "daily-sleep-temperature-derivations",
        "list",
        page_size=10_000,
        grain="daily",
    ),
    QuerySpec(
        "capture_sleep_respiratory",
        "respiratory-rate-sleep-summary",
        "list",
        page_size=10_000,
        grain="sample",
    ),
    QuerySpec(
        "capture_run_vo2",
        "run-vo2-max",
        "list",
        page_size=10_000,
        grain="sample",
    ),
    QuerySpec("capture_floors", "floors", "daily-rollup", max_days=90, grain="daily"),
    QuerySpec(
        "capture_active_minutes",
        "active-minutes",
        "daily-rollup",
        max_days=14,
        grain="daily",
    ),
    QuerySpec(
        "capture_time_in_hr_zone",
        "time-in-heart-rate-zone",
        "daily-rollup",
        max_days=90,
        grain="daily",
    ),
    QuerySpec(
        "capture_calories_in_hr_zone",
        "calories-in-heart-rate-zone",
        "daily-rollup",
        max_days=14,
        grain="daily",
    ),
    QuerySpec(
        "capture_daily_hr_zones",
        "daily-heart-rate-zones",
        "reconcile",
        grain="daily",
    ),
)

SUPPORTED_CAPTURE_DATA_TYPES = frozenset(spec.data_type for spec in CAPTURE_SPECS)


# A failed ghealth query may affect only a subset of the composite daily row.
# Keep this mapping close to the query definitions so partial imports can retain
# exactly those prior fields, rather than merging every old field back into the
# row.  This deliberately uses the existing daily-record schema.
DAILY_FIELDS_BY_QUERY: dict[str, tuple[str, ...]] = {
    "steps": ("steps",),
    "distance": ("distance_km",),
    "active_energy": ("active_energy_kcal",),
    "active_minutes": ("active_minutes",),
    "rhr": ("resting_heart_rate_bpm",),
    "hrv": ("hrv_ms",),
    "spo2": ("spo2_percent",),
    "respiratory": ("respiratory_rate",),
    "vo2": ("vo2max",),
    "weight": ("weight_kg",),
    "body_fat": ("body_fat_percent",),
    "height": ("height_cm",),
    "sleep": (
        "sleep_start",
        "sleep_end",
        "sleep_hours",
        "deep_minutes",
        "light_minutes",
        "rem_minutes",
        "awake_minutes",
        "sleep_source",
    ),
}


class CommandRunner:
    def __init__(
        self, command: str, timeout: int = 90, *, profile: str | None = None
    ):
        self.command = command
        self.timeout = timeout
        self.profile = profile

    def run(self, arguments: list[str]) -> dict[str, Any]:
        environment = os.environ.copy()
        # ghealth can be configured to print tables or CSV. The adapter accepts
        # only JSON, so make the machine interface deterministic in foreground
        # shells as well as scheduled jobs.
        environment["GHEALTH_FORMAT"] = "json"
        if self.profile is not None:
            environment["GHEALTH_PROFILE"] = self.profile
        try:
            process = subprocess.Popen(
                [self.command, *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise GHealthError("ghealth command not found; run doctor and reinstall or update the configured executable") from exc

        buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        total_seen = 0
        lock = threading.Lock()
        exceeded = threading.Event()

        def drain(name: str, stream: Any) -> None:
            nonlocal total_seen
            try:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        return
                    with lock:
                        remaining = max(MAX_RESPONSE_BYTES - total_seen, 0)
                        if remaining:
                            buffers[name].extend(chunk[:remaining])
                        total_seen += len(chunk)
                        if total_seen > MAX_RESPONSE_BYTES:
                            exceeded.set()
                            try:
                                process.kill()
                            except OSError:
                                pass
                            return
            finally:
                stream.close()

        stdout_thread = threading.Thread(
            target=drain, args=("stdout", process.stdout), daemon=True
        )
        stderr_thread = threading.Thread(
            target=drain, args=("stderr", process.stderr), daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            return_code = process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            stdout_thread.join()
            stderr_thread.join()
            raise GHealthError(f"ghealth timed out after {self.timeout}s") from exc
        stdout_thread.join()
        stderr_thread.join()

        if exceeded.is_set():
            raise GHealthError("ghealth response exceeded the local size limit")

        stdout_bytes = bytes(buffers["stdout"])
        stderr_bytes = bytes(buffers["stderr"])
        stdout_size = len(stdout_bytes)
        stderr_size = len(stderr_bytes)
        diagnostic = stderr_bytes or stdout_bytes
        digest = hashlib.sha256(diagnostic).hexdigest()[:16] if diagnostic else "empty"
        if return_code != 0:
            lower = diagnostic.decode("utf-8", errors="replace").lower()
            category = (
                "unsupported_flag"
                if "unknown flag" in lower or "flag provided but not defined" in lower
                else "command_failed"
            )
            raise GHealthError(
                f"ghealth exit {return_code}; category={category}; "
                f"stdout_bytes={stdout_size}; stderr_bytes={stderr_size}; diagnostic_sha256={digest}"
            )
        try:
            parsed = json.loads(stdout_bytes.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError as exc:
            raise GHealthError(
                f"ghealth returned invalid JSON; stdout_bytes={stdout_size}; diagnostic_sha256={digest}"
            ) from exc
        if not isinstance(parsed, dict):
            raise GHealthError("ghealth returned a non-object JSON payload")
        return parsed


class FixtureRunner:
    """Read synthetic JSON fixtures instead of invoking ghealth."""

    def __init__(self, directory: Path):
        self.directory = directory

    def run(self, arguments: list[str]) -> dict[str, Any]:
        if arguments[:2] == ["auth", "status"]:
            candidate = self.directory / "auth-status.json"
        else:
            try:
                data_type = arguments[arguments.index("data") + 1]
            except (ValueError, IndexError) as exc:
                raise GHealthError(f"unsupported fixture command: {' '.join(arguments)}") from exc
            candidate = self.directory / f"{data_type}.json"
        if not candidate.exists():
            return {"dataPoints": []}
        parsed = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise GHealthError(f"fixture must contain an object: {candidate}")
        return parsed


class GHealthAdapter:
    def __init__(
        self,
        runner: CommandRunner | FixtureRunner,
        sleep_source_priority: list[str] | None = None,
        timezone_name: str | None = None,
    ):
        self.runner = runner
        self.sleep_source_priority = sleep_source_priority or []
        self.timezone_name = timezone_name

    def auth_status(self) -> dict[str, Any]:
        try:
            status = self.runner.run(["auth", "status", "--validate"])
        except GHealthError as exc:
            if "category=unsupported_flag" not in str(exc).lower():
                raise
            status = self.runner.run(["auth", "status"])
        authenticated = status.get("authenticated")
        expired = status.get("expired")
        # Missing, string-valued, or otherwise ambiguous authentication state
        # is not proof of a usable session.  Only the literal JSON boolean true
        # is accepted.
        if authenticated is not True or expired is True:
            raise GHealthError("ghealth authentication is not currently usable; run ghealth setup or ghealth auth login")
        # Account identifiers and unknown provider fields must not leak through
        # `doctor` or get copied into support logs.
        return {
            "authenticated": authenticated if isinstance(authenticated, bool) else None,
            "expired": expired if isinstance(expired, bool) else None,
            "scope_count": len(status.get("scopes")) if isinstance(status.get("scopes"), list) else None,
        }

    def timezone_status(self) -> dict[str, Any]:
        """Compare OHA's day boundary with ghealth's active profile, without changing it.

        ghealth selects a profile using its own ``--profile``/``GHEALTH_PROFILE``
        rules. Asking ghealth which profile is active is safer than reading its TOML
        file directly, and projecting only the timezone avoids exposing project IDs,
        scopes, or profile names through ``doctor`` output.
        """
        if not self.timezone_name:
            raise GHealthError("Open Health Agent has no configured IANA timezone")

        profiles_payload = self.runner.run(
            ["config", "profiles", "list", "--format", "json"]
        )
        profiles = profiles_payload.get("profiles")
        if not isinstance(profiles, list):
            raise GHealthError(
                "ghealth did not return its active profile; reinstall the pinned ghealth version and run doctor again"
            )

        pinned_profile = getattr(self.runner, "profile", None)
        if isinstance(pinned_profile, str) and pinned_profile:
            active_name = pinned_profile
            active_type = "default" if pinned_profile == "default" else "custom"
        else:
            active_name = "default"
            active_type = "default"
            for entry in profiles:
                if not isinstance(entry, dict) or entry.get("active") is not True:
                    continue
                name = entry.get("name")
                if isinstance(name, str) and name:
                    active_name = name
                    active_type = "default" if name == "default" else "custom"
                break

        config_payload = self.runner.run(["config", "show", "--format", "json"])
        default_profile = config_payload.get("Default", config_payload.get("default", {}))
        named_profiles = config_payload.get("Profiles", config_payload.get("profiles", {}))
        if active_name == "default":
            selected = default_profile
        elif isinstance(named_profiles, dict) and active_name in named_profiles:
            selected = named_profiles[active_name]
        else:
            # This mirrors ghealth's own ActiveProfile fallback when an environment
            # variable names a profile that does not exist.
            selected = default_profile
            active_type = "default-fallback"

        if not isinstance(selected, dict):
            raise GHealthError(
                "ghealth active profile configuration is unreadable; run ghealth config show locally and repair it"
            )
        configured = selected.get("timezone", selected.get("Timezone"))
        configured_timezone = configured.strip() if isinstance(configured, str) and configured.strip() else None
        return {
            "open_health_agent_timezone": self.timezone_name,
            "ghealth_timezone": configured_timezone,
            "matches": configured_timezone == self.timezone_name,
            "active_profile_type": active_type,
        }

    def require_matching_timezone(self) -> dict[str, Any]:
        status = self.timezone_status()
        if status["matches"]:
            return status
        actual = status["ghealth_timezone"] or "not explicitly configured"
        raise GHealthError(
            "ghealth timezone does not match Open Health Agent: "
            f"expected {self.timezone_name}, found {actual}; run "
            f"ghealth config set timezone {self.timezone_name} for the active profile, then run doctor again"
        )

    @staticmethod
    def _date_windows(
        start: str, end: str, max_days: int | None
    ) -> list[tuple[str, str]]:
        if max_days is None:
            return [(start, end)]
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
        if last < first:
            raise GHealthError("ghealth query end date is before its start date")
        windows: list[tuple[str, str]] = []
        cursor = first
        while cursor <= last:
            window_end = min(cursor + timedelta(days=max_days - 1), last)
            windows.append((cursor.isoformat(), window_end.isoformat()))
            cursor = window_end + timedelta(days=1)
        return windows

    def _query_window(
        self, spec: QuerySpec, start: str, end: str
    ) -> list[dict[str, Any]]:
        base = ["data", spec.data_type, spec.operation]
        if spec.use_date_range:
            base.extend(["--from", start])
            if spec.include_to:
                base.extend(["--to", end])
        if spec.detail:
            base.append("--detail")
        if spec.operation == "list":
            base.extend(["--limit", str(spec.page_size or 10_000)])
        collected: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            arguments = list(base)
            if page_token:
                arguments.extend(["--page-token", page_token])
            payload = self.runner.run(arguments)
            collected.extend(points(payload))
            next_token = payload.get("nextPageToken")
            if (
                not isinstance(next_token, str)
                or not next_token
                or spec.operation != "list"
            ):
                break
            if next_token == page_token:
                raise GHealthError(f"pagination token repeated for {spec.data_type}")
            page_token = next_token
        else:
            raise GHealthError(
                f"pagination exceeded {MAX_PAGES} pages for {spec.data_type}"
            )
        if spec.use_date_range and not spec.include_to:
            # ECG accepts only a lower-bound provider filter. Enforce the
            # caller's upper bound locally so a historical sync cannot import
            # observations outside its requested range.
            collected = [
                point
                for point in collected
                if (direct_date(point, timezone_name=self.timezone_name) or end) <= end
            ]
        return collected

    def query(self, spec: QuerySpec, start: str, end: str) -> list[dict[str, Any]]:
        if not spec.use_date_range:
            return self._query_window(spec, start, end)
        collected: list[dict[str, Any]] = []
        for window_start, window_end in self._date_windows(
            start, end, spec.max_days
        ):
            collected.extend(self._query_window(spec, window_start, window_end))
        return collected

    def fetch(self, start: str, end: str, batch_id: str) -> dict[str, Any]:
        self.auth_status()
        raw: dict[str, list[dict[str, Any]]] = {}
        errors: list[str] = []
        failed_query_keys: list[str] = []
        cache: dict[
            tuple[str, str, bool, int | None, int | None, bool, bool],
            list[dict[str, Any]] | GHealthError,
        ] = {}

        def cached_query(spec: QuerySpec) -> list[dict[str, Any]]:
            cache_key = (
                spec.data_type,
                spec.operation,
                spec.detail,
                spec.page_size,
                spec.max_days,
                spec.use_date_range,
                spec.include_to,
            )
            cached = cache.get(cache_key)
            if isinstance(cached, GHealthError):
                raise cached
            if cached is not None:
                return cached
            try:
                queried = self.query(spec, start, end)
            except GHealthError as exc:
                cache[cache_key] = exc
                raise
            cache[cache_key] = queried
            return queried

        for spec in QUERY_SPECS:
            try:
                raw[spec.key] = cached_query(spec)
            except GHealthError as exc:
                raw[spec.key] = []
                failed_query_keys.append(spec.key)
                errors.append(f"{spec.key}: {redact(str(exc))}")
        captured: list[tuple[QuerySpec, list[dict[str, Any]]]] = []
        capture_errors: list[str] = []
        capture_outcomes: dict[str, list[tuple[QuerySpec, bool, int]]] = defaultdict(
            list
        )
        for spec in CAPTURE_SPECS:
            try:
                rows = cached_query(spec)
                captured.append((spec, rows))
                capture_outcomes[spec.data_type].append((spec, True, len(rows)))
            except GHealthError as exc:
                capture_errors.append(f"{spec.key}: {redact(str(exc))}")
                capture_outcomes[spec.data_type].append((spec, False, 0))
        normalized = normalize_payloads(
            raw,
            batch_id,
            self.sleep_source_priority,
            self.timezone_name,
        )
        wearable, wearable_data_until = normalize_wearable_payloads(
            captured, batch_id, self.timezone_name
        )
        normalized["wearable"] = wearable
        normalized["wearable_data_until"] = wearable_data_until
        combined_cutoffs = [
            cutoff
            for value in (normalized.get("data_until"), wearable_data_until)
            if (cutoff := aware_datetime(value, self.timezone_name)) is not None
        ]
        normalized["data_until"] = (
            utc_iso(max(combined_cutoffs)) if combined_cutoffs else None
        )
        coverage_imported_at = utc_now()
        capture_coverage: list[dict[str, Any]] = []
        for data_type in sorted(SUPPORTED_CAPTURE_DATA_TYPES):
            outcomes = capture_outcomes[data_type]
            successes = sum(1 for _, succeeded, _ in outcomes if succeeded)
            status = (
                "success"
                if successes == len(outcomes)
                else "partial"
                if successes
                else "failed"
            )
            capture_coverage.append(
                {
                    "record_id": stable_record_id(
                        "wearable_coverage", [data_type]
                    ),
                    "data_type": data_type,
                    "operations": sorted({spec.operation for spec, _, _ in outcomes}),
                    "grains": sorted({spec.grain for spec, _, _ in outcomes}),
                    "status": status,
                    "query_count": len(outcomes),
                    "successful_query_count": successes,
                    "failed_query_count": len(outcomes) - successes,
                    "imported_record_count": sum(
                        count for _, succeeded, count in outcomes if succeeded
                    ),
                    "last_checked_at": coverage_imported_at,
                    "batch_id": batch_id,
                    "imported_at": coverage_imported_at,
                }
            )
        normalized["wearable_coverage"] = capture_coverage
        normalized["capture_type_count"] = len(SUPPORTED_CAPTURE_DATA_TYPES)
        normalized["successful_capture_type_count"] = sum(
            1 for row in capture_coverage if row["status"] == "success"
        )
        normalized["capture_query_count"] = len(CAPTURE_SPECS)
        normalized["capture_errors"] = capture_errors
        normalized["errors"] = list(dict.fromkeys([*errors, *capture_errors]))
        normalized["failed_query_keys"] = failed_query_keys
        return normalized


def choose_latest(
    points_for_day: list[dict[str, Any]],
    timezone_name: str | None = None,
) -> dict[str, Any] | None:
    if not points_for_day:
        return None

    minimum = datetime.min.replace(tzinfo=timezone.utc)

    def key(point: dict[str, Any]) -> tuple[datetime, str]:
        return point_cutoff(point, timezone_name) or minimum, source_name(point)

    return sorted(points_for_day, key=key)[-1]


def merge_partial_daily(
    current: dict[str, Any],
    previous: dict[str, Any] | None,
    failed_query_keys: list[str] | tuple[str, ...] | set[str],
) -> dict[str, Any]:
    """Retain only prior daily fields whose own query failed.

    The daily record has one aggregate ``sources`` column rather than a
    field-level source map.  When any stale value is retained, union the prior
    aggregate source labels with the current ones and make the ambiguity
    explicit in ``quality``.  No new schema fields are introduced.
    """

    output = dict(current)
    failed_daily_keys = sorted(
        {key for key in failed_query_keys if key in DAILY_FIELDS_BY_QUERY}
    )
    if not failed_daily_keys:
        return output

    stale_fields: list[str] = []
    prior = previous or {}
    for query_key in failed_daily_keys:
        for field in DAILY_FIELDS_BY_QUERY[query_key]:
            if field in prior and prior[field] not in (None, ""):
                output[field] = prior[field]
                stale_fields.append(field)

    base_quality = str(
        output.get("quality") or "automatic ghealth import; blanks mean unavailable"
    ).split("; partial; failed daily queries:", 1)[0]
    # Repeated hourly partial imports must replace, rather than indefinitely
    # append, the prior partial annotation.
    base_quality = base_quality.split("; partial;", 1)[0]
    details = [f"partial; failed daily queries: {', '.join(failed_daily_keys)}"]
    if stale_fields:
        details.append(
            "stale fields retained from prior sync: "
            + ", ".join(sorted(set(stale_fields)))
        )
        current_sources = {
            value.strip()
            for value in str(output.get("sources") or "").split(",")
            if value.strip()
        }
        prior_sources = {
            value.strip()
            for value in str(prior.get("sources") or "").split(",")
            if value.strip()
        }
        output["sources"] = ", ".join(sorted(current_sources | prior_sources))
        if not output.get("data_until") and prior.get("data_until"):
            output["data_until"] = prior["data_until"]
    else:
        details.append("no prior value available for failed queries")
    output["quality"] = f"{base_quality}; {'; '.join(details)}"
    return output


def normalize_wearable_payloads(
    captured: list[tuple[QuerySpec, list[dict[str, Any]]]],
    batch_id: str,
    timezone_name: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Preserve ghealth's typed JSON while adding stable ledger identity.

    These records are the comprehensive capture layer.  They are deliberately
    separate from the smaller semantic views used for decisions, and the
    provider payload is not projected into Agent context or Excel.
    """

    imported_at = utc_now()
    imported_datetime = aware_datetime(imported_at, timezone_name)
    if imported_datetime is None:
        imported_datetime = datetime.now(timezone.utc)
    records: list[dict[str, Any]] = []
    cutoffs: list[datetime] = []

    for spec, rows in captured:
        for point in rows:
            day = direct_date(
                point,
                prefer_end=spec.data_type == "sleep",
                timezone_name=timezone_name,
            )
            source = source_name(point)
            external_id = explicit(point, "id", "name", "externalId")
            timestamp = next(
                (
                    point.get(key)
                    for key in ("time", "start", "startTime", "startDateTime")
                    if isinstance(point.get(key), str)
                ),
                None,
            )
            end_time = next(
                (
                    point.get(key)
                    for key in ("end", "endTime", "endDateTime")
                    if isinstance(point.get(key), str)
                ),
                None,
            )
            if external_id:
                identity: list[Any] = [
                    spec.data_type,
                    spec.operation,
                    str(external_id),
                ]
            else:
                # Date/time and source distinguish parallel streams. The
                # canonical point is the final fallback for untimed catalogs
                # and records that expose no temporal identity. Keeping values
                # out of a timed identity lets a provider correction update the
                # existing row instead of creating a contradictory duplicate.
                identity = [
                    spec.data_type,
                    spec.operation,
                    source or "unknown-source",
                    day or "",
                    timestamp or "",
                    end_time or "",
                ]
                if not any((day, timestamp, end_time)):
                    identity.append(
                        json.dumps(
                            point,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
            record_id = stable_record_id("wearable", identity)
            cutoff = point_cutoff(point, timezone_name)
            if cutoff is None and day:
                cutoff = fallback_day_cutoff(
                    day, imported_datetime, timezone_name
                )
            if cutoff is not None:
                cutoffs.append(cutoff)
            records.append(
                {
                    "record_id": record_id,
                    "date": day,
                    "data_type": spec.data_type,
                    "operation": spec.operation,
                    "grain": spec.grain,
                    "time": timestamp,
                    "end_time": end_time,
                    "source": source,
                    "external_id": str(external_id or ""),
                    "data_until": utc_iso(cutoff),
                    "provider_data": point,
                    "batch_id": batch_id,
                    "imported_at": imported_at,
                }
            )

    records.sort(
        key=lambda row: (
            str(row.get("data_type") or ""),
            str(row.get("operation") or ""),
            str(row.get("date") or ""),
            str(row.get("time") or ""),
            str(row.get("record_id") or ""),
        )
    )
    return records, utc_iso(max(cutoffs)) if cutoffs else None


def normalize_payloads(
    raw: dict[str, list[dict[str, Any]]],
    batch_id: str,
    sleep_source_priority: list[str] | None = None,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    imported_at = utc_now()
    imported_datetime = aware_datetime(imported_at, timezone_name)
    if imported_datetime is None:  # Defensive: utc_now is always valid ISO.
        imported_datetime = datetime.now(timezone.utc)
    daily: dict[str, dict[str, Any]] = {}
    daily_cutoffs: dict[str, list[datetime]] = defaultdict(list)
    event_cutoffs: list[datetime] = []

    def cutoff_for(day: str, point: dict[str, Any]) -> datetime:
        return point_cutoff(point, timezone_name) or fallback_day_cutoff(
            day, imported_datetime, timezone_name
        )

    def note_daily_cutoff(day: str, point: dict[str, Any]) -> None:
        cutoff = cutoff_for(day, point)
        daily_cutoffs[day].append(cutoff)
        event_cutoffs.append(cutoff)

    def note_event_cutoff(day: str, point: dict[str, Any]) -> None:
        event_cutoffs.append(cutoff_for(day, point))

    def day_row(day: str) -> dict[str, Any]:
        return daily.setdefault(
            day,
            {
                "record_id": day,
                "date": day,
                "sources": [],
                "quality": "automatic ghealth import; blanks mean unavailable",
                "batch_id": batch_id,
                "imported_at": imported_at,
            },
        )

    metric_specs = {
        "steps": ("steps", ("countSum",), 1.0),
        "distance": ("distance_km", ("millimetersSum",), 1_000_000.0),
        "active_energy": ("active_energy_kcal", ("kcalSum",), 1.0),
        "rhr": ("resting_heart_rate_bpm", ("beatsPerMinute",), 1.0),
        "hrv": ("hrv_ms", ("averageHeartRateVariabilityMilliseconds",), 1.0),
        "spo2": ("spo2_percent", ("averagePercentage",), 1.0),
        "respiratory": ("respiratory_rate", ("breathsPerMinute",), 1.0),
        "vo2": (
            "vo2max",
            (
                "vo2Max",
                "vo2MaxMillilitersPerKilogramPerMinute",
                "millilitersPerKilogramPerMinute",
            ),
            1.0,
        ),
    }
    for key, (target, paths, divisor) in metric_specs.items():
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for point in raw.get(key, []):
            day = direct_date(point, timezone_name=timezone_name)
            if day:
                grouped[day].append(point)
        for day, candidates in grouped.items():
            chosen = choose_latest(candidates, timezone_name)
            if not chosen:
                continue
            value = number(explicit(chosen, *paths))
            if value is None:
                continue
            row = day_row(day)
            row[target] = round(value / divisor, 4)
            source = source_name(chosen)
            if source and source not in row["sources"]:
                row["sources"].append(source)
            note_daily_cutoff(day, chosen)

    grouped_active_minutes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in raw.get("active_minutes", []):
        day = direct_date(point, timezone_name=timezone_name)
        if day:
            grouped_active_minutes[day].append(point)
    for day, candidates in grouped_active_minutes.items():
        chosen = choose_latest(candidates, timezone_name)
        value = active_minutes_value(chosen or {})
        if chosen is None or value is None:
            continue
        row = day_row(day)
        row["active_minutes"] = round(value, 4)
        source = source_name(chosen)
        if source and source not in row["sources"]:
            row["sources"].append(source)
        note_daily_cutoff(day, chosen)

    measurements: list[dict[str, Any]] = []
    measurement_specs = {
        "weight": ("体重", ("weightGrams",), 1000.0, "kg", "weight_kg"),
        "body_fat": ("体脂率", ("percentage",), 1.0, "%", "body_fat_percent"),
        "height": ("身高", ("heightMillimeters",), 10.0, "cm", "height_cm"),
    }
    for key, (metric, paths, divisor, unit, daily_key) in measurement_specs.items():
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for point in raw.get(key, []):
            day = direct_date(point, timezone_name=timezone_name)
            value = number(explicit(point, *paths))
            if not day or value is None:
                continue
            standardized = round(value / divisor, 4)
            source = source_name(point)
            timestamp = next((point.get(name) for name in ("time", "end", "start") if isinstance(point.get(name), str)), "")
            external_id = explicit(point, "id", "name")
            if external_id:
                # A provider-issued ID is the immutable identity. Source labels
                # are mutable metadata (for example after an app rename or
                # migration) and must not turn a correction into a duplicate.
                # Keep the metric namespace because measurement APIs may reuse
                # an identifier across data types.
                identity = [metric, str(external_id)]
            else:
                # Timestamp is the best available source identity when the API
                # omits an ID. Include value only as a last-resort discriminator
                # for multiple same-day, untimed measurements.
                identity = [
                    source or "unknown-source",
                    metric,
                    day,
                    timestamp or f"untimed:{standardized}:{unit}",
                ]
            record_id = stable_record_id("measurement", identity)
            measurements.append(
                {
                    "record_id": record_id,
                    "date": day,
                    "time": timestamp,
                    "metric": metric,
                    "value": standardized,
                    "second_value": None,
                    "unit": unit,
                    "source": source,
                    "method": "ghealth automatic import",
                    "confidence": "source reported",
                    "original_text": "",
                    "notes": "",
                    "imported_at": imported_at,
                }
            )
            grouped[day].append(point | {"__standardized": standardized})
            note_event_cutoff(day, point)
        for day, candidates in grouped.items():
            chosen = choose_latest(candidates, timezone_name)
            if not chosen:
                continue
            row = day_row(day)
            row[daily_key] = chosen["__standardized"]
            source = source_name(chosen)
            if source and source not in row["sources"]:
                row["sources"].append(source)
            note_daily_cutoff(day, chosen)

    sleep_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in raw.get("sleep", []):
        day = direct_date(point, prefer_end=True, timezone_name=timezone_name)
        if day:
            sleep_by_day[day].append(point)

    priorities = sleep_source_priority or []

    def sleep_score(point: dict[str, Any]) -> tuple[int, int, float]:
        source = source_name(point)
        priority = next((index for index, prefix in enumerate(priorities) if source.startswith(prefix)), len(priorities))
        stage = explicit(point, "stageMinutes")
        completeness = sum(1 for name in ("DEEP", "LIGHT", "CORE", "REM", "AWAKE") if isinstance(stage, dict) and number(stage.get(name)) is not None)
        total = number(explicit(point, "totalMinutes"))
        if total is None:
            asleep = number(explicit(point, "minutesAsleep")) or 0
            awake = number(explicit(point, "minutesAwake")) or 0
            total = asleep + awake
        return priority, -completeness, -(total or 0)

    for day, candidates in sleep_by_day.items():
        chosen = sorted(candidates, key=sleep_score)[0]
        stage = explicit(chosen, "stageMinutes")
        stage = stage if isinstance(stage, dict) else {}
        deep = number(stage.get("DEEP"))
        light = number(stage.get("LIGHT"))
        if light is None:
            light = number(stage.get("CORE"))
        rem = number(stage.get("REM"))
        awake = number(stage.get("AWAKE"))
        if awake is None:
            awake = number(explicit(chosen, "minutesAwake"))
        asleep = number(explicit(chosen, "minutesAsleep"))
        components = [value for value in (deep, light, rem) if value is not None]
        if asleep is None and components:
            asleep = sum(components)
        if asleep is None:
            total = number(explicit(chosen, "totalMinutes"))
            if total is not None:
                asleep = max(total - (awake or 0), 0)
        row = day_row(day)
        row.update(
            {
                "sleep_start": explicit(chosen, "start", "startTime"),
                "sleep_end": explicit(chosen, "end", "endTime"),
                "sleep_hours": round(asleep / 60, 3) if asleep is not None else None,
                "deep_minutes": deep,
                "light_minutes": light,
                "rem_minutes": rem,
                "awake_minutes": awake,
                "sleep_source": source_name(chosen),
            }
        )
        if row["sleep_source"] and row["sleep_source"] not in row["sources"]:
            row["sources"].append(row["sleep_source"])
        note_daily_cutoff(day, chosen)

    workouts: list[dict[str, Any]] = []
    for point in raw.get("exercise", []):
        day = direct_date(point, timezone_name=timezone_name)
        if not day:
            continue
        start = explicit(point, "start", "startTime")
        source = source_name(point)
        external_id = explicit(point, "id", "name", "externalId")
        duration = duration_seconds(explicit(point, "activeDuration", "duration"))
        duration_minutes = round(duration / 60, 3) if duration is not None else None
        distance_mm = number(explicit(point, "metricsSummary.distanceMillimeters", "distanceMillimeters"))
        workout_type = explicit(point, "displayName", "exerciseType", "activityType", "type") or "exercise"
        if external_id:
            # Source is deliberately excluded when an immutable external ID is
            # present so source-label corrections update the existing workout.
            identity = [str(external_id)]
        else:
            identity = [
                source or "unknown-source",
                day,
                start or f"untimed:{workout_type}:{duration_minutes}",
                str(workout_type),
            ]
        record_id = stable_record_id("workout", identity)
        workouts.append(
            {
                "record_id": record_id,
                "date": day,
                "start_time": start,
                "workout_type": str(workout_type),
                "duration_minutes": duration_minutes,
                "distance_km": round(distance_mm / 1_000_000, 4) if distance_mm is not None else None,
                "calories_kcal": number(explicit(point, "metricsSummary.caloriesKcal", "caloriesKcal")),
                "average_heart_rate_bpm": number(explicit(point, "metricsSummary.averageHeartRateBeatsPerMinute", "averageHeartRateBeatsPerMinute")),
                "max_heart_rate_bpm": number(explicit(point, "metricsSummary.maxHeartRateBeatsPerMinute", "maxHeartRateBeatsPerMinute")),
                "intensity_rpe": None,
                "muscle_groups": "",
                "training_source": source,
                "external_id": str(external_id or ""),
                "method": "ghealth automatic import",
                "confidence": "source reported",
                "original_text": "",
                "notes": "",
                "imported_at": imported_at,
            }
        )
        note_event_cutoff(day, point)

    for day, row in daily.items():
        row["sources"] = ", ".join(sorted(set(row["sources"])))
        cutoffs = daily_cutoffs.get(day) or [
            fallback_day_cutoff(day, imported_datetime, timezone_name)
        ]
        row["data_until"] = utc_iso(max(cutoffs))

    data_until = utc_iso(max(event_cutoffs)) if event_cutoffs else None

    return {
        "daily": sorted(daily.values(), key=lambda row: row["date"]),
        "measurements": measurements,
        "workouts": workouts,
        "data_until": data_until,
    }
