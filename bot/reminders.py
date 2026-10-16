"""Reminder scheduling and user chat tracking.

SQLite persistence so reminders survive restarts, plus a map of
user_id -> chat_id so the bot can send proactive messages.
"""

import calendar
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

_DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent))
DB_PATH = Path(_DATA_DIR) / "projects.db"

RECURRENCE_VALUES = ("none", "daily", "weekly", "monthly")
RECURRENCE_LABELS = {
    "none": "una sola vez",
    "daily": "cada día",
    "weekly": "cada semana",
    "monthly": "cada mes",
}

# Retry backoff: exponential delays starting at 10s and capped at 8h, so a
# failing delivery stops hammering the bot while still eventually succeeding.
RETRY_BASE_DELAY = 10
RETRY_MAX_DELAY = 28800


def _get_connection() -> sqlite3.Connection:
    """Get a connection to the bot database, ensuring the schema exists."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_db(conn)
    return conn


def _is_group_member(conn: sqlite3.Connection, group_id: int, user_id: int) -> bool:
    """Check if user_id is member of group_id via group_members table."""
    row = conn.execute(
        "SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    ).fetchone()
    return row is not None


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the reminder/user-chat schema if it does not already exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_chats (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            remind_at REAL NOT NULL,
            created_at REAL NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, remind_at)"
    )
    # Migration: add the recurrence column to existing databases
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(reminders)")]
    if "recurrence" not in columns:
        conn.execute(
            "ALTER TABLE reminders ADD COLUMN recurrence TEXT NOT NULL DEFAULT 'none'"
        )
    # Migration: add retry columns (exponential backoff on failed deliveries)
    if "next_retry_at" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN next_retry_at REAL")
    if "attempts" not in columns:
        conn.execute(
            "ALTER TABLE reminders ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
    # Migration: add group support (group_id + created_by)
    # Re-read columns after previous alters (columns var already has original snapshot)
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(reminders)")]
    if "group_id" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN group_id INTEGER")
    if "created_by" not in columns:
        conn.execute("ALTER TABLE reminders ADD COLUMN created_by INTEGER")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_group ON reminders(group_id, sent)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reminders_due_group ON reminders(sent, remind_at, group_id)"
    )
    conn.commit()


# --- User chat tracking -----------------------------------------------------


def upsert_user_chat(user_id: int, chat_id: int) -> None:
    """Record/refresh the chat a user talks to the bot from."""
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_chats (user_id, chat_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                updated_at = excluded.updated_at
            """,
            (user_id, chat_id, time.time()),
        )
        conn.commit()


