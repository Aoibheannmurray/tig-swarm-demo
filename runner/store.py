"""Enrollment store for the hosted runner.

One row per contributor who has opted into cloud-run: their encrypted key
bundle, the fleet plan snapshot the runner launches, and lifecycle status.
SQLite in the runner's own DATA_DIR (a Railway volume) — separate from the
coordination server's DB; the two services never share storage.

Sync sqlite3 (not aiosqlite): the runner's request volume is tiny (a handful
of enroll/status calls) and the supervisor already serializes fleet mutations,
so a connection-per-call keeps the store dependency-free.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("RUNNER_DATA_DIR", Path(__file__).parent / "data"))
DB_PATH = DATA_DIR / "runner.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS enrollments (
    username TEXT PRIMARY KEY,
    -- Fernet token over a JSON {env_var_name: value} map (runner.vault).
    encrypted_keys TEXT NOT NULL,
    -- The fleet plan snapshot the supervisor launches (JSON); mirrors the
    -- contributor's console config at enrollment / last update.
    config_json TEXT,
    -- 'stopped' | 'running' | 'error'; the supervisor owns transitions.
    status TEXT NOT NULL DEFAULT 'stopped',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def upsert_enrollment(
    username: str, encrypted_keys: str, config: dict | None, timestamp: str,
) -> None:
    """Create or update a contributor's enrollment. Preserves the existing
    status (an update while running stays running) and created_at."""
    config_json = json.dumps(config) if config is not None else None
    with _connect() as conn:
        existing = conn.execute(
            "SELECT created_at FROM enrollments WHERE username = ?", (username,),
        ).fetchone()
        created = existing["created_at"] if existing else timestamp
        conn.execute(
            "INSERT INTO enrollments "
            "(username, encrypted_keys, config_json, status, created_at, updated_at) "
            "VALUES (?, ?, ?, COALESCE("
            "  (SELECT status FROM enrollments WHERE username = ?), 'stopped'), ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "  encrypted_keys = excluded.encrypted_keys, "
            "  config_json = excluded.config_json, "
            "  updated_at = excluded.updated_at",
            (username, encrypted_keys, config_json, username, created, timestamp),
        )


def get_enrollment(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM enrollments WHERE username = ?", (username,),
        ).fetchone()
    return dict(row) if row else None


def list_enrollments() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM enrollments ORDER BY username",
        ).fetchall()
    return [dict(r) for r in rows]


def set_status(username: str, status: str, timestamp: str, error: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE enrollments SET status = ?, last_error = ?, updated_at = ? "
            "WHERE username = ?",
            (status, error, timestamp, username),
        )


def delete_enrollment(username: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM enrollments WHERE username = ?", (username,))


def config_of(row: dict) -> dict | None:
    raw = row.get("config_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
