from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

from oha.config import create_config
import oha.scheduler as scheduler


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def patch_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(scheduler.Path, "home", classmethod(lambda cls: home))
    # Windows CI exercises the generated Linux/macOS definitions by mocking
    # platform.system(); os.getuid is not present on native Windows.
    monkeypatch.setattr(scheduler.os, "getuid", lambda: 501, raising=False)


def assert_private_mode(path: Path) -> None:
    assert path.exists()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_linux_scheduler_definition_status_and_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    private_home = tmp_path / "private"
    executable = make_executable(host_home / ".local" / "bin" / "ghealth")
    config = create_config(private_home, timezone="UTC", ghealth_command=str(executable))

    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: Path("/opt/oha/health_agent.py"))

    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> tuple[int, str]:
        calls.append(command)
        if command[:2] == ["loginctl", "show-user"]:
            return 0, "yes"
        return 0, "enabled"

    monkeypatch.setattr(scheduler, "_run", fake_run)

    installed = scheduler.install_linux(config, 3600)
    service = Path(installed["service"])
    timer = Path(installed["timer"])
    assert service.exists() and timer.exists()
    assert str(private_home.resolve()) in service.read_text(encoding="utf-8")
    assert "/opt/oha/health_agent.py" in service.read_text(encoding="utf-8")
    assert "Environment=GHEALTH_PROFILE=default" in service.read_text(encoding="utf-8")
    assert "Environment=GHEALTH_FORMAT=json" in service.read_text(encoding="utf-8")
    assert_private_mode(service)
    assert_private_mode(timer)

    status = scheduler.scheduler_status(config)
    assert status["installed"] is True
    assert status["configuration_matches"] is True
    assert status["ghealth_available"] is True
    assert status["linger"] is True

    removed = scheduler.uninstall_scheduler(config)
    assert removed["removed"] is True
    assert not service.exists() and not timer.exists()
    assert any("disable" in command for command in calls)


def test_scheduler_resolves_local_bin_and_persists_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    executable = make_executable(host_home / ".local" / "bin" / "ghealth")
    config = create_config(tmp_path / "private", timezone="UTC", ghealth_command="ghealth")

    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.shutil, "which", lambda _name: None)
    assert scheduler.resolve_ghealth_executable("ghealth") == executable.resolve()


def test_scheduler_rejects_missing_ghealth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_home(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="before enabling hourly sync"):
        scheduler.resolve_ghealth_executable("missing-ghealth")


def test_linux_status_warns_when_user_linger_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    executable = make_executable(tmp_path / "bin" / "ghealth")
    config = create_config(tmp_path / "private", timezone="UTC", ghealth_command=str(executable))
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: Path("/opt/oha/health_agent.py"))
    monkeypatch.setattr(
        scheduler,
        "_run",
        lambda command: (0, "no") if command and command[0] == "loginctl" else (0, "enabled"),
    )
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    (user_dir / "open-health-agent-sync.service").write_text(
        "Environment=GHEALTH_PROFILE=default\n"
        "Environment=GHEALTH_FORMAT=json\n"
        f"ExecStart=/python /opt/oha/health_agent.py --home {config.home_path} sync\n",
        encoding="utf-8",
    )
    (user_dir / "open-health-agent-sync.timer").write_text("[Timer]\n", encoding="utf-8")

    status = scheduler.scheduler_status(config)
    assert status["installed"] is True
    assert status["linger"] is False
    assert status["logout_warning"] is True