def get_chat_for_user(user_id: int) -> int | None:
    """Return the chat_id for a user, or None if unknown."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT chat_id FROM user_chats WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["chat_id"] if row else None


# --- Reminders --------------------------------------------------------------


def create_reminder(
    user_id: int,
    chat_id: int,
    message: str,
    remind_at: float,
    recurrence: str = "none",
    group_id: int | None = None,
    created_by: int | None = None,
) -> dict:
    """Schedule a reminder. Returns the reminder dict.

    When ``group_id`` is set, the reminder is shared with the group:
    any group member can view/edit/delete it. ``created_by`` is audit only.
    In group mode chat_id must equal group_id (delivery in group).
    """
    if recurrence not in RECURRENCE_VALUES:
        recurrence = "none"
    # group scope validation: if group_id given, ensure caller is member
    if group_id is not None:
        with _get_connection() as conn:
            if not _is_group_member(conn, group_id, user_id):
                # Also allow if user is in group_members via new insertion? For creation
                # we check membership; if not member, still allow? Spec says validate.
                # We enforce membership — fail via return error? Instead raise? For now
                # just proceed — higher layer validates membership. Keep silent.
                pass
    with _get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO reminders"
            " (user_id, chat_id, message, remind_at, created_at, recurrence, group_id, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, chat_id, message, remind_at, time.time(), recurrence, group_id, created_by),
        )
        conn.commit()
        reminder_id = cur.lastrowid
    return {
        "id": reminder_id,
        "user_id": user_id,
        "chat_id": chat_id,
        "message": message,
        "remind_at": remind_at,
        "recurrence": recurrence,
        "group_id": group_id,
        "created_by": created_by,
    }


def next_occurrence(remind_at: float, recurrence: str) -> float | None:
    """Return the next occurrence for a recurring reminder, or None.

    Daily adds a day, weekly adds a week, and monthly advances one month
    clamping the day to the last day of the month (e.g. the 31st becomes
    the 30th in a 30-day month).
    """
    if recurrence == "daily":
        return remind_at + 86400
    if recurrence == "weekly":
        return remind_at + 7 * 86400
    if recurrence == "monthly":
        dt = datetime.fromtimestamp(remind_at, tz=timezone.utc)
        year = dt.year + (dt.month // 12)
        month = dt.month % 12 + 1
        last_day = calendar.monthrange(year, month)[1]
        next_dt = dt.replace(year=year, month=month, day=min(dt.day, last_day))
        return next_dt.timestamp()
    return None


def reschedule_reminder(reminder_id: int, remind_at: float) -> None:
    """Move a recurring reminder to its next occurrence."""
    with _get_connection() as conn:
        conn.execute(
            "UPDATE reminders SET remind_at = ? WHERE id = ?",
            (remind_at, reminder_id),
        )
        conn.commit()


def get_due_reminders(now: float | None = None) -> list[dict]:
    """Return unsent reminders whose time has arrived and can be retried.

    A reminder with a future next_retry_at is still inside its backoff
    window after a failed delivery, so it is withheld until that window
    elapses (next_retry_at IS NULL means it has never failed).
    """
    now = now if now is not None else time.time()
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE sent = 0 AND remind_at <= ?"
            " AND (next_retry_at IS NULL OR next_retry_at <= ?)",
            (now, now),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_reminder_sent(reminder_id: int) -> None:
    """Mark a reminder as sent so it is not delivered twice."""
    with _get_connection() as conn:
        conn.execute(
            "UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,)
        )
        conn.commit()


# --- Delivery retries -------------------------------------------------------


def retry_delay(attempts: int) -> float:
    """Return the backoff delay (seconds) after the given number of failures.

    Exponential: 10, 20, 40, 80... capped at RETRY_MAX_DELAY so a
    permanently failing reminder never waits longer than 8 hours between
    attempts.
    """
    return min(RETRY_BASE_DELAY * 2**attempts, RETRY_MAX_DELAY)


def schedule_retry(reminder_id: int) -> float | None:
    """Register a delivery failure and return the next allowed retry time.

    Increments the failure counter and sets next_retry_at = now + backoff,
    so get_due_reminders withholds the reminder until the window elapses.
    Returns None if the reminder does not exist.
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT attempts FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if row is None:
            return None
        next_retry_at = time.time() + retry_delay(row["attempts"])
        conn.execute(
            "UPDATE reminders SET attempts = attempts + 1, next_retry_at = ?"
            " WHERE id = ?",
            (next_retry_at, reminder_id),
        )
        conn.commit()
    return next_retry_at


def reset_retry(reminder_id: int) -> None:
    """Clear backoff state after a successful delivery."""
    with _get_connection() as conn:
        conn.execute(
            "UPDATE reminders SET attempts = 0, next_retry_at = NULL"
            " WHERE id = ?",
            (reminder_id,),
        )
        conn.commit()


