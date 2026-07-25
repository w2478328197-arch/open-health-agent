from __future__ import annotations

import json
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


DASHBOARD_PATH = Path(__file__).resolve().parents[2] / "assets" / "web-dashboard.html"
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_MAX_PAGE_SIZE = 200
_SUBTYPE_FIELDS = {
    "measurement": "metric",
    "workout": "workout_type",
    "food": "meal",
    "goal": "status",
}
_SUBTYPE_ALIASES = {
    "food": {
        "饮料": ("饮品", "饮料"),
        "未分类": ("未注明", "未注明（图片）", "未注明（图片补充）"),
    },
    "workout": {"其他": ("锻炼",)},
}
_SUBTYPE_LABELS = {"goal": {"active": "进行中", "retired": "历史"}}
_DAILY_GROUPS = {
    "sleep": (
        "睡眠",
        (
            "sleep_hours",
            "sleep_start",
            "sleep_end",
            "awake_minutes",
            "deep_minutes",
            "light_minutes",
            "rem_minutes",
        ),
    ),
    "activity": (
        "活动",
        ("steps", "distance_km", "active_energy_kcal", "active_minutes"),
    ),
    "recovery": (
        "心肺恢复",
        (
            "resting_heart_rate_bpm",
            "hrv_ms",
            "respiratory_rate",
            "spo2_percent",
            "vo2max",
        ),
    ),
    "body": ("身体组成", ("weight_kg", "body_fat_percent", "height_cm")),
}


def _canonical_subtype(kind: str, value: Any) -> str:
    selected = str(value or "").strip()
    if not selected:
        return ""
    for canonical, aliases in _SUBTYPE_ALIASES.get(kind, {}).items():
        if selected in aliases:
            return canonical
    return selected


def _subtype_label(kind: str, value: str) -> str:
    return _SUBTYPE_LABELS.get(kind, {}).get(value, value)


