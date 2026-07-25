from __future__ import annotations

import json
from pathlib import Path

import pytest

import oha.database as database_module
from oha.database import HealthDatabase, stable_record_id


def test_stable_record_id_is_deterministic_and_kind_scoped() -> None:
    parts = ["2026-01-02", "synthetic", 42]
    assert stable_record_id("food", parts) == stable_record_id("food", parts)
    assert stable_record_id("food", parts) != stable_record_id("measurement", parts)


def test_upsert_is_record_idempotent_and_audited(tmp_path: Path) -> None:
    path = tmp_path / "health.sqlite3"
    with HealthDatabase(path) as database:
        payload = {
            "record_id": "synthetic-measurement-1",
            "date": "2026-01-02",
            "metric": "体重",
            "value": 70,
            "unit": "kg",
            "source": "synthetic",
        }
        database.upsert("measurement", payload)
        database.upsert("measurement", payload | {"value": 69.8})
        database.upsert(
            "measurement",
            payload | {"value": 69.8, "batch_id": "new-batch", "imported_at": "later"},
        )

        rows = database.list_records("measurement")
        assert len(rows) == 1
        assert rows[0]["value"] == 69.8
        audit = database.connection.execute(
            "SELECT action, payload_json FROM audit_events ORDER BY event_id"
        ).fetchall()
        assert [row["action"] for row in audit] == ["create", "update"]
        assert json.loads(audit[-1]["payload_json"])["value"] == 69.8


def test_partial_daily_merge_preserves_prior_fields(tmp_path: Path) -> None:
    with HealthDatabase(tmp_path / "health.sqlite3") as database:
        database.upsert(
            "daily",
            {
                "record_id": "2026-01-02",
                "date": "2026-01-02",
                "steps": 1000,
                "weight_kg": 70.0,
                "batch_id": "complete",
            },
        )
        database.upsert(
            "daily",
            {
                "record_id": "2026-01-02",
                "date": "2026-01-02",
                "steps": 1200,
                "batch_id": "partial",
            },
            merge_existing=True,
        )
        row = database.get("daily", "2026-01-02")
        assert row is not None
        assert row["steps"] == 1200
        assert row["weight_kg"] == 70.0


def test_wearable_coverage_combines_empty_type_status_and_observations(
    tmp_path: Path,
) -> None:
    with HealthDatabase(tmp_path / "health.sqlite3") as database:
        database.upsert(
            "wearable_coverage",
            {
                "record_id": "coverage-heart-rate",
                "data_type": "heart-rate",
                "operations": ["list"],
                "grains": ["sample"],
                "status": "success",
                "failed_query_count": 0,
                "last_checked_at": "2026-01-03T00:00:00+00:00",
            },
        )
        database.upsert(
            "wearable_coverage",
            {
                "record_id": "coverage-ecg",
                "data_type": "electrocardiogram",
                "operations": ["list"],
                "grains": ["waveform"],
                "status": "failed",
                "failed_query_count": 1,
                "last_checked_at": "2026-01-03T00:00:00+00:00",
            },
        )
        database.upsert(
            "wearable",
            {
                "record_id": "wearable-heart-rate-1",
                "date": "2026-01-02",
                "data_type": "heart-rate",
                "operation": "list",
                "grain": "sample",
                "source": "synthetic.watch",
                "data_until": "2026-01-02T08:00:00+00:00",
                "provider_data": {"beatsPerMinute": 70},
            },
        )
        coverage = {
            row["data_type"]: row for row in database.wearable_coverage()
        }

    assert coverage["heart-rate"]["record_count"] == 1
    assert coverage["heart-rate"]["first_date"] == "2026-01-02"
    assert coverage["heart-rate"]["source_count"] == 1
    assert coverage["electrocardiogram"]["record_count"] == 0
    assert coverage["electrocardiogram"]["status"] == "failed"


def test_delete_requires_existing_record_and_preserves_audit(tmp_path: Path) -> None:
    with HealthDatabase(tmp_path / "health.sqlite3") as database:
        database.upsert("goal", {"record_id": "goal-1", "date": "2026-01-01"})
        assert database.delete("goal", "goal-1", "synthetic correction") is True
        assert database.delete("goal", "goal-1", "repeat") is False
        assert database.get("goal", "goal-1") is None
        actions = [
            row["action"]
            for row in database.connection.execute(
                "SELECT action FROM audit_events ORDER BY event_id"
            ).fetchall()
        ]
        assert actions == ["create", "delete"]


def test_sync_run_status_round_trip(tmp_path: Path) -> None:
    with HealthDatabase(tmp_path / "health.sqlite3") as database:
        database.begin_sync("batch-1", "2026-01-01", "2026-01-03")
        database.finish_sync("batch-1", "partial", 2, 1, 1, "2026-01-02T20:00:00Z", ["synthetic gap"])
        run = database.list_sync_runs()[0]
        assert run["status"] == "partial"
        assert run["daily_count"] == 2
        assert run["errors"] == ["synthetic gap"]


def test_same_second_sync_runs_use_insertion_order_for_latest_and_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        database_module,
        "utc_now",
        lambda: "2026-01-02T03:04:05+00:00",
    )
    batches = ["z-oldest", "y-older", "b-newer", "a-newest"]

    with HealthDatabase(tmp_path / "health.sqlite3") as database:
        for batch_id in batches:
            database.begin_sync(batch_id, "2026-01-01", "2026-01-02")
            database.finish_sync(batch_id, "success", 1, 0, 0, "2026-01-02", [])

        schema = database.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sync_runs'"
        ).fetchone()[0]
        assert "WITHOUT ROWID" not in schema.upper()
        assert [run["batch_id"] for run in database.list_sync_runs()] == list(
            reversed(batches)
        )

        database.prune_history(audit_limit=1, sync_limit=2)

        assert [run["batch_id"] for run in database.list_sync_runs()] == [
            "a-newest",
            "b-newer",
        ]


def test_history_retention_is_bounded(tmp_path: Path) -> None:
    with HealthDatabase(tmp_path / "health.sqlite3") as database:
        for index in range(5):
            database.upsert(
                "measurement",
                {
                    "record_id": f"measurement-{index}",
                    "date": "2026-01-02",
                    "metric": "synthetic",
                    "value": index,
                },
            )
            batch = f"batch-{index}"
            database.begin_sync(batch, "2026-01-01", "2026-01-02")
            database.finish_sync(batch, "success", 1, 0, 0, "2026-01-02", [])

        database.prune_history(audit_limit=2, sync_limit=3)
        audit_count = database.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
        sync_count = database.connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
        assert audit_count == 2
        assert sync_count == 3
