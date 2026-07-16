from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import oha.database as database_module
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
                    "record_id": "stale-profile-goal",
                    "status": "active",
                    "priority": 1,
                    "original_text": "不应采用的 profile 投影目标",
                }
            ],
        },
    )
    with HealthDatabase(config.database) as database:
        database.upsert(
            "goal",
            {
                "record_id": "goal-synthetic",
                "date": today.isoformat(),
                "effective_date": today.isoformat(),
                "status": "active",
                "priority": 1,
                "original_text": "合成测试目标：规律运动",
                "safety_constraints": "",
            },
        )
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
    assert "不应采用的 profile 投影目标" not in str(context)
    assert context["profile"]["medications_affecting_exercise"] == [
        "synthetic exercise-affecting medication"
    ]
    assert context["profile"]["portion_mode"] == "weighed"
    assert not any("no explicit user goal" in gap for gap in context["data_gaps"])
    assert context["freshness"]["today_data_until"].endswith("10:00:00+00:00")
    assert context["today"]["recent_measurements"][-1]["metric"] == "空腹血糖"
    assert context["energy"]["estimated_ree_kcal"] == 1666
    assert context["energy"]["estimated_maintenance_intake_kcal"] is None
    assert "only 1/7 completed days have active energy" in context["data_gaps"]
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


def test_context_uses_later_inserted_sync_when_runs_share_a_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        database_module,
        "utc_now",
        lambda: "2026-01-02T03:04:05+00:00",
    )
    config = create_config(tmp_path / "private-home", timezone="UTC")
    save_profile(config, {"goals": []})
    with HealthDatabase(config.database) as database:
        database.begin_sync("z-earlier-success", "2026-01-01", "2026-01-02")
        database.finish_sync(
            "z-earlier-success", "success", 1, 0, 0, "2026-01-02", []
        )
        database.begin_sync("a-later-partial", "2026-01-01", "2026-01-02")
        database.finish_sync(
            "a-later-partial", "partial", 0, 0, 0, None, ["synthetic gap"]
        )

        context = build_context(config, database, "2026-01-02")

    assert context["freshness"]["latest_sync"]["status"] == "partial"
    assert "latest sync status is partial" in context["data_gaps"]


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
        database.upsert(
            "goal",
            {
                "record_id": "future-goal",
                "date": "2026-07-13",
                "effective_date": "2026-07-13",
                "status": "active",
                "priority": 1,
                "original_text": "未来才生效的合成目标",
                "safety_constraints": "",
            },
        )
        context = build_context(config, database, "2026-01-02")

    assert context["profile"]["active_goals"] == []
    assert "no explicit user goal; use WHO age/life-stage baseline" in context["data_gaps"]
    assert "1 confirmed goal(s) are not effective on the selected date" in context["data_gaps"]


def test_context_omits_record_ids_original_wording_and_media_references(tmp_path: Path) -> None:
    config = create_config(tmp_path / "private-home", timezone="UTC")
    save_profile(config, {"goals": []})
    with HealthDatabase(config.database) as database:
        database.upsert(
            "food",
            {
                "record_id": "private-food-id",
                "date": "2026-01-02",
                "food_name": "合成测试餐",
                "summary_nutrients": {"energy_kcal": 500},
                "original_text": "不应进入建议上下文的原话",
                "image_reference": "private-signed-media-reference",
                "notes": "不应进入建议上下文的备注",
            },
        )
        database.upsert(
            "workout",
            {
                "record_id": "private-workout-id",
                "date": "2026-01-02",
                "workout_type": "合成训练",
                "external_id": "private-provider-id",
                "original_text": "不应进入建议上下文的训练原话",
            },
        )
        context = build_context(config, database, "2026-01-02")

    encoded = str(context)
    for secret in (
        "private-food-id",
        "private-workout-id",
        "private-provider-id",
        "private-signed-media-reference",
        "不应进入建议上下文的原话",
        "不应进入建议上下文的备注",
        "不应进入建议上下文的训练原话",
    ):
        assert secret not in encoded
    assert context["privacy"]["mode"] == "advice_minimized"
    assert context["today"]["food_items"][0]["food_name"] == "合成测试餐"


def test_context_keeps_committed_sqlite_goal_when_profile_projection_is_invalid(
    tmp_path: Path,
) -> None:
    config = create_config(tmp_path / "private-home", timezone="UTC")
    save_profile(config, {"lean_mass_kg": 60, "goals": []})
    with HealthDatabase(config.database) as database:
        database.upsert(
            "goal",
            {
                "record_id": "durable-goal",
                "date": "2026-01-02",
                "effective_date": "2026-01-02",
                "status": "active",
                "priority": 1,
                "original_text": "SQLite 中已提交的合成目标",
                "safety_constraints": "",
            },
        )
        (config.home_path / "profile.json").write_text("{", encoding="utf-8")

        context = build_context(config, database, "2026-01-02")

    assert context["profile"]["active_goals"] == [
        {
            "effective_date": "2026-01-02",
            "original_text": "SQLite 中已提交的合成目标",
            "priority": 1,
            "safety_constraints": "",
            "status": "active",
        }
    ]
    assert context["profile"]["lean_mass_kg"] is None
    assert (
        "profile projection is unavailable; SQLite goals remain active and non-goal profile fields are omitted"
        in context["data_gaps"]
    )
