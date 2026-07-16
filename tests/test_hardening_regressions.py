from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

import health_agent as health_cli
from oha.database import HealthDatabase


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "skills" / "open-health-agent" / "scripts" / "health_agent.py"
FIXTURES = Path(__file__).parent / "fixtures" / "ghealth"


def run_cli(
    *arguments: str, expected_code: int = 0
) -> tuple[subprocess.CompletedProcess[str], dict]:
    environment = os.environ.copy()
    if "--fixture-dir" in arguments:
        environment["OPEN_HEALTH_AGENT_TEST_MODE"] = "1"
    completed = subprocess.run(
        [sys.executable, str(CLI), *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == expected_code, completed.stderr or completed.stdout
    stream = completed.stdout if expected_code == 0 else completed.stderr
    return completed, json.loads(stream)


def initialize(home: Path, workbook: Path | None = None) -> tuple[str, ...]:
    base = ("--home", str(home))
    arguments = [*base, "init", "--timezone", "UTC"]
    if workbook is not None:
        arguments.extend(("--workbook", str(workbook)))
    run_cli(*arguments)
    return base


def incompatible_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "健康日报"
    sheet.append(["synthetic incompatible header"])
    workbook.save(path)
    workbook.close()


@pytest.mark.parametrize(
    "artifact_name",
    [
        "AGENTS.md",
        "profile.json",
        "state.json",
        "health.sqlite3",
        "health.sqlite3-wal",
        "health.sqlite3-shm",
        "selected-workbook",
    ],
)
def test_first_init_never_claims_or_removes_orphaned_managed_data(
    tmp_path: Path, artifact_name: str
) -> None:
    home = tmp_path / "synthetic-home"
    home.mkdir()
    workbook = tmp_path / "existing-ledger.xlsx"
    if artifact_name == "selected-workbook":
        protected = workbook
    else:
        protected = home / artifact_name
    sentinel = b"synthetic-private-sentinel"
    protected.write_bytes(sentinel)
    arguments = [
        "--home",
        str(home),
        "init",
        "--timezone",
        "UTC",
        "--workbook",
        str(workbook),
    ]

    _, failure = run_cli(*arguments, expected_code=1)

    assert failure["code"] == "initialization_conflict"
    assert protected.read_bytes() == sentinel
    assert not (home / "config.json").exists()
    assert str(tmp_path) not in json.dumps(failure)


@pytest.mark.parametrize("database_variant", ["zero-byte", "wrong-schema"])
def test_invalid_canonical_database_is_never_initialized_and_doctor_is_read_only(
    tmp_path: Path,
    database_variant: str,
) -> None:
    home = tmp_path / "synthetic-home"
    workbook_path = tmp_path / "synthetic-ledger.xlsx"
    base = initialize(home, workbook_path)
    database_path = home / "health.sqlite3"

    database_path.unlink()
    for suffix in ("-wal", "-shm"):
        database_path.with_name(database_path.name + suffix).unlink(missing_ok=True)
    if database_variant == "zero-byte":
        database_path.touch()
    else:
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")

    protected_before = {
        "database": database_path.read_bytes(),
        "workbook": workbook_path.read_bytes(),
        "config": (home / "config.json").read_bytes(),
        "profile": (home / "profile.json").read_bytes(),
        "state": (home / "state.json").read_bytes(),
    }

    _, doctor = run_cli(*base, "doctor", "--offline")
    sqlite_check = next(
        item for item in doctor["checks"] if item["check"] == "SQLite integrity"
    )
    assert sqlite_check["ok"] is False
    assert str(tmp_path) not in json.dumps(doctor)
    assert database_path.read_bytes() == protected_before["database"]
    assert workbook_path.read_bytes() == protected_before["workbook"]
    assert (home / "config.json").read_bytes() == protected_before["config"]
    assert (home / "profile.json").read_bytes() == protected_before["profile"]
    assert (home / "state.json").read_bytes() == protected_before["state"]

    _, rejected = run_cli(
        *base,
        "context",
        "--date",
        "2026-01-02",
        expected_code=1,
    )
    assert rejected["code"] == "canonical_database_missing"
    assert database_path.read_bytes() == protected_before["database"]
    assert workbook_path.read_bytes() == protected_before["workbook"]


def test_partial_record_correction_preserves_provenance_and_user_wording(
    tmp_path: Path,
) -> None:
    home = tmp_path / "synthetic-home"
    base = initialize(home)
    initial = {
        "date": "2026-01-02",
        "time": "08:00",
        "metric": "synthetic metric",
        "value": 70,
        "unit": "kg",
        "source": "synthetic-user-source",
        "method": "synthetic-text-channel",
        "source_event_id": "synthetic-event-001",
        "original_text": "synthetic original wording",
        "notes": "synthetic private note",
    }
    _, recorded = run_cli(
        *base,
        "record",
        "--kind",
        "measurement",
        "--json",
        json.dumps(initial),
    )
    record_id = recorded["record"]["record_id"]

    run_cli(
        *base,
        "record",
        "--kind",
        "measurement",
        "--record-id",
        record_id,
        "--json",
        json.dumps({"value": 69.8}),
    )

    with HealthDatabase(home / "health.sqlite3", create=False) as database:
        corrected = database.get("measurement", record_id)
    assert corrected is not None
    assert corrected["value"] == 69.8
    for field in ("source_event_id", "source", "method", "original_text", "notes"):
        assert corrected[field] == initial[field]


def test_goal_safety_constraints_are_available_to_advice_context(tmp_path: Path) -> None:
    home = tmp_path / "synthetic-home"
    base = initialize(home)
    safety_constraint = "synthetic safety constraint"
    run_cli(
        *base,
        "goal",
        "set",
        "--text",
        "synthetic movement goal",
        "--effective-date",
        "2026-01-02",
        "--safety-constraint",
        safety_constraint,
    )

    _, context = run_cli(*base, "context", "--date", "2026-01-02")
    goals = context["profile"]["active_goals"]
    assert len(goals) == 1
    assert goals[0]["safety_constraints"] == safety_constraint


def test_sync_export_failure_sets_pending_and_later_fixture_sync_clears_it(
    tmp_path: Path,
) -> None:
    home = tmp_path / "synthetic-home"
    workbook_path = tmp_path / "synthetic-ledger.xlsx"
    base = initialize(home, workbook_path)
    incompatible_workbook(workbook_path)

    _, failed = run_cli(
        *base,
        "sync",
        "--from-date",
        "2026-01-01",
        "--to-date",
        "2026-01-03",
        "--fixture-dir",
        str(FIXTURES),
        expected_code=1,
    )
    assert failed["code"] == "workbook_incompatible"
    failed_state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert failed_state["workbook_export_pending"] is True
    assert failed_state["workbook_export_pending_since"]

    workbook_path.unlink()
    _, succeeded = run_cli(
        *base,
        "sync",
        "--from-date",
        "2026-01-01",
        "--to-date",
        "2026-01-03",
        "--fixture-dir",
        str(FIXTURES),
    )
    assert succeeded["status"] == "success"
    recovered_state = json.loads((home / "state.json").read_text(encoding="utf-8"))
    assert recovered_state["workbook_export_pending"] is False
    assert recovered_state["workbook_export_pending_since"] is None


def test_first_init_rejects_existing_workbook_and_is_retryable(tmp_path: Path) -> None:
    home = tmp_path / "synthetic-home"
    incompatible_path = tmp_path / "incompatible-ledger.xlsx"
    incompatible_workbook(incompatible_path)
    incompatible_before = incompatible_path.read_bytes()

    _, failed = run_cli(
        "--home",
        str(home),
        "init",
        "--timezone",
        "UTC",
        "--workbook",
        str(incompatible_path),
        expected_code=1,
    )
    assert failed["code"] == "initialization_conflict"
    assert incompatible_path.read_bytes() == incompatible_before
    for owned_name in (
        "config.json",
        "AGENTS.md",
        "profile.json",
        "state.json",
        "health.sqlite3",
        "health.sqlite3-wal",
        "health.sqlite3-shm",
    ):
        assert not (home / owned_name).exists()

    retry_path = tmp_path / "retry-ledger.xlsx"
    _, retried = run_cli(
        "--home",
        str(home),
        "init",
        "--timezone",
        "UTC",
        "--workbook",
        str(retry_path),
    )
    assert retried["status"] == "ready"
    assert (home / "config.json").is_file()
    assert (home / "health.sqlite3").is_file()
    assert retry_path.is_file()


def test_sync_batch_rolls_back_every_health_row_when_one_upsert_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "synthetic-home"
    base = initialize(home)
    monkeypatch.setenv("OPEN_HEALTH_AGENT_TEST_MODE", "1")
    original_upsert = HealthDatabase.upsert
    calls = 0

    def fail_second_upsert(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("synthetic mid-batch failure")
        return original_upsert(self, *args, **kwargs)

    monkeypatch.setattr(HealthDatabase, "upsert", fail_second_upsert)

    with pytest.raises(sqlite3.OperationalError, match="mid-batch failure"):
        health_cli.command_sync(
            health_cli.argparse.Namespace(
                home=str(home),
                from_date="2026-01-01",
                to_date="2026-01-03",
                lookback_days=None,
                fixture_dir=str(FIXTURES),
                scheduled=False,
                quiet=False,
                static_runtime_fingerprint=None,
                runtime_fingerprint=None,
                verbose_path=False,
            )
        )

    with HealthDatabase(home / "health.sqlite3", create=False) as database:
        assert database.list_records("daily") == []
        assert database.list_records("measurement") == []
        assert database.list_records("workout") == []
        runs = database.list_sync_runs(limit=1)
    assert runs[0]["status"] == "failed"
    assert runs[0]["daily_count"] == 0
    assert runs[0]["measurement_count"] == 0
    assert runs[0]["workout_count"] == 0


def test_scheduler_log_capping_never_follows_symlinks(tmp_path: Path) -> None:
    home = tmp_path / "synthetic-home"
    initialize(home)
    config = health_cli.load_config(home)
    log_path = home / "logs" / "scheduler.out.log"
    target = tmp_path / "protected-file"
    protected = b"p" * 128
    target.write_bytes(protected)
    log_path.symlink_to(target)

    health_cli._cap_scheduler_logs(config, maximum_bytes=16)

    assert target.read_bytes() == protected
    assert log_path.is_symlink()
    log_path.unlink()
    log_path.write_bytes(b"x" * 128)
    log_path.chmod(0o644)

    health_cli._cap_scheduler_logs(config, maximum_bytes=16)

    assert log_path.read_bytes() == b""
    if os.name != "nt":
        assert log_path.stat().st_mode & 0o777 == 0o600


def test_workbook_outbox_is_durable_before_record_or_sync_database_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "synthetic-home"
    initialize(home)
    monkeypatch.setenv("OPEN_HEALTH_AGENT_TEST_MODE", "1")
    original_export = health_cli.export_workbook
    observed_operations: list[str] = []

    def observe_pending(config, database, template):
        state = json.loads((home / "state.json").read_text(encoding="utf-8"))
        assert state["workbook_export_pending"] is True
        operation = state["workbook_export_pending_operation"]
        assert isinstance(operation, str) and operation
        observed_operations.append(operation)
        return original_export(config, database, template)

    monkeypatch.setattr(health_cli, "export_workbook", observe_pending)
    health_cli.command_record(
        health_cli.argparse.Namespace(
            home=str(home),
            kind="measurement",
            record_id=None,
            json_payload=json.dumps(
                {
                    "date": "2026-01-02",
                    "metric": "synthetic",
                    "value": 1,
                    "unit": "test",
                }
            ),
            file=None,
        )
    )
    health_cli.command_sync(
        health_cli.argparse.Namespace(
            home=str(home),
            from_date="2026-01-01",
            to_date="2026-01-03",
            lookback_days=None,
            fixture_dir=str(FIXTURES),
            scheduled=False,
            quiet=False,
            static_runtime_fingerprint=None,
            runtime_fingerprint=None,
            verbose_path=False,
        )
    )

    assert observed_operations[0].startswith("record:measurement:")
    assert observed_operations[1].startswith("sync_fixture_manual_")


def test_record_result_exposes_only_the_opaque_record_identifier(tmp_path: Path) -> None:
    home = tmp_path / "synthetic-home"
    base = initialize(home)
    private_fields = {
        "original_text": "synthetic private original text",
        "source_event_id": "synthetic-private-event-id",
        "notes": "synthetic private notes",
        "image_reference": "synthetic-private-image-reference",
    }
    payload = {
        "consumed": True,
        "date": "2026-01-02",
        "food_name": "synthetic consumed food",
        **private_fields,
    }

    _, result = run_cli(
        *base,
        "record",
        "--kind",
        "food",
        "--json",
        json.dumps(payload),
    )

    assert set(result["record"]) == {"record_id"}
    encoded = json.dumps(result)
    assert "workbook" not in result
    assert str(tmp_path) not in encoded
    for key, value in private_fields.items():
        assert key not in result["record"]
        assert value not in encoded


def test_record_export_pending_response_never_exposes_the_workbook_path(
    tmp_path: Path,
) -> None:
    home = tmp_path / "synthetic-home"
    workbook_path = tmp_path / "private-synthetic-ledger.xlsx"
    base = initialize(home, workbook_path)
    incompatible_workbook(workbook_path)

    _, result = run_cli(
        *base,
        "record",
        "--kind",
        "measurement",
        "--json",
        json.dumps(
            {
                "date": "2026-01-02",
                "metric": "synthetic metric",
                "value": 70,
                "unit": "kg",
            }
        ),
    )

    assert result["status"] == "recorded_export_pending"
    assert result["database_write"] == "succeeded"
    assert result["workbook_export"] == "failed"
    assert "workbook" not in result
    assert str(tmp_path) not in json.dumps(result)


def test_noop_and_invalid_records_do_not_create_sqlite_backups(tmp_path: Path) -> None:
    home = tmp_path / "synthetic-home"
    base = initialize(home)
    payload = {
        "date": "2026-01-02",
        "time": "08:00",
        "metric": "synthetic metric",
        "value": 70,
        "unit": "kg",
        "source_event_id": "synthetic-event-001",
    }
    run_cli(
        *base,
        "record",
        "--kind",
        "measurement",
        "--json",
        json.dumps(payload),
    )
    backup_directory = home / "backups"
    backups_after_write = {
        path.name: path.read_bytes()
        for path in backup_directory.glob("health-ledger-db-*.sqlite3")
    }
    assert backups_after_write

    run_cli(
        *base,
        "record",
        "--kind",
        "measurement",
        "--json",
        json.dumps(payload),
    )
    _, invalid = run_cli(
        *base,
        "record",
        "--kind",
        "measurement",
        "--json",
        json.dumps(payload | {"value": "not-a-number"}),
        expected_code=1,
    )
    assert invalid["code"] == "invalid_input"

    backups_after_noops = {
        path.name: path.read_bytes()
        for path in backup_directory.glob("health-ledger-db-*.sqlite3")
    }
    assert backups_after_noops == backups_after_write
