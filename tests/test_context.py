from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from oha.config import create_config, save_profile
from oha.context import build_context
from oha.database import HealthDatabase


def test_context_uses_active_goal_and_marks_current_day_incomplete(tmp_path: Path) -> None:
    config = create_config(tmp_path / "private-home", timezone="UTC")
    today = datetime.now(ZoneInfo("UTC")).date()
    yesterday = today - timedelta(days=1)
    save_profile(
        config,
        {
            "lean_mass_kg": 60,
            "age_group": "adult",
            "life_stage": "general",
            "health_constraints": ["synthetic test constraint"],
            "medications_affecting_exercise": ["synthetic exercise-affecting medication"],
            "portion_mode": "weighed",
            "goals": [
                {
                    "record_id": "goal-synthetic",
                    "status": "active",
                    "priority": 1,
                    "original_text": "合成测试目标：规律运动",
                }
            ],
        },
    )
    with HealthDatabase(config.database) as database:
        database.upsert(
            "daily",
            {
                "record_id": yesterday.isoformat(),
                "date": yesterday.isoformat(),
                "active_energy_kcal": 400,
                "data_until": f"{yesterday.isoformat()}T23:00:00+00:00",
            },
        )
        database.upsert(
            "daily",
            {
                "record_id": today.isoformat(),
                "date": today.isoformat(),
                "active_energy_kcal": 150,
                "data_until": f"{today.isoformat()}T10:00:00+00:00",
            },
        )
        database.upsert(
            "measurement",
            {
                "record_id": "synthetic-glucose",
                "date": today.isoformat(),
                "metric": "空腹血糖",
                "value": 5.2,
                "unit": "mmol/L",
                "source": "manual",
            },
        )
        context = build_context(config, database, today.isoformat())

    assert context["day_complete"] is False
    assert context["profile"]["active_goals"][0]["original_text"] == "合成测试目标：规律运动"
    assert context["profile"]["medications_affecting_exercise"] == [
        "synthetic exercise-affecting medication"
    ]
    assert context["profile"]["portion_mode"] == "weighed"
    assert not any("no explicit user goal" in gap for gap in context["data_gaps"])
    assert context["freshness"]["today_data_until"].endswith("10:00:00+00:00")
    assert context["today"]["recent_measurements"][-1]["metric"] == "空腹血糖"
    assert context["energy"]["estimated_ree_kcal"] == 1666
    assert context["energy"]["estimated_maintenance_intake_kcal"] == 2300
    assert "same-day values are partial" in context["energy"]["warning"]


def test_context_falls_back_when_goal_and_lean_mass_are_missing(tmp_path: Path) -> None:
    config = create_config(tmp_path / "private-home", timezone="UTC")
    save_profile(config, {"goals": []})
    with HealthDatabase(config.database) as database:
        context = build_context(config, database, "2026-01-02")

    assert context["profile"]["active_goals"] == []
    assert context["energy"]["estimated_ree_kcal"] is None
    assert context["energy"]["recorded_food_tef_range_kcal"] == {
        "low": None,
        "midpoint": None,
        "high": None,
    }
    assert "no explicit user goal; use WHO age/life-stage baseline" in context["data_gaps"]
    assert "no user-confirmed lean mass; do not calculate FFM-based REE" in context["data_gaps"]


def test_context_does_not_apply_a_goal_before_its_effective_date(tmp_path: Path) -> None:
    config = create_config(tmp_path / "private-home", timezone="UTC")
    save_profile(
        config,
        {
            "goals": [
                {
                    "record_id": "future-goal",
                    "status": "active",
                    "priority": 1,
                    "effective_date": "2026-07-13",
                    "original_text": "未来才生效的合成目标",
                }
            ]
        },
    )
    with HealthDatabase(config.database) as database:
        context = build_context(config, database, "2026-01-02")

    assert context["profile"]["active_goals"] == []
    assert "no explicit user goal; use WHO age/life-stage baseline" in context["data_gaps"]
    assert "1 confirmed goal(s) are not effective on the selected date" in context["data_gaps"]
