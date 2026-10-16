"""Daily per-user media usage counters (images and audio).

SQLite persistence so the daily caps survive restarts. A "day" is the
local calendar date (YYYY-MM-DD) passed in by the caller so it matches
the bot's configured timezone; the count resets automatically when the
date changes.

The cap is enforced atomically: ``try_increment`` checks AND increments
in a single SQLite statement, so two concurrent requests from the same
user can never both pass the check (no check-then-increment race). A
failed media operation gives its slot back via ``refund_usage``, so
failed attempts never burn the daily quota.

Media files themselves are never persisted — only the counters.
"""

import os
import sqlite3
from pathlib import Path

_DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent))
DB_PATH = Path(_DATA_DIR) / "media_usage.db"

IMAGE_TYPE = "image"
AUDIO_TYPE = "audio"


def _get_connection() -> sqlite3.Connection:
    """Get a connection to the media usage database, creating the table."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS media_usage (
            user_id INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, media_type, day)
        )
        """
    )
    conn.commit()
    return conn


def get_usage(user_id: int, media_type: str, day: str) -> int:
    """Return how many items of a media type the user processed on a day."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT count FROM media_usage"
            " WHERE user_id = ? AND media_type = ? AND day = ?",
            (user_id, media_type, day),
        ).fetchone()
    return row[0] if row else 0


def try_increment(user_id: int, media_type: str, day: str, limit: int) -> bool:
    """Atomically reserve one unit when the user is under ``limit``.

    Single SQLite statement — the check and the increment cannot race, so
    two concurrent requests from the same user can never both exceed the
    cap. Returns True (and increments) when the user still has quota today,
    or False when the cap is reached. A non-positive limit disables the cap
    (always True, nothing counted), mirroring the old ``is_within_limit``.
    """
    if limit <= 0:
        return True
    with _get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO media_usage (user_id, media_type, day, count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, media_type, day) DO UPDATE SET
                count = media_usage.count + 1
            WHERE media_usage.count < ?
            """,
            (user_id, media_type, day, limit),
        )
        conn.commit()
        return cur.rowcount > 0


def refund_usage(user_id: int, media_type: str, day: str) -> None:
    """Give back one reserved unit after a failed operation (floor at 0).

    Callers reserve a slot with ``try_increment`` before the expensive
    media operation; when that operation fails, the slot is refunded so
    failed attempts never burn the daily cap.
    """
    with _get_connection() as conn:
        conn.execute(
            """
            UPDATE media_usage SET count = MAX(count - 1, 0)
            WHERE user_id = ? AND media_type = ? AND day = ?
            """,
            (user_id, media_type, day),
        )
        conn.commit()


def remaining(user_id: int, media_type: str, day: str, limit: int) -> int:
    """Return how many items the user may still process today.

    A non-positive limit means unlimited (cap disabled).
    """
    if limit <= 0:
        return -1
    used = get_usage(user_id, media_type, day)
    return max(0, limit - used)
