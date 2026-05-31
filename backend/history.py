import json
import os
import time
from pathlib import Path

from sqlalchemy import create_engine, event, text
from langchain_community.chat_message_histories import SQLChatMessageHistory
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

from logger import get_logger

log = get_logger(__name__)

_DB_PATH = Path(os.getenv("HISTORY_DB_PATH", str(Path(__file__).parent / "chat_history.db")))
HISTORY_WINDOW = 5

_engine = create_engine(
    f"sqlite:///{_DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(_engine, "connect")
def _set_wal(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=30000")


def _ensure_tables():
    with _engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id      TEXT PRIMARY KEY,
                title           TEXT,
                legislation_ids TEXT NOT NULL DEFAULT '[]',
                created_at      REAL NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS members (
                member_id       TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                district        TEXT NOT NULL,
                files_json      TEXT NOT NULL DEFAULT '[]',
                created_at      REAL NOT NULL,
                last_seeded_at  REAL
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS message_sources (
                session_id      TEXT NOT NULL,
                exchange_index  INTEGER NOT NULL,
                sources_json    TEXT NOT NULL DEFAULT '[]',
                followups_json  TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (session_id, exchange_index)
            )
        """))
        conn.commit()
    log.info("Database tables ready at %s", _DB_PATH)


_ensure_tables()


def get_history(session_id: str) -> SQLChatMessageHistory:
    return SQLChatMessageHistory(session_id=session_id, connection=_engine)


def load_recent(session_id: str) -> list[BaseMessage]:
    return get_history(session_id).messages[-(HISTORY_WINDOW * 2):]


def save_exchange(
    session_id: str,
    question: str,
    answer: str,
    sources: list[str] | None = None,
    followups: list[str] | None = None,
    member_id: str | None = None,
) -> None:
    h = get_history(session_id)
    exchange_index = len(h.messages) // 2
    h.add_user_message(question)
    h.add_ai_message(answer)
    with _engine.connect() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO sessions (session_id, title, legislation_ids, created_at) "
                "VALUES (:sid, :title, :legs, :ts)"
            ),
            {"sid": session_id, "title": question[:120],
             "legs": json.dumps([member_id] if member_id else []), "ts": time.time()},
        )
        conn.execute(
            text(
                "INSERT OR REPLACE INTO message_sources "
                "(session_id, exchange_index, sources_json, followups_json) "
                "VALUES (:sid, :idx, :src, :fup)"
            ),
            {"sid": session_id, "idx": exchange_index,
             "src": json.dumps(sources or []), "fup": json.dumps(followups or [])},
        )
        conn.commit()
    log.info("Saved exchange for session %s (member=%s)", session_id, member_id)


def list_sessions() -> list[dict]:
    with _engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT session_id, title, legislation_ids, created_at "
                "FROM sessions ORDER BY created_at DESC"
            )
        ).fetchall()
    return [
        {
            "session_id": r[0],
            "title": r[1],
            "legislation_ids": json.loads(r[2]),
            "created_at": r[3],
        }
        for r in rows
    ]


def get_session_messages(session_id: str) -> list[dict]:
    msgs = get_history(session_id).messages
    with _engine.connect() as conn:
        rows = conn.execute(
            text("SELECT exchange_index, sources_json, followups_json "
                 "FROM message_sources WHERE session_id = :sid"),
            {"sid": session_id},
        ).fetchall()
    sources_map = {r[0]: (json.loads(r[1]), json.loads(r[2])) for r in rows}

    result = []
    exchange_idx = 0
    for m in msgs:
        is_ai = isinstance(m, AIMessage)
        entry: dict = {
            "role": "user" if isinstance(m, HumanMessage) else "assistant",
            "content": m.content,
        }
        if is_ai:
            src, fup = sources_map.get(exchange_idx, ([], []))
            entry["sources"] = src
            entry["followups"] = fup
            exchange_idx += 1
        result.append(entry)
    return result


def delete_session(session_id: str) -> None:
    get_history(session_id).clear()
    with _engine.connect() as conn:
        conn.execute(text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id})
        conn.commit()
    log.info("Deleted session %s", session_id)