def test_linux_status_is_not_installed_when_timer_is_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    executable = make_executable(tmp_path / "bin" / "ghealth")
    config = create_config(tmp_path / "private", timezone="UTC", ghealth_command=str(executable))
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: Path("/opt/oha/health_agent.py"))

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "is-active" in command:
            return 3, "inactive"
        if command and command[0] == "loginctl":
            return 0, "yes"
        return 0, "enabled"

    monkeypatch.setattr(scheduler, "_run", fake_run)
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    (user_dir / "open-health-agent-sync.service").write_text(
        "Environment=GHEALTH_PROFILE=default\n"
        "Environment=GHEALTH_FORMAT=json\n"
        f"ExecStart=/python /opt/oha/health_agent.py --home {config.home_path} sync\n",
        encoding="utf-8",
    )
    (user_dir / "open-health-agent-sync.timer").write_text("[Timer]\n", encoding="utf-8")

    status = scheduler.scheduler_status(config)
    assert status["enabled"] is True
    assert status["active"] is False
    assert status["installed"] is False


def test_macos_scheduler_and_keepawake_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    executable = make_executable(tmp_path / "bin" / "ghealth")
    config = create_config(tmp_path / "private", timezone="UTC", ghealth_command=str(executable))
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: Path("/opt/oha/health_agent.py"))
    monkeypatch.setattr(scheduler, "_run", lambda _command: (0, "ok"))

    result = scheduler.install_macos(config, 3600)
    schedule_path = Path(result["path"])
    payload = plistlib.loads(schedule_path.read_bytes())
    assert payload["StartInterval"] == 3600
    assert str(config.home_path) in payload["ProgramArguments"]
    assert payload["EnvironmentVariables"] == {
        "GHEALTH_FORMAT": "json",
        "GHEALTH_PROFILE": "default",
    }
    assert_private_mode(schedule_path)
    assert scheduler.scheduler_status(config)["installed"] is True
    assert scheduler.uninstall_scheduler(config)["removed"] is True

    awake = scheduler.install_macos_ac_keepawake(config)
    awake_path = Path(awake["path"])
    awake_payload = plistlib.loads(awake_path.read_bytes())
    assert awake_payload["ProgramArguments"] == ["/usr/bin/caffeinate", "-s"]
    assert scheduler.keepawake_status()["installed"] is True
    assert scheduler.uninstall_macos_ac_keepawake()["removed"] is True


def test_macos_scheduler_uninstall_failure_keeps_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    path = host_home / "Library" / "LaunchAgents" / f"{scheduler.SYNC_LABEL}.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"Label": scheduler.SYNC_LABEL}))
    monkeypatch.setattr(scheduler, "_run", lambda _command: (5, "permission denied"))

    with pytest.raises(RuntimeError, match="no definition was removed"):
        scheduler.uninstall_scheduler(config)

    assert path.exists()


def test_macos_keepawake_uninstall_failure_keeps_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    path = host_home / "Library" / "LaunchAgents" / f"{scheduler.AWAKE_LABEL}.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"Label": scheduler.AWAKE_LABEL}))
    monkeypatch.setattr(scheduler, "_run", lambda _command: (5, "permission denied"))

    with pytest.raises(RuntimeError, match="no definition was removed"):
        scheduler.uninstall_macos_ac_keepawake()

    assert path.exists()


def test_linux_uninstall_disable_failure_keeps_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    service = user_dir / "open-health-agent-sync.service"
    timer = user_dir / "open-health-agent-sync.timer"
    service.write_text("service definition", encoding="utf-8")
    timer.write_text("timer definition", encoding="utf-8")
    monkeypatch.setattr(scheduler, "_run", lambda _command: (1, "user bus unavailable"))

    with pytest.raises(RuntimeError, match="no definition was removed"):
        scheduler.uninstall_scheduler(config)

    assert service.read_text(encoding="utf-8") == "service definition"
    assert timer.read_text(encoding="utf-8") == "timer definition"


def test_linux_uninstall_reload_failure_restores_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    service = user_dir / "open-health-agent-sync.service"
    timer = user_dir / "open-health-agent-sync.timer"
    service.write_text("service definition", encoding="utf-8")
    timer.write_text("timer definition", encoding="utf-8")

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "daemon-reload" in command:
            return 1, "reload failed"
        return 0, "disabled"

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="definitions were restored"):
        scheduler.uninstall_scheduler(config)

    assert service.read_text(encoding="utf-8") == "service definition"
    assert timer.read_text(encoding="utf-8") == "timer definition"
    assert not list(user_dir.glob(".*.removing.*"))


