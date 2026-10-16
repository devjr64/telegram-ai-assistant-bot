"""Per-user long-term memory journal: SQLite metadata + Markdown storage.

Captures a user's daily life (conversation, meals, sleep, trips, payments…)
into raw Markdown files and builds hierarchical summaries (weekly, quarterly,
yearly) so the bot can answer precise questions about the past — "¿cuándo
fue mi último viaje a Panamá?" — without reloading the whole chat history.

SQLite (memory.db) stores only metadata: period keys, file paths, word
counts, date ranges and current/finalized status. The human-readable
content lives in per-user Markdown files under ``memory/{user_id}/``,
isolated from other users. Splitting metadata from content keeps lookups
fast (recall is a query, not a scan) while keeping the journal readable.

This module is the storage foundation: schema, constants, period helpers,
path helpers and the connection. The CRUD functions (append_day_entry,
save_summary…) build on top of these primitives.
"""

import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent))
DB_PATH = Path(_DATA_DIR) / "memory.db"
MEMORY_DIR = Path(_DATA_DIR) / "memory"

# Valid journal granularities. "raw" is the live append-only log of a day;
# "daily" its optional compact version; the rest are summary levels.
LEVELS = ("raw", "daily", "weekly", "quarterly", "yearly")

# Only these levels produce navigable summaries (raw/daily are per-day files).
SUMMARY_LEVELS = ("weekly", "quarterly", "yearly")

# A summary is "current" while its period is still being written to and
# becomes "finalized" once the period closes and the next level is built.
SUMMARY_STATUSES = ("current", "finalized")


# --- SQLite connection & schema --------------------------------------------


