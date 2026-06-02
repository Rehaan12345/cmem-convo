"""
Registry of council members, backed by the shared SQLAlchemy engine (SQLite locally,
PostgreSQL on Railway). The `members` table is created by history.py on startup.
"""
import json
import time

from sqlalchemy import text

from history import engine
from logger import get_logger

log = get_logger(__name__)


def upsert_member(member_id: str, name: str, district: str, files: list[dict]) -> None:
    now = time.time()
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO members (member_id, name, district, files_json, created_at, last_seeded_at)
                VALUES (:mid, :name, :district, :files, :now, :now)
                ON CONFLICT (member_id) DO UPDATE SET
                  name           = EXCLUDED.name,
                  district       = EXCLUDED.district,
                  files_json     = EXCLUDED.files_json,
                  last_seeded_at = EXCLUDED.last_seeded_at
            """),
            {"mid": member_id, "name": name, "district": district,
             "files": json.dumps(files), "now": now},
        )
        conn.commit()
    log.info("Upserted member '%s' (%d files)", member_id, len(files))


def list_members() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT member_id, name, district, last_seeded_at FROM members ORDER BY member_id")
        ).fetchall()
    return [{"id": r[0], "name": r[1], "district": r[2], "last_seeded_at": r[3]} for r in rows]


def get_member(member_id: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT member_id, name, district, files_json FROM members WHERE member_id = :mid"),
            {"mid": member_id},
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "district": row[2], "files": json.loads(row[3])}


def delete_member(member_id: str) -> None:
    with engine.connect() as conn:
        conn.execute(
            text("DELETE FROM members WHERE member_id = :mid"),
            {"mid": member_id},
        )
        conn.commit()
    log.info("Deleted member '%s' from registry", member_id)
