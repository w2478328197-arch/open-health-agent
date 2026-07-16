from __future__ import annotations

import os
import plistlib
from pathlib import Path, PurePosixPath

import pytest

from oha.config import create_config
import oha.scheduler as scheduler


RUNTIME_FINGERPRINT = "a" * 64
STATIC_RUNTIME_FINGERPRINT = "b" * 64


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


class FakeSystemd:
    def __init__(
        self,
        *,
        enabled: bool = False,
        timer_active: bool = False,
        service_active: bool = False,
        linger: str = "yes",
        user_dir: Path | None = None,
    ) -> None:
        self.enabled = enabled
        self.timer_active = timer_active
        self.service_active = service_active
        self.linger = linger
        self.user_dir = user_dir
        self.drop_in_paths = ""
        self.need_daemon_reload = "no"
        self.fragment_overrides: dict[str, Path] = {}
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> tuple[int, str]:
        self.calls.append(command)
        if command[:2] == ["loginctl", "show-user"]:
            return 0, self.linger
        if not command or command[0] != "systemctl":
            return 0, "ok"
        if "show" in command:
            if self.user_dir is None:
                return 1, "unit metadata unavailable"
            unit = command[command.index("show") + 1]
            fragment = self.fragment_overrides.get(unit, self.user_dir / unit)
            return (
                0,
                f"FragmentPath={fragment.resolve()}\n"
                f"DropInPaths={self.drop_in_paths}\n"
                f"NeedDaemonReload={self.need_daemon_reload}",
            )
        unit = command[-1]
        if "is-enabled" in command:
            return (0, "enabled") if self.enabled else (1, "disabled")
        if "is-active" in command:
            active = (
                self.service_active
                if unit.endswith(".service")
                else self.timer_active
            )
            return (0, "active") if active else (3, "inactive")
        if "disable" in command:
            self.enabled = False
            if "--now" in command:
                self.timer_active = False
            return 0, "disabled"
        if "enable" in command:
            self.enabled = True
            if "--now" in command:
                self.timer_active = True
            return 0, "enabled"
        if "stop" in command:
            if unit.endswith(".service"):
                self.service_active = False
            else:
                self.timer_active = False
            return 0, "stopped"
        if "start" in command:
            if unit.endswith(".service"):
                self.service_active = True
            else:
                self.timer_active = True
            return 0, "started"
        return 0, "ok"


def launchd_effective_output(label: str, path: Path, payload: dict) -> str:
    arguments = payload.get("ProgramArguments", [])
    environment = payload.get("EnvironmentVariables", {})
    lines = [
        f"gui/501/{label} = {{",
        f"    path = {path}",
        f"    program = {arguments[0]}",
        "    arguments = {",
        *[f"        {argument}" for argument in arguments],
        "    }",
    ]
    if payload.get("StandardOutPath") is not None:
        lines.append(f"    stdout path = {payload['StandardOutPath']}")
    if payload.get("StandardErrorPath") is not None:
        lines.append(f"    stderr path = {payload['StandardErrorPath']}")
    lines.extend(
        [
            "    environment = {",
            *[f"        {key} => {value}" for key, value in environment.items()],
            "    }",
        ]
    )
    if payload.get("StartInterval") is not None:
        lines.append(f'    "Interval" => {payload["StartInterval"]}')
    lines.append("}")
    return "\n".join(lines)


class FakeLaunchd:
    def __init__(self) -> None:
        self.loaded: dict[str, tuple[Path, dict]] = {}
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> tuple[int, str]:
        self.calls.append(command)
        if "print" in command:
            label = command[-1].rsplit("/", 1)[-1]
            loaded = self.loaded.get(label)
            if loaded is None:
                return 113, "Could not find service in domain"
            path, payload = loaded
            return 0, launchd_effective_output(label, path, payload)
        if "bootstrap" in command:
            path = Path(command[-1])
            payload = plistlib.loads(path.read_bytes())
            self.loaded[str(payload["Label"])] = (path, payload)
            return 0, "bootstrapped"
        if "bootout" in command:
            target = command[-1]
            if target.endswith(".plist"):
                selected = [
                    label
                    for label, (path, _payload) in self.loaded.items()
                    if path == Path(target)
                ]
            else:
                selected = [target.rsplit("/", 1)[-1]]
            for label in selected:
                self.loaded.pop(label, None)
            return 0, "unloaded"
        return 0, "ok"


