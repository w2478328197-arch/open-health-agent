from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .constants import (
    DEFAULT_BACKUP_RETENTION,
    DEFAULT_HOME_NAME,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_PLANNING_TEF_FRACTION,
)


@dataclass
class Config:
    version: int = 1
    home: str = ""
    workbook_path: str = ""
    database_path: str = ""
    timezone: str = "UTC"
    ghealth_command: str = "ghealth"
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    backup_retention: int = DEFAULT_BACKUP_RETENTION
    audit_event_retention: int = 5000
    sync_run_retention: int = 1000
    raw_summary_retention: int = 336
    planning_tef_fraction: float = DEFAULT_PLANNING_TEF_FRACTION
    sleep_source_priority: list[str] = field(default_factory=list)
    activity_energy_semantics: str = "active_only"

    @property
    def home_path(self) -> Path:
        return Path(self.home).expanduser().resolve()

    @property
    def workbook(self) -> Path:
        return Path(self.workbook_path).expanduser().resolve()

    @property
    def database(self) -> Path:
        return Path(self.database_path).expanduser().resolve()


def default_home() -> Path:
    override = os.environ.get("OPEN_HEALTH_AGENT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / DEFAULT_HOME_NAME).resolve()


def config_path(home: Path | None = None) -> Path:
    root = (home or default_home()).expanduser().resolve()
    return root / "config.json"


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        if os.name != "nt":
            try:
                directory_descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                # Some synchronized/network filesystems do not support
                # directory fsync. The file itself is already durable; retain
                # portable best-effort behavior while strengthening local FS.
                pass
    finally:
        if temp.exists():
            temp.unlink()


def create_config(
    home: Path,
    workbook: Path | None = None,
    timezone: str = "UTC",
    ghealth_command: str = "ghealth",
) -> Config:
    root = home.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    selected_workbook = (workbook or (root / "健康档案.xlsx")).expanduser().resolve()
    return Config(
        home=str(root),
        workbook_path=str(selected_workbook),
        database_path=str(root / "health.sqlite3"),
        timezone=timezone,
        ghealth_command=ghealth_command,
    )


def save_config(config: Config) -> Path:
    path = config.home_path / "config.json"
    atomic_write_json(path, asdict(config))
    return path


def load_config(home: Path | None = None) -> Config:
    expected_home = (home or default_home()).expanduser().resolve()
    path = config_path(expected_home)
    if not path.exists():
        raise FileNotFoundError("configuration not found for the selected private data home; run init first")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a JSON object")
    config = Config(**raw)
    for field_name in ("home", "workbook_path", "database_path", "timezone"):
        value = getattr(config, field_name)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"{field_name} must be a non-empty string")
    if config.home_path != expected_home:
        raise ValueError("configuration home mismatch with the selected private data directory")
    if config.database.parent != expected_home:
        raise ValueError("database_path must remain directly inside the private data home")
    if config.workbook.suffix.lower() != ".xlsx":
        raise ValueError("workbook_path must use the .xlsx format")
    if not isinstance(config.ghealth_command, str) or not config.ghealth_command.strip() or "\x00" in config.ghealth_command:
        raise ValueError("ghealth_command must be a non-empty command or executable path")
    try:
        ZoneInfo(config.timezone)
    except Exception as exc:
        raise ValueError(f"invalid IANA timezone: {config.timezone}") from exc
    if not isinstance(config.activity_energy_semantics, str) or config.activity_energy_semantics not in {"active_only", "total_energy"}:
        raise ValueError("activity_energy_semantics must be active_only or total_energy")
    if isinstance(config.planning_tef_fraction, bool) or not isinstance(config.planning_tef_fraction, (int, float)) or not 0 <= config.planning_tef_fraction < 0.5:
        raise ValueError("planning_tef_fraction must be between 0 and 0.5")
    if not isinstance(config.lookback_days, int) or isinstance(config.lookback_days, bool) or not 1 <= config.lookback_days <= 90:
        raise ValueError("lookback_days must be between 1 and 90")
    if isinstance(config.backup_retention, bool) or not isinstance(config.backup_retention, int) or not 0 <= config.backup_retention <= 365:
        raise ValueError("backup_retention must be an integer between 0 and 365")
    for field_name, minimum, maximum in (
        ("audit_event_retention", 100, 100000),
        ("sync_run_retention", 24, 10000),
        ("raw_summary_retention", 24, 10000),
    ):
        value = getattr(config, field_name)
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{field_name} must be an integer between {minimum} and {maximum}")
    if not isinstance(config.sleep_source_priority, list) or not all(
        isinstance(value, str) and value for value in config.sleep_source_priority
    ):
        raise ValueError("sleep_source_priority must be a list of non-empty strings")
    return config


def initialize_local_files(
    config: Config,
    agents_template: Path,
    profile_template: Path,
    force: bool = False,
) -> dict[str, str]:
    root = config.home_path
    paths = {
        "agents": root / "AGENTS.md",
        "profile": root / "profile.json",
        "state": root / "state.json",
    }
    for directory in (root / "backups", root / "logs", root / "locks", root / "raw"):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

    for source, destination in (
        (agents_template, paths["agents"]),
        (profile_template, paths["profile"]),
    ):
        if destination.exists() and not force:
            continue
        if destination.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = destination.with_name(f"{destination.name}.backup-{stamp}")
            shutil.copy2(destination, backup)
            try:
                os.chmod(backup, 0o600)
            except OSError:
                pass
        shutil.copy2(source, destination)
        try:
            os.chmod(destination, 0o600)
        except OSError:
            pass

    if force or not paths["state"].exists():
        atomic_write_json(
            paths["state"],
            {
                "onboarding_explained_at": None,
                "onboarding_consent_at": None,
                "last_sync_at": None,
                "last_successful_sync_at": None,
            },
        )
    return {key: str(value) for key, value in paths.items()}


def load_profile(config: Config) -> dict[str, Any]:
    path = config.home_path / "profile.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_profile(config: Config, profile: dict[str, Any]) -> None:
    atomic_write_json(config.home_path / "profile.json", profile)
