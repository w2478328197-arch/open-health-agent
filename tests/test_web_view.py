from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from oha.database import HealthDatabase
from oha.web_view import (
    create_dashboard_server,
    dashboard_records,
    dashboard_summary,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dashboard_serves_private_records_without_mutating_database(tmp_path: Path) -> None:
    database_path = tmp_path / "health.sqlite3"
    with HealthDatabase(database_path) as database:
        database.upsert(
            "measurement",
            {
                "record_id": "measurement-1",
                "date": "2026-07-17",
                "time": "08:30:00",
                "metric": "resting_heart_rate",
                "value": 52,
                "unit": "bpm",
                "source": "synthetic-test",
            },
        )
    before = _sha256(database_path)
    try:
        server = create_dashboard_server(database_path, host="127.0.0.1", port=0)
    except PermissionError:
        pytest.skip("the execution sandbox does not permit loopback socket binding")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base}/", timeout=5) as response:
            html = response.read().decode("utf-8")
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert "Open Health" in html
            assert "原始数据" in html
            assert 'id="subnav"' in html
            assert "state.subtype" in html
            assert "https://" not in html

        with urlopen(f"{base}/api/summary", timeout=5) as response:
            summary = json.load(response)
            assert summary["counts"]["measurement"] == 1
            assert summary["total_records"] == 1

        with urlopen(
            f"{base}/api/records?kind=measurement&limit=25&offset=0", timeout=5
        ) as response:
            result = json.load(response)
            assert result["total"] == 1
            assert result["items"][0]["metric"] == "resting_heart_rate"
            assert result["items"][0]["value"] == 52

        with pytest.raises(HTTPError) as error:
            urlopen(Request(f"{base}/api/records", method="POST"), timeout=5)
        assert error.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert _sha256(database_path) == before


def test_dashboard_refuses_non_loopback_binding(tmp_path: Path) -> None:
    database_path = tmp_path / "health.sqlite3"
    with HealthDatabase(database_path):
        pass

    with pytest.raises(ValueError, match="loopback"):
        create_dashboard_server(database_path, host="0.0.0.0", port=0)


def test_dashboard_groups_and_filters_records_by_subcategory(tmp_path: Path) -> None:
    database_path = tmp_path / "health.sqlite3"
    with HealthDatabase(database_path) as database:
        for record_id, metric in (("weight", "体重"), ("fat", "体脂率")):
            database.upsert(
                "measurement",
                {
                    "record_id": record_id,
                    "date": "2026-07-17",
                    "metric": metric,
                    "value": 1,
                    "unit": "synthetic",
                },
            )
        for record_id, meal in (("drink-1", "饮品"), ("drink-2", "饮料")):
            database.upsert(
                "food",
                {
                    "record_id": record_id,
                    "date": "2026-07-17",
                    "food_name": "synthetic",
                    "meal": meal,
                    "consumed": True,
                },
            )

    summary = dashboard_summary(database_path)
    assert summary["subcategories"]["measurement"] == [
        {"value": "体脂率", "label": "体脂率", "count": 1},
        {"value": "体重", "label": "体重", "count": 1},
    ]
    assert summary["subcategories"]["food"] == [
        {"value": "饮料", "label": "饮料", "count": 2}
    ]

    filtered = dashboard_records(
        database_path, kind="measurement", subtype="体重"
    )
    assert filtered["total"] == 1
    assert filtered["items"][0]["metric"] == "体重"

    drinks = dashboard_records(database_path, kind="food", subtype="饮料")
    assert drinks["total"] == 2