def test_linux_scheduler_definition_status_and_uninstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    private_home = tmp_path / "private"
    executable = make_executable(host_home / ".local" / "bin" / "ghealth")
    config = create_config(private_home, timezone="UTC", ghealth_command=str(executable))

    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    monkeypatch.setattr(
        scheduler,
        "ghealth_runtime_fingerprint",
        lambda _config, _profile=None: RUNTIME_FINGERPRINT,
    )
    monkeypatch.setattr(
        scheduler,
        "static_runtime_fingerprint",
        lambda _config: STATIC_RUNTIME_FINGERPRINT,
    )

    systemd = FakeSystemd(user_dir=host_home / ".config" / "systemd" / "user")
    monkeypatch.setattr(scheduler, "_run", systemd)

    installed = scheduler.install_linux(
        config,
        3600,
        proxy_environment={
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "NO_PROXY": "localhost,127.0.0.1",
        },
        static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
    )
    service = Path(installed["service"])
    timer = Path(installed["timer"])
    assert service.exists() and timer.exists()
    assert str(private_home.resolve()) in service.read_text(encoding="utf-8")
    assert "/opt/oha/health_agent.py" in service.read_text(encoding="utf-8")
    assert "--scheduled" in service.read_text(encoding="utf-8")
    assert "Environment=GHEALTH_PROFILE=default" in service.read_text(encoding="utf-8")
    assert "Environment=GHEALTH_FORMAT=json" in service.read_text(encoding="utf-8")
    assert "Environment=HTTPS_PROXY=http://127.0.0.1:7890" in service.read_text(
        encoding="utf-8"
    )
    assert_private_mode(service)
    assert_private_mode(timer)

    status = scheduler.scheduler_status(config)
    assert status["installed"] is True
    assert status["configuration_matches"] is True
    assert status["ghealth_available"] is True
    assert status["proxy_configured"] is True
    assert status["proxy_environment_keys"] == ["HTTPS_PROXY", "NO_PROXY"]
    assert "127.0.0.1:7890" not in str(status)
    assert status["linger"] is True

    service.write_text(
        service.read_text(encoding="utf-8").replace(
            '"sync"', '"doctor"', 1
        ),
        encoding="utf-8",
    )
    drifted = scheduler.scheduler_status(config)
    assert drifted["scheduled_trigger_pinned"] is False
    assert drifted["configuration_matches"] is False
    assert drifted["installed"] is False

    removed = scheduler.uninstall_scheduler(config)
    assert removed["removed"] is True
    assert not service.exists() and not timer.exists()
    assert any("disable" in command for command in systemd.calls)


def test_scheduler_resolves_local_bin_and_persists_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    executable = make_executable(host_home / ".local" / "bin" / "ghealth")
    config = create_config(tmp_path / "private", timezone="UTC", ghealth_command="ghealth")

    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.shutil, "which", lambda _name: None)
    assert scheduler.resolve_ghealth_executable("ghealth") == executable.resolve()


