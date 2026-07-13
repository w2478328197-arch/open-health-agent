from __future__ import annotations

import json
from pathlib import Path

import pytest

from oha.config import create_config, load_config, save_config


def test_load_config_rejects_copied_home_redirection(tmp_path: Path) -> None:
    expected = tmp_path / "expected"
    other = tmp_path / "other"
    config = create_config(other, timezone="UTC")
    expected.mkdir()
    (expected / "config.json").write_text(
        json.dumps(config.__dict__),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="home mismatch"):
        load_config(expected)


def test_load_config_validates_timezone_retention_and_database_location(tmp_path: Path) -> None:
    home = tmp_path / "private"
    config = create_config(home, timezone="UTC")
    save_config(config)

    raw = json.loads((home / "config.json").read_text(encoding="utf-8"))
    raw["timezone"] = "Not/A_Timezone"
    (home / "config.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid IANA timezone"):
        load_config(home)

    raw["timezone"] = "UTC"
    raw["backup_retention"] = -1
    (home / "config.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="backup_retention"):
        load_config(home)

    raw["backup_retention"] = 14
    raw["database_path"] = str(tmp_path / "outside.sqlite3")
    (home / "config.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="database_path"):
        load_config(home)


def test_load_config_requires_xlsx_workbook(tmp_path: Path) -> None:
    home = tmp_path / "private"
    config = create_config(home, workbook=tmp_path / "unsafe.xlsm", timezone="UTC")
    save_config(config)
    with pytest.raises(ValueError, match=".xlsx"):
        load_config(home)
