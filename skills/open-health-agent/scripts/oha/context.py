from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config, load_profile
from .constants import DAILY_FIELD_MAP, SUMMARY_NUTRIENT_FIELDS
from .database import HealthDatabase
from .energy import estimated_ree, planning_intake, retrospective_tdee, tef_from_macros
from .ghealth_adapter import SUPPORTED_CAPTURE_DATA_TYPES
from .profile import profile_invalid_fields
from .state import state_validation_error


_MAX_CONTEXT_TEXT = 500
_DAILY_CONTEXT_FIELDS = tuple(
    field for field in DAILY_FIELD_MAP if field not in {"batch_id", "imported_at", "notes"}
)
_MEASUREMENT_CONTEXT_FIELDS = (
    "date",
    "time",
    "metric",
    "value",
    "second_value",
    "unit",
    "source",
    "method",
    "confidence",
)
_WORKOUT_CONTEXT_FIELDS = (
    "date",
    "start_time",
    "workout_type",
    "duration_minutes",
    "distance_km",
    "calories_kcal",
    "average_heart_rate_bpm",
    "max_heart_rate_bpm",
    "intensity_rpe",
    "muscle_groups",
    "training_source",
    "method",
    "confidence",
)


def _bounded_text(value: Any, limit: int = _MAX_CONTEXT_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    return value if len(value) <= limit else value[:limit] + "…"


def _context_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, list):
        return [
            bounded
            for item in value[:20]
            if (bounded := _bounded_text(item, 200)) is not None
        ]
    return None


