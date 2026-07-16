from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any

from .database import HealthDatabase, stable_record_id, utc_now


SUPPORTED_KINDS = {"measurement", "workout", "food"}


def _text(value: Any, label: str, required: bool = False) -> str:
    if value is None:
        result = ""
    elif isinstance(value, str):
        result = value.strip()
    else:
        raise ValueError(f"{label} must be text")
    if required and not result:
        raise ValueError(f"{label} is required")
    return result


def _date(value: Any) -> str:
    result = _text(value, "date", required=True)
    date.fromisoformat(result)
    return result


def _number(value: Any, label: str, *, required: bool = False, minimum: float | None = None) -> float | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"{label} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return result


def _validate_time(value: Any, label: str) -> str:
    result = _text(value, label)
    if not result:
        return ""
    candidate = result.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        for time_format in ("%H:%M", "%H:%M:%S"):
            try:
                parsed = datetime.strptime(result, time_format)
                return parsed.strftime("%H:%M:%S") if time_format == "%H:%M:%S" else result
            except ValueError:
                continue
        raise ValueError(f"{label} must be HH:MM, HH:MM:SS, or ISO 8601")
    return result


def _entry_method(payload: dict[str, Any], default: str = "manual") -> str:
    """Accept the schema name and common agent-facing alias without ambiguity."""

    method = _text(payload.get("method"), "method")
    alias = _text(payload.get("entry_method"), "entry_method")
    if method and alias and method != alias:
        raise ValueError("method and entry_method must match when both are supplied")
    return method or alias or default


def _source_event_id(payload: dict[str, Any]) -> str:
    value = _text(payload.get("source_event_id"), "source_event_id")
    if len(value) > 512:
        raise ValueError("source_event_id must be at most 512 characters")
    return value


