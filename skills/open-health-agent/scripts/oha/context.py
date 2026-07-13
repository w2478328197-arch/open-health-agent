from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config, load_profile
from .database import HealthDatabase
from .energy import estimated_ree, planning_intake, retrospective_tdee, tef_from_macros


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
    profile = load_profile(config)
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
    if ree is not None and average_active is not None and config.activity_energy_semantics == "active_only":
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
    all_goals = profile.get("goals", []) if isinstance(profile.get("goals", []), list) else []
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

    gaps = []
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
    if freshness_hours is not None and freshness_hours > 6:
        gaps.append(f"latest successful sync is {freshness_hours:.1f} hours old")
    if not foods:
        gaps.append("no food entries recorded for the selected date")
    elif not macros_complete:
        gaps.append(f"only {macro_coverage}/{len(foods)} food entries have complete macros; retrospective TEF is unavailable")
    if config.activity_energy_semantics == "total_energy":
        gaps.append("energy source is total_energy; do not add REE, workouts, or TEF as separate components")

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "selected_date": today,
        "day_complete": selected_date < now.date(),
        "profile": {
            "lean_mass_kg": lean_mass,
            "age_group": profile.get("age_group"),
            "life_stage": profile.get("life_stage"),
            "health_constraints": profile.get("health_constraints", []),
            "medications_affecting_exercise": profile.get(
                "medications_affecting_exercise", []
            ),
            "portion_mode": profile.get("portion_mode", "range"),
            "active_goals": active_goals,
        },
        "freshness": {
            "latest_sync": latest,
            "latest_successful_sync": successful,
            "hours_since_success": round(freshness_hours, 2) if freshness_hours is not None else None,
            "today_data_until": today_daily.get("data_until") if today_daily else None,
        },
        "today": {
            "health": today_daily,
            "workouts": today_workouts,
            "food_items": foods,
            "recorded_nutrition_totals": food_totals,
            "nutrition_field_coverage": food_coverage,
            "recent_measurements": measurements[-20:],
            "recent_blood_pressure": recent_bp[-5:],
        },
        "trends": {
            "daily_28d": daily,
            "workouts_28d": workouts,
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
        "data_gaps": gaps,
    }
