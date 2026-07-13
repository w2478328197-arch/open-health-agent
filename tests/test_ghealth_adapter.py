from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from oha.database import HealthDatabase
import oha.ghealth_adapter as adapter_module
from oha.ghealth_adapter import (
    CommandRunner,
    FixtureRunner,
    GHealthAdapter,
    GHealthError,
    merge_partial_daily,
    normalize_payloads,
    redact,
)


FIXTURES = Path(__file__).parent / "fixtures" / "ghealth"


def fixture_payload(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["dataPoints"]


def test_fixture_fetch_normalizes_latest_rollup_and_preferred_sleep_source() -> None:
    adapter = GHealthAdapter(FixtureRunner(FIXTURES), sleep_source_priority=["preferred."])
    result = adapter.fetch("2026-01-01", "2026-01-03", "batch-synthetic")

    assert result["errors"] == []
    assert len(result["daily"]) == 1
    daily = result["daily"][0]
    assert daily["date"] == "2026-01-02"
    assert daily["steps"] == 4321
    assert daily["distance_km"] == 3.21
    assert daily["active_energy_kcal"] == 456
    assert daily["resting_heart_rate_bpm"] == 58
    assert daily["sleep_source"] == "preferred.sleep"
    assert daily["sleep_hours"] == 6.917
    assert daily["light_minutes"] == 230
    assert result["measurements"][0]["value"] == 72
    assert result["workouts"][0]["external_id"] == "synthetic-workout-001"
    assert result["workouts"][0]["record_id"].startswith("wor_")


def test_stable_normalization_and_database_upsert_deduplicate_reimports(tmp_path: Path) -> None:
    raw = {
        "weight": fixture_payload("weight.json"),
        "exercise": fixture_payload("exercise.json"),
        "sleep": fixture_payload("sleep.json"),
    }
    first = normalize_payloads(raw, "batch-one", ["preferred."])
    second = normalize_payloads(raw, "batch-two", ["preferred."])

    assert [row["record_id"] for row in first["measurements"]] == [
        row["record_id"] for row in second["measurements"]
    ]
    assert [row["record_id"] for row in first["workouts"]] == [
        row["record_id"] for row in second["workouts"]
    ]

    with HealthDatabase(tmp_path / "health.sqlite3") as database:
        for normalized in (first, second):
            for row in normalized["measurements"]:
                database.upsert("measurement", row)
            for row in normalized["workouts"]:
                database.upsert("workout", row)
        assert len(database.list_records("measurement")) == 1
        assert len(database.list_records("workout")) == 1


def test_redaction_removes_oauth_material() -> None:
    oauth_prefix = "ya" + "29."
    sample = f"access_token=secret-value authorization: Bearer-value code=oauth-code {oauth_prefix}synthetic-token"
    cleaned = redact(sample)
    assert "secret-value" not in cleaned
    assert "oauth-code" not in cleaned
    assert f"{oauth_prefix}synthetic-token" not in cleaned


def test_redaction_covers_json_secrets_and_bearer_headers() -> None:
    header = "Author" + "ization: Bea" + "rer " + "sensitive-access-token"
    refresh_token = "1" + "//" + "sensitive-refresh"
    client_secret = "GOC" + "SPX-" + "sensitive"
    raw = json.dumps(
        {"refresh_token": refresh_token, "client_secret": client_secret}
    ) + "\n" + header
    cleaned = redact(raw)
    for secret in (refresh_token, client_secret, "sensitive-access-token"):
        assert secret not in cleaned
    assert "[REDACTED]" in cleaned


def test_auth_status_returns_only_non_identifying_fields() -> None:
    status = GHealthAdapter(FixtureRunner(FIXTURES)).auth_status()
    assert status == {"authenticated": True, "expired": None, "scope_count": None}
    assert "account" not in status


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"authenticated": False},
        {"authenticated": "true"},
        {"authenticated": True, "expired": True},
    ],
)
def test_auth_status_requires_literal_true_and_unexpired(payload: dict) -> None:
    class AuthRunner:
        def run(self, _arguments: list[str]) -> dict:
            return payload

    with pytest.raises(GHealthError, match="authentication is not currently usable"):
        GHealthAdapter(AuthRunner()).auth_status()  # type: ignore[arg-type]


def test_partial_daily_merge_preserves_only_failed_query_fields_and_sources() -> None:
    previous = {
        "record_id": "2026-01-02",
        "date": "2026-01-02",
        "steps": 1000,
        "weight_kg": 70.0,
        "body_fat_percent": 20.0,
        "sources": "prior.steps, prior.body",
        "data_until": "2026-01-02T22:00:00+00:00",
        "quality": "automatic ghealth import; blanks mean unavailable",
    }
    current = {
        "record_id": "2026-01-02",
        "date": "2026-01-02",
        "weight_kg": 69.5,
        "sources": "current.weight",
        "data_until": "2026-01-02T23:00:00+00:00",
        "quality": "automatic ghealth import; blanks mean unavailable",
    }

    merged = merge_partial_daily(current, previous, ["steps"])

    assert merged["steps"] == 1000
    assert merged["weight_kg"] == 69.5
    # body_fat succeeded with an empty result, so the prior value must not be
    # resurrected merely because another query failed.
    assert "body_fat_percent" not in merged
    assert merged["sources"] == "current.weight, prior.body, prior.steps"
    assert "partial; failed daily queries: steps" in merged["quality"]
    assert "stale fields retained from prior sync: steps" in merged["quality"]

    repeated = merge_partial_daily(merged, merged, ["steps"])
    assert repeated["quality"].count("; partial;") == 1