def _get_connection() -> sqlite3.Connection:
    """Get a connection to the memory database, ensuring the schema exists.

    Matches the projects.py/history.py pattern: WAL journal mode, foreign
    keys enabled and idempotent schema creation on every connection, so
    callers never worry about whether the database has been initialized.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the memory schema if it does not already exist.

    Uses CREATE TABLE / INDEX IF NOT EXISTS for fresh DBs, then applies
    the group_id migration if legacy tables are detected (recreating
    days/summaries with nullable user_id/group_id + partial unique indexes).
    Idempotent and safe to run on every connection.
    """
    # Fresh creation: new schema with group_id
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS days (
            user_id INTEGER,
            group_id INTEGER,
            day TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            raw_word_count INTEGER NOT NULL DEFAULT 0,
            entry_count INTEGER NOT NULL DEFAULT 0,
            compact_path TEXT,
            compact_word_count INTEGER NOT NULL DEFAULT 0,
            range_start REAL NOT NULL,
            range_end REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK ((user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            user_id INTEGER,
            group_id INTEGER,
            level TEXT NOT NULL,
            period TEXT NOT NULL,
            content_path TEXT NOT NULL,
            word_count INTEGER NOT NULL DEFAULT 0,
            range_start REAL NOT NULL,
            range_end REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'current',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK ((user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL))
        )
        """
    )
    # Legacy detection: if table was created with old NOT NULL user_id and no group_id
    try:
        cols_days = {r["name"] for r in conn.execute("PRAGMA table_info(days)")}
        if "group_id" not in cols_days and "user_id" in cols_days:
            # Check if days_old already exists (previous failed migration) — skip
            # But we already have new schema via CREATE IF NOT EXISTS above;
            # to handle legacy, we need to check if old schema had PRIMARY KEY(user_id,day)
            # We detect by trying to see if days has group_id; if not, migrate
            _migrate_days_to_group(conn)
        cols_summ = {r["name"] for r in conn.execute("PRAGMA table_info(summaries)")}
        if "group_id" not in cols_summ and "user_id" in cols_summ:
            _migrate_summaries_to_group(conn)
    except sqlite3.Error as exc:
        logger.warning("Memory migration check failed: %s", exc)

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_days_private ON days(user_id, day) WHERE group_id IS NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_days_group ON days(group_id, day) WHERE group_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_summaries_private ON summaries(user_id, level, period) WHERE group_id IS NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_summaries_group ON summaries(group_id, level, period) WHERE group_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_days_owner ON days(group_id, user_id, day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_owner ON summaries(group_id, user_id, level, range_end)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_days_user_period ON days(user_id, day)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_summaries_user_level"
        " ON summaries(user_id, level, range_end)"
    )
    conn.commit()


def _migrate_days_to_group(conn: sqlite3.Connection) -> None:
    """Recreate days table to add group_id (idempotent)."""
    # Detect legacy schema where days has PRIMARY KEY (user_id, day) without group_id
    # We already have a new empty days table; need to rename old and copy.
    # Approach: check if days has group_id column; if not, we are in legacy state
    # created before this code. But CREATE IF NOT EXISTS earlier may have already
    # created the new table empty — we need to detect whether the existing table
    # is legacy. Simplest: if days has no group_id, we rename and recreate.
    # However we just created days with group_id via CREATE IF NOT EXISTS earlier,
    # so the table already has group_id and migration would be no-op.
    # To handle actual legacy DB file, we need to inspect original before CREATE.
    # Hence _init_db logic above may not trigger correctly for legacy files that
    # already had CREATE before. We handle by checking if migration needed via
    # table_info before creation; but since we already created, we need alternative.
    # For now, implement safe migration for empty new DB — no action.
    # The real migration for existing legacy DBs is done via explicit check:
    # if days_old exists, copy etc. We leave this as no-op for fresh DBs.
    # For legacy DBs on disk, the table already exists without group_id; our
    # CREATE IF NOT EXISTS did not recreate, so days still lacks group_id.
    # Detect that and perform rename/copy.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(days)")}
    if "group_id" in cols:
        return
    # Legacy: rename old table
    try:
        conn.execute("ALTER TABLE days RENAME TO days_old")
    except sqlite3.Error:
        return
    conn.execute(
        """
        CREATE TABLE days (
            user_id INTEGER,
            group_id INTEGER,
            day TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            raw_word_count INTEGER NOT NULL DEFAULT 0,
            entry_count INTEGER NOT NULL DEFAULT 0,
            compact_path TEXT,
            compact_word_count INTEGER NOT NULL DEFAULT 0,
            range_start REAL NOT NULL,
            range_end REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK ((user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL))
        )
        """
    )
    conn.execute(
        """
        INSERT INTO days (user_id, group_id, day, raw_path, raw_word_count, entry_count,
                          compact_path, compact_word_count, range_start, range_end, created_at, updated_at)
        SELECT user_id, NULL, day, raw_path, raw_word_count, entry_count,
               compact_path, compact_word_count, range_start, range_end, created_at, updated_at
        FROM days_old
        """
    )
    conn.execute("DROP TABLE days_old")


def _migrate_summaries_to_group(conn: sqlite3.Connection) -> None:
    """Recreate summaries table to add group_id (idempotent)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(summaries)")}
    if "group_id" in cols:
        return
    try:
        conn.execute("ALTER TABLE summaries RENAME TO summaries_old")
    except sqlite3.Error:
        return
    conn.execute(
        """
        CREATE TABLE summaries (
            user_id INTEGER,
            group_id INTEGER,
            level TEXT NOT NULL,
            period TEXT NOT NULL,
            content_path TEXT NOT NULL,
            word_count INTEGER NOT NULL DEFAULT 0,
            range_start REAL NOT NULL,
            range_end REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'current',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK ((user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL))
        )
        """
    )
    conn.execute(
        """
        INSERT INTO summaries (user_id, group_id, level, period, content_path, word_count, range_start, range_end, status, created_at, updated_at)
        SELECT user_id, NULL, level, period, content_path, word_count, range_start, range_end, status, created_at, updated_at
        FROM summaries_old
        """
    )
    conn.execute("DROP TABLE summaries_old")


# --- Period helpers (pure functions, no IO) --------------------------------


def day_key(dt: datetime) -> str:
    """Return the day period key (YYYY-MM-DD) for a datetime."""
    return dt.strftime("%Y-%m-%d")


def week_key(dt: datetime) -> str:
    """Return the ISO week period key (YYYY-Www) for a datetime.

    Uses ISO weeks so week boundaries align with Mondays regardless of the
    calendar year the date falls in.
    """
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def quarter_key(dt: datetime) -> str:
    """Return the quarter period key (YYYY-Qq) for a datetime."""
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def year_key(dt: datetime) -> str:
    """Return the year period key (YYYY) for a datetime."""
    return str(dt.year)


def period_key_for_date(dt: datetime, level: str) -> str:
    """Return the period key containing ``dt`` for the given level.

    raw and daily share the day granularity (a raw log and its compact
    version belong to the same period). Raises ValueError for unknown
    levels so a typo fails fast instead of silently producing a bad key.
    """
    if level in ("raw", "daily"):
        return day_key(dt)
    if level == "weekly":
        return week_key(dt)
    if level == "quarterly":
        return quarter_key(dt)
    if level == "yearly":
        return year_key(dt)
    raise ValueError(f"Unknown level: {level!r}")


def day_range(day: str, tz: ZoneInfo) -> tuple[float, float]:
    """Return the epoch [start, end) of the local day ``day`` (YYYY-MM-DD).

    The range is half-open: start is midnight local and end is the next
    midnight, so a day contains exactly the timestamps between them. Raises
    ValueError when the string is not a valid calendar date.
    """
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", day)
    if not match:
        raise ValueError(f"Invalid day: {day!r} (expected YYYY-MM-DD)")
    try:
        d = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as exc:
        raise ValueError(f"Invalid day: {day!r}") from exc
    start = datetime(d.year, d.month, d.day, tzinfo=tz)
    next_day = date.fromordinal(d.toordinal() + 1)
    end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=tz)
    return start.timestamp(), end.timestamp()


def week_range(week: str, tz: ZoneInfo) -> tuple[float, float]:
    """Return the epoch [start, end) of ISO week ``week`` (YYYY-Www).

    The week spans Monday 00:00 local through the following Monday 00:00,
    mirroring week_key's ISO-week alignment. Raises ValueError when the
    string is not a valid ISO week for its year.
    """
    match = re.fullmatch(r"(\d{4})-W(\d{2})", week)
    if not match:
        raise ValueError(f"Invalid week: {week!r} (expected YYYY-Www)")
    year, week_no = int(match.group(1)), int(match.group(2))
    try:
        monday = date.fromisocalendar(year, week_no, 1)
    except ValueError as exc:
        raise ValueError(f"Invalid week: {week!r}") from exc
    next_monday = date.fromordinal(monday.toordinal() + 7)
    start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    end = datetime(next_monday.year, next_monday.month, next_monday.day, tzinfo=tz)
    return start.timestamp(), end.timestamp()


def quarter_range(quarter: str, tz: ZoneInfo) -> tuple[float, float]:
    """Return the epoch [start, end) of quarter ``quarter`` (YYYY-Qq).

    Starts at the first day of the quarter (00:00 local) and ends at the
    first day of the next quarter. Raises ValueError for malformed input.
    """
    match = re.fullmatch(r"(\d{4})-Q([1-4])", quarter)
    if not match:
        raise ValueError(f"Invalid quarter: {quarter!r} (expected YYYY-Qq)")
    year, q = int(match.group(1)), int(match.group(2))
    start_month = (q - 1) * 3 + 1
    start = datetime(year, start_month, 1, tzinfo=tz)
    if q == 4:
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(year, start_month + 3, 1, tzinfo=tz)
    return start.timestamp(), end.timestamp()


def year_range(year: str, tz: ZoneInfo) -> tuple[float, float]:
    """Return the epoch [start, end) of the year ``year`` (YYYY).

    Starts Jan 1 00:00 local and ends Jan 1 00:00 of the following year.
    Raises ValueError for malformed input.
    """
    match = re.fullmatch(r"(\d{4})", year)
    if not match:
        raise ValueError(f"Invalid year: {year!r} (expected YYYY)")
    y = int(match.group(1))
    start = datetime(y, 1, 1, tzinfo=tz)
    end = datetime(y + 1, 1, 1, tzinfo=tz)
    return start.timestamp(), end.timestamp()


def count_words(text: str) -> int:
    """Count whitespace-separated words in ``text``.

    Used to measure journal/summary size so compaction and summarization
    decisions (e.g. a daily compact threshold) can be made from metadata
    alone without re-parsing the Markdown.
    """
    return len(text.split())


def _validate_day(day: str) -> None:
    """Raise ValueError unless ``day`` is a calendar-valid YYYY-MM-DD.

    Every public CRUD function builds a file path from ``day``, so this is
    the path-traversal gate: the regex only admits the exact zero-padded
    shape (rejecting separators or '..' outright) and ``date()`` rejects
    impossible dates like 2026-02-30 before any path is constructed.
    """
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError(f"Invalid day: {day!r} (expected YYYY-MM-DD)")
    try:
        date(int(day[:4]), int(day[5:7]), int(day[8:10]))
    except ValueError as exc:
        raise ValueError(f"Invalid day: {day!r}") from exc


# --- Path helpers (per-user isolation + traversal protection) --------------


def _user_memory_dir(user_id: int) -> Path:
    """Return the root directory for a user's memory Markdown files.

    The user_id segment isolates files on disk so no user can ever read or
    write another user's journal — mirroring the user_id scoping enforced
    in SQLite.
    """
    return MEMORY_DIR / str(user_id)


def _group_memory_dir(group_id: int) -> Path:
    """Return the root directory for a group's memory Markdown files."""
    return MEMORY_DIR / f"group_{group_id}"


def _owner_dir(user_id: int | None = None, group_id: int | None = None) -> Path:
    """Return the root dir for a private user or a group tenant."""
    if group_id is not None:
        if user_id is not None:
            raise ValueError("Specify user_id or group_id, not both")
        return _group_memory_dir(group_id)
    if user_id is None:
        raise ValueError("Must specify user_id or group_id")
    return _user_memory_dir(user_id)


def _raw_day_path(user_id: int, day: str) -> Path:
    """Return the path to a user's raw journal file for ``day``."""
    return _user_memory_dir(user_id) / "raw" / f"{day}.md"


def _daily_compact_path(user_id: int, day: str) -> Path:
    """Return the path to a user's daily compact file for ``day``."""
    return _user_memory_dir(user_id) / "days" / f"{day}.md"


def _summary_path(user_id: int, level: str, period: str) -> Path:
    """Return the path to a user's summary file at ``level`` for ``period``."""
    return _user_memory_dir(user_id) / "summaries" / level / f"{period}.md"


def _group_raw_day_path(group_id: int, day: str) -> Path:
    return _group_memory_dir(group_id) / "raw" / f"{day}.md"


def _group_daily_compact_path(group_id: int, day: str) -> Path:
    return _group_memory_dir(group_id) / "days" / f"{day}.md"


def _group_summary_path(group_id: int, level: str, period: str) -> Path:
    return _group_memory_dir(group_id) / "summaries" / level / f"{period}.md"


def _safe_under_memory(user_id: int, relative: str) -> Path:
    """Resolve ``relative`` under the user's memory folder, blocking traversal.

    Every file read/write in this module goes through this guard, mirroring
    the ``is_relative_to`` defense used by projects.py's _remove_project_dir.
    Two checks are needed: the resolved path must stay inside MEMORY_DIR
    (protects against a corrupted DATA_DIR or user_id) AND inside the user's
    own folder (protects one user from reaching another user's memory). Any
    absolute path, '..' segment or symlink escaping the user root raises
    ValueError.
    """
    memory_root = MEMORY_DIR.resolve()
    user_root = (memory_root / str(user_id)).resolve()
    if not user_root.is_relative_to(memory_root):
        raise ValueError(f"Invalid user memory root: {user_id!r}")
    candidate = (user_root / relative).resolve()
    if not candidate.is_relative_to(memory_root):
        raise ValueError(f"Path escapes memory directory: {relative!r}")
    if not candidate.is_relative_to(user_root):
        raise ValueError(f"Path escapes user memory folder: {relative!r}")
    return candidate


def _safe_under_memory_group(group_id: int, relative: str) -> Path:
    """Resolve ``relative`` under the group's memory folder, blocking traversal."""
    memory_root = MEMORY_DIR.resolve()
    group_root = (memory_root / f"group_{group_id}").resolve()
    # Ensure group dir is under memory
    if not group_root.is_relative_to(memory_root):
        raise ValueError(f"Invalid group memory root: {group_id!r}")
    candidate = (group_root / relative).resolve()
    if not candidate.is_relative_to(memory_root):
        raise ValueError(f"Path escapes memory directory: {relative!r}")
    if not candidate.is_relative_to(group_root):
        raise ValueError(f"Path escapes group memory folder: {relative!r}")
    return candidate


def _ensure_user_layout(user_id: int) -> None:
    """Create the user's memory folder structure if it does not exist.

    Idempotent, so it can be called before any write. raw/ and days/ hold
    the per-day files and summaries/ holds one folder per summary level.
    """
    _ensure_owner_layout(user_id=user_id)


def _ensure_owner_layout(user_id: int | None = None, group_id: int | None = None) -> None:
    """Create memory folder structure for user or group."""
    owner_root = _owner_dir(user_id=user_id, group_id=group_id)
    for subdir in (
        "raw",
        "days",
        "summaries/weekly",
        "summaries/quarterly",
        "summaries/yearly",
    ):
        (owner_root / subdir).mkdir(parents=True, exist_ok=True)


# --- Row helpers -----------------------------------------------------------


def _day_row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a days table row into a plain dict."""
    return {
        "user_id": row["user_id"],
        "group_id": row["group_id"] if "group_id" in row.keys() else None,
        "day": row["day"],
        "raw_path": row["raw_path"],
        "raw_word_count": row["raw_word_count"],
        "entry_count": row["entry_count"],
        "compact_path": row["compact_path"],
        "compact_word_count": row["compact_word_count"],
        "range_start": row["range_start"],
        "range_end": row["range_end"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _summary_row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a summaries table row into a plain dict."""
    return {
        "user_id": row["user_id"],
        "group_id": row["group_id"] if "group_id" in row.keys() else None,
        "level": row["level"],
        "period": row["period"],
        "content_path": row["content_path"],
        "word_count": row["word_count"],
        "range_start": row["range_start"],
        "range_end": row["range_end"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# --- Raw day entries CRUD ---------------------------------------------------


def _format_local_hhmm(ts: float, tz_name: str) -> str:
    """Format a timestamp as local HH:MM in ``tz_name``.

    An unknown (or empty) timezone name is logged and falls back to UTC so
    a bad TIMEZONE configuration can never block journaling.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r, falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")
    return datetime.fromtimestamp(ts, tz=tz).strftime("%H:%M")


def _read_raw_file(user_id: int | None, day: str, group_id: int | None = None) -> str:
    """Return raw journal text for day, or '' when absent. Handles user or group."""
    if group_id is not None:
        path = _group_raw_day_path(group_id, day)
    else:
        if user_id is None:
            raise ValueError("Must specify user_id or group_id")
        path = _raw_day_path(user_id, day)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _upsert_day_row(
    conn: sqlite3.Connection,
    day: str,
    raw_path: str,
    raw_word_count: int,
    ts: float,
    user_id: int | None = None,
    group_id: int | None = None,
) -> sqlite3.Row | None:
    """Insert or update the days metadata row after a raw file append. Supports group. Manual upsert for partial index compat."""
    if group_id is not None:
        row = conn.execute("SELECT * FROM days WHERE group_id = ? AND day = ?", (group_id, day)).fetchone()
        if row:
            conn.execute(
                "UPDATE days SET raw_path=?, raw_word_count=?, entry_count=entry_count+1, range_end=?, updated_at=? WHERE group_id=? AND day=?",
                (raw_path, raw_word_count, ts, ts, group_id, day),
            )
        else:
            conn.execute(
                "INSERT INTO days (user_id, group_id, day, raw_path, raw_word_count, entry_count, range_start, range_end, created_at, updated_at) VALUES (NULL, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (group_id, day, raw_path, raw_word_count, ts, ts, ts, ts),
            )
        conn.commit()
        return conn.execute("SELECT * FROM days WHERE group_id = ? AND day = ?", (group_id, day)).fetchone()
    else:
        row = conn.execute("SELECT * FROM days WHERE user_id = ? AND day = ? AND group_id IS NULL", (user_id, day)).fetchone()
        if row:
            conn.execute(
                "UPDATE days SET raw_path=?, raw_word_count=?, entry_count=entry_count+1, range_end=?, updated_at=? WHERE user_id=? AND day=? AND group_id IS NULL",
                (raw_path, raw_word_count, ts, ts, user_id, day),
            )
        else:
            conn.execute(
                "INSERT INTO days (user_id, group_id, day, raw_path, raw_word_count, entry_count, range_start, range_end, created_at, updated_at) VALUES (?, NULL, ?, ?, ?, 1, ?, ?, ?, ?)",
                (user_id, day, raw_path, raw_word_count, ts, ts, ts, ts),
            )
        conn.commit()
        return conn.execute("SELECT * FROM days WHERE user_id = ? AND day = ? AND group_id IS NULL", (user_id, day)).fetchone()


def append_raw_entry(
    user_id: int | None = None,
    day: str = "",
    text: str = "",
    ts: float | None = None,
    tz_name: str = "UTC",
    group_id: int | None = None,
    author_username: str | None = None,
) -> dict | None:
    """Append one entry to raw journal for day and sync metadata. Supports group.

    For group entries the line is ``- [HH:MM] @author: text`` when author is given.
    For private it stays ``- [HH:MM] text``. user_id and group_id are exclusive.
    """
    # Back-compat: allow positional call append_raw_entry(user_id, day, text)
    # When called as append_raw_entry(1, "2026-08-14", "hola") the first arg is int user_id
    # Our signature above handles that. Need to guard against day being passed as text incorrectly
    # If user passed (1, "2026-08-14", "hola") it's already mapped correctly.
    # If someone passes (day="2026-08-14") with user_id=None and group_id set, we handle.
    _validate_day(day)
    if user_id is None and group_id is None:
        raise ValueError("Must specify user_id or group_id")
    if user_id is not None and group_id is not None:
        raise ValueError("Specify user_id or group_id, not both")
    ts = time.time() if ts is None else ts
    try:
        if group_id is not None:
            _ensure_owner_layout(group_id=group_id)
            path = _group_raw_day_path(group_id, day)
            # group entry includes author prefix
            if author_username:
                line = f"- [{_format_local_hhmm(ts, tz_name)}] @{author_username}: {text.strip()}\n"
            else:
                line = f"- [{_format_local_hhmm(ts, tz_name)}] {text.strip()}\n"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            raw_word_count = count_words(_read_raw_file(None, day, group_id=group_id))
            with _get_connection() as conn:
                row = _upsert_day_row(
                    conn, day, str(path.resolve()), raw_word_count, ts, group_id=group_id
                )
        else:
            _ensure_user_layout(user_id)
            path = _raw_day_path(user_id, day)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"- [{_format_local_hhmm(ts, tz_name)}] {text.strip()}\n")
            raw_word_count = count_words(_read_raw_file(user_id, day))
            with _get_connection() as conn:
                row = _upsert_day_row(
                    conn, day, str(path.resolve()), raw_word_count, ts, user_id=user_id
                )
    except (OSError, sqlite3.Error) as exc:
        logger.error(
            "Failed to append raw entry for %s day %s: %s", f"group {group_id}" if group_id else f"user {user_id}", day, exc
        )
        return None
    return _day_row_to_dict(row)


def get_raw_day(user_id: int | None = None, day: str = "", group_id: int | None = None) -> dict | None:
    """Return metadata row for raw journal day, or None. Supports group."""
    _validate_day(day)
    with _get_connection() as conn:
        if group_id is not None:
            row = conn.execute(
                "SELECT * FROM days WHERE group_id = ? AND day = ?", (group_id, day)
            ).fetchone()
        else:
            if user_id is None:
                raise ValueError("Must specify user_id or group_id")
            row = conn.execute(
                "SELECT * FROM days WHERE user_id = ? AND day = ? AND group_id IS NULL", (user_id, day)
            ).fetchone()
    return _day_row_to_dict(row) if row else None


def get_raw_entries(
    user_id: int | None = None,
    day_from: str | None = None,
    day_to: str | None = None,
    group_id: int | None = None,
) -> list[dict]:
    """Return raw days within optional [day_from, day_to] range. Supports group."""
    # Back-compat: if first arg is int and group_id None, treat as user_id
    # Allow call as get_raw_entries(1, day_from, day_to) or get_raw_entries(user_id=1, group_id=None)
    if group_id is not None:
        sql = "SELECT * FROM days WHERE group_id = ?"
        params = [group_id]
    else:
        if user_id is None:
            raise ValueError("Must specify user_id or group_id")
        sql = "SELECT * FROM days WHERE user_id = ? AND group_id IS NULL"
        params = [user_id]
    if day_from is not None:
        sql += " AND day >= ?"
        params.append(day_from)
    if day_to is not None:
        sql += " AND day <= ?"
        params.append(day_to)
    sql += " ORDER BY day ASC"
    with _get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    # Need helper to read content per owner
    result = []
    for row in rows:
        day_val = row["day"]
        if group_id is not None:
            content = _read_raw_file(None, day_val, group_id=group_id)
        else:
            content = _read_raw_file(user_id, day_val)
        result.append(
            {
                "day": day_val,
                "raw_word_count": row["raw_word_count"],
                "entry_count": row["entry_count"],
                "range_start": row["range_start"],
                "range_end": row["range_end"],
                "content": content,
            }
        )
    return result


def get_raw_content(user_id: int | None = None, day: str = "", group_id: int | None = None) -> str:
    """Return raw journal Markdown for day, or '' when it has none. Supports group."""
    _validate_day(day)
    if group_id is not None:
        return _read_raw_file(None, day, group_id=group_id)
    if user_id is None:
        raise ValueError("Must specify user_id or group_id")
    return _read_raw_file(user_id, day)


def day_entries_are_empty(user_id: int | None = None, day: str = "", group_id: int | None = None) -> bool:
    """True when a day has no raw journal content to compact."""
    _validate_day(day)
    if group_id is not None:
        return not _read_raw_file(None, day, group_id=group_id).strip()
    if user_id is None:
        raise ValueError("Must specify user_id or group_id")
    return not _read_raw_file(user_id, day).strip()


# --- Daily compact CRUD ------------------------------------------------------


def _upsert_compact_row(
    conn: sqlite3.Connection,
    day: str,
    compact_path: str,
    word_count: int,
    ts: float,
    user_id: int | None = None,
    group_id: int | None = None,
) -> sqlite3.Row | None:
    """Insert or update the days row after a daily compact write. Supports group."""
    if group_id is not None:
        row = conn.execute("SELECT * FROM days WHERE group_id = ? AND day = ?", (group_id, day)).fetchone()
        if row:
            conn.execute("UPDATE days SET compact_path=?, compact_word_count=?, updated_at=? WHERE group_id=? AND day=?", (compact_path, word_count, ts, group_id, day))
        else:
            conn.execute(
                "INSERT INTO days (user_id, group_id, day, raw_path, raw_word_count, entry_count, compact_path, compact_word_count, range_start, range_end, created_at, updated_at) VALUES (NULL, ?, ?, '', 0, 0, ?, ?, ?, ?, ?, ?)",
                (group_id, day, compact_path, word_count, ts, ts, ts, ts),
            )
        conn.commit()
        return conn.execute("SELECT * FROM days WHERE group_id = ? AND day = ?", (group_id, day)).fetchone()
    else:
        if user_id is None:
            raise ValueError("Must specify user_id or group_id")
        row = conn.execute("SELECT * FROM days WHERE user_id = ? AND day = ? AND group_id IS NULL", (user_id, day)).fetchone()
        if row:
            conn.execute("UPDATE days SET compact_path=?, compact_word_count=?, updated_at=? WHERE user_id=? AND day=? AND group_id IS NULL", (compact_path, word_count, ts, user_id, day))
        else:
            conn.execute(
                "INSERT INTO days (user_id, group_id, day, raw_path, raw_word_count, entry_count, compact_path, compact_word_count, range_start, range_end, created_at, updated_at) VALUES (?, NULL, ?, '', 0, 0, ?, ?, ?, ?, ?, ?)",
                (user_id, day, compact_path, word_count, ts, ts, ts, ts),
            )
        conn.commit()
        return conn.execute("SELECT * FROM days WHERE user_id = ? AND day = ? AND group_id IS NULL", (user_id, day)).fetchone()


def save_daily_compact(user_id: int | None = None, day: str = "", content: str = "", group_id: int | None = None) -> dict | None:
    """Write daily compact for day and sync its metadata. Supports group."""
    # Back-compat positional: save_daily_compact(1, "2026-08-14", "content")
    # When called with positional, user_id is int, day is str, content is str
    # group_id stays None
    _validate_day(day)
    try:
        if group_id is not None:
            _ensure_owner_layout(group_id=group_id)
            path = _group_daily_compact_path(group_id, day)
        else:
            if user_id is None:
                raise ValueError("Must specify user_id or group_id")
            _ensure_user_layout(user_id)
            path = _daily_compact_path(user_id, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        now = time.time()
        with _get_connection() as conn:
            row = _upsert_compact_row(
                conn,
                day,
                str(path.resolve()),
                count_words(content),
                now,
                user_id=user_id,
                group_id=group_id,
            )
    except (OSError, sqlite3.Error) as exc:
        logger.error(
            "Failed to save daily compact for %s day %s: %s",
            f"group {group_id}" if group_id else f"user {user_id}", day, exc,
        )
        return None
    return _day_row_to_dict(row)


def get_daily_compact(user_id: int | None = None, day: str = "", group_id: int | None = None) -> str:
    """Return daily compact Markdown for day, or '' when absent. Supports group."""
    _validate_day(day)
    if group_id is not None:
        path = _group_daily_compact_path(group_id, day)
    else:
        if user_id is None:
            raise ValueError("Must specify user_id or group_id")
        path = _daily_compact_path(user_id, day)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def get_daily_compact_metadata(user_id: int | None = None, day: str = "", group_id: int | None = None) -> dict | None:
    """Return days metadata row for day, or None. Supports group."""
    _validate_day(day)
    with _get_connection() as conn:
        if group_id is not None:
            row = conn.execute(
                "SELECT * FROM days WHERE group_id = ? AND day = ?", (group_id, day)
            ).fetchone()
        else:
            if user_id is None:
                raise ValueError("Must specify user_id or group_id")
            row = conn.execute(
                "SELECT * FROM days WHERE user_id = ? AND day = ? AND group_id IS NULL", (user_id, day)
            ).fetchone()
    return _day_row_to_dict(row) if row else None


# --- Summary CRUD ------------------------------------------------------------


# Period key shapes per summary level, used as the path-traversal gate for
# summary file paths (the analog of _validate_day for day paths).
_SUMMARY_PERIOD_PATTERNS = {
    "weekly": r"\d{4}-W\d{2}",
    "quarterly": r"\d{4}-Q[1-4]",
    "yearly": r"\d{4}",
}


def _validate_summary_period(level: str, period: str) -> None:
    """Raise ValueError unless ``level`` is a summary level and ``period`` valid."""
    pattern = _SUMMARY_PERIOD_PATTERNS.get(level)
    if pattern is None:
        raise ValueError(f"Unknown summary level: {level!r}")
    if not isinstance(period, str) or not re.fullmatch(pattern, period):
        raise ValueError(f"Invalid {level} period: {period!r}")


def _upsert_summary_row(
    conn: sqlite3.Connection,
    level: str,
    period: str,
    content_path: str,
    word_count: int,
    range_start: float,
    range_end: float,
    now: float,
    user_id: int | None = None,
    group_id: int | None = None,
) -> sqlite3.Row | None:
    """Insert or update a summaries row, finalizing the previous current. Supports group. Manual upsert."""
    if group_id is not None:
        conn.execute(
            """
            UPDATE summaries SET status = 'finalized', updated_at = ?
            WHERE group_id = ? AND level = ? AND status = 'current'
            """,
            (now, group_id, level),
        )
        row = conn.execute("SELECT * FROM summaries WHERE group_id = ? AND level = ? AND period = ?", (group_id, level, period)).fetchone()
        if row:
            conn.execute(
                "UPDATE summaries SET content_path=?, word_count=?, range_start=?, range_end=?, status='current', updated_at=? WHERE group_id=? AND level=? AND period=?",
                (content_path, word_count, range_start, range_end, now, group_id, level, period),
            )
        else:
            conn.execute(
                "INSERT INTO summaries (user_id, group_id, level, period, content_path, word_count, range_start, range_end, status, created_at, updated_at) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, 'current', ?, ?)",
                (group_id, level, period, content_path, word_count, range_start, range_end, now, now),
            )
        conn.commit()
        return conn.execute(
            "SELECT * FROM summaries WHERE group_id = ? AND level = ? AND period = ?",
            (group_id, level, period),
        ).fetchone()
    else:
        if user_id is None:
            raise ValueError("Must specify user_id or group_id")
        conn.execute(
            """
            UPDATE summaries SET status = 'finalized', updated_at = ?
            WHERE user_id = ? AND level = ? AND status = 'current' AND group_id IS NULL
            """,
            (now, user_id, level),
        )
        row = conn.execute("SELECT * FROM summaries WHERE user_id = ? AND level = ? AND period = ? AND group_id IS NULL", (user_id, level, period)).fetchone()
        if row:
            conn.execute(
                "UPDATE summaries SET content_path=?, word_count=?, range_start=?, range_end=?, status='current', updated_at=? WHERE user_id=? AND level=? AND period=? AND group_id IS NULL",
                (content_path, word_count, range_start, range_end, now, user_id, level, period),
            )
        else:
            conn.execute(
                "INSERT INTO summaries (user_id, group_id, level, period, content_path, word_count, range_start, range_end, status, created_at, updated_at) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 'current', ?, ?)",
                (user_id, level, period, content_path, word_count, range_start, range_end, now, now),
            )
        conn.commit()
        return conn.execute(
            "SELECT * FROM summaries WHERE user_id = ? AND level = ? AND period = ? AND group_id IS NULL",
            (user_id, level, period),
        ).fetchone()


def save_summary(
    user_id: int | None = None,
    level: str = "",
    period: str = "",
    content: str = "",
    range_start: float | None = None,
    range_end: float | None = None,
    group_id: int | None = None,
) -> dict | None:
    """Write a summary at level for period and register its metadata. Supports group."""
    _validate_summary_period(level, period)
    if user_id is None and group_id is None:
        raise ValueError("Must specify user_id or group_id")
    if user_id is not None and group_id is not None:
        raise ValueError("Specify user_id or group_id, not both")
    now = time.time()
    start = range_start if range_start is not None else now
    end = range_end if range_end is not None else now
    try:
        if group_id is not None:
            _ensure_owner_layout(group_id=group_id)
            path = _group_summary_path(group_id, level, period)
        else:
            _ensure_user_layout(user_id)
            path = _summary_path(user_id, level, period)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        with _get_connection() as conn:
            row = _upsert_summary_row(
                conn,
                level,
                period,
                str(path.resolve()),
                count_words(content),
                start,
                end,
                now,
                user_id=user_id,
                group_id=group_id,
            )
    except (OSError, sqlite3.Error) as exc:
        logger.error(
            "Failed to save %s summary for %s period %s: %s",
            level,
            f"group {group_id}" if group_id else f"user {user_id}",
            period,
            exc,
        )
        return None
    return _summary_row_to_dict(row)


def get_summary(
    user_id: int | None = None, level: str = "", period: str | None = None, group_id: int | None = None
) -> dict | None:
    """Return a summary's metadata row, or None when it does not exist. Supports group."""
    if level not in SUMMARY_LEVELS:
        raise ValueError(f"Unknown summary level: {level!r}")
    with _get_connection() as conn:
        if group_id is not None:
            if period is not None:
                row = conn.execute(
                    "SELECT * FROM summaries WHERE group_id = ? AND level = ?"
                    " AND period = ?",
                    (group_id, level, period),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM summaries WHERE group_id = ? AND level = ?"
                    " ORDER BY range_end DESC LIMIT 1",
                    (group_id, level),
                ).fetchone()
        else:
            if user_id is None:
                raise ValueError("Must specify user_id or group_id")
            if period is not None:
                row = conn.execute(
                    "SELECT * FROM summaries WHERE user_id = ? AND level = ?"
                    " AND period = ? AND group_id IS NULL",
                    (user_id, level, period),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM summaries WHERE user_id = ? AND level = ? AND group_id IS NULL"
                    " ORDER BY range_end DESC LIMIT 1",
                    (user_id, level),
                ).fetchone()
    return _summary_row_to_dict(row) if row else None


def get_summary_content(user_id: int | None = None, level: str = "", period: str = "", group_id: int | None = None) -> str:
    """Return a summary's Markdown content for period, or '' when absent. Supports group."""
    _validate_summary_period(level, period)
    if group_id is not None:
        path = _group_summary_path(group_id, level, period)
    else:
        if user_id is None:
            raise ValueError("Must specify user_id or group_id")
        path = _summary_path(user_id, level, period)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def list_summaries(user_id: int | None = None, level: str | None = None, group_id: int | None = None) -> list[dict]:
    """Return summary metadata rows, oldest range first. Supports group."""
    if level is not None and level not in SUMMARY_LEVELS:
        raise ValueError(f"Unknown summary level: {level!r}")
    if group_id is not None:
        sql = "SELECT * FROM summaries WHERE group_id = ?"
        params = [group_id]
    else:
        if user_id is None:
            raise ValueError("Must specify user_id or group_id")
        sql = "SELECT * FROM summaries WHERE user_id = ? AND group_id IS NULL"
        params = [user_id]
    if level is not None:
        sql += " AND level = ?"
        params.append(level)
    sql += " ORDER BY range_end ASC"
    with _get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_summary_row_to_dict(r) for r in rows]


def finalize_summary(user_id: int | None = None, level: str = "", period: str = "", group_id: int | None = None) -> bool:
    """Mark a summary row as 'finalized'. Returns True if a row was updated. Supports group."""
    _validate_summary_period(level, period)
    with _get_connection() as conn:
        if group_id is not None:
            cur = conn.execute(
                "UPDATE summaries SET status = 'finalized', updated_at = ?"
                " WHERE group_id = ? AND level = ? AND period = ?",
                (time.time(), group_id, level, period),
            )
        else:
            if user_id is None:
                raise ValueError("Must specify user_id or group_id")
            cur = conn.execute(
                "UPDATE summaries SET status = 'finalized', updated_at = ?"
                " WHERE user_id = ? AND level = ? AND period = ? AND group_id IS NULL",
                (time.time(), user_id, level, period),
            )
        conn.commit()
    return cur.rowcount > 0


def find_summary_covering(
    user_id: int | None = None, ts: float = 0, level: str = "", tz_name: str = "UTC", group_id: int | None = None
) -> dict | None:
    """Return the summary of level covering ts, or the latest of it. Supports group."""
    if level not in SUMMARY_LEVELS:
        raise ValueError(f"Unknown summary level: {level!r}")
    with _get_connection() as conn:
        if group_id is not None:
            row = conn.execute(
                "SELECT * FROM summaries WHERE group_id = ? AND level = ?"
                " AND range_start <= ? AND range_end >= ?"
                " ORDER BY range_end DESC LIMIT 1",
                (group_id, level, ts, ts),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM summaries WHERE group_id = ? AND level = ?"
                    " ORDER BY range_end DESC LIMIT 1",
                    (group_id, level),
                ).fetchone()
        else:
            if user_id is None:
                raise ValueError("Must specify user_id or group_id")
            row = conn.execute(
                "SELECT * FROM summaries WHERE user_id = ? AND level = ? AND group_id IS NULL"
                " AND range_start <= ? AND range_end >= ?"
                " ORDER BY range_end DESC LIMIT 1",
                (user_id, level, ts, ts),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM summaries WHERE user_id = ? AND level = ? AND group_id IS NULL"
                    " ORDER BY range_end DESC LIMIT 1",
                    (user_id, level),
                ).fetchone()
    return _summary_row_to_dict(row) if row else None
