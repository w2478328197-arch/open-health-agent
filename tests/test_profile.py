from __future__ import annotations

import json
from pathlib import Path

import pytest

from oha.config import create_config, initialize_local_files, load_config, save_config
from oha.database import HealthDatabase
from oha.profile import (
    GOALS_START,
    goal_views_status,
    rebuild_goal_views,
    set_goal,
    set_profile_value,
)


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "skills" / "open-health-agent" / "assets"


def initialized_config(tmp_path: Path):
    config = create_config(tmp_path / "private", timezone="UTC")
    save_config(config)
    initialize_local_files(
        config,
        ASSETS / "AGENTS.md.template",
        ASSETS / "profile.example.json",
    )
    return config


def test_goal_preserves_exact_user_whitespace(tmp_path: Path) -> None:
    config = initialized_config(tmp_path)
    original = "  我的合成测试目标\n保留这一行。  "
    with HealthDatabase(config.database) as database:
        saved = set_goal(config, database, original, effective_date="2026-01-02")
    assert saved["original_text"] == original
    assert "我的合成测试目标" in (config.home_path / "AGENTS.md").read_text(encoding="utf-8")


def test_goal_projection_inserts_backslash_sequences_literally(tmp_path: Path) -> None:
    config = initialized_config(tmp_path)
    original = r"合成目标保留 \1 和 \g<0> 原样"
    with HealthDatabase(config.database) as database:
        saved = set_goal(config, database, original, effective_date="2026-01-02")
        assert goal_views_status(config, database)["ok"] is True
    assert saved["original_text"] == original
    assert original in (config.home_path / "AGENTS.md").read_text(encoding="utf-8")


def test_safety_constraint_rejects_reserved_goal_markers_before_writing(
    tmp_path: Path,
) -> None:
    config = initialized_config(tmp_path)
    profile_before = (config.home_path / "profile.json").read_bytes()
    agents_before = (config.home_path / "AGENTS.md").read_bytes()
    with HealthDatabase(config.database) as database:
        with pytest.raises(ValueError, match="reserved marker"):
            set_goal(
                config,
                database,
                "合成目标",
                effective_date="2026-01-02",
                safety_constraints=f"不要注入 {GOALS_START}",
            )
        assert database.list_records("goal") == []
    assert (config.home_path / "profile.json").read_bytes() == profile_before
    assert (config.home_path / "AGENTS.md").read_bytes() == agents_before


def test_profile_validates_lean_mass_and_updates_operational_timezone(tmp_path: Path) -> None:
    config = initialized_config(tmp_path)
    with pytest.raises(ValueError, match="between 20 and 200"):
        set_profile_value(config, "lean_mass_kg", 5)
    profile = set_profile_value(config, "lean_mass_kg", 60)
    assert profile["lean_mass_kg"] == 60.0

    set_profile_value(config, "timezone", "Asia/Shanghai")
    assert load_config(config.home_path).timezone == "Asia/Shanghai"

    set_profile_value(config, "activity_energy_semantics", "total_energy")
    assert load_config(config.home_path).activity_energy_semantics == "total_energy"
    with pytest.raises(ValueError, match="active_only or total_energy"):
        set_profile_value(config, "activity_energy_semantics", "ambiguous")


def test_goal_views_are_detected_and_rebuilt_from_sqlite(tmp_path: Path) -> None:
    config = initialized_config(tmp_path)
    with HealthDatabase(config.database) as database:
        goal = set_goal(
            config,
            database,
            "精确保留的合成目标",
            effective_date="2026-01-02",
        )
        assert goal_views_status(config, database)["ok"] is True

        profile_path = config.home_path / "profile.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["goals"] = []
        profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        agents_path = config.home_path / "AGENTS.md"
        agents_path.write_text(
            agents_path.read_text(encoding="utf-8").replace(
                "精确保留的合成目标", "错误的投影视图"
            ),
            encoding="utf-8",
        )

        broken = goal_views_status(config, database)
        assert broken["ok"] is False
        assert broken["profile_matches_database"] is False
        assert broken["agents_matches_database"] is False

        repaired = rebuild_goal_views(config, database)
        assert repaired["ok"] is True
        assert json.loads(profile_path.read_text(encoding="utf-8"))["goals"][0][
            "record_id"
        ] == goal["record_id"]
        assert "精确保留的合成目标" in agents_path.read_text(encoding="utf-8")


def test_goal_repair_refuses_missing_agents_markers_before_changing_profile(
    tmp_path: Path,
) -> None:
    config = initialized_config(tmp_path)
    with HealthDatabase(config.database) as database:
        set_goal(config, database, "合成目标", effective_date="2026-01-02")
        profile_path = config.home_path / "profile.json"
        profile_before = profile_path.read_bytes()
        agents_path = config.home_path / "AGENTS.md"
        agents_path.write_text(
            agents_path.read_text(encoding="utf-8").replace(
                "<!-- OPEN_HEALTH_AGENT_GOALS:START -->", ""
            ),
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="markers are missing or duplicated"):
            rebuild_goal_views(config, database)
        assert profile_path.read_bytes() == profile_before
