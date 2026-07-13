from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

import health_agent
from oha.config import create_config, save_config
from oha.ghealth_adapter import GHealthAdapter, GHealthError


class ConfigRunner:
    def __init__(self, active: str = "default", default_timezone: str | None = "UTC"):
        self.active = active
        self.default_timezone = default_timezone
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str]) -> dict[str, Any]:
        self.calls.append(arguments)
        if arguments[:3] == ["config", "profiles", "list"]:
            return {
                "profiles": [
                    {
                        "name": "default",
                        "active": self.active == "default",
                        "project_id": "must-not-leak",
                    },
                    {
                        "name": "private-profile-name",
                        "active": self.active == "private-profile-name",
                        "project_id": "also-must-not-leak",
                    },
                ]
            }
        if arguments[:2] == ["config", "show"]:
            default: dict[str, Any] = {"project_id": "must-not-leak"}
            if self.default_timezone is not None:
                default["timezone"] = self.default_timezone
            return {
                "Default": default,
                "Profiles": {
                    "private-profile-name": {
                        "project_id": "also-must-not-leak",
                        "timezone": "Asia/Shanghai",
                    }
                },
            }
        raise AssertionError(f"unexpected arguments: {arguments}")


def test_timezone_status_uses_active_custom_profile_without_leaking_identifiers() -> None:
    runner = ConfigRunner(active="private-profile-name")
    status = GHealthAdapter(runner, timezone_name="Asia/Shanghai").timezone_status()

    assert status == {
        "open_health_agent_timezone": "Asia/Shanghai",
        "ghealth_timezone": "Asia/Shanghai",
        "matches": True,
        "active_profile_type": "custom",
    }
    assert "private-profile-name" not in str(status)
    assert "must-not-leak" not in str(status)
    assert runner.calls == [
        ["config", "profiles", "list", "--format", "json"],
        ["config", "show", "--format", "json"],
    ]


def test_missing_or_different_ghealth_timezone_blocks_real_sync() -> None:
    runner = ConfigRunner(default_timezone=None)
    adapter = GHealthAdapter(runner, timezone_name="UTC")

    with pytest.raises(GHealthError, match=r"ghealth config set timezone UTC"):
        adapter.require_matching_timezone()


def test_doctor_reports_timezone_mismatch_and_action_without_mutating_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = create_config(tmp_path / "private", timezone="Asia/Shanghai", ghealth_command=sys.executable)
    save_config(config)
    runner = ConfigRunner(default_timezone="UTC")
    monkeypatch.setattr(health_agent, "CommandRunner", lambda _command: runner)
    monkeypatch.setattr(health_agent, "scheduler_status", lambda _config: {"installed": False})

    result = health_agent.command_doctor(Namespace(home=str(config.home_path), offline=True))
    check = next(item for item in result["checks"] if item["check"] == "ghealth timezone")

    assert result["status"] == "needs_attention"
    assert check["ok"] is False
    assert check["detail"]["ghealth_timezone"] == "UTC"
    assert "ghealth config set timezone Asia/Shanghai" in check["action"]
    # The validation path is read-only: it issues only list/show commands.
    assert all(call[:2] != ["config", "set"] for call in runner.calls)


def test_doctor_does_not_mark_ambiguous_authentication_as_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = create_config(
        tmp_path / "private", timezone="UTC", ghealth_command=sys.executable
    )
    save_config(config)

    class AmbiguousAuthAdapter:
        def timezone_status(self) -> dict[str, Any]:
            return {
                "open_health_agent_timezone": "UTC",
                "ghealth_timezone": "UTC",
                "matches": True,
                "active_profile_type": "default",
            }

        def auth_status(self) -> dict[str, Any]:
            # Defense in depth: doctor must not trust an adapter result unless
            # authenticated is the literal boolean true.
            return {"authenticated": None, "expired": None, "scope_count": None}

    monkeypatch.setattr(
        health_agent,
        "GHealthAdapter",
        lambda *_args, **_kwargs: AmbiguousAuthAdapter(),
    )
    monkeypatch.setattr(
        health_agent, "scheduler_status", lambda _config: {"installed": False}
    )

    result = health_agent.command_doctor(
        Namespace(home=str(config.home_path), offline=False)
    )
    check = next(
        item for item in result["checks"] if item["check"] == "ghealth authentication"
    )

    assert check["ok"] is False
    assert result["status"] == "needs_attention"


def test_real_sync_checks_timezone_before_creating_a_sync_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = create_config(tmp_path / "private", timezone="Asia/Shanghai", ghealth_command=sys.executable)
    save_config(config)
    runner = ConfigRunner(default_timezone="UTC")
    monkeypatch.setattr(health_agent, "CommandRunner", lambda _command: runner)

    with pytest.raises(GHealthError, match=r"ghealth config set timezone Asia/Shanghai"):
        health_agent.command_sync(
            Namespace(
                home=str(config.home_path),
                from_date="2026-01-01",
                to_date="2026-01-02",
                fixture_dir=None,
                quiet=False,
            )
        )

    assert not config.database.exists()


def test_public_errors_keep_timezone_and_scheduler_failures_actionable_without_echoing_details() -> None:
    private_detail = "/" + "Users/" + "private-person/.open-health-agent"
    timezone_payload = health_agent._public_exception_payload(
        GHealthError(f"ghealth timezone mismatch near {private_detail}")
    )
    scheduler_payload = health_agent._public_exception_payload(
        RuntimeError(f"systemctl failed while reading {private_detail}")
    )

    assert timezone_payload["code"] == "ghealth_timezone_mismatch"
    assert "same IANA timezone" in timezone_payload["error"]
    assert scheduler_payload["code"] == "scheduler_operation_failed"
    assert "kept or restored" in scheduler_payload["error"]
    assert private_detail not in str(timezone_payload)
    assert private_detail not in str(scheduler_payload)


@pytest.mark.parametrize(
    "schedule_state",
    [
        {"installed": False, "definition_exists": False, "loaded": True},
        {"installed": False, "definition_exists": False, "active": True},
        {"installed": False, "definition_exists": False, "enabled": True},
    ],
)
def test_doctor_treats_orphan_scheduler_runtime_as_requiring_ghealth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schedule_state: dict[str, bool],
) -> None:
    config = create_config(
        tmp_path / "private",
        timezone="UTC",
        ghealth_command="definitely-missing-ghealth",
    )
    save_config(config)
    monkeypatch.setattr(health_agent, "scheduler_status", lambda _config: schedule_state)

    result = health_agent.command_doctor(
        Namespace(home=str(config.home_path), offline=True)
    )
    check = next(
        item for item in result["checks"] if item["check"] == "ghealth command"
    )
    assert check["ok"] is False
    assert check["required"] is True
    assert result["status"] == "needs_attention"
