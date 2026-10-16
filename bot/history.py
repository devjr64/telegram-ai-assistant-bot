import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent))
DB_PATH = Path(_DATA_DIR) / "chat_history.db"


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    content: str


def _get_connection() -> sqlite3.Connection:
    """Get a connection to the SQLite database, creating tables if needed."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id, chat_id, created_at)"
    )
    _migrate_add_chat_id(conn)
    conn.commit()
    return conn


def _migrate_add_chat_id(conn: sqlite3.Connection) -> None:
    """Add chat_id column to existing tables that lack it.

    Existing rows get chat_id = user_id (private chat behavior).
    """
    cursor = conn.execute("PRAGMA table_info(chat_history)")
    columns = {row[1] for row in cursor.fetchall()}
    if "chat_id" in columns:
        return
    conn.execute("ALTER TABLE chat_history ADD COLUMN chat_id INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE chat_history SET chat_id = user_id WHERE chat_id = 0")


def add_message(user_id: int, role: str, content: str, chat_id: int | None = None) -> None:
    """Append a message to the user's chat history.

    When ``chat_id`` is provided the message is scoped to that specific
    chat (group or private).  When omitted it defaults to ``user_id``
    which preserves backward compatibility for private chats.
    """
    effective_chat_id = chat_id if chat_id is not None else user_id
    with _get_connection() as conn:
        conn.execute(
            "INSERT INTO chat_history (user_id, chat_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, effective_chat_id, role, content, time.time()),
        )
        conn.commit()


def get_history(user_id: int, max_messages: int, chat_id: int | None = None) -> list[dict[str, str]]:
    """Retrieve the most recent messages for a user in a specific chat.

    The window is the LAST ``max_messages * 2`` rows (user + assistant per
    exchange) so the model sees the recent conversation, not the beginning
    of it. SQLite is queried DESC (newest first) and the result reversed
    before returning so the LLM receives chronological order.

    When ``chat_id`` is provided, only messages from that chat are returned.
    When omitted it defaults to ``user_id`` for backward compatibility.
    """
    effective_chat_id = chat_id if chat_id is not None else user_id
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_history "
            "WHERE user_id = ? AND chat_id = ? "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (user_id, effective_chat_id, max_messages * 2),
        ).fetchall()

    # DESC fetched newest-first; reverse to present the window oldest-first.
    return [{"role": row[0], "content": row[1]} for row in reversed(rows)]


def get_history_with_timestamps(
    user_id: int, max_messages: int, chat_id: int | None = None
) -> list[dict]:
    """Retrieve the most recent messages with ``created_at`` timestamps.

    Same window logic as :func:`get_history` but each dict also carries
    ``created_at`` (float epoch seconds) so callers can inject per-message
    dates for the AI model. Keeps ``get_history`` unchanged for backward
    compatibility with existing callers and tests.
    """
    effective_chat_id = chat_id if chat_id is not None else user_id
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat_history "
            "WHERE user_id = ? AND chat_id = ? "
            "ORDER BY created_at DESC "
            "LIMIT ?",
            (user_id, effective_chat_id, max_messages * 2),
        ).fetchall()

    return [
        {"role": row[0], "content": row[1], "created_at": row[2]}
        for row in reversed(rows)
    ]


def clear_history(user_id: int, chat_id: int | None = None) -> None:
    """Delete chat history for a user, optionally scoped to a specific chat.

    When ``chat_id`` is provided, only messages from that chat are deleted.
    When omitted, all messages for the user are deleted (backward compat).
    """
    with _get_connection() as conn:
        if chat_id is not None:
            conn.execute(
                "DELETE FROM chat_history WHERE user_id = ? AND chat_id = ?",
                (user_id, chat_id),
            )
        else:
            conn.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
        conn.commit()



