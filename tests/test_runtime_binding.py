from __future__ import annotations

import json
from pathlib import Path

import pytest

from oha.config import create_config
import oha.scheduler as scheduler


def make_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    executable = tmp_path / "bin" / "ghealth"
    executable.parent.mkdir(parents=True)
    executable.write_text("synthetic ghealth runtime v1", encoding="utf-8")
    executable.chmod(0o700)
    scripts = tmp_path / "installed-skill" / "scripts"
    scripts.mkdir(parents=True)
    entrypoint = scripts / "health_agent.py"
    module = scripts / "oha" / "adapter.py"
    module.parent.mkdir()
    entrypoint.write_text("# synthetic entrypoint v1\n", encoding="utf-8")
    module.write_text("# synthetic module v1\n", encoding="utf-8")
    (scripts / "requirements.txt").write_text(
        "synthetic pinned requirements\n", encoding="utf-8"
    )
    assets = scripts.parent / "assets"
    assets.mkdir()
    (assets / "health-ledger.xlsx").write_bytes(b"synthetic workbook template v1")
    config = create_config(
        tmp_path / "private", timezone="UTC", ghealth_command=str(executable)
    )
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: entrypoint)
    return config, executable, module


def test_static_runtime_fingerprint_never_executes_ghealth_and_binds_oha_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, executable, module = make_runtime(tmp_path, monkeypatch)

    def forbidden_run(_command):
        raise AssertionError("static fingerprint must not execute ghealth")

    monkeypatch.setattr(scheduler, "_run", forbidden_run)
    initial = scheduler.static_runtime_fingerprint(config)

    module.write_text("# synthetic module v2\n", encoding="utf-8")
    changed_oha = scheduler.static_runtime_fingerprint(config)
    executable.write_text("synthetic ghealth runtime v2", encoding="utf-8")
    changed_ghealth = scheduler.static_runtime_fingerprint(config)
    config.lookback_days += 1
    changed_config = scheduler.static_runtime_fingerprint(config)
    template = module.parent.parent.parent / "assets" / "health-ledger.xlsx"
    template.write_bytes(b"synthetic workbook template v2")
    changed_template = scheduler.static_runtime_fingerprint(config)

    assert initial != changed_oha
    assert changed_oha != changed_ghealth
    assert changed_ghealth != changed_config
    assert changed_config != changed_template
    assert all(
        len(value) == 64
        for value in (
            initial,
            changed_oha,
            changed_ghealth,
            changed_config,
            changed_template,
        )
    )


def test_account_bound_runtime_changes_with_account_or_project_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _executable, _module = make_runtime(tmp_path, monkeypatch)
    identity = {
        "email": "synthetic-one@example.invalid",
        "project_id": "synthetic-project-one",
    }

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "profiles" in command:
            return 0, json.dumps(
                {
                    "profiles": [
                        {
                            "name": "athlete-1",
                            "active": True,
                            "project_id": identity["project_id"],
                        }
                    ]
                }
            )
        if command[1:3] == ["auth", "status"]:
            return 0, json.dumps(
                {
                    "authenticated": True,
                    "expired": False,
                    "email": identity["email"],
                    "auth_method": "oauth",
                    "scopes": ["scope-b", "scope-a"],
                }
            )
        raise AssertionError(f"unexpected synthetic command shape: {command}")

    monkeypatch.setattr(scheduler, "_run", fake_run)
    initial = scheduler.ghealth_runtime_fingerprint(config, "athlete-1")
    identity["email"] = "synthetic-two@example.invalid"
    changed_account = scheduler.ghealth_runtime_fingerprint(config, "athlete-1")
    identity["project_id"] = "synthetic-project-two"
    changed_project = scheduler.ghealth_runtime_fingerprint(config, "athlete-1")

    assert len({initial, changed_account, changed_project}) == 3


def test_background_identity_binding_fails_closed_without_stable_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _executable, _module = make_runtime(tmp_path, monkeypatch)

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "profiles" in command:
            return 0, json.dumps(
                {
                    "profiles": [
                        {
                            "name": "default",
                            "active": True,
                            "project_id": "synthetic-project",
                        }
                    ]
                }
            )
        return 0, json.dumps(
            {
                "authenticated": True,
                "expired": False,
                "auth_method": "environment-token",
                "scopes": ["scope-a"],
            }
        )

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="stable authenticated account identity"):
        scheduler.ghealth_runtime_fingerprint(config, "default")
