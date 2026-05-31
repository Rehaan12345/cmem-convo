"""
SQLite-backed registry of council members.

All reads/writes go through the `members` table in chat_history.db,
created by history.py on startup.
"""
import json
import os
import time
import sqlite3
from pathlib import Path

from logger import get_logger

log = get_logger(__name__)

_DB_PATH = Path(os.getenv("HISTORY_DB_PATH", str(Path(__file__).parent / "chat_history.db")))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def upsert_member(member_id: str, name: str, district: str, files: list[dict]) -> None:
    now = time.time()
    with _conn() as c:
        c.execute("""
            INSERT INTO members (member_id, name, district, files_json, created_at, last_seeded_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(member_id) DO UPDATE SET
              name           = excluded.name,
              district       = excluded.district,
              files_json     = excluded.files_json,
              last_seeded_at = excluded.last_seeded_at
        """, (member_id, name, district, json.dumps(files), now, now))
    log.info("Upserted member '%s' (%d files)", member_id, len(files))


def list_members() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT member_id, name, district, last_seeded_at FROM members ORDER BY member_id"
        ).fetchall()
    return [{"id": r[0], "name": r[1], "district": r[2], "last_seeded_at": r[3]} for r in rows]


def get_member(member_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT member_id, name, district, files_json FROM members WHERE member_id = ?",
            (member_id,),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "district": row[2], "files": json.loads(row[3])}


def delete_member(member_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM members WHERE member_id = ?", (member_id,))
    log.info("Deleted member '%s' from registry", member_id)