def _source_event_item_id(payload: dict[str, Any], source_event_id: str) -> str:
    value = _text(payload.get("source_event_item_id"), "source_event_item_id")
    if not value:
        return ""
    if not source_event_id:
        raise ValueError("source_event_item_id requires source_event_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError("source_event_item_id must be an opaque item key of at most 128 safe characters")
    return value


def _clean_original_text(payload: dict[str, Any]) -> str:
    value = payload.get("original_text", "")
    if not isinstance(value, str):
        raise ValueError("original_text must be text")
    # Preserve the user's wording byte-for-byte apart from rejecting an all-blank value.
    return value if value.strip() else ""


def _normalize_measurement(payload: dict[str, Any]) -> dict[str, Any]:
    day = _date(payload.get("date"))
    metric = _text(payload.get("metric"), "metric", required=True)
    value = _number(payload.get("value"), "value", required=True)
    second_value = _number(payload.get("second_value"), "second_value")
    unit = _text(payload.get("unit"), "unit", required=True)
    is_blood_pressure = "血压" in metric or "blood pressure" in metric.casefold() or unit.casefold() == "mmhg"
    if is_blood_pressure:
        if second_value is None:
            raise ValueError("blood pressure requires value=systolic and second_value=diastolic")
        if value <= second_value:
            raise ValueError("blood pressure systolic value must exceed diastolic second_value")
        if not 40 <= value <= 300 or not 20 <= second_value <= 200:
            raise ValueError("blood pressure values are outside the supported validation range")
    timestamp = _validate_time(payload.get("time"), "time")
    source = _text(payload.get("source") or "user", "source", required=True)
    method = _entry_method(payload)
    source_event_id = _source_event_id(payload)
    source_event_item_id = _source_event_item_id(payload, source_event_id)
    original_text = _clean_original_text(payload)
    if source_event_item_id:
        identity = [source, method, source_event_id, source_event_item_id]
    elif source_event_id:
        identity = [source, method, source_event_id, metric]
    else:
        identity = [day, timestamp, metric, unit, source, method, original_text]
    record_id = _text(payload.get("record_id"), "record_id") or stable_record_id(
        # Keep the source event identity independent of a corrected reading so
        # a re-parse updates the same row instead of creating a contradiction.
        "measurement",
        identity,
    )
    return {
        "record_id": record_id,
        "date": day,
        "time": timestamp,
        "metric": metric,
        "value": value,
        "second_value": second_value,
        "unit": unit,
        "source": source,
        "method": method,
        "source_event_id": source_event_id,
        "source_event_item_id": source_event_item_id,
        "confidence": _text(payload.get("confidence") or "user-reported", "confidence"),
        "original_text": original_text,
        "notes": _text(payload.get("notes"), "notes"),
        "imported_at": _text(payload.get("imported_at"), "imported_at") or utc_now(),
    }


def _normalize_workout(payload: dict[str, Any]) -> dict[str, Any]:
    day = _date(payload.get("date"))
    start_time = _validate_time(payload.get("start_time"), "start_time")
    workout_type = _text(payload.get("workout_type"), "workout_type", required=True)
    source = _text(payload.get("training_source") or payload.get("source") or "user", "training_source")
    method = _entry_method(payload)
    source_event_id = _source_event_id(payload)
    source_event_item_id = _source_event_item_id(payload, source_event_id)
    original_text = _clean_original_text(payload)
    numeric_fields = {
        "duration_minutes": _number(payload.get("duration_minutes"), "duration_minutes", minimum=0),
        "distance_km": _number(payload.get("distance_km"), "distance_km", minimum=0),
        "calories_kcal": _number(payload.get("calories_kcal"), "calories_kcal", minimum=0),
        "average_heart_rate_bpm": _number(payload.get("average_heart_rate_bpm"), "average_heart_rate_bpm", minimum=0),
        "max_heart_rate_bpm": _number(payload.get("max_heart_rate_bpm"), "max_heart_rate_bpm", minimum=0),
        "intensity_rpe": _number(payload.get("intensity_rpe"), "intensity_rpe", minimum=0),
    }
    if numeric_fields["intensity_rpe"] is not None and numeric_fields["intensity_rpe"] > 10:
        raise ValueError("intensity_rpe must be between 0 and 10")
    external_id = _text(payload.get("external_id"), "external_id")
    if source_event_item_id:
        identity = [source, method, source_event_id, source_event_item_id]
    elif source_event_id:
        identity = [source, method, source_event_id, workout_type]
    else:
        identity = [
            external_id,
            day,
            start_time,
            workout_type,
            source,
            method,
            original_text,
        ]
    record_id = _text(payload.get("record_id"), "record_id") or stable_record_id(
        "workout", identity
    )
    return {
        "record_id": record_id,
        "date": day,
        "start_time": start_time,
        "workout_type": workout_type,
        **numeric_fields,
        "muscle_groups": _text(payload.get("muscle_groups"), "muscle_groups"),
        "training_source": source,
        "external_id": external_id,
        "method": method,
        "source_event_id": source_event_id,
        "source_event_item_id": source_event_item_id,
        "confidence": _text(payload.get("confidence") or "user-reported", "confidence"),
        "original_text": original_text,
        "notes": _text(payload.get("notes"), "notes"),
        "imported_at": _text(payload.get("imported_at"), "imported_at") or utc_now(),
    }


def _normalize_nutrients(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("nutrients must be a list")
    output: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"nutrients[{index}] must be an object")
        amount = _number(item.get("amount"), f"nutrients[{index}].amount", required=True, minimum=0)
        output.append(
            {
                "id": _text(item.get("id"), f"nutrients[{index}].id", required=True),
                "name": _text(item.get("name"), f"nutrients[{index}].name", required=True),
                "amount": amount,
                "unit": _text(item.get("unit"), f"nutrients[{index}].unit", required=True),
                "source": _text(item.get("source"), f"nutrients[{index}].source"),
                "basis": _text(item.get("basis"), f"nutrients[{index}].basis"),
                "retrieved_at": _text(item.get("retrieved_at"), f"nutrients[{index}].retrieved_at"),
                "notes": _text(item.get("notes"), f"nutrients[{index}].notes"),
            }
        )
    return output


def _normalize_food(payload: dict[str, Any]) -> dict[str, Any]:
    # This gate prevents purchase lists, meal ideas, and hypothetical plans from
    # silently becoming consumed-food records.
    if payload.get("consumed") is not True:
        raise ValueError("food records require consumed=true; plans and purchases must not be logged as intake")
    day = _date(payload.get("date"))
    timestamp = _validate_time(payload.get("time"), "time")
    food_name = _text(payload.get("food_name"), "food_name", required=True)
    meal = _text(payload.get("meal"), "meal")
    source = _text(payload.get("source") or "agent estimate", "source")
    method = _entry_method(payload)
    source_event_id = _source_event_id(payload)
    source_event_item_id = _source_event_item_id(payload, source_event_id)
    original_text = _clean_original_text(payload)
    grams = {
        "estimated_grams": _number(payload.get("estimated_grams"), "estimated_grams", minimum=0),
        "grams_low": _number(payload.get("grams_low"), "grams_low", minimum=0),
        "grams_high": _number(payload.get("grams_high"), "grams_high", minimum=0),
    }
    if grams["grams_low"] is not None and grams["grams_high"] is not None and grams["grams_low"] > grams["grams_high"]:
        raise ValueError("grams_low cannot exceed grams_high")
    summary = payload.get("summary_nutrients") or {}
    if not isinstance(summary, dict):
        raise ValueError("summary_nutrients must be an object")
    clean_summary: dict[str, float | None] = {}
    for key, value in summary.items():
        if not isinstance(key, str):
            raise ValueError("summary_nutrients keys must be text")
        clean_summary[key] = _number(value, f"summary_nutrients.{key}", minimum=0)
    if source_event_item_id:
        identity = [source, method, source_event_id, source_event_item_id]
    elif source_event_id:
        identity = [source, method, source_event_id, meal, food_name]
    else:
        identity = [day, timestamp, meal, food_name, source, method, original_text]
    record_id = _text(payload.get("record_id"), "record_id") or stable_record_id(
        "food", identity
    )
    return {
        "record_id": record_id,
        "date": day,
        "time": timestamp,
        "meal": meal,
        "food_name": food_name,
        **grams,
        "summary_nutrients": clean_summary,
        "nutrients": _normalize_nutrients(payload.get("nutrients")),
        "source": source,
        "method": method,
        "source_event_id": source_event_id,
        "source_event_item_id": source_event_item_id,
        "estimation_notes": _text(payload.get("estimation_notes"), "estimation_notes"),
        "confidence": _text(payload.get("confidence") or "estimated", "confidence"),
        "image_reference": _text(payload.get("image_reference"), "image_reference"),
        "original_text": original_text,
        "notes": _text(payload.get("notes"), "notes"),
        "recorded_at": _text(payload.get("recorded_at"), "recorded_at") or utc_now(),
        "consumed": True,
    }


def normalize_record(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported record kind: {kind}")
    if not isinstance(payload, dict):
        raise ValueError("record payload must be a JSON object")
    if kind == "measurement":
        return _normalize_measurement(payload)
    if kind == "workout":
        return _normalize_workout(payload)
    return _normalize_food(payload)


def record(database: HealthDatabase, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_record(kind, payload)
    database.upsert(kind, normalized, normalized["record_id"])
    return normalized
