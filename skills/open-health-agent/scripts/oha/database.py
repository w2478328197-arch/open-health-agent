from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator


# Import metadata changes on every overlapping sync even when the health value
# did not. Ignoring these fields for equality prevents an hourly job from
# duplicating the full health payload in the audit table indefinitely.
_IMPORT_METADATA_FIELDS = {"batch_id", "data_until", "imported_at"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_record_id(kind: str, parts: Iterable[Any]) -> str:
    material = canonical_json([kind, *list(parts)]).encode("utf-8")
    return f"{kind[:3]}_{hashlib.sha256(material).hexdigest()[:24]}"


class HealthDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._initialize()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "HealthDatabase":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL,
                record_id TEXT NOT NULL,
                event_date TEXT,
                source TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (kind, record_id)
            );
            CREATE INDEX IF NOT EXISTS idx_records_kind_date
                ON records(kind, event_date);

            CREATE TABLE IF NOT EXISTS sync_runs (
                batch_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                from_date TEXT,
                to_date TEXT,
                status TEXT NOT NULL,
                daily_count INTEGER NOT NULL DEFAULT 0,
                workout_count INTEGER NOT NULL DEFAULT 0,
                measurement_count INTEGER NOT NULL DEFAULT 0,
                data_until TEXT,
                errors_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                action TEXT NOT NULL,
                kind TEXT,
                record_id TEXT,
                payload_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def upsert(
        self,
        kind: str,
        payload: dict[str, Any],
        record_id: str | None = None,
        *,
        merge_existing: bool = False,
    ) -> str:
        selected_id = record_id or str(payload.get("record_id") or "")
        if not selected_id:
            raise ValueError(f"record_id is required for {kind}")
        normalized = dict(payload)
        normalized["record_id"] = selected_id
        now = utc_now()
        with self.transaction():
            existing = self.connection.execute(
                "SELECT created_at, payload_json FROM records WHERE kind=? AND record_id=?",
                (kind, selected_id),
            ).fetchone()
            existing_payload = json.loads(existing["payload_json"]) if existing else None
            if merge_existing and existing_payload:
                normalized = existing_payload | normalized
                normalized["record_id"] = selected_id

            # A re-import with only a new batch/timestamp is a true no-op. Source
            # corrections still update because every health field remains part
            # of this comparison.
            if existing_payload:
                old_business = {
                    key: value
                    for key, value in existing_payload.items()
                    if key not in _IMPORT_METADATA_FIELDS
                }
                new_business = {
                    key: value
                    for key, value in normalized.items()
                    if key not in _IMPORT_METADATA_FIELDS
                }
                if canonical_json(old_business) == canonical_json(new_business):
                    return selected_id

            event_date = str(normalized.get("date") or "") or None
            source = str(normalized.get("source") or normalized.get("training_source") or "") or None
            encoded = canonical_json(normalized)
            created_at = existing["created_at"] if existing else now
            self.connection.execute(
                """
                INSERT INTO records(kind, record_id, event_date, source, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, record_id) DO UPDATE SET
                    event_date=excluded.event_date,
                    source=excluded.source,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (kind, selected_id, event_date, source, encoded, created_at, now),
            )
            action = "update" if existing else "create"
            self.connection.execute(
                "INSERT INTO audit_events(occurred_at, action, kind, record_id, payload_json) VALUES (?, ?, ?, ?, ?)",
                (now, action, kind, selected_id, encoded),
            )
        return selected_id

    def delete(self, kind: str, record_id: str, reason: str) -> bool:
        with self.transaction():
            existing = self.connection.execute(
                "SELECT payload_json FROM records WHERE kind=? AND record_id=?",
                (kind, record_id),
            ).fetchone()
            if not existing:
                return False
            self.connection.execute(
                "DELETE FROM records WHERE kind=? AND record_id=?",
                (kind, record_id),
            )
            self.connection.execute(
                "INSERT INTO audit_events(occurred_at, action, kind, record_id, payload_json) VALUES (?, 'delete', ?, ?, ?)",
                (utc_now(), kind, record_id, canonical_json({"reason": reason})),
            )
        return True

    def list_records(
        self,
        kind: str,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["kind=?"]
        params: list[Any] = [kind]
        if from_date:
            clauses.append("event_date>=?")
            params.append(from_date)
        if to_date:
            clauses.append("event_date<=?")
            params.append(to_date)
        rows = self.connection.execute(
            f"SELECT payload_json FROM records WHERE {' AND '.join(clauses)} ORDER BY event_date, created_at, record_id",
            params,
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def get(self, kind: str, record_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload_json FROM records WHERE kind=? AND record_id=?",
            (kind, record_id),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def begin_sync(self, batch_id: str, from_date: str, to_date: str) -> None:
        self.connection.execute(
            """
            INSERT INTO sync_runs(batch_id, started_at, from_date, to_date, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (batch_id, utc_now(), from_date, to_date),
        )
        self.connection.commit()

    def finish_sync(
        self,
        batch_id: str,
        status: str,
        daily_count: int,
        workout_count: int,
        measurement_count: int,
        data_until: str | None,
        errors: list[str],
    ) -> None:
        self.connection.execute(
            """
            UPDATE sync_runs SET finished_at=?, status=?, daily_count=?, workout_count=?,
                measurement_count=?, data_until=?, errors_json=? WHERE batch_id=?
            """,
            (
                utc_now(),
                status,
                daily_count,
                workout_count,
                measurement_count,
                data_until,
                canonical_json(errors),
                batch_id,
            ),
        )
        self.connection.commit()

    def list_sync_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["errors"] = json.loads(item.pop("errors_json"))
            output.append(item)
        return output

    def prune_history(self, *, audit_limit: int, sync_limit: int) -> None:
        if audit_limit < 1 or sync_limit < 1:
            raise ValueError("history retention limits must be positive")
        with self.transaction():
            self.connection.execute(
                """
                DELETE FROM audit_events
                WHERE event_id NOT IN (
                    SELECT event_id FROM audit_events ORDER BY event_id DESC LIMIT ?
                )
                """,
                (audit_limit,),
            )
            self.connection.execute(
                """
                DELETE FROM sync_runs
                WHERE batch_id NOT IN (
                    SELECT batch_id FROM sync_runs ORDER BY started_at DESC, batch_id DESC LIMIT ?
                )
                """,
                (sync_limit,),
            )
