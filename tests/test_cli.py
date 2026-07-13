from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

import health_agent as health_cli
from oha.database import HealthDatabase


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "open-health-agent"
CLI = SKILL_ROOT / "scripts" / "health_agent.py"
TEMPLATE = SKILL_ROOT / "assets" / "health-ledger.xlsx"
FIXTURES = Path(__file__).parent / "fixtures" / "ghealth"


def assert_private_mode(path: Path) -> None:
    assert path.exists()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def run_cli(*arguments: str, expected_code: int = 0) -> tuple[subprocess.CompletedProcess[str], dict]:
    completed = subprocess.run(
        [sys.executable, str(CLI), *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expected_code, completed.stderr or completed.stdout
    stream = completed.stdout if expected_code == 0 else completed.stderr
    return completed, json.loads(stream)


def test_cli_help_lists_core_workflows() -> None:
    completed = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    for command in ("init", "sync", "record", "goal", "context", "scheduler"):
        assert command in completed.stdout

    record_help = subprocess.run(
        [sys.executable, str(CLI), "record", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert record_help.returncode == 0
    for field in ("second_value", "consumed=true", "summary_nutrients", "original_text"):
        assert field in record_help.stdout


def test_emit_round_trips_unicode_through_an_ascii_only_console(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AsciiOnlyStream:
        def __init__(self) -> None:
            self.value = ""

        def write(self, value: str) -> int:
            value.encode("ascii")
            self.value += value
            return len(value)

        def flush(self) -> None:
            return None

    stream = AsciiOnlyStream()
    monkeypatch.setattr(sys, "stdout", stream)

    health_cli.emit({"工作簿": "健康档案.xlsx"})

    assert json.loads(stream.value) == {"工作簿": "健康档案.xlsx"}


@pytest.mark.skipif(not TEMPLATE.exists(), reason="workbook asset is still being generated")
def test_cli_private_end_to_end_with_synthetic_ghealth(tmp_path: Path) -> None:
    home = tmp_path / "private-health-home"
    workbook_path = tmp_path / "健康档案.xlsx"
    base = ("--home", str(home))

    _, initialized = run_cli(
        *base,
        "init",
        "--workbook",
        str(workbook_path),
        "--timezone",
        "UTC",
        "--ghealth-command",
        "missing-ghealth-for-synthetic-test",
    )
    assert initialized["status"] == "ready"
    assert Path(initialized["database"]).exists()
    assert workbook_path.exists()
    assert_private_mode(home / "AGENTS.md")
    assert_private_mode(home / "profile.json")

    _, doctor = run_cli(*base, "doctor", "--offline")
    assert doctor["status"] == "ok"
    ghealth_check = next(
        item for item in doctor["checks"] if item["check"] == "ghealth command"
    )
    assert ghealth_check["ok"] is False
    assert ghealth_check["required"] is False
    assert next(
        item for item in doctor["checks"] if item["check"] == "workbook managed schema"
    )["ok"] is True
    assert next(
        item for item in doctor["checks"] if item["check"] == "goal projections"
    )["ok"] is True

    private_before = {
        path.name: path.read_bytes()
        for path in (
            home / "config.json",
            home / "AGENTS.md",
            home / "profile.json",
            home / "state.json",
            home / "health.sqlite3",
            workbook_path,
        )
    }
    _, repeated_init = run_cli(*base, "init")
    assert repeated_init["existing_configuration_reused"] is True
    assert repeated_init["workbook_exported"] is False
    private_after = {
        path.name: path.read_bytes()
        for path in (
            home / "config.json",
            home / "AGENTS.md",
            home / "profile.json",
            home / "state.json",
            home / "health.sqlite3",
            workbook_path,
        )
    }
    assert private_after == private_before

    _, consent_error = run_cli(*base, "onboarding", "grant-consent", expected_code=1)
    assert consent_error["code"] == "invalid_input"
    assert "Skill explanation" in consent_error["error"]
    run_cli(*base, "onboarding", "mark-explained")
    _, consent = run_cli(*base, "onboarding", "grant-consent")
    assert consent["onboarding_explained_at"]
    assert consent["onboarding_consent_at"]

    for _ in range(2):
        _, synced = run_cli(
            *base,
            "sync",
            "--from-date",
            "2026-01-01",
            "--to-date",
            "2026-01-03",
            "--fixture-dir",
            str(FIXTURES),
        )
        assert synced["status"] == "success"
        assert synced["counts"] == {"daily": 1, "measurements": 1, "workouts": 1}

    food_payload = {
        "consumed": True,
        "date": "2026-01-02",
        "time": "12:30",
        "meal": "午餐",
        "food_name": "合成测试餐",
        "grams_low": 250,
        "grams_high": 350,
        "summary_nutrients": {
            "energy_kcal": 600,
            "protein_g": 35,
            "fat_g": 20,
            "carbohydrate_g": 70,
        },
        "source": "synthetic test estimate",
        "original_text": "我吃了这份合成测试餐",
    }
    _, recorded = run_cli(
        *base,
        "record",
        "--kind",
        "food",
        "--json",
        json.dumps(food_payload, ensure_ascii=False),
    )
    assert recorded["record"]["consumed"] is True

    exact_goal = "合成测试目标：每周规律完成训练"
    _, goal = run_cli(
        *base,
        "goal",
        "set",
        "--text",
        exact_goal,
        "--priority",
        "1",
        "--effective-date",
        "2026-01-02",
    )
    assert goal["goal"]["original_text"] == exact_goal
    assert exact_goal in (home / "AGENTS.md").read_text(encoding="utf-8")

    run_cli(*base, "profile", "set", "--key", "lean_mass_kg", "--value", "60")
    _, context = run_cli(*base, "context", "--date", "2026-01-02")
    assert context["profile"]["active_goals"][0]["original_text"] == exact_goal
    assert context["today"]["health"]["steps"] == 4321
    assert context["today"]["food_items"][0]["food_name"] == "合成测试餐"
    assert context["energy"]["estimated_ree_kcal"] == 1666

    connection = sqlite3.connect(home / "health.sqlite3")
    try:
        counts = dict(
            connection.execute(
                "SELECT kind, COUNT(*) FROM records GROUP BY kind"
            ).fetchall()
        )
    finally:
        connection.close()
    assert counts["daily"] == 1
    assert counts["measurement"] == 1
    assert counts["workout"] == 1
    assert counts["food"] == 1
    assert counts["goal"] == 1

    workbook = load_workbook(workbook_path, read_only=True, data_only=False)
    try:
        assert workbook["健康日报"].max_row == 2
        assert workbook["健康测量"].max_row == 2
        assert workbook["训练记录"].max_row == 2
        assert workbook["饮食记录"].max_row == 2
        assert workbook["目标历史"].max_row == 2
    finally:
        workbook.close()


@pytest.mark.skipif(not TEMPLATE.exists(), reason="workbook asset is still being generated")
def test_force_init_restores_existing_config_when_export_fails(tmp_path: Path) -> None:
    home = tmp_path / "private-health-home"
    original_workbook = tmp_path / "健康档案.xlsx"
    base = ("--home", str(home))

    run_cli(
        *base,
        "init",
        "--workbook",
        str(original_workbook),
        "--timezone",
        "UTC",
    )
    original_config = json.loads((home / "config.json").read_text(encoding="utf-8"))

    incompatible_workbook = tmp_path / "不兼容健康档案.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "健康日报"
    sheet.append(["错误表头"])
    workbook.save(incompatible_workbook)
    workbook.close()

    _, failed = run_cli(
        *base,
        "init",
        "--force",
        "--workbook",
        str(incompatible_workbook),
        expected_code=1,
    )
    assert failed["code"] == "workbook_incompatible"
    assert failed["type"] == "RuntimeError"
    assert "健康日报" not in json.dumps(failed, ensure_ascii=False)

    restored_config = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert restored_config == original_config
    assert restored_config["workbook_path"] == str(original_workbook)


@pytest.mark.skipif(not TEMPLATE.exists(), reason="workbook asset is still being generated")
def test_cli_doctor_detects_and_repairs_goal_projection_drift(tmp_path: Path) -> None:
    home = tmp_path / "private-health-home"
    base = ("--home", str(home))
    run_cli(
        *base,
        "init",
        "--workbook",
        str(tmp_path / "健康档案.xlsx"),
        "--timezone",
        "UTC",
        "--ghealth-command",
        "missing-optional-ghealth",
    )
    exact_goal = "用于目标投影修复的合成目标"
    run_cli(
        *base,
        "goal",
        "set",
        "--text",
        exact_goal,
        "--effective-date",
        "2026-01-02",
    )

    profile_path = home / "profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["goals"] = []
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    agents_path = home / "AGENTS.md"
    agents_path.write_text(
        agents_path.read_text(encoding="utf-8").replace(exact_goal, "错误投影"),
        encoding="utf-8",
    )

    _, before = run_cli(*base, "doctor", "--offline")
    assert before["status"] == "needs_attention"
    projection = next(
        item for item in before["checks"] if item["check"] == "goal projections"
    )
    assert projection["ok"] is False

    _, repaired = run_cli(*base, "goal", "repair")
    assert repaired["status"] == "goal_views_rebuilt"
    assert repaired["projection"]["ok"] is True
    assert exact_goal in agents_path.read_text(encoding="utf-8")

    _, after = run_cli(*base, "doctor", "--offline")
    assert after["status"] == "ok"


@pytest.mark.skipif(not TEMPLATE.exists(), reason="workbook asset is still being generated")
def test_partial_sync_marks_existing_daily_only_row_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "private-health-home"
    workbook_path = tmp_path / "健康档案.xlsx"
    run_cli(
        "--home",
        str(home),
        "init",
        "--workbook",
        str(workbook_path),
        "--timezone",
        "UTC",
    )
    with HealthDatabase(home / "health.sqlite3") as database:
        database.upsert(
            "daily",
            {
                "record_id": "2026-01-02",
                "date": "2026-01-02",
                "steps": 1000,
                "sources": "prior.steps.source",
                "data_until": "2026-01-02T20:00:00+00:00",
                "quality": "automatic ghealth import; blanks mean unavailable",
                "batch_id": "prior",
                "imported_at": "2026-01-02T20:05:00+00:00",
            },
        )

    class FailedStepsAdapter:
        def fetch(self, _start: str, _end: str, _batch_id: str) -> dict:
            return {
                "daily": [],
                "measurements": [],
                "workouts": [],
                "data_until": None,
                "errors": ["steps: synthetic failure"],
                "failed_query_keys": ["steps"],
            }

    monkeypatch.setattr(
        health_cli,
        "GHealthAdapter",
        lambda *_args, **_kwargs: FailedStepsAdapter(),
    )
    result = health_cli.command_sync(
        Namespace(
            home=str(home),
            from_date="2026-01-02",
            to_date="2026-01-02",
            fixture_dir=str(tmp_path),
            quiet=False,
        )
    )

    assert result["status"] == "failed"
    with HealthDatabase(home / "health.sqlite3") as database:
        daily = database.get("daily", "2026-01-02")
    assert daily is not None
    assert daily["steps"] == 1000
    assert daily["sources"] == "prior.steps.source"
    assert "partial; failed daily queries: steps" in daily["quality"]
    assert "stale fields retained from prior sync: steps" in daily["quality"]


@pytest.mark.skipif(not TEMPLATE.exists(), reason="workbook asset is still being generated")
def test_sync_summary_records_workbook_export_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "private-health-home"
    workbook_path = tmp_path / "健康档案.xlsx"
    run_cli(
        "--home",
        str(home),
        "init",
        "--workbook",
        str(workbook_path),
        "--timezone",
        "UTC",
    )
    private_marker = "private-health-detail-must-not-leak"

    def fail_export(*_args, **_kwargs):
        raise RuntimeError(private_marker)

    monkeypatch.setattr(health_cli, "export_workbook", fail_export)
    with pytest.raises(RuntimeError, match=private_marker):
        health_cli.command_sync(
            Namespace(
                home=str(home),
                from_date="2026-01-01",
                to_date="2026-01-03",
                fixture_dir=str(FIXTURES),
                quiet=False,
            )
        )

    summaries = list((home / "raw").glob("*.summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    assert summary["status"] == "failed_export"
    assert private_marker not in json.dumps(summary, ensure_ascii=False)
    with HealthDatabase(home / "health.sqlite3") as database:
        sync_run = database.list_sync_runs(1)[0]
    assert sync_run["status"] == "failed_export"
    assert json.loads((home / "state.json").read_text(encoding="utf-8"))["last_sync_status"] == "failed"


def test_cli_runtime_error_redacts_health_text_and_private_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_health_text = "我的血压是非常私密的 188/120"
    private_database = "/" + "Users/" + "private-person/.open-health-agent/health.sqlite3"

    def fail_with_private_exception(_arguments) -> dict:
        raise RuntimeError(f"record={private_health_text}; database={private_database}")

    monkeypatch.setattr(health_cli, "command_context", fail_with_private_exception)
    monkeypatch.setattr(sys, "argv", ["health-agent", "context"])

    assert health_cli.main() == 1
    payload = json.loads(capsys.readouterr().err)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload == {
        "status": "error",
        "code": "operation_failed",
        "type": "RuntimeError",
        "error": "The operation could not complete; run doctor and retry.",
    }
    assert private_health_text not in serialized
    assert private_database not in serialized


def test_cli_parser_error_does_not_echo_unrecognized_private_arguments() -> None:
    private_health_text = "我刚刚吃了私密测试餐"
    private_database = "/" + "Users/" + "private-person/.open-health-agent/health.sqlite3"
    completed = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "context",
            "--unexpected-private-value",
            private_health_text,
            private_database,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    payload = json.loads(completed.stderr)
    assert payload["code"] == "invalid_command"
    assert payload["type"] == "CLIUsageError"
    assert private_health_text not in completed.stderr
    assert private_database not in completed.stderr