def _project(row: dict[str, Any] | None, fields: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return {
        field: projected
        for field in fields
        if field in row and (projected := _context_scalar(row.get(field))) is not None
    }


def _safe_food(row: dict[str, Any]) -> dict[str, Any]:
    projected = _project(
        row,
        (
            "date",
            "time",
            "meal",
            "food_name",
            "estimated_grams",
            "grams_low",
            "grams_high",
            "source",
            "confidence",
        ),
    ) or {}
    summary = row.get("summary_nutrients")
    if isinstance(summary, dict):
        projected["summary_nutrients"] = {
            key: float(value)
            for key, value in summary.items()
            if key in SUMMARY_NUTRIENT_FIELDS
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        }
    return projected


def _safe_goal(goal: dict[str, Any]) -> dict[str, Any]:
    return _project(
        goal,
        ("status", "priority", "effective_date", "original_text", "safety_constraints"),
    ) or {}


def _safe_sync_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    return _project(
        run,
        (
            "started_at",
            "finished_at",
            "from_date",
            "to_date",
            "status",
            "daily_count",
            "workout_count",
            "measurement_count",
            "data_until",
        ),
    )


def _local_now(config: Config) -> datetime:
    try:
        return datetime.now(ZoneInfo(config.timezone))
    except Exception:
        return datetime.now(timezone.utc)


def _food_totals(foods: list[dict[str, Any]]) -> tuple[dict[str, float | None], dict[str, int]]:
    fields = (
        "energy_kcal",
        "protein_g",
        "fat_g",
        "carbohydrate_g",
        "fiber_g",
        "sugar_g",
        "sodium_mg",
        "potassium_mg",
        "calcium_mg",
        "iron_mg",
        "magnesium_mg",
    )
    totals: dict[str, float | None] = {}
    coverage: dict[str, int] = {}
    for field in fields:
        values = []
        for food in foods:
            nutrients = food.get("summary_nutrients") or {}
            value = nutrients.get(field) if isinstance(nutrients, dict) else None
            if isinstance(value, (int, float)):
                values.append(float(value))
        totals[field] = round(sum(values), 3) if values else None
        coverage[field] = len(values)
    return totals, coverage


def build_context(config: Config, database: HealthDatabase, target_date: str | None = None) -> dict[str, Any]:
    now = _local_now(config)
    selected_date = date.fromisoformat(target_date) if target_date else now.date()
    today = selected_date.isoformat()
    start_28 = (selected_date - timedelta(days=27)).isoformat()
    daily = database.list_records("daily", from_date=start_28, to_date=today)
    measurements = database.list_records("measurement", from_date=start_28, to_date=today)
    workouts = database.list_records("workout", from_date=start_28, to_date=today)
    foods = database.list_records("food", from_date=today, to_date=today)
    goals = database.list_records("goal")
    wearable_coverage = database.wearable_coverage()
    profile_projection_available = True
    try:
        loaded_profile = load_profile(config)
    except (OSError, UnicodeError, json.JSONDecodeError):
        loaded_profile = None
    if isinstance(loaded_profile, dict) and not profile_invalid_fields(loaded_profile):
        profile = loaded_profile
    else:
        profile = {}
        profile_projection_available = False
    sync_runs = database.list_sync_runs(limit=30)

    daily_by_date = {str(row.get("date")): row for row in daily if row.get("date")}
    today_daily = daily_by_date.get(today)
    completed_dates = [
        (selected_date - timedelta(days=offset)).isoformat()
        for offset in range(1, 8)
    ]
    active_values = [
        float(daily_by_date[day]["active_energy_kcal"])
        for day in completed_dates
        if day in daily_by_date and isinstance(daily_by_date[day].get("active_energy_kcal"), (int, float))
    ]
    average_active = mean(active_values) if active_values else None

    food_totals, food_coverage = _food_totals(foods)
    macro_coverage = min(
        food_coverage.get("protein_g", 0),
        food_coverage.get("carbohydrate_g", 0),
        food_coverage.get("fat_g", 0),
    )
    macros_complete = bool(foods) and macro_coverage == len(foods)
    tef = None
    if macros_complete:
        tef = tef_from_macros(
            food_totals.get("protein_g") or 0,
            food_totals.get("carbohydrate_g") or 0,
            food_totals.get("fat_g") or 0,
        )

    lean_mass = profile.get("lean_mass_kg")
    ree = estimated_ree(float(lean_mass)) if isinstance(lean_mass, (int, float)) else None
    maintenance = None
    if (
        ree is not None
        and average_active is not None
        and len(active_values) >= 4
        and config.activity_energy_semantics == "active_only"
    ):
        maintenance = planning_intake(
            ree,
            average_active,
            goal_delta_kcal=0,
            tef_fraction=config.planning_tef_fraction,
        )
    partial_energy_accounting = None
    if (
        ree is not None
        and today_daily
        and isinstance(today_daily.get("active_energy_kcal"), (int, float))
        and config.activity_energy_semantics == "active_only"
        and tef is not None
    ):
        partial_energy_accounting = retrospective_tdee(
            ree,
            float(today_daily["active_energy_kcal"]),
            tef.midpoint,
        )

    successful = next((run for run in sync_runs if run.get("status") == "success"), None)
    latest = sync_runs[0] if sync_runs else None
    freshness_hours = None
    if successful and successful.get("finished_at"):
        try:
            finished = datetime.fromisoformat(successful["finished_at"].replace("Z", "+00:00"))
            freshness_hours = max(0.0, (datetime.now(timezone.utc) - finished.astimezone(timezone.utc)).total_seconds() / 3600)
        except ValueError:
            pass

    recent_bp = [
        row
        for row in measurements
        if "血压" in str(row.get("metric", "")) or str(row.get("unit", "")) == "mmHg"
    ]
    # SQLite is canonical. A stale, malformed, or temporarily unwritable
    # profile projection must never make a committed goal disappear from the
    # advice context.
    all_goals = goals
    active_goals = sorted(
        [
            goal
            for goal in all_goals
            if isinstance(goal, dict)
            and str(goal.get("status", "")).startswith("active")
            and str(goal.get("effective_date") or goal.get("date") or "0001-01-01") <= today
        ],
        key=lambda item: int(item.get("priority", 100)),
    )
    future_goals = [
        goal
        for goal in all_goals
        if isinstance(goal, dict)
        and str(goal.get("status", "")).startswith("active")
        and str(goal.get("effective_date") or goal.get("date") or "0001-01-01") > today
    ]
    today_workouts = [row for row in workouts if row.get("date") == today]
    try:
        state = json.loads((config.home_path / "state.json").read_text(encoding="utf-8"))
        state_valid = state_validation_error(state) is None
        if not state_valid:
            state = {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        state = {}
        state_valid = False

    gaps = []
    if not profile_projection_available:
        gaps.append(
            "profile projection is unavailable; SQLite goals remain active and non-goal profile fields are omitted"
        )
    if not state_valid:
        gaps.append(
            "operational state is unavailable; consent and workbook projection freshness are unknown"
        )
    if not active_goals:
        gaps.append("no explicit user goal; use WHO age/life-stage baseline")
    if future_goals:
        gaps.append(f"{len(future_goals)} confirmed goal(s) are not effective on the selected date")
    if ree is None:
        gaps.append("no user-confirmed lean mass; do not calculate FFM-based REE")
    if average_active is None:
        gaps.append("no completed-day active-energy baseline")
    elif len(active_values) < 4:
        gaps.append(f"only {len(active_values)}/7 completed days have active energy")
    if latest is None:
        gaps.append("no ghealth sync run recorded")
    elif latest.get("status") != "success":
        gaps.append(f"latest sync status is {latest.get('status')}")
    captured_type_count = len(wearable_coverage)
    failed_capture_types = [
        row
        for row in wearable_coverage
        if row.get("status") in {"partial", "failed"}
    ]
    if latest is not None and captured_type_count < len(SUPPORTED_CAPTURE_DATA_TYPES):
        gaps.append(
            "wearable capture coverage is "
            f"{captured_type_count}/{len(SUPPORTED_CAPTURE_DATA_TYPES)} data types"
        )
    if failed_capture_types:
        gaps.append(
            f"{len(failed_capture_types)} wearable data type(s) had a failed query"
        )
    if freshness_hours is not None and freshness_hours > 6:
        gaps.append(f"latest successful sync is {freshness_hours:.1f} hours old")
    if not foods:
        gaps.append("no food entries recorded for the selected date")
    elif not macros_complete:
        gaps.append(f"only {macro_coverage}/{len(foods)} food entries have complete macros; retrospective TEF is unavailable")
    if config.activity_energy_semantics == "total_energy":
        gaps.append("energy source is total_energy; do not add REE, workouts, or TEF as separate components")
    if state.get("workbook_export_pending") is True:
        gaps.append("SQLite contains a durable change that is not yet reflected in the Excel export")
    if len(daily) < 14:
        gaps.append(f"only {len(daily)}/28 dates are available for the 28-day trend")

    safe_goals = [_safe_goal(goal) for goal in active_goals[:20]]
    safe_today_foods = [_safe_food(row) for row in foods[-50:]]
    safe_today_workouts = [
        projected
        for row in today_workouts[-50:]
        if (projected := _project(row, _WORKOUT_CONTEXT_FIELDS)) is not None
    ]
    safe_measurements = [
        projected
        for row in measurements[-20:]
        if (projected := _project(row, _MEASUREMENT_CONTEXT_FIELDS)) is not None
    ]
    safe_recent_bp = [
        projected
        for row in recent_bp[-5:]
        if (projected := _project(row, _MEASUREMENT_CONTEXT_FIELDS)) is not None
    ]
    safe_daily = [
        projected
        for row in daily
        if (projected := _project(row, _DAILY_CONTEXT_FIELDS)) is not None
    ]
    safe_workouts = [
        projected
        for row in workouts[-100:]
        if (projected := _project(row, _WORKOUT_CONTEXT_FIELDS)) is not None
    ]
    safe_wearable_coverage = [
        projected
        for row in wearable_coverage
        if (
            projected := _project(
                row,
                (
                    "data_type",
                    "operations",
                    "grains",
                    "status",
                    "record_count",
                    "first_date",
                    "last_date",
                    "data_until",
                    "source_count",
                    "failed_query_count",
                ),
            )
        )
        is not None
    ]

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "selected_date": today,
        "day_complete": selected_date < now.date(),
        "profile": {
            "lean_mass_kg": lean_mass,
            "age_group": _context_scalar(profile.get("age_group")),
            "life_stage": _context_scalar(profile.get("life_stage")),
            "health_constraints": _context_scalar(profile.get("health_constraints", [])),
            "medications_affecting_exercise": _context_scalar(
                profile.get("medications_affecting_exercise", [])
            ),
            "portion_mode": _context_scalar(profile.get("portion_mode", "range")),
            "active_goals": safe_goals,
        },
        "freshness": {
            "latest_sync": _safe_sync_run(latest),
            "latest_successful_sync": _safe_sync_run(successful),
            "hours_since_success": round(freshness_hours, 2) if freshness_hours is not None else None,
            "today_data_until": today_daily.get("data_until") if today_daily else None,
            "workbook_projection_status": "unknown"
            if not state_valid
            else (
                "pending"
                if state.get("workbook_export_pending") is True
                else "current"
            ),
        },
        "today": {
            "health": _project(today_daily, _DAILY_CONTEXT_FIELDS),
            "workouts": safe_today_workouts,
            "food_items": safe_today_foods,
            "food_item_count": len(foods),
            "recorded_nutrition_totals": food_totals,
            "nutrition_field_coverage": food_coverage,
            "recent_measurements": safe_measurements,
            "recent_blood_pressure": safe_recent_bp,
        },
        "trends": {
            "daily_28d": safe_daily,
            "workouts_28d": safe_workouts,
            "coverage": {
                "daily_dates": len(daily_by_date),
                "active_energy_completed_days": len(active_values),
                "workout_records": len(workouts),
            },
            "wearable_capture": {
                "supported_data_types": len(SUPPORTED_CAPTURE_DATA_TYPES),
                "checked_data_types": captured_type_count,
                "streams": safe_wearable_coverage,
                "decision_policy": (
                    "coverage metadata only; raw samples and waveforms require "
                    "explicit semantic mapping before analysis"
                ),
            },
            "completed_7d_active_energy_values": active_values,
            "average_completed_active_energy_kcal": round(average_active, 1) if average_active is not None else None,
        },
        "energy": {
            "formula_id": "cunningham_1991_ffm",
            "estimated_ree_kcal": round(ree, 1) if ree is not None else None,
            "planning_tef_fraction": config.planning_tef_fraction,
            "estimated_maintenance_intake_kcal": round(maintenance / 50) * 50 if maintenance is not None else None,
            "recorded_food_tef_range_kcal": {
                "low": round(tef.low, 1) if tef is not None else None,
                "midpoint": round(tef.midpoint, 1) if tef is not None else None,
                "high": round(tef.high, 1) if tef is not None else None,
            },
            "partial_day_ree_plus_active_plus_recorded_tef_kcal": (
                round(partial_energy_accounting, 1)
                if partial_energy_accounting is not None
                else None
            ),
            "semantics": config.activity_energy_semantics,
            "warning": (
                "same-day values are partial; wearable energy is an estimate and must not be eaten back 1:1; "
                "component calculations are valid only for active_only semantics"
            ),
        },
        "privacy": {
            "mode": "advice_minimized",
            "omitted": [
                "record IDs",
                "external IDs",
                "original food/measurement/workout wording",
                "image references",
                "free-form notes",
                "raw wearable samples and ECG waveforms",
                "sync batch IDs",
                "import timestamps",
            ],
            "limits": {
                "food_items": 50,
                "measurements": 20,
                "workouts_28d": 100,
                "text_characters": _MAX_CONTEXT_TEXT,
            },
        },
        "data_gaps": gaps,
    }