def test_scheduler_status_resolves_a_command_name_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    executable = make_executable(tmp_path / "bin" / "ghealth")
    config = create_config(tmp_path / "private", timezone="UTC", ghealth_command="ghealth")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(scheduler.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(scheduler, "_run", lambda _command: (1, "not loaded"))

    assert scheduler.scheduler_status(config)["ghealth_available"] is True


def test_scheduler_proxy_environment_is_strict_and_file_is_owner_only(
    tmp_path: Path,
) -> None:
    assert scheduler.validate_proxy_environment(
        {
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "NO_PROXY": "localhost,127.0.0.1,::1",
        }
    ) == {
        "HTTPS_PROXY": "http://127.0.0.1:7890",
        "NO_PROXY": "localhost,127.0.0.1,::1",
    }
    with pytest.raises(ValueError):
        scheduler.validate_proxy_environment(
            {"HTTPS_PROXY": "http://user:password@proxy.example:8080"}
        )
    with pytest.raises(ValueError, match="unsupported"):
        scheduler.validate_proxy_environment({"PATH": "/tmp/bin"})

    proxy_file = tmp_path / "proxy.env"
    proxy_file.write_text(
        "# only allowlisted keys are consumed\n"
        "HTTPS_PROXY=http://127.0.0.1:7890\n"
        "UNRELATED_SECRET=must-not-be-read\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        proxy_file.chmod(0o644)
        with pytest.raises(PermissionError, match="owner-only"):
            scheduler.proxy_environment_from_file(proxy_file)
        proxy_file.chmod(0o600)
    with pytest.raises(ValueError, match="unsupported key"):
        scheduler.proxy_environment_from_file(proxy_file)
    proxy_file.write_text(
        "HTTPS_PROXY=http://127.0.0.1:7890\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        proxy_file.chmod(0o600)
    assert scheduler.proxy_environment_from_file(proxy_file) == {
        "HTTPS_PROXY": "http://127.0.0.1:7890"
    }


def test_scheduler_rejects_missing_ghealth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_home(monkeypatch, tmp_path)
    monkeypatch.setattr(scheduler.shutil, "which", lambda _name: None)
    with pytest.raises(FileNotFoundError, match="before enabling hourly sync"):
        scheduler.resolve_ghealth_executable("missing-ghealth")


def test_install_scheduler_rejects_static_drift_before_executing_ghealth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = make_executable(tmp_path / "bin" / "ghealth")
    config = create_config(
        tmp_path / "private", timezone="UTC", ghealth_command=str(executable)
    )
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        scheduler, "static_runtime_fingerprint", lambda _config: "c" * 64
    )

    def forbidden_profile_resolution(_executable: Path) -> str:
        raise AssertionError("ghealth must not execute before the static gate")

    monkeypatch.setattr(
        scheduler, "_resolve_active_ghealth_profile", forbidden_profile_resolution
    )
    with pytest.raises(RuntimeError, match="differs from the last successful manual sync"):
        scheduler.install_scheduler(
            config,
            expected_static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            expected_runtime_fingerprint=RUNTIME_FINGERPRINT,
        )


def test_linux_status_warns_when_user_linger_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    executable = make_executable(tmp_path / "bin" / "ghealth")
    config = create_config(tmp_path / "private", timezone="UTC", ghealth_command=str(executable))
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    monkeypatch.setattr(
        scheduler,
        "ghealth_runtime_fingerprint",
        lambda _config, _profile=None: RUNTIME_FINGERPRINT,
    )
    monkeypatch.setattr(
        scheduler,
        "static_runtime_fingerprint",
        lambda _config: STATIC_RUNTIME_FINGERPRINT,
    )
    systemd = FakeSystemd(
        linger="no", user_dir=host_home / ".config" / "systemd" / "user"
    )
    monkeypatch.setattr(scheduler, "_run", systemd)
    scheduler.install_linux(
        config,
        3600,
        static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
    )

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
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    monkeypatch.setattr(
        scheduler,
        "ghealth_runtime_fingerprint",
        lambda _config, _profile=None: RUNTIME_FINGERPRINT,
    )
    monkeypatch.setattr(
        scheduler,
        "static_runtime_fingerprint",
        lambda _config: STATIC_RUNTIME_FINGERPRINT,
    )

    systemd = FakeSystemd(user_dir=host_home / ".config" / "systemd" / "user")
    monkeypatch.setattr(scheduler, "_run", systemd)
    scheduler.install_linux(
        config,
        3600,
        static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
    )
    systemd.timer_active = False

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
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    monkeypatch.setattr(
        scheduler,
        "ghealth_runtime_fingerprint",
        lambda _config, _profile=None: RUNTIME_FINGERPRINT,
    )
    monkeypatch.setattr(
        scheduler,
        "static_runtime_fingerprint",
        lambda _config: STATIC_RUNTIME_FINGERPRINT,
    )
    launchd = FakeLaunchd()
    monkeypatch.setattr(scheduler, "_run", launchd)

    result = scheduler.install_macos(
        config,
        3600,
        proxy_environment={"HTTPS_PROXY": "http://127.0.0.1:7890"},
        static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
    )
    schedule_path = Path(result["path"])
    payload = plistlib.loads(schedule_path.read_bytes())
    assert payload["StartInterval"] == 3600
    assert str(config.home_path) in payload["ProgramArguments"]
    assert "--scheduled" in payload["ProgramArguments"]
    assert payload["EnvironmentVariables"] == scheduler.SAFE_RUNTIME_ENVIRONMENT | {
        "GHEALTH_FORMAT": "json",
        "GHEALTH_PROFILE": "default",
        "HTTPS_PROXY": "http://127.0.0.1:7890",
    }
    assert_private_mode(schedule_path)
    status = scheduler.scheduler_status(config)
    assert status["installed"] is True
    assert status["log_files_safe"] is True
    assert status["proxy_environment_keys"] == ["HTTPS_PROXY"]
    assert "127.0.0.1:7890" not in str(status)

    payload["ProgramArguments"][payload["ProgramArguments"].index("sync")] = "doctor"
    schedule_path.write_bytes(plistlib.dumps(payload, sort_keys=True))
    drifted = scheduler.scheduler_status(config)
    assert drifted["scheduled_trigger_pinned"] is False
    assert drifted["configuration_matches"] is False
    assert drifted["installed"] is False
    assert scheduler.uninstall_scheduler(config)["removed"] is True

    awake = scheduler.install_macos_ac_keepawake(config)
    awake_path = Path(awake["path"])
    awake_payload = plistlib.loads(awake_path.read_bytes())
    assert awake_payload["ProgramArguments"] == ["/usr/bin/caffeinate", "-s"]
    awake_status = scheduler.keepawake_status()
    assert awake_status["installed"] is True
    assert awake_status["configuration_matches"] is True
    assert awake_status["definition_owner_only"] is True
    assert awake_status["effective_runtime_matches"] is True
    assert scheduler.uninstall_macos_ac_keepawake()["removed"] is True


def test_macos_scheduler_rejects_symlinked_log_directory_before_launchd_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    target = tmp_path / "redirected-logs"
    target.mkdir(mode=0o700)
    (config.home_path / "logs").symlink_to(target, target_is_directory=True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        scheduler,
        "_run",
        lambda command: (calls.append(command) or (0, "unexpected")),
    )

    with pytest.raises(PermissionError, match="non-symlink"):
        scheduler.install_macos(
            config,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

    assert calls == []
    assert list(target.iterdir()) == []


def test_macos_scheduler_rejects_unsafe_log_file_before_launchd_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    logs = config.home_path / "logs"
    logs.mkdir(mode=0o700)
    target = tmp_path / "must-not-change"
    target.write_text("private\n", encoding="utf-8")
    target.chmod(0o600)
    (logs / "scheduler.out.log").symlink_to(target)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        scheduler,
        "_run",
        lambda command: (calls.append(command) or (0, "unexpected")),
    )

    with pytest.raises(PermissionError, match="non-symlink"):
        scheduler.install_macos(
            config,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

    assert calls == []
    assert target.read_text(encoding="utf-8") == "private\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
def test_macos_scheduler_rejects_fifo_log_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    logs = config.home_path / "logs"
    logs.mkdir(mode=0o700)
    os.mkfifo(logs / "scheduler.out.log", mode=0o600)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        scheduler,
        "_run",
        lambda command: (calls.append(command) or (0, "unexpected")),
    )

    with pytest.raises(PermissionError, match="regular files"):
        scheduler.install_macos(
            config,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

    assert calls == []


def test_macos_scheduler_preserves_private_logs_and_status_rejects_broad_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    monkeypatch.setattr(
        scheduler,
        "ghealth_runtime_fingerprint",
        lambda _config, _profile=None: RUNTIME_FINGERPRINT,
    )
    monkeypatch.setattr(
        scheduler,
        "static_runtime_fingerprint",
        lambda _config: STATIC_RUNTIME_FINGERPRINT,
    )
    logs = config.home_path / "logs"
    logs.mkdir(mode=0o700)
    output = logs / "scheduler.out.log"
    error = logs / "scheduler.err.log"
    output.write_text("existing output\n", encoding="utf-8")
    error.write_text("existing error\n", encoding="utf-8")
    output.chmod(0o600)
    error.chmod(0o600)
    launchd = FakeLaunchd()
    monkeypatch.setattr(scheduler, "_run", launchd)

    scheduler.install_macos(
        config,
        static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
    )

    assert output.read_text(encoding="utf-8") == "existing output\n"
    assert error.read_text(encoding="utf-8") == "existing error\n"
    assert scheduler.scheduler_status(config)["log_files_safe"] is True
    if os.name != "nt":
        output.chmod(0o644)
        unsafe = scheduler.scheduler_status(config)
        assert unsafe["log_files_safe"] is False
        assert unsafe["installed"] is False


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

    systemd = FakeSystemd(enabled=True, timer_active=True)

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "daemon-reload" in command:
            return 1, "reload failed"
        return systemd(command)

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
    systemd = FakeSystemd(timer_active=True)
    monkeypatch.setattr(scheduler, "_run", systemd)
    result = scheduler.uninstall_scheduler(config)

    assert result["removed"] is True
    assert result["runtime_stopped"] is True
    assert result["definition_removed"] is False
    assert any("stop" in command for command in systemd.calls)


def test_linux_uninstall_reports_false_only_after_runtime_absence_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")

    systemd = FakeSystemd()
    monkeypatch.setattr(scheduler, "_run", systemd)
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
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    monkeypatch.setattr(
        scheduler,
        "_run",
        FakeSystemd(user_dir=host_home / ".config" / "systemd" / "user"),
    )

    result = scheduler.install_linux(
        config,
        3600,
        "athlete-1",
        static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
    )
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
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    service = user_dir / "open-health-agent-sync.service"
    timer = user_dir / "open-health-agent-sync.timer"
    service.write_text("old service", encoding="utf-8")
    timer.write_text("old timer", encoding="utf-8")
    enabled_attempts = 0
    systemd = FakeSystemd(enabled=True, user_dir=user_dir)

    def fake_run(command: list[str]) -> tuple[int, str]:
        nonlocal enabled_attempts
        if "enable" in command:
            enabled_attempts += 1
            if enabled_attempts == 1:
                return 1, "new enable failed"
        return systemd(command)

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="previous definitions and enabled state were restored"):
        scheduler.install_linux(
            config,
            3600,
            "athlete-1",
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

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
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )

    systemd = FakeSystemd(
        user_dir=host_home / ".config" / "systemd" / "user"
    )

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "enable" in command:
            return 1, "enable failed"
        return systemd(command)

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="new scheduler was rolled back"):
        scheduler.install_linux(
            config,
            3600,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

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
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    service = user_dir / "open-health-agent-sync.service"
    timer = user_dir / "open-health-agent-sync.timer"
    service.write_text("old service", encoding="utf-8")
    timer.write_text("old timer", encoding="utf-8")
    reloads = 0
    systemd = FakeSystemd(enabled=True)

    def fake_run(command: list[str]) -> tuple[int, str]:
        nonlocal reloads
        if "daemon-reload" in command:
            reloads += 1
            return (1, "new reload failed") if reloads == 1 else (0, "old reloaded")
        return systemd(command)

    monkeypatch.setattr(scheduler, "_run", fake_run)

    with pytest.raises(RuntimeError, match="new scheduler was not kept"):
        scheduler.install_linux(
            config,
            3600,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

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
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
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
        scheduler.install_macos(
            config,
            3600,
            "athlete-1",
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

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


@pytest.mark.parametrize("job", ["scheduler", "keepawake"])
def test_macos_install_aborts_when_loaded_state_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job: str
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    label = scheduler.SYNC_LABEL if job == "scheduler" else scheduler.AWAKE_LABEL
    path = host_home / "Library" / "LaunchAgents" / f"{label}.plist"
    path.parent.mkdir(parents=True)
    original = plistlib.dumps({"Label": label, "Old": True})
    path.write_bytes(original)
    calls: list[list[str]] = []

    def fake_run(command: list[str]) -> tuple[int, str]:
        calls.append(command)
        if "print" in command:
            return 5, "permission denied"
        raise AssertionError("no launchd mutation may run after unknown state")

    monkeypatch.setattr(scheduler, "_run", fake_run)
    with pytest.raises(RuntimeError, match="could not verify"):
        if job == "scheduler":
            scheduler.install_macos(
                config,
                static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
                runtime_fingerprint=RUNTIME_FINGERPRINT,
            )
        else:
            scheduler.install_macos_ac_keepawake(config)

    assert path.read_bytes() == original
    assert len(calls) == 1


def test_launchd_incomplete_rollback_keeps_managed_definition_for_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    print_calls = 0

    def fake_run(command: list[str]) -> tuple[int, str]:
        nonlocal print_calls
        if "print" in command:
            print_calls += 1
            if print_calls == 1:
                return 113, "Could not find service in domain"
            return 0, "loaded"
        if "bootstrap" in command:
            return 0, "bootstrapped"
        if "kickstart" in command:
            return 5, "kickstart failed"
        if "bootout" in command:
            return 5, "permission denied"
        return 0, "ok"

    monkeypatch.setattr(scheduler, "_run", fake_run)
    with pytest.raises(RuntimeError, match="Rollback was incomplete"):
        scheduler.install_macos(
            config,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

    path = host_home / "Library" / "LaunchAgents" / f"{scheduler.SYNC_LABEL}.plist"
    assert path.exists()
    assert plistlib.loads(path.read_bytes())["Label"] == scheduler.SYNC_LABEL


def test_linux_install_failure_restores_active_but_disabled_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    service = user_dir / "open-health-agent-sync.service"
    timer = user_dir / "open-health-agent-sync.timer"
    service.write_text("old service", encoding="utf-8")
    timer.write_text("old timer", encoding="utf-8")
    systemd = FakeSystemd(
        enabled=False, timer_active=True, user_dir=user_dir
    )

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "enable" in command and "--now" in command:
            return 1, "new enable failed"
        return systemd(command)

    monkeypatch.setattr(scheduler, "_run", fake_run)
    with pytest.raises(RuntimeError, match="previous definitions and enabled state were restored"):
        scheduler.install_linux(
            config,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

    assert service.read_text(encoding="utf-8") == "old service"
    assert timer.read_text(encoding="utf-8") == "old timer"
    assert systemd.enabled is False
    assert systemd.timer_active is True


def test_linux_install_rollback_stops_service_spawned_by_failed_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    user_dir = host_home / ".config" / "systemd" / "user"
    systemd = FakeSystemd(user_dir=user_dir)

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "enable" in command and "--now" in command:
            systemd.service_active = True
            return 1, "enable failed after service activation"
        return systemd(command)

    monkeypatch.setattr(scheduler, "_run", fake_run)
    with pytest.raises(RuntimeError, match="new scheduler was rolled back"):
        scheduler.install_linux(
            config,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

    assert systemd.service_active is False
    assert any(
        "stop" in command and command[-1].endswith(".service")
        for command in systemd.calls
    )


def test_linux_install_unknown_prior_state_does_not_mutate_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    user_dir = host_home / ".config" / "systemd" / "user"
    user_dir.mkdir(parents=True)
    service = user_dir / "open-health-agent-sync.service"
    timer = user_dir / "open-health-agent-sync.timer"
    service.write_text("old service", encoding="utf-8")
    timer.write_text("old timer", encoding="utf-8")

    def fake_run(command: list[str]) -> tuple[int, str]:
        if "is-enabled" in command:
            return 1, "Failed to connect to bus: unknown response"
        raise AssertionError("no later query or mutation may run")

    monkeypatch.setattr(scheduler, "_run", fake_run)
    with pytest.raises(RuntimeError, match="no definition was removed or changed"):
        scheduler.install_linux(
            config,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

    assert service.read_text(encoding="utf-8") == "old service"
    assert timer.read_text(encoding="utf-8") == "old timer"


def test_linux_uninstall_stops_running_service_without_unit_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    systemd = FakeSystemd(service_active=True)
    monkeypatch.setattr(scheduler, "_run", systemd)

    result = scheduler.uninstall_scheduler(config)

    assert result["removed"] is True
    assert result["service_runtime_stopped"] is True
    assert result["timer_runtime_stopped"] is False
    assert systemd.service_active is False
    assert any(
        "stop" in command and command[-1].endswith(".service")
        for command in systemd.calls
    )


@pytest.mark.parametrize("drift", ["drop-in", "reload-needed", "fragment"])
def test_linux_status_rejects_effective_systemd_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    host_home = tmp_path / "home"
    executable = make_executable(tmp_path / "bin" / "ghealth")
    config = create_config(
        tmp_path / "private", timezone="UTC", ghealth_command=str(executable)
    )
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    monkeypatch.setattr(
        scheduler,
        "static_runtime_fingerprint",
        lambda _config: STATIC_RUNTIME_FINGERPRINT,
    )
    monkeypatch.setattr(
        scheduler,
        "ghealth_runtime_fingerprint",
        lambda _config, _profile=None: RUNTIME_FINGERPRINT,
    )
    user_dir = host_home / ".config" / "systemd" / "user"
    systemd = FakeSystemd(user_dir=user_dir)
    monkeypatch.setattr(scheduler, "_run", systemd)
    scheduler.install_linux(
        config,
        static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
    )
    assert scheduler.scheduler_status(config)["installed"] is True

    if drift == "drop-in":
        systemd.drop_in_paths = str(tmp_path / "override.conf")
    elif drift == "reload-needed":
        systemd.need_daemon_reload = "yes"
    else:
        systemd.fragment_overrides["open-health-agent-sync.service"] = (
            tmp_path / "other.service"
        )

    status = scheduler.scheduler_status(config)
    assert status["installed"] is False
    assert status["effective_runtime_matches"] is False
    assert status["configuration_matches"] is False


def test_linux_install_rolls_back_before_enable_when_effective_unit_is_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    user_dir = host_home / ".config" / "systemd" / "user"
    systemd = FakeSystemd(user_dir=user_dir)
    systemd.drop_in_paths = str(tmp_path / "override.conf")
    monkeypatch.setattr(scheduler, "_run", systemd)

    with pytest.raises(RuntimeError, match="drop-in"):
        scheduler.install_linux(
            config,
            static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
            runtime_fingerprint=RUNTIME_FINGERPRINT,
        )

    assert not (user_dir / "open-health-agent-sync.service").exists()
    assert not (user_dir / "open-health-agent-sync.timer").exists()
    assert systemd.enabled is False
    assert systemd.timer_active is False


def test_macos_status_rejects_stale_loaded_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    executable = make_executable(tmp_path / "bin" / "ghealth")
    config = create_config(
        tmp_path / "private", timezone="UTC", ghealth_command=str(executable)
    )
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        scheduler, "_entrypoint", lambda: PurePosixPath("/opt/oha/health_agent.py")
    )
    monkeypatch.setattr(
        scheduler,
        "static_runtime_fingerprint",
        lambda _config: STATIC_RUNTIME_FINGERPRINT,
    )
    monkeypatch.setattr(
        scheduler,
        "ghealth_runtime_fingerprint",
        lambda _config, _profile=None: RUNTIME_FINGERPRINT,
    )
    launchd = FakeLaunchd()
    monkeypatch.setattr(scheduler, "_run", launchd)
    result = scheduler.install_macos(
        config,
        static_runtime_fingerprint=STATIC_RUNTIME_FINGERPRINT,
        runtime_fingerprint=RUNTIME_FINGERPRINT,
    )
    assert scheduler.scheduler_status(config)["installed"] is True

    path, loaded_payload = launchd.loaded[scheduler.SYNC_LABEL]
    stale_payload = plistlib.loads(plistlib.dumps(loaded_payload))
    stale_payload["StartInterval"] = 7200
    launchd.loaded[scheduler.SYNC_LABEL] = (path, stale_payload)

    status = scheduler.scheduler_status(config)
    assert status["configuration_matches"] is True
    assert status["effective_runtime_matches"] is False
    assert status["installed"] is False
    assert Path(result["path"]).exists()


def test_keepawake_status_requires_exact_owner_only_effective_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "home"
    config = create_config(tmp_path / "private", timezone="UTC")
    patch_home(monkeypatch, host_home)
    monkeypatch.setattr(scheduler.platform, "system", lambda: "Darwin")
    launchd = FakeLaunchd()
    monkeypatch.setattr(scheduler, "_run", launchd)
    result = scheduler.install_macos_ac_keepawake(config)
    path = Path(result["path"])
    assert scheduler.keepawake_status()["installed"] is True

    loaded_path, loaded_payload = launchd.loaded[scheduler.AWAKE_LABEL]
    stale_payload = plistlib.loads(plistlib.dumps(loaded_payload))
    stale_payload["ProgramArguments"] = ["/usr/bin/caffeinate", "-i"]
    launchd.loaded[scheduler.AWAKE_LABEL] = (loaded_path, stale_payload)
    stale = scheduler.keepawake_status()
    assert stale["configuration_matches"] is True
    assert stale["effective_runtime_matches"] is False
    assert stale["installed"] is False

    launchd.loaded[scheduler.AWAKE_LABEL] = (loaded_path, loaded_payload)
    drifted_on_disk = plistlib.loads(path.read_bytes())
    drifted_on_disk["KeepAlive"] = False
    path.write_bytes(plistlib.dumps(drifted_on_disk, sort_keys=True))
    drifted = scheduler.keepawake_status()
    assert drifted["configuration_matches"] is False
    assert drifted["installed"] is False

    if os.name != "nt":
        path.write_bytes(plistlib.dumps(scheduler._macos_keepawake_payload(), sort_keys=True))
        path.chmod(0o644)
        permissive = scheduler.keepawake_status()
        assert permissive["definition_owner_only"] is False
        assert permissive["installed"] is False