@pytest.mark.parametrize("job", ["scheduler", "keepawake"])
def test_macos_uninstall_stops_loaded_orphan_without_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job: str
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> tuple[int, str]:
        calls.append(command)
        return 0, "loaded"

    monkeypatch.setattr(scheduler, "_run", fake_run)
    result = (
        scheduler.uninstall_scheduler(config)
        if job == "scheduler"
        else scheduler.uninstall_macos_ac_keepawake()
    )

    expected_label = scheduler.SYNC_LABEL if job == "scheduler" else scheduler.AWAKE_LABEL
    assert result["removed"] is True
    assert result["runtime_stopped"] is True
    assert result["definition_removed"] is False
    assert ["launchctl", "bootout", f"gui/501/{expected_label}"] in calls


@pytest.mark.parametrize("job", ["scheduler", "keepawake"])
def test_macos_uninstall_removes_unloaded_definition_without_bootout_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job: str
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    label = scheduler.SYNC_LABEL if job == "scheduler" else scheduler.AWAKE_LABEL
    path = host_home / "Library" / "LaunchAgents" / f"{label}.plist"
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps({"Label": label}))
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> tuple[int, str]:
        calls.append(command)
        if "print" in command:
            return 113, "Could not find service in domain"
        raise AssertionError("bootout must not run for an unloaded definition")

    monkeypatch.setattr(scheduler, "_run", fake_run)
    result = (
        scheduler.uninstall_scheduler(config)
        if job == "scheduler"
        else scheduler.uninstall_macos_ac_keepawake()
    )

    assert result["removed"] is True
    assert result["runtime_stopped"] is False
    assert result["definition_removed"] is True
    assert not path.exists()
    assert not any("bootout" in command for command in calls)


def test_linux_uninstall_stops_loaded_orphan_without_unit_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> tuple[int, str]:
        calls.append(command)
        if "is-active" in command:
            return 0, "active"
        if "is-enabled" in command:
            return 1, "disabled"
        return 0, "ok"

    monkeypatch.setattr(scheduler, "_run", fake_run)
    result = scheduler.uninstall_scheduler(config)

    assert result["removed"] is True
    assert result["runtime_stopped"] is True
    assert result["definition_removed"] is False
    assert any("stop" in command for command in calls)


def test_linux_uninstall_reports_false_only_after_runtime_absence_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "is-active" in command:
            return 3, "inactive"
        if "is-enabled" in command:
            return 1, "disabled"
        raise AssertionError("no mutation command should run")

    monkeypatch.setattr(scheduler, "_run", fake_run)
    result = scheduler.uninstall_scheduler(config)
    assert result["removed"] is False
    assert result["runtime_stopped"] is False
    assert result["definition_removed"] is False


def test_active_ghealth_profile_resolution_is_projected_and_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = make_executable(tmp_path / "bin" / "ghealth")
    payload = (
        '{"profiles":['
        '{"name":"default","active":false,"project_id":"must-not-leak"},'
        '{"name":"athlete-1","active":true,"project_id":"also-private"}]}'
    )
    monkeypatch.setattr(scheduler, "_run", lambda _command: (0, payload))

    assert scheduler._resolve_active_ghealth_profile(executable) == "athlete-1"


def test_linux_install_pins_selected_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: Path("/opt/oha/health_agent.py"))
    monkeypatch.setattr(scheduler, "_run", lambda _command: (0, "ok"))

    result = scheduler.install_linux(config, 3600, "athlete-1")
    service_text = Path(result["service"]).read_text(encoding="utf-8")

    assert "Environment=GHEALTH_PROFILE=athlete-1" in service_text
    assert "Environment=GHEALTH_FORMAT=json" in service_text


