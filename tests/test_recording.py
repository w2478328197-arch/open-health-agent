from __future__ import annotations

from pathlib import Path

import pytest

from oha.database import HealthDatabase
from oha.recording import normalize_record, record


def test_food_requires_confirmed_consumption() -> None:
    payload = {
        "date": "2026-01-02",
        "food_name": "菜单上的合成测试餐",
        "summary_nutrients": {"energy_kcal": 500},
    }
    with pytest.raises(ValueError, match="consumed=true"):
        normalize_record("food", payload)


def test_food_preserves_original_text_and_portion_uncertainty() -> None:
    original = "  我吃了这份合成测试餐，份量大约一碗。\n"
    normalized = normalize_record(
        "food",
        {
            "consumed": True,
            "date": "2026-01-02",
            "time": "12:30",
            "meal": "午餐",
            "food_name": "合成测试餐",
            "estimated_grams": 300,
            "grams_low": 250,
            "grams_high": 350,
            "original_text": original,
            "summary_nutrients": {
                "energy_kcal": 600,
                "protein_g": 35,
                "fat_g": 20,
                "carbohydrate_g": 70,
            },
            "nutrients": [
                {
                    "id": "sodium",
                    "name": "钠",
                    "amount": 650,
                    "unit": "mg",
                    "source": "synthetic estimate",
                    "basis": "consumed portion",
                }
            ],
        },
    )
    assert normalized["original_text"] == original
    assert normalized["grams_low"] == 250
    assert normalized["grams_high"] == 350
    assert normalized["nutrients"][0]["amount"] == 650


def test_food_rejects_negative_nutrients_and_inverted_range() -> None:
    base = {
        "consumed": True,
        "date": "2026-01-02",
        "food_name": "合成测试餐",
    }
    with pytest.raises(ValueError, match="at least 0"):
        normalize_record("food", base | {"summary_nutrients": {"protein_g": -1}})
    with pytest.raises(ValueError, match="grams_low cannot exceed grams_high"):
        normalize_record("food", base | {"grams_low": 400, "grams_high": 200})


def test_workout_validates_rpe_and_uses_stable_id() -> None:
    payload = {
        "date": "2026-01-02",
        "start_time": "18:00",
        "workout_type": "合成抗阻训练",
        "duration_minutes": 45,
        "intensity_rpe": 7,
        "original_text": "完成了 45 分钟训练",
    }
    first = normalize_record("workout", payload)
    second = normalize_record("workout", payload)
    assert first["record_id"] == second["record_id"]
    with pytest.raises(ValueError, match="between 0 and 10"):
        normalize_record("workout", payload | {"intensity_rpe": 11})


def test_record_reimport_updates_one_database_row(tmp_path: Path) -> None:
    payload = {
        "record_id": "synthetic-measurement",
        "date": "2026-01-02",
        "metric": "体重",
        "value": 70,
        "unit": "kg",
        "original_text": "测试值",
    }
    with HealthDatabase(tmp_path / "health.sqlite3") as database:
        record(database, "measurement", payload)
        record(database, "measurement", payload | {"value": 69.8})
        rows = database.list_records("measurement")
    assert len(rows) == 1
    assert rows[0]["value"] == 69.8


def test_manual_measurement_correction_keeps_source_event_id() -> None:
    payload = {
        "date": "2026-01-02",
        "time": "08:00",
        "metric": "体重",
        "value": 70,
        "unit": "kg",
        "source": "user",
        "original_text": "今早体重七十公斤",
    }
    first = normalize_record("measurement", payload)
    corrected = normalize_record("measurement", payload | {"value": 69.8})
    assert first["record_id"] == corrected["record_id"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_manual_records_reject_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        normalize_record(
            "measurement",
            {
                "date": "2026-01-02",
                "metric": "体重",
                "value": value,
                "unit": "kg",
            },
        )


def test_blood_pressure_requires_a_valid_systolic_diastolic_pair() -> None:
    base = {
        "date": "2026-01-02",
        "metric": "血压",
        "value": 120,
        "unit": "mmHg",
        "original_text": "合成测试血压",
    }
    with pytest.raises(ValueError, match="second_value"):
        normalize_record("measurement", base)
    with pytest.raises(ValueError, match="exceed"):
        normalize_record("measurement", base | {"value": 80, "second_value": 120})

    normalized = normalize_record("measurement", base | {"second_value": 80})
    assert normalized["value"] == 120
    assert normalized["second_value"] == 80
