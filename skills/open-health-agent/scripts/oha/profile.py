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


class GoalProjectionError(RuntimeError):
    """A SQLite goal is safe, but one or more file projections need repair."""

    def __init__(self, code: str, *, database_state: str) -> None:
        self.code = code
        self.database_state = database_state
        super().__init__(
            f"goal projection update failed ({code}); SQLite is authoritative and its "
            f"goal state is {database_state}. Restore a valid private projection document "
            "if needed, then retry goal repair."
        )


class ProfileProjectionError(RuntimeError):
    """Operational config committed, but its private profile view is pending."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(
            f"profile projection update failed ({code}); operational config is authoritative"
        )


def profile_invalid_fields(profile: Any) -> list[str]:
    """Return schema-invalid fields without projecting any private value."""

    if not isinstance(profile, dict):
        return ["profile"]
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
    if profile.get("portion_mode", "range") not in {"range", "weighed", "exact"}:
        invalid.append("portion_mode")
    timezone_name = profile.get("timezone")
    if timezone_name is not None:
        if not isinstance(timezone_name, str):
            invalid.append("timezone")
        else:
            try:
                ZoneInfo(timezone_name)
            except Exception:
                invalid.append("timezone")
    semantics = profile.get("activity_energy_semantics")
    if semantics is not None and semantics not in {"active_only", "total_energy"}:
        invalid.append("activity_energy_semantics")
    return sorted(set(invalid))


def validate_profile_value(key: str, value: Any) -> Any:
    """Validate and normalize one supported field without mutating projections."""

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
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("lean_mass_kg must be a finite number or null")
        if not 20 <= float(value) <= 200:
            raise ValueError("lean_mass_kg must be between 20 and 200 kg")
        return float(value)
    if key in {"age_group", "life_stage"} and value is not None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be non-empty text or null")
    elif key in {"health_constraints", "medications_affecting_exercise"}:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(f"{key} must be a JSON list of non-empty text values")
    elif key == "portion_mode" and value not in {"range", "weighed", "exact"}:
        raise ValueError("portion_mode must be range, weighed, or exact")
    elif key == "timezone":
        if not isinstance(value, str):
            raise ValueError("timezone must be an IANA timezone name")
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError("timezone must be a valid IANA timezone name") from exc
    elif key == "activity_energy_semantics" and value not in {
        "active_only",
        "total_energy",
    }:
        raise ValueError("activity_energy_semantics must be active_only or total_energy")
    return value


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


def _write_goal_views_from_database(
    config: Config,
    database: HealthDatabase,
    *,
    database_state: str,
) -> dict[str, Any]:
    """Project canonical SQLite goals without trusting either existing file view.

    Each file replacement is atomic. The caller holds the shared ledger lock in
    normal CLI use. A failure is deliberately not allowed to roll back SQLite:
    the database remains the auditable source of truth and this idempotent
    projection can be retried with ``goal repair``.
    """

    goals = database.list_records("goal")
    try:
        profile = load_profile(config)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GoalProjectionError(
            "profile_document_unavailable", database_state=database_state
        ) from exc
    if not isinstance(profile, dict):
        raise GoalProjectionError(
            "profile_document_invalid", database_state=database_state
        )

    # Validate the second projection before changing the first one. This avoids
    # a known-bad marker block causing an otherwise avoidable partial refresh.
    try:
        agents_content = (config.home_path / "AGENTS.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise GoalProjectionError(
            "agents_document_unavailable", database_state=database_state
        ) from exc
    if _extract_goal_block(agents_content) is None:
        raise GoalProjectionError(
            "agents_markers_invalid", database_state=database_state
        )

    projected_profile = dict(profile)
    projected_profile["goals"] = goals
    projected_profile["updated_at"] = utc_now()
    try:
        save_profile(config, projected_profile)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise GoalProjectionError(
            "profile_write_failed", database_state=database_state
        ) from exc
    try:
        update_agents_goals(config, goals)
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise GoalProjectionError(
            "agents_write_failed", database_state=database_state
        ) from exc

    status = goal_views_status(config, database)
    if not status["ok"]:
        raise GoalProjectionError(
            "projection_verification_failed", database_state=database_state
        )
    return status


def rebuild_goal_views(config: Config, database: HealthDatabase) -> dict[str, Any]:
    """Rebuild profile/AGENTS goal projections from the SQLite source of truth."""

    return _write_goal_views_from_database(
        config,
        database,
        database_state="unchanged",
    )


def normalize_goal(
    config: Config,
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
    return {
        "record_id": record_id,
        "date": effective,
        "effective_date": effective,
        "status": status,
        "priority": priority,
        "original_text": exact,
        "safety_constraints": constraints,
        "recorded_at": utc_now(),
    }


def set_goal(
    config: Config,
    database: HealthDatabase,
    original_text: str,
    effective_date: str | None = None,
    priority: int = 100,
    safety_constraints: str = "",
    *,
    normalized_goal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = normalized_goal or normalize_goal(
        config,
        original_text,
        effective_date,
        priority,
        safety_constraints,
    )
    record_id = str(payload["record_id"])
    database.upsert("goal", payload, record_id)
    _write_goal_views_from_database(
        config,
        database,
        database_state="committed",
    )
    return payload


def retire_goal(config: Config, database: HealthDatabase, record_id: str, reason: str = "") -> dict[str, Any]:
    selected = database.get("goal", record_id)
    if selected is None:
        raise KeyError(f"goal not found: {record_id}")
    selected = dict(selected)
    selected["status"] = "retired"
    selected["retired_at"] = utc_now()
    selected["retirement_reason"] = reason
    database.upsert("goal", selected, record_id)
    _write_goal_views_from_database(
        config,
        database,
        database_state="committed",
    )
    return selected


def set_profile_value(config: Config, key: str, value: Any, source: str = "user-confirmed") -> dict[str, Any]:
    value = validate_profile_value(key, value)
    profile = load_profile(config)
    if not isinstance(profile, dict):
        raise ValueError("private profile must be a JSON object")
    operational_config_changed = False
    if key == "timezone":
        config.timezone = value
        save_config(config)
        operational_config_changed = True
    elif key == "activity_energy_semantics":
        config.activity_energy_semantics = value
        save_config(config)
        operational_config_changed = True
    profile[key] = value
    profile.setdefault("field_provenance", {})[key] = {
        "source": source,
        "confirmed_at": utc_now(),
    }
    profile["updated_at"] = utc_now()
    try:
        save_profile(config, profile)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        if operational_config_changed:
            raise ProfileProjectionError("profile_write_failed") from exc
        raise
    return profile