def test_linux_install_enable_failure_restores_previous_definitions_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: Path("/opt/oha/health_agent.py"))
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    service = user_dir / "open-health-agent-sync.service"
    timer = user_dir / "open-health-agent-sync.timer"
    service.write_text("old service", encoding="utf-8")
    timer.write_text("old timer", encoding="utf-8")
    enabled_attempts = 0

    def fake_run(command: list[str]) -> tuple[int, str]:
        nonlocal enabled_attempts
        if "is-enabled" in command:
            return 0, "enabled"
        if "enable" in command:
            enabled_attempts += 1
            return (1, "new enable failed") if enabled_attempts == 1 else (0, "old restored")
        return 0, "ok"

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="previous definitions and enabled state were restored"):
        scheduler.install_linux(config, 3600, "athlete-1")

    assert service.read_text(encoding="utf-8") == "old service"
    assert timer.read_text(encoding="utf-8") == "old timer"
    assert enabled_attempts == 2


def test_linux_first_install_enable_failure_leaves_no_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: Path("/opt/oha/health_agent.py"))

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "enable" in command:
            return 1, "enable failed"
        return 0, "ok"

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="new scheduler was rolled back"):
        scheduler.install_linux(config, 3600)

    user_dir = host_home / ".config" / "systemd" / "user"
    assert not (user_dir / "open-health-agent-sync.service").exists()
    assert not (user_dir / "open-health-agent-sync.timer").exists()


def test_linux_install_reload_failure_restores_previous_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: Path("/opt/oha/health_agent.py"))
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    service = user_dir / "open-health-agent-sync.service"
    timer = user_dir / "open-health-agent-sync.timer"
    service.write_text("old service", encoding="utf-8")
    timer.write_text("old timer", encoding="utf-8")
    reloads = 0

    def fake_run(command: list[str]) -> tuple[int, str]:
        nonlocal reloads
        if "is-enabled" in command:
            return 0, "enabled"
        if "daemon-reload" in command:
            reloads += 1
            return (1, "new reload failed") if reloads == 1 else (0, "old reloaded")
        return 0, "ok"

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="new scheduler was not kept"):
        scheduler.install_linux(config, 3600)

    assert service.read_text(encoding="utf-8") == "old service"
    assert timer.read_text(encoding="utf-8") == "old timer"
    assert reloads == 2


def test_macos_install_bootstrap_failure_restores_previous_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(scheduler, "_entrypoint", lambda: Path("/opt/oha/health_agent.py"))
    path = host_home / "Library" / "LaunchAgents" / f"{scheduler.SYNC_LABEL}.plist"
    path.parent.mkdir(parents=True)
    old_payload = plistlib.dumps({"Label": scheduler.SYNC_LABEL, "Old": True})
    path.write_bytes(old_payload)

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "print" in command or "bootout" in command or "kickstart" in command:
            return 0, "ok"
        if "bootstrap" in command:
            current = plistlib.loads(path.read_bytes())
            return (0, "old restored") if current.get("Old") else (5, "new bootstrap failed")
        return 0, "ok"

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="previous definition and loaded state were restored"):
        scheduler.install_macos(config, 3600, "athlete-1")

    assert path.read_bytes() == old_payload


def test_macos_keepawake_bootstrap_failure_restores_previous_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    path = host_home / "Library" / "LaunchAgents" / f"{scheduler.AWAKE_LABEL}.plist"
    path.parent.mkdir(parents=True)
    old_payload = plistlib.dumps({"Label": scheduler.AWAKE_LABEL, "Old": True})
    path.write_bytes(old_payload)

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "print" in command or "bootout" in command or "kickstart" in command:
            return 0, "ok"
        if "bootstrap" in command:
            current = plistlib.loads(path.read_bytes())
            return (0, "old restored") if current.get("Old") else (5, "new bootstrap failed")
        return 0, "ok"

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="previous definition and loaded state were restored"):
        scheduler.install_macos_ac_keepawake(config)

    assert path.read_bytes() == old_payload