def test_partial_daily_without_prior_value_is_marked_partial_not_stale() -> None:
    merged = merge_partial_daily(
        {
            "record_id": "2026-01-02",
            "date": "2026-01-02",
            "steps": 1200,
            "sources": "current.steps",
        },
        None,
        ["hrv"],
    )
    assert "failed daily queries: hrv" in merged["quality"]
    assert "no prior value available" in merged["quality"]
    assert "stale fields retained" not in merged["quality"]


def test_data_cutoffs_are_aware_compared_and_specific_to_each_daily_row() -> None:
    normalized = normalize_payloads(
        {
            "steps": [
                {
                    "date": "2026-01-02",
                    "end": "2026-01-02T23:30:00-08:00",
                    "countSum": 1000,
                    "source": "source.day.one",
                },
                {
                    "date": "2026-01-03",
                    "end": "2026-01-03T07:00:00+00:00",
                    "countSum": 2000,
                    "source": "source.day.two",
                },
            ]
        },
        "batch-aware-cutoff",
        timezone_name="UTC",
    )

    rows = {row["date"]: row for row in normalized["daily"]}
    assert rows["2026-01-02"]["data_until"] == "2026-01-03T07:30:00+00:00"
    assert rows["2026-01-03"]["data_until"] == "2026-01-03T07:00:00+00:00"
    # Raw lexical ISO max would choose the second timestamp; instant-aware max
    # correctly chooses the first.
    assert normalized["data_until"] == "2026-01-03T07:30:00+00:00"
    for row in rows.values():
        assert datetime.fromisoformat(row["data_until"]).tzinfo is not None


def test_external_ids_survive_source_corrections_without_duplicates() -> None:
    first = normalize_payloads(
        {
            "weight": [
                {
                    "id": "measurement-001",
                    "date": "2026-01-02",
                    "time": "2026-01-02T08:00:00Z",
                    "weightGrams": 70000,
                    "source": "synthetic.source",
                }
            ],
            "exercise": [
                {
                    "id": "workout-001",
                    "date": "2026-01-02",
                    "start": "2026-01-02T09:00:00Z",
                    "displayName": "Strength",
                    "duration": 1800,
                    "source": "synthetic.source",
                }
            ],
        },
        "batch-one",
    )
    second = normalize_payloads(
        {
            "weight": [
                {
                    "id": "measurement-001",
                    "date": "2026-01-02",
                    "time": "2026-01-02T08:00:00Z",
                    "weightGrams": 69900,
                    "source": "synthetic.source.renamed",
                }
            ],
            "exercise": [
                {
                    "id": "workout-001",
                    "date": "2026-01-02",
                    "start": "2026-01-02T09:00:00Z",
                    "displayName": "Strength",
                    "duration": 1860,
                    "source": "synthetic.source.renamed",
                }
            ],
        },
        "batch-two",
    )
    assert first["measurements"][0]["record_id"] == second["measurements"][0]["record_id"]
    assert first["workouts"][0]["record_id"] == second["workouts"][0]["record_id"]
    assert second["measurements"][0]["value"] == 69.9
    assert second["workouts"][0]["duration_minutes"] == 31
    assert second["measurements"][0]["source"] == "synthetic.source.renamed"
    assert second["workouts"][0]["training_source"] == "synthetic.source.renamed"


def test_repeated_partial_merge_does_not_grow_quality_text() -> None:
    previous = {
        "record_id": "2026-01-02",
        "date": "2026-01-02",
        "steps": 4321,
        "sources": "synthetic.watch",
        "quality": "automatic ghealth import; blanks mean unavailable",
    }
    first = adapter_module.merge_partial_daily(previous, previous, ["steps"])
    second = adapter_module.merge_partial_daily(first, first, ["steps"])
    assert second["quality"] == first["quality"]
    assert second["quality"].count("partial; failed daily queries") == 1


def test_timestamp_only_events_use_configured_local_date() -> None:
    result = normalize_payloads(
        {
            "exercise": [
                {
                    "id": "timezone-workout",
                    "start": "2026-01-01T23:30:00Z",
                    "displayName": "Walk",
                    "duration": 600,
                    "source": "synthetic.source",
                }
            ]
        },
        "batch-timezone",
        timezone_name="Asia/Shanghai",
    )
    assert result["workouts"][0]["date"] == "2026-01-02"


def test_command_errors_do_not_echo_health_or_account_payloads() -> None:
    secret = "synthetic-account@example.invalid weight=70 sleep=6.5"
    with pytest.raises(GHealthError) as failure:
        CommandRunner(sys.executable).run(
            ["-c", f"import sys; sys.stderr.write({secret!r}); raise SystemExit(7)"]
        )
    assert secret not in str(failure.value)
    assert "diagnostic_sha256=" in str(failure.value)
    assert "category=command_failed" in str(failure.value)


def test_command_output_is_stopped_at_combined_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "MAX_RESPONSE_BYTES", 1024)
    with pytest.raises(GHealthError, match="exceeded the local size limit"):
        CommandRunner(sys.executable).run(
            ["-c", "import sys; sys.stdout.write('x' * 4096); sys.stdout.flush()"]
        )


@pytest.mark.skipif(os.name == "nt", reason="executable script fixture uses a POSIX shebang")
def test_command_runner_forces_json_format_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "fake-ghealth"
    executable.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import json, os\n"
        "print(json.dumps({'format': os.environ.get('GHEALTH_FORMAT')}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("GHEALTH_FORMAT", "table")

    assert CommandRunner(str(executable)).run([]) == {"format": "json"}