def _dashboard_subcategories(
    connection: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for kind, field in _SUBTYPE_FIELDS.items():
        counts: dict[str, int] = {}
        rows = connection.execute(
            "SELECT payload_json FROM records WHERE kind=?", (kind,)
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            value = _canonical_subtype(kind, payload.get(field))
            if value:
                counts[value] = counts.get(value, 0) + 1
        result[kind] = [
            {"value": value, "label": _subtype_label(kind, value), "count": count}
            for value, count in sorted(
                counts.items(), key=lambda item: _subtype_label(kind, item[0])
            )
        ]

    daily_rows = connection.execute(
        "SELECT payload_json FROM records WHERE kind='daily'"
    ).fetchall()
    daily_payloads = [json.loads(row["payload_json"]) for row in daily_rows]
    result["daily"] = [
        {
            "value": value,
            "label": label,
            "count": sum(
                1
                for payload in daily_payloads
                if any(payload.get(field) not in (None, "") for field in fields)
            ),
        }
        for value, (label, fields) in _DAILY_GROUPS.items()
    ]
    return result


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    path = database_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def dashboard_summary(database_path: Path) -> dict[str, Any]:
    with _connect_read_only(database_path) as connection:
        rows = connection.execute(
            "SELECT kind, COUNT(*) AS count, MIN(event_date) AS first_date, "
            "MAX(event_date) AS last_date FROM records GROUP BY kind ORDER BY kind"
        ).fetchall()
        latest_sync = connection.execute(
            "SELECT finished_at, status, trigger, from_date, to_date, daily_count, "
            "workout_count, measurement_count FROM sync_runs "
            "ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        subcategories = _dashboard_subcategories(connection)
    counts = {row["kind"]: row["count"] for row in rows}
    ranges = {
        row["kind"]: {"first": row["first_date"], "last": row["last_date"]}
        for row in rows
    }
    return {
        "counts": counts,
        "ranges": ranges,
        "total_records": sum(counts.values()),
        "latest_sync": dict(latest_sync) if latest_sync else None,
        "subcategories": subcategories,
        "read_only": True,
    }


def dashboard_records(
    database_path: Path,
    *,
    kind: str | None = None,
    subtype: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    query: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    selected_limit = max(1, min(int(limit), _MAX_PAGE_SIZE))
    selected_offset = max(0, int(offset))
    clauses: list[str] = []
    params: list[Any] = []
    if kind and kind != "all":
        if len(kind) > 64:
            raise ValueError("invalid record kind")
        clauses.append("kind=?")
        params.append(kind)
    if subtype:
        if not kind or kind == "all":
            raise ValueError("subcategory requires a record kind")
        if kind != "daily":
            field = _SUBTYPE_FIELDS.get(kind)
            if field is None or len(subtype) > 64:
                raise ValueError("invalid record subcategory")
            aliases = _SUBTYPE_ALIASES.get(kind, {}).get(subtype, (subtype,))
            placeholders = ",".join("?" for _ in aliases)
            clauses.append(
                f"json_extract(payload_json, '$.{field}') IN ({placeholders})"
            )
            params.extend(aliases)
    if from_date:
        clauses.append("event_date>=?")
        params.append(from_date)
    if to_date:
        clauses.append("event_date<=?")
        params.append(to_date)
    if query:
        clauses.append("payload_json LIKE ?")
        params.append(f"%{query[:120]}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect_read_only(database_path) as connection:
        total = connection.execute(
            f"SELECT COUNT(*) AS count FROM records{where}", params
        ).fetchone()["count"]
        rows = connection.execute(
            "SELECT kind, record_id, event_date, source, payload_json, created_at, updated_at "
            f"FROM records{where} ORDER BY event_date DESC, updated_at DESC, record_id "
            "LIMIT ? OFFSET ?",
            [*params, selected_limit, selected_offset],
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        payload["_kind"] = row["kind"]
        payload["_record_id"] = row["record_id"]
        payload["_event_date"] = row["event_date"]
        payload["_source"] = row["source"]
        payload["_created_at"] = row["created_at"]
        payload["_updated_at"] = row["updated_at"]
        items.append(payload)
    return {
        "items": items,
        "total": total,
        "limit": selected_limit,
        "offset": selected_offset,
        "read_only": True,
    }


class _DashboardHandler(BaseHTTPRequestHandler):
    database_path: Path
    dashboard_html: bytes

    def log_message(self, format: str, *args: object) -> None:
        del format, args
        return None

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._headers(content_type, len(body))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        selected = urlsplit(self.path)
        try:
            if selected.path == "/":
                self._send_bytes(HTTPStatus.OK, self.dashboard_html, "text/html; charset=utf-8")
                return
            if selected.path == "/favicon.ico":
                self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return
            if selected.path == "/api/summary":
                self._send_json(HTTPStatus.OK, dashboard_summary(self.database_path))
                return
            if selected.path == "/api/records":
                params = parse_qs(selected.query, keep_blank_values=False)
                result = dashboard_records(
                    self.database_path,
                    kind=(params.get("kind") or [None])[0],
                    subtype=(params.get("subtype") or [None])[0],
                    from_date=(params.get("from") or [None])[0],
                    to_date=(params.get("to") or [None])[0],
                    query=(params.get("q") or [None])[0],
                    limit=int((params.get("limit") or ["50"])[0]),
                    offset=int((params.get("offset") or ["0"])[0]),
                )
                self._send_json(HTTPStatus.OK, result)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except (ValueError, TypeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "data_unavailable"})

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only"})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def create_dashboard_server(
    database_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    dashboard_path: Path = DASHBOARD_PATH,
) -> ThreadingHTTPServer:
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("health dashboard must bind to a loopback address")
    resolved_database = database_path.expanduser().resolve()
    with _connect_read_only(resolved_database) as connection:
        connection.execute("SELECT 1 FROM records LIMIT 1").fetchone()
    html = dashboard_path.read_bytes()

    class Handler(_DashboardHandler):
        database_path = resolved_database
        dashboard_html = html

    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    return server
