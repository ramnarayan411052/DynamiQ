"""SQLite persistence for the DynamiQ dashboard."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND_DIR / ".env")


def _database_path() -> Path:
    configured_path = os.getenv("SQLITE_PATH", "data/dynamiq.sqlite3").strip()
    if not configured_path:
        raise RuntimeError("SQLITE_PATH is empty. Set it in backend/.env.")
    path = Path(configured_path)
    if not path.is_absolute():
        path = BACKEND_DIR / path
    return path.resolve()


@contextmanager
def _connection() -> Iterator[sqlite3.Connection]:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize() -> None:
    with _connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS resources (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                name TEXT NOT NULL,
                active INTEGER NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (kind, id)
            );
            CREATE TABLE IF NOT EXISTS task_rows (
                collection_name TEXT NOT NULL,
                row_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (collection_name, row_id),
                UNIQUE (collection_name, position)
            );
            CREATE TABLE IF NOT EXISTS schedule_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_resources_kind_active
                ON resources (kind, active);
            """
        )


def check_connection() -> None:
    with _connection() as connection:
        connection.execute("SELECT 1").fetchone()


def _validate_collection_name(collection_name: str) -> None:
    if collection_name not in {"tasks", "archived_tasks"}:
        raise ValueError("Unsupported dashboard collection.")


def get_resources(kind: str | None = None, include_inactive: bool = True) -> list[dict]:
    query = "SELECT kind, id, name, active, data FROM resources"
    clauses: list[str] = []
    parameters: list[object] = []
    if kind is not None:
        clauses.append("kind = ?")
        parameters.append(kind)
    if not include_inactive:
        clauses.append("active = 1")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY kind, id"

    with _connection() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [
        {
            "kind": row["kind"],
            "id": row["id"],
            "name": row["name"],
            "active": bool(row["active"]),
            "data": json.loads(row["data"]),
        }
        for row in rows
    ]


def save_resource(kind: str, row: dict, *, create_only: bool = False) -> bool:
    values = (
        kind,
        str(row["id"]),
        row["name"],
        bool(row.get("active", True)),
        json.dumps(row.get("data", {}), ensure_ascii=False),
    )
    with _connection() as connection:
        if create_only:
            try:
                connection.execute(
                    "INSERT INTO resources (kind, id, name, active, data) VALUES (?, ?, ?, ?, ?)",
                    values,
                )
            except sqlite3.IntegrityError:
                return False
            return True

        result = connection.execute(
            "UPDATE resources SET name = ?, active = ?, data = ? WHERE kind = ? AND id = ?",
            (values[2], values[3], values[4], values[0], values[1]),
        )
        return result.rowcount > 0


def set_resource_active(kind: str, resource_id: str, active: bool) -> bool:
    with _connection() as connection:
        result = connection.execute(
            "UPDATE resources SET active = ? WHERE kind = ? AND id = ?",
            (bool(active), kind, resource_id),
        )
        return result.rowcount > 0


def remove_resource(kind: str, resource_id: str) -> bool:
    with _connection() as connection:
        result = connection.execute(
            "DELETE FROM resources WHERE kind = ? AND id = ?",
            (kind, resource_id),
        )
        return result.rowcount > 0


def get_rows(collection_name: str) -> list[dict]:
    _validate_collection_name(collection_name)
    with _connection() as connection:
        rows = connection.execute(
            "SELECT data FROM task_rows WHERE collection_name = ? ORDER BY position",
            (collection_name,),
        ).fetchall()
    return [json.loads(row["data"]) for row in rows]


def replace_rows(collection_name: str, id_field: str, rows: list[dict]) -> None:
    _validate_collection_name(collection_name)
    ids = [str(row.get(id_field, "")).strip() for row in rows]
    if any(not value for value in ids) or len({value.casefold() for value in ids}) != len(ids):
        raise ValueError(f"{id_field} values must be non-empty and unique.")

    encoded_rows = [
        (collection_name, row_id, index, json.dumps(row, ensure_ascii=False))
        for index, (row_id, row) in enumerate(zip(ids, rows))
    ]
    with _connection() as connection:
        connection.execute(
            "DELETE FROM task_rows WHERE collection_name = ?",
            (collection_name,),
        )
        connection.executemany(
            "INSERT INTO task_rows (collection_name, row_id, position, data) VALUES (?, ?, ?, ?)",
            encoded_rows,
        )


def get_latest_schedule() -> dict | None:
    with _connection() as connection:
        row = connection.execute(
            "SELECT data FROM schedule_state WHERE id = 1"
        ).fetchone()
    return json.loads(row["data"]) if row else None


def save_latest_schedule(data: dict) -> None:
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO schedule_state (id, data) VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET data = excluded.data
            """,
            (json.dumps(data, ensure_ascii=False),),
        )
