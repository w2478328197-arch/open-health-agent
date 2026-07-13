from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config, load_profile, save_config, save_profile
from .database import HealthDatabase, canonical_json, stable_record_id, utc_now

GOALS_START = "<!-- OPEN_HEALTH_AGENT_GOALS:START -->"
GOALS_END = "<!-- OPEN_HEALTH_AGENT_GOALS:END -->"


def _validate_user_text(text: str, label: str) -> str:
    if not isinstance(text, str):
        raise ValueError(f"{label} must be text")
    if not text.strip():
        raise ValueError(f"{label} cannot be empty")
    if GOALS_START in text or GOALS_END in text:
        raise ValueError(f"{label} contains a reserved marker")
    return text


def _local_today(config: Config) -> str:
    try:
        return datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    except Exception:
        return date.today().isoformat()


def _quote_exact(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _render_goal_block(goals: list[dict[str, Any]]) -> str:
    active = [goal for goal in goals if str(goal.get("status", "")).startswith("active")]
    lines = [GOALS_START, "## 用户已确认的当前目标（原话）", ""]
    if not active:
        lines.extend(["尚未记录明确目标。使用 WHO 一般健康建议作为冷启动基线。", ""])
    for goal in sorted(active, key=lambda item: (int(item.get("priority", 100)), item.get("effective_date", ""))):
        lines.extend(
            [
                f"### {goal['record_id']} · 优先级 {goal.get('priority', 100)} · {goal.get('status', 'active')}",
                "",
                f"生效日期：{goal.get('effective_date', '')}",
                "",
                "用户原话：",
                "",
                _quote_exact(str(goal.get("original_text", ""))),
                "",
            ]
        )
        constraints = str(goal.get("safety_constraints") or "").strip()
        if constraints:
            lines.extend(["安全约束：", "", _quote_exact(constraints), ""])
    lines.append(GOALS_END)
    return "\n".join(lines)


def _extract_goal_block(content: str) -> str | None:
    pattern = re.compile(re.escape(GOALS_START) + r".*?" + re.escape(GOALS_END), re.DOTALL)
    matches = pattern.findall(content)
    return matches[0] if len(matches) == 1 else None


def update_agents_goals(config: Config, goals: list[dict[str, Any]]) -> Path:
    path = config.home_path / "AGENTS.md"
    if not path.exists():
        raise FileNotFoundError(f"local AGENTS.md is missing: {path}")
    content = path.read_text(encoding="utf-8")
    if _extract_goal_block(content) is None:
        raise RuntimeError("AGENTS.md goal markers are missing or duplicated; refusing an uncontrolled rewrite")
    replacement = _render_goal_block(goals)
    pattern = re.compile(re.escape(GOALS_START) + r".*?" + re.escape(GOALS_END), re.DOTALL)
    # A replacement callback inserts exact user wording literally. Passing the
    # string directly would interpret legitimate goal text such as ``\1`` or
    # ``\g<0>`` as regular-expression replacement syntax.
    updated, count = pattern.subn(lambda _match: replacement, content, count=1)
    if count != 1:
        raise RuntimeError("could not update the AGENTS.md goal block")
    temporary = path.with_suffix(".md.tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def goal_views_status(config: Config, database: HealthDatabase) -> dict[str, Any]:
    """Check the three goal projections without returning private goal wording."""

    database_goals = database.list_records("goal")
    try:
        profile = load_profile(config)
    except (OSError, UnicodeError, json.JSONDecodeError):
        profile = None
    profile_goals = profile.get("goals") if isinstance(profile, dict) else None
    profile_valid = isinstance(profile_goals, list) and all(
        isinstance(item, dict) for item in profile_goals
    )

    def ordered(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(items, key=lambda item: str(item.get("record_id") or ""))

    profile_matches = bool(
        profile_valid
        and canonical_json(ordered(database_goals)) == canonical_json(ordered(profile_goals))
    )
    try:
        agents_content = (config.home_path / "AGENTS.md").read_text(encoding="utf-8")
        actual_block = _extract_goal_block(agents_content)
    except (OSError, UnicodeError):
        actual_block = None
    agents_matches = actual_block == _render_goal_block(database_goals)
    return {
        "ok": profile_matches and agents_matches,
        "database_goal_count": len(database_goals),
        "profile_document_valid": profile_valid,
        "profile_matches_database": profile_matches,
        "agents_markers_valid": actual_block is not None,
        "agents_matches_database": agents_matches,
    }


def rebuild_goal_views(config: Config, database: HealthDatabase) -> dict[str, Any]:
    """Rebuild profile/AGENTS goal projections from the SQLite source of truth."""

    current_status = goal_views_status(config, database)
    if not current_status["agents_markers_valid"]:
        raise RuntimeError(
            "AGENTS.md goal markers are missing or duplicated; restore the standard marker block before goal repair"
        )
    profile = load_profile(config)
    if not isinstance(profile, dict):
        raise ValueError("profile document must be a JSON object before goal repair")
    goals = database.list_records("goal")
    profile["goals"] = goals
    profile["updated_at"] = utc_now()
    save_profile(config, profile)
    update_agents_goals(config, goals)
    return goal_views_status(config, database)


def set_goal(
    config: Config,
    database: HealthDatabase,
    original_text: str,
    effective_date: str | None = None,
    priority: int = 100,
    safety_constraints: str = "",
) -> dict[str, Any]:
    exact = _validate_user_text(original_text, "goal text")
    if not 1 <= priority <= 999:
        raise ValueError("priority must be between 1 and 999; smaller numbers are more important")
    effective = effective_date or _local_today(config)
    date.fromisoformat(effective)
    constraints = (
        _validate_user_text(safety_constraints, "safety constraint")
        if safety_constraints.strip()
        else ""
    )
    status = "active_with_safety_constraint" if constraints else "active"
    record_id = stable_record_id("goal", [effective, exact])
    payload = {
        "record_id": record_id,
        "date": effective,
        "effective_date": effective,
        "status": status,
        "priority": priority,
        "original_text": exact,
        "safety_constraints": constraints,
        "recorded_at": utc_now(),
    }
    database.upsert("goal", payload, record_id)
    profile = load_profile(config)
    goals = [item for item in profile.get("goals", []) if item.get("record_id") != record_id]
    goals.append(payload)
    profile["goals"] = goals
    profile["updated_at"] = utc_now()
    save_profile(config, profile)
    update_agents_goals(config, goals)
    return payload


def retire_goal(config: Config, database: HealthDatabase, record_id: str, reason: str = "") -> dict[str, Any]:
    profile = load_profile(config)
    goals = list(profile.get("goals", []))
    selected = next((item for item in goals if item.get("record_id") == record_id), None)
    if selected is None:
        raise KeyError(f"goal not found: {record_id}")
    selected["status"] = "retired"
    selected["retired_at"] = utc_now()
    selected["retirement_reason"] = reason
    database.upsert("goal", selected, record_id)
    profile["goals"] = goals
    profile["updated_at"] = utc_now()
    save_profile(config, profile)
    update_agents_goals(config, goals)
    return selected


def set_profile_value(config: Config, key: str, value: Any, source: str = "user-confirmed") -> dict[str, Any]:
    allowed = {
        "lean_mass_kg",
        "age_group",
        "life_stage",
        "timezone",
        "activity_energy_semantics",
        "portion_mode",
        "health_constraints",
        "medications_affecting_exercise",
    }
    if key not in allowed:
        raise ValueError(f"unsupported profile field: {key}")
    if key == "lean_mass_kg" and value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("lean_mass_kg must be a finite number or null")
        if not 20 <= float(value) <= 200:
            raise ValueError("lean_mass_kg must be between 20 and 200 kg")
        value = float(value)
    elif key in {"age_group", "life_stage"} and value is not None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be non-empty text or null")
    elif key in {"health_constraints", "medications_affecting_exercise"}:
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            raise ValueError(f"{key} must be a JSON list of non-empty text values")
    elif key == "portion_mode":
        if value not in {"range", "weighed", "exact"}:
            raise ValueError("portion_mode must be range, weighed, or exact")
    elif key == "timezone":
        if not isinstance(value, str):
            raise ValueError("timezone must be an IANA timezone name")
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError("timezone must be a valid IANA timezone name") from exc
        config.timezone = value
        save_config(config)
    elif key == "activity_energy_semantics":
        if value not in {"active_only", "total_energy"}:
            raise ValueError("activity_energy_semantics must be active_only or total_energy")
        config.activity_energy_semantics = value
        save_config(config)
    profile = load_profile(config)
    profile[key] = value
    profile.setdefault("field_provenance", {})[key] = {
        "source": source,
        "confirmed_at": utc_now(),
    }
    profile["updated_at"] = utc_now()
    save_profile(config, profile)
    return profile