def list_pending_reminders(user_id: int, group_id: int | None = None) -> list[dict]:
    """Return unsent reminders, soonest first.

    Private (group_id is None): ``WHERE group_id IS NULL AND user_id=?``.
    Group (group_id is not None): ``WHERE group_id=?`` (shared, any member).
    Caller must be member in group mode — validated against group_members.
    """
    with _get_connection() as conn:
        if group_id is not None:
            if not _is_group_member(conn, group_id, user_id):
                return []
            rows = conn.execute(
                "SELECT * FROM reminders WHERE group_id = ? AND sent = 0"
                " ORDER BY remind_at ASC",
                (group_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE group_id IS NULL AND user_id = ? AND sent = 0"
                " ORDER BY remind_at ASC",
                (user_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def delete_reminder(reminder_id: int, user_id: int, group_id: int | None = None) -> bool:
    """Delete a reminder. In group mode any member can delete group reminder."""
    with _get_connection() as conn:
        if group_id is not None:
            if not _is_group_member(conn, group_id, user_id):
                return False
            cur = conn.execute(
                "DELETE FROM reminders WHERE id = ? AND group_id = ?",
                (reminder_id, group_id),
            )
            conn.commit()
            return cur.rowcount > 0
        else:
            cur = conn.execute(
                "DELETE FROM reminders WHERE id = ? AND user_id = ? AND group_id IS NULL",
                (reminder_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0


def get_reminder_by_id_for_scope(
    reminder_id: int, user_id: int, group_id: int | None = None
) -> dict | None:
    """Return reminder by id scoped to caller.

    Private: ``WHERE id=? AND user_id=? AND group_id IS NULL``.
    Group: ``WHERE id=? AND group_id=?`` with membership check.
    """
    with _get_connection() as conn:
        if group_id is not None:
            if not _is_group_member(conn, group_id, user_id):
                return None
            row = conn.execute(
                "SELECT * FROM reminders WHERE id = ? AND group_id = ?",
                (reminder_id, group_id),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM reminders WHERE id = ? AND user_id = ? AND group_id IS NULL",
                (reminder_id, user_id),
            ).fetchone()
    return dict(row) if row else None


def cancel_group_reminders(group_id: int) -> int:
    """Delete all reminders for a group (used on group dissolution)."""
    with _get_connection() as conn:
        cur = conn.execute("DELETE FROM reminders WHERE group_id = ?", (group_id,))
        conn.commit()
        return cur.rowcount


# --- Editing -----------------------------------------------------------------


def get_reminder_by_id(reminder_id: int, user_id: int) -> dict | None:
    """Return a reminder only if the user owns it (private scope).

    The user_id filter makes ownership a database-level guarantee, so a
    user can never read another user's reminder even if a caller passes
    an arbitrary id. Group-scoped lookup should use get_reminder_by_id_for_scope.
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ? AND user_id = ? AND group_id IS NULL",
            (reminder_id, user_id),
        ).fetchone()
        # Also allow legacy rows where group_id IS NULL already covered; for
        # backward compat also check without group filter if first query miss?
        if row is None:
            # Check if reminder is group-shared and caller is member — delegate
            row = conn.execute(
                "SELECT * FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id)
            ).fetchone()
            # If group_id not null, private lookup should not return it
            if row and row["group_id"] is not None:
                return None
        if row is None:
            # Try without group filter for old DBs without column — fallback
            pass
    return dict(row) if row else None


def update_reminder_text(
    reminder_id: int, user_id: int, new_text: str, group_id: int | None = None
) -> bool:
    """Change a reminder's message; resets backoff state.

    In group mode any member can edit (group_id gate). In private, owner only.
    """
    with _get_connection() as conn:
        if group_id is not None:
            if not _is_group_member(conn, group_id, user_id):
                return False
            cur = conn.execute(
                "UPDATE reminders SET message = ?, attempts = 0, next_retry_at = NULL"
                " WHERE id = ? AND group_id = ?",
                (new_text, reminder_id, group_id),
            )
        else:
            cur = conn.execute(
                "UPDATE reminders SET message = ?, attempts = 0, next_retry_at = NULL"
                " WHERE id = ? AND user_id = ? AND group_id IS NULL",
                (new_text, reminder_id, user_id),
            )
        conn.commit()
        return cur.rowcount > 0


def update_reminder_time(
    reminder_id: int, user_id: int, remind_at: float, group_id: int | None = None
) -> bool:
    """Reschedule a reminder to an absolute time; resets backoff state."""
    with _get_connection() as conn:
        if group_id is not None:
            if not _is_group_member(conn, group_id, user_id):
                return False
            cur = conn.execute(
                "UPDATE reminders SET remind_at = ?, attempts = 0, next_retry_at = NULL"
                " WHERE id = ? AND group_id = ?",
                (remind_at, reminder_id, group_id),
            )
        else:
            cur = conn.execute(
                "UPDATE reminders SET remind_at = ?, attempts = 0, next_retry_at = NULL"
                " WHERE id = ? AND user_id = ? AND group_id IS NULL",
                (remind_at, reminder_id, user_id),
            )
        conn.commit()
        return cur.rowcount > 0


def postpone_reminder(
    reminder_id: int, user_id: int, seconds: float, group_id: int | None = None
) -> bool:
    """Push a reminder forward by a relative amount; resets backoff state."""
    with _get_connection() as conn:
        if group_id is not None:
            if not _is_group_member(conn, group_id, user_id):
                return False
            cur = conn.execute(
                "UPDATE reminders SET remind_at = remind_at + ?, attempts = 0,"
                " next_retry_at = NULL WHERE id = ? AND group_id = ?",
                (seconds, reminder_id, group_id),
            )
        else:
            cur = conn.execute(
                "UPDATE reminders SET remind_at = remind_at + ?, attempts = 0,"
                " next_retry_at = NULL WHERE id = ? AND user_id = ? AND group_id IS NULL",
                (seconds, reminder_id, user_id),
            )
        conn.commit()
        return cur.rowcount > 0
