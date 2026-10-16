"""Proactive background engine: morning/evening check-ins and summary jobs.

The bot can reach out to the user on its own instead of only answering
messages: a warm morning check-in (greeting + pending items/patterns), an
evening close ("how was your day?"), and the nightly/weekly/quarterly/yearly
summary generation jobs that keep the memory hierarchy fresh.

Design rules:
- Best-effort everywhere. Proactive messages have no persistent retry queue
  (unlike bot.reminders): a failed delivery is retried a few times in-process
  with exponential backoff and then logged, never blocking the job loop.
- Daily cap per user via the proactive_log table, so a config or scheduling
  bug can never spam the user more than PROACTIVE_MAX_DAILY times a day.
- Time windows (morning/evening) are computed from the configured times and
  act as a guard on top of the job schedule in bot.py.
- Every driver callback swallows and logs exceptions so one user's failure
  (or a transient API error) never crashes the whole job.

Deliberately does NOT import bot.tools or bot.handlers: tools imports the
memory/tools modules and would create an import cycle; ``_chat_model``
mirrors handlers' logic locally, while ``_day_key`` delegates to the
shared bot.timeutil helpers instead.
"""

import asyncio
import logging
import os
import sqlite3
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot import memory as memory_store
from bot import reminders
from bot import summarizer
from bot.ai_client import chat_completion, OpenCodeError, RateLimitError
from bot.config import Settings, load_settings
from bot.timeutil import day_key

logger = logging.getLogger(__name__)

_DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent))
# The proactive log lives in the same database file as reminders/projects:
# both are "bot operational state", and sharing the file keeps backup and
# volume handling identical.
DB_PATH = Path(_DATA_DIR) / "projects.db"

# Delivery is best-effort (see module docstring): up to 3 in-process attempts
# with exponential backoff (10s, 20s), then the message is given up on.
MAX_DELIVERY_ATTEMPTS = 3
RETRY_BASE_DELAY = 10
RETRY_MAX_DELAY = 28800

# Cap used when truncating raw journal content for prompt contexts: keeps the
# model prompt small while preserving enough signal for a check-in message.
_CONTENT_TRUNCATE_CHARS = 500
_WEEK_SUMMARY_TRUNCATE_CHARS = 800

# Catch-up windows for the summary jobs. A server that was down when a period
# closed (e.g. the Monday weekly job or the quarterly/yearly closing jobs) has
# its missed summaries generated on the next run, looking back this far. The
# windows bound the catch-up cost: each missing period costs at most one model
# call (ensure_summary returns None without calling the model when a period
# has no journal content).
COMPACTION_CATCHUP_DAYS = 3
WEEKLY_CATCHUP_WEEKS = 8
QUARTERLY_CATCHUP_QUARTERS = 4
YEARLY_CATCHUP_YEARS = 2


# --- proactive_log: SQLite connection & schema ------------------------------


def _get_connection() -> sqlite3.Connection:
    """Get a connection to the bot database, ensuring the schema exists.

    Mirrors reminders.py exactly: WAL journal mode, row factory and
    idempotent schema creation on every connection. Because the module-level
    DB_PATH is read at call time, tests can monkeypatch bot.proactive.DB_PATH
    to an isolated temp file.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the proactive_log and user_proactive_settings tables.

    proactive_log tracks the daily cap AND which windows already sent today
    (morning_sent_at/evening_sent_at), so a periodic check-in job never
    double-sends a window. user_proactive_settings stores per-user check-in
    times; NULL/absent means "use the global PROACTIVE_* defaults".
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proactive_log (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            last_message TEXT,
            morning_sent_at REAL,
            evening_sent_at REAL,
            PRIMARY KEY (user_id, day)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_proactive_settings (
            user_id INTEGER PRIMARY KEY,
            morning_time TEXT,
            evening_time TEXT,
            updated_at REAL NOT NULL
        )
        """
    )
    # Migration for proactive_log tables created before the sent markers.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(proactive_log)")}
    if "morning_sent_at" not in columns:
        conn.execute(
            "ALTER TABLE proactive_log ADD COLUMN morning_sent_at REAL"
        )
    if "evening_sent_at" not in columns:
        conn.execute(
            "ALTER TABLE proactive_log ADD COLUMN evening_sent_at REAL"
        )
    conn.commit()


def get_daily_count(user_id: int, day: str) -> int:
    """Return how many proactive messages were sent to ``user_id`` on ``day``."""
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT count FROM proactive_log WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
    return row["count"] if row else 0


def increment_daily_count(user_id: int, day: str, text: str) -> int:
    """Record one sent proactive message; return the new count for the day.

    The upsert is atomic (no check-then-insert race): a new day starts at 1
    and an existing day increments, so concurrent jobs can never double-count
    a message. The last message text is kept for debugging/audit.
    """
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO proactive_log (user_id, day, count, last_message)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET
                count = count + 1,
                last_message = excluded.last_message
            """,
            (user_id, day, text),
        )
        conn.commit()
        row = conn.execute(
            "SELECT count FROM proactive_log WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
    return row["count"] if row else 0


def can_send(user_id: int, day: str, max_daily: int) -> bool:
    """True when the user still has proactive messages left for ``day``.

    ``max_daily <= 0`` disables the feature entirely (fail-closed): a
    misconfigured cap can never flood the user.
    """
    return get_daily_count(user_id, day) < max_daily


# --- Per-user check-in settings ----------------------------------------------


def get_checkin_times(user_id: int, settings: Settings) -> tuple[str, str]:
    """Return the user's (morning, evening) check-in times as "HH:MM".

    Falls back to the global PROACTIVE_MORNING_TIME/EVENING_TIME settings
    when the user has no custom times — the per-user values are the
    defaults unless the user changed them from the chat (set_checkin_time).
    """
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT morning_time, evening_time FROM user_proactive_settings"
            " WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return settings.proactive_morning_time, settings.proactive_evening_time
    morning = row["morning_time"] or settings.proactive_morning_time
    evening = row["evening_time"] or settings.proactive_evening_time
    return morning, evening


def set_checkin_time(user_id: int, period: str, hhmm: str) -> dict:
    """Set a user's morning/evening check-in time; returns ``{"ok": ...}``.

    ``period`` must be "morning" or "evening" and ``hhmm`` a valid 24h
    "HH:MM" — anything else returns a Spanish error so the model can relay
    it. The other window's value is preserved; updating just one field of
    the row (never deleting the other). The user_id always comes from the
    Telegram update, never from model args.
    """
    if period not in ("morning", "evening"):
        return {"ok": False, "error": "Periodo inválido: usa 'morning' o 'evening'"}
    if _parse_hhmm(hhmm) is None:
        return {
            "ok": False,
            "error": f"Hora inválida: '{hhmm}'. Usa formato HH:MM (24h), por ejemplo 09:30.",
        }
    column = "morning_time" if period == "morning" else "evening_time"
    with _get_connection() as conn:
        conn.execute(
            f"""
            INSERT INTO user_proactive_settings (user_id, {column}, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                {column} = excluded.{column},
                updated_at = excluded.updated_at
            """,
            (user_id, hhmm, time_module.time()),
        )
        conn.commit()
    logger.info("User %d set %s check-in to %s", user_id, period, hhmm)
    morning, evening = get_checkin_times(user_id, load_settings())
    return {
        "ok": True,
        "period": period,
        "time": hhmm,
        "morning": morning,
        "evening": evening,
    }


def checkin_sent_today(user_id: int, day: str, period: str) -> bool:
    """True when the user already received that window's check-in on ``day``.

    Lets the periodic check-in job send each window at most once per day
    even though the job fires every few minutes: after a successful send
    the window is marked and later fires in the same window are no-ops.
    """
    column = "morning_sent_at" if period == "morning" else "evening_sent_at"
    with _get_connection() as conn:
        row = conn.execute(
            f"SELECT {column} AS sent FROM proactive_log"
            " WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
    return row is not None and row["sent"] is not None


def mark_checkin_sent(user_id: int, day: str, period: str, ts: float | None = None) -> None:
    """Record that the user received ``period``'s check-in on ``day``.

    Idempotent: a window already marked stays marked. The log row is
    created if missing (a window can be sent before any other counting
    happened), and the daily count is left untouched here.
    """
    column = "morning_sent_at" if period == "morning" else "evening_sent_at"
    ts = time_module.time() if ts is None else ts
    with _get_connection() as conn:
        conn.execute(
            f"""
            INSERT INTO proactive_log (user_id, day, count, {column})
            VALUES (?, ?, 0, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET
                {column} = COALESCE({column}, excluded.{column})
            """,
            (user_id, day, ts),
        )
        conn.commit()


# --- Time helpers ------------------------------------------------------------


def now_local(settings: Settings) -> datetime:
    """Return the current datetime in the bot's configured timezone."""
    return datetime.now(ZoneInfo(settings.timezone))


def _day_key(settings: Settings, dt: datetime | None = None) -> str:
    """Return the YYYY-MM-DD period key for ``dt`` (now by default).

    Delegates the formatting to ``bot.timeutil.day_key`` so the logic is
    single-sourced, but keeps the ``now_local(settings)`` indirection for
    the default (tests patch ``now_local`` to freeze "today"). Importing
    bot.timeutil is safe here: it only depends on bot.config, not on the
    tool/handler graph (which would create an import cycle).
    """
    return day_key(dt if dt is not None else now_local(settings))


def _parse_hhmm(value: str) -> time | None:
    """Parse "HH:MM" into a time, or None (and log) when malformed.

    A bad PROACTIVE_MORNING_TIME/EVENING_TIME config or a bad value from the
    set_checkin_time tool must not crash the jobs; returning None disables
    the affected window instead.
    """
    try:
        hour, minute = value.strip().split(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        logger.warning("Invalid proactive time %r; window disabled", value)
        return None


def _add_hours(t: time, hours: int) -> time:
    """Return the time-of-day ``hours`` later, crossing midnight if needed."""
    return (datetime.combine(date.today(), t) + timedelta(hours=hours)).time()


def window_of(
    settings: Settings,
    morning_time: str | None = None,
    evening_time: str | None = None,
) -> str | None:
    """Return "morning"/"evening" when now falls in a proactive window.

    A window is the 3 hours starting at the configured morning/evening time
    (HH:MM, 24h). ``morning_time``/``evening_time`` override the global
    settings defaults so per-user check-in times can be honored; None falls
    back to the Settings value. Returns None outside both windows; morning
    wins if the two windows overlap (misconfiguration).
    """
    now = now_local(settings).time()
    morning = _parse_hhmm(morning_time or settings.proactive_morning_time)
    if morning is not None and morning <= now < _add_hours(morning, 3):
        return "morning"
    evening = _parse_hhmm(evening_time or settings.proactive_evening_time)
    if evening is not None and evening <= now < _add_hours(evening, 3):
        return "evening"
    return None


def _chat_model(settings: Settings) -> str:
    """Return the chat model for the configured provider.

    Mirrors handlers._active_model: the chat provider/model is fixed by
    configuration and the check-ins must use the same model as the
    conversation, so insights and memory stay consistent.
    """
    if settings.ai_provider == "gemini":
        return settings.gemini_model
    return settings.model



def _list_group_ids() -> list[int]:
    """Return distinct group_ids from group_members (best-effort)."""
    try:
        from bot.projects import _get_connection as _get_projects_conn
        with _get_projects_conn() as conn:
            rows = conn.execute("SELECT DISTINCT group_id FROM group_members").fetchall()
            return [r[0] for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to list group_ids for proactive: %s", exc)
        return []


def _authorized_users(settings: Settings) -> list[int]:
    """Return the authorized user IDs sorted, for deterministic iteration."""
    return sorted(settings.authorized_users)


# --- Context builders (read memory; no model calls) -------------------------


def _truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with an ellipsis when needed."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


_SPANISH_WEEKDAYS = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)


def _hoy_line(settings: Settings) -> str:
    """Return 'Hoy es YYYY-MM-DD (weekday) — zona TZ' in user's timezone.

    Best-effort: falls back to UTC and English weekday on bad timezone.
    Pure helper — no IO beyond now_local.
    """
    try:
        now = now_local(settings)
        tz_name = settings.timezone
    except Exception:
        now = datetime.now(ZoneInfo("UTC"))
        tz_name = "UTC"
    # Monday=0 -> Spanish
    weekday = _SPANISH_WEEKDAYS[now.weekday()]
    return f"Hoy es {now.strftime('%Y-%m-%d')} ({weekday}) — zona {tz_name}"


def _fallback_last_entry(user_id: int, settings: Settings) -> str:
    """Return 'Última entrada: YYYY-MM-DD (hace N días): ...' when last 7 days empty.

    Scans back up to 30 days for the last non-empty raw day. Returns '' when
    recent days have content or when nothing is found. Best-effort — never
    raises, truncates content to _CONTENT_TRUNCATE_CHARS.
    """
    try:
        now = now_local(settings)
        # Check if last 7 days have any content — if yes, no fallback needed
        day_from_7 = _day_key(settings, now - timedelta(days=6))
        day_to = _day_key(settings, now)
        has_recent = any(
            e["content"].strip()
            for e in memory_store.get_raw_entries(user_id, day_from_7, day_to)
        )
        if has_recent:
            return ""
        # Scan 7..30 days back
        for offset in range(7, 31):
            day = _day_key(settings, now - timedelta(days=offset))
            content = memory_store.get_raw_content(user_id, day).strip()
            if content:
                truncated = _truncate(content, _CONTENT_TRUNCATE_CHARS)
                # Exact delta in calendar days
                try:
                    entry_date = date.fromisoformat(day)
                    delta = (now.date() - entry_date).days
                except Exception:
                    delta = offset
                return f"Última entrada: {day} (hace {delta} días): {truncated}"
        return ""
    except Exception:
        return ""


def _last_entry_before_today(user_id: int, settings: Settings) -> str:
    """Return last entry before today (up to 30 days) when today is empty.

    Used by evening check-in: if hoy está vacío, el modelo necesita saber
    cuándo fue la última vez que el usuario compartió algo, aunque fuera hace
    1-6 días. Best-effort, never raises.
    """
    try:
        now = now_local(settings)
        for offset in range(1, 31):
            day = _day_key(settings, now - timedelta(days=offset))
            content = memory_store.get_raw_content(user_id, day).strip()
            if content:
                truncated = _truncate(content, _CONTENT_TRUNCATE_CHARS)
                try:
                    entry_date = date.fromisoformat(day)
                    delta = (now.date() - entry_date).days
                except Exception:
                    delta = offset
                return f"Última entrada: {day} (hace {delta} días): {truncated}"
        return ""
    except Exception:
        return ""


def _week_summary_context(user_id: int, settings: Settings) -> str:
    """Return the current (latest) weekly summary text, or '' when absent.

    The current weekly summary is the best compact view of the week's
    patterns for both check-in builders; absent until the weekly job first
    runs, in which case the builders just omit it.
    """
    row = memory_store.get_summary(user_id, "weekly")
    if row is None:
        return ""
    content = memory_store.get_summary_content(
        user_id, "weekly", row["period"]
    ).strip()
    return _truncate(content, _WEEK_SUMMARY_TRUNCATE_CHARS) if content else ""


def _journal_context(
    user_id: int, settings: Settings, days_back: int = 7
) -> str:
    """Build a compact text of the last ``days_back`` raw days + weekly summary.

    Each existing day contributes "YYYY-MM-DD: <content>" with the content
    truncated per day, so the prompt carries the recent narrative without
    the full raw volume. The current weekly summary is appended when it
    exists. Returns '' when there is nothing at all.
    """
    now = now_local(settings)
    day_from = _day_key(settings, now - timedelta(days=days_back - 1))
    day_to = _day_key(settings, now)
    parts: list[str] = []
    for entry in memory_store.get_raw_entries(user_id, day_from, day_to):
        content = entry["content"].strip()
        if not content:
            continue
        parts.append(f"{entry['day']}: {_truncate(content, _CONTENT_TRUNCATE_CHARS)}")
    week = _week_summary_context(user_id, settings)
    if week:
        parts.append(f"Resumen semanal: {week}")
    return "\n".join(parts)


def _today_reminders(
    user_id: int, settings: Settings, hours: int = 24
) -> str:
    """Return pending reminders due within the next ``hours`` hours.

    Format: one "• {message} ({dd/mm HH:MM})" line per reminder, in the
    user's local timezone; overdue-but-unsent reminders are included (they
    are still pending and worth mentioning). Returns "Ninguno" when there
    are no reminders in the window.
    """
    deadline = now_local(settings).timestamp() + hours * 3600
    due = [
        reminder
        for reminder in reminders.list_pending_reminders(user_id)
        if reminder["remind_at"] <= deadline
    ]
    if not due:
        return "Ninguno"
    lines = []
    for reminder in due:
        when = datetime.fromtimestamp(
            reminder["remind_at"], ZoneInfo(settings.timezone)
        ).strftime("%d/%m %H:%M")
        lines.append(f"• {reminder['message']} ({when})")
    return "\n".join(lines)


def _yesterday_context(user_id: int, settings: Settings) -> str:
    """Return yesterday's journal (daily compact preferred), truncated.

    The daily compact is the model-distilled version of the day and is
    preferred when it exists (mirroring summarizer._day_source); the raw
    journal is the fallback for days below the compaction threshold.
    """
    yesterday = _day_key(settings, now_local(settings) - timedelta(days=1))
    content = memory_store.get_daily_compact(user_id, yesterday).strip()
    if not content:
        content = memory_store.get_raw_content(user_id, yesterday).strip()
    return _truncate(content, _CONTENT_TRUNCATE_CHARS)


# --- Message generation (calls the chat model, best-effort) -----------------


async def _generate_message(
    client: AsyncOpenAI, model: str, prompt: str,
    session_id: str | None = None, provider: str = "opencode",
) -> str | None:
    """Ask the chat model for a proactive message; None on API failure.

    Every API error (OpenCodeError and its RateLimitError subclass) is
    caught here so a check-in job never sees an exception — a transient
    outage simply yields None and the job skips that user for this run,
    with no message sent and no daily count consumed.
    """
    try:
        text = await chat_completion(
            client=client,
            model=model,
            system_prompt="",
            messages=[{"role": "user", "content": prompt}],
            session_id=session_id,
            provider=provider,
        )
    except (OpenCodeError, RateLimitError) as exc:
        logger.warning(
            "Proactive message generation failed (%s): %s",
            type(exc).__name__,
            exc,
        )
        return None
    return text.strip()


async def build_morning_message(
    client: AsyncOpenAI, model: str, user_id: int, settings: Settings,
    session_id: str | None = None,
) -> str | None:
    """Generate the morning check-in message for a user, or None on failure.

    Context is today's date + yesterday's journal + weekly summary + pending
    reminders + fallback last entry when last 7 days are empty, so the model
    can notice gaps like 10 days of silence.
    """
    yesterday = _yesterday_context(user_id, settings)
    week = _week_summary_context(user_id, settings)
    reminders_text = _today_reminders(user_id, settings)

    blocks: list[str] = [_hoy_line(settings)]
    fallback = _fallback_last_entry(user_id, settings)
    if fallback:
        blocks.append(fallback)
    if yesterday:
        blocks.append(f"Ayer:\n{yesterday}")
    if week:
        blocks.append(f"Resumen de la semana:\n{week}")
    blocks.append(f"Recordatorios próximos:\n{reminders_text}")

    prompt = (
        "Eres un asistente personal cercano y cálido. Escribe un check-in "
        "matutino para el usuario, en español.\n\n"
        "Contexto del usuario:\n"
        + "\n\n".join(blocks)
        + "\n\nInstrucciones:\n"
        "- Máximo 3 oraciones, tono cálido y natural.\n"
        "- Si el contexto tiene algo relevante (lo de ayer, pendientes, "
        "patrones, recordatorios próximos), menciónalo brevemente.\n"
        "- Si el contexto no tiene nada relevante, saluda corto y haz una "
        "pregunta abierta (por ejemplo, qué tiene planeado hoy).\n"
        "- Devuelve SOLO el texto del mensaje: sin markdown, sin comillas, "
        "sin encabezados."
    )
    return await _generate_message(
        client, model, prompt,
        session_id=session_id, provider=settings.ai_provider,
    )


async def build_evening_message(
    client: AsyncOpenAI, model: str, user_id: int, settings: Settings,
    session_id: str | None = None,
) -> str | None:
    """Generate the evening check-in message for a user, or None on failure.

    Context is hoy + today's raw journal (if any) + weekly summary + fallback
    last entry when hoy está vacío, so the model can notice gaps.
    """
    today = _day_key(settings)
    today_content = _truncate(
        memory_store.get_raw_content(user_id, today).strip(), _CONTENT_TRUNCATE_CHARS
    )
    week = _week_summary_context(user_id, settings)

    hoy = _hoy_line(settings)
    content_blocks: list[str] = []
    if today_content:
        content_blocks.append(f"Hoy:\n{today_content}")
    else:
        last = _last_entry_before_today(user_id, settings)
        if last:
            content_blocks.append(last)
    if week:
        content_blocks.append(f"Resumen de la semana:\n{week}")

    if content_blocks:
        context = f"{hoy}\n\n" + "\n\n".join(content_blocks)
    else:
        context = f"{hoy}\n\n(El usuario no compartió nada hoy.)"

    prompt = (
        "Eres un asistente personal cercano y cálido. Escribe un mensaje de "
        "cierre vespertino para el usuario, en español.\n\n"
        "Contexto del usuario:\n"
        + context
        + "\n\nInstrucciones:\n"
        "- Máximo 3 oraciones, tono cálido y natural.\n"
        "- Si el usuario compartió cosas hoy, refleja algo notable.\n"
        "- Si se ve un patrón en la semana (racha, ausencia, pendiente), "
        "menciónalo con naturalidad.\n"
        "- Si no hay nada, pregunta cómo le fue el día.\n"
        "- Devuelve SOLO el texto del mensaje: sin markdown, sin comillas."
    )
    return await _generate_message(
        client, model, prompt,
        session_id=session_id, provider=settings.ai_provider,
    )


# --- Delivery ----------------------------------------------------------------


def _retry_delay(attempts: int) -> float:
    """Return the backoff delay (seconds) after ``attempts`` failures.

    Mirrors reminders.retry_delay: 10, 20, 40… capped at RETRY_MAX_DELAY.
    The proactive retry loop only ever reaches the first two values (10, 20),
    but keeping the same formula makes the policy obvious and testable.
    """
    return min(RETRY_BASE_DELAY * 2**attempts, RETRY_MAX_DELAY)


async def _backoff_sleep(attempts: int) -> None:
    """Sleep between delivery attempts using exponential backoff.

    Indirection over asyncio.sleep so tests can patch this helper instead of
    waiting the real 10/20 seconds, and so the bot's event loop stays
    responsive (a blocking time.sleep would freeze message handling).
    """
    await asyncio.sleep(_retry_delay(attempts))


async def send_proactive(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
) -> bool:
    """Deliver a proactive message, best-effort with exponential backoff.

    Sends with Markdown first; on malformed entities (BadRequest — the model
    sometimes emits unbalanced * or _) the same text is sent plain, matching
    handlers._send_markdown_safe. Any other failure is treated as transient
    and retried up to MAX_DELIVERY_ATTEMPTS times with _backoff_sleep
    between attempts. Returns True on any successful send; unlike reminders
    there is no persistent retry queue, so a permanently failing delivery is
    logged and dropped.
    """
    for attempt in range(MAX_DELIVERY_ATTEMPTS):
        try:
            await context.bot.send_message(
                chat_id, text, parse_mode=ParseMode.MARKDOWN
            )
            return True
        except BadRequest:
            # Malformed Markdown is a content problem, not a network one:
            # fall back to plain text immediately and only retry if that
            # send also fails.
            try:
                await context.bot.send_message(chat_id, text)
                return True
            except Exception as exc:  # noqa: BLE001 - best-effort delivery
                logger.warning(
                    "Proactive plain-text fallback failed for chat %d: %s",
                    chat_id,
                    exc,
                )
        except Exception as exc:  # noqa: BLE001 - best-effort delivery
            logger.warning(
                "Proactive delivery attempt %d failed for chat %d: %s",
                attempt + 1,
                chat_id,
                exc,
            )
        if attempt < MAX_DELIVERY_ATTEMPTS - 1:
            await _backoff_sleep(attempt)
    logger.error(
        "Proactive message not delivered to chat %d after %d attempts",
        chat_id,
        MAX_DELIVERY_ATTEMPTS,
    )
    return False


# --- Driver callbacks (used by bot.py jobs) ---------------------------------


async def run_checkins(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job: deliver each user's morning/evening check-in when it is due.

    A single periodic driver (bot.py runs it every 15 minutes) replaces the
    old fixed-time daily jobs: each user may have custom check-in times (set
    from the chat via set_checkin_time) with the global PROACTIVE_* settings
    as defaults, so "when it is due" is resolved per user.

    For every user in the current window the window marker is checked BEFORE
    building the message — a window is delivered at most once a day — then
    the daily cap, then the chat is resolved. The count and the sent marker
    are only written after a successful delivery, so a failed send can be
    retried on the next firing without double-charging.
    """
    try:
        settings = context.bot_data["settings"]
        if not settings.memory_enabled or not settings.proactive_enabled:
            return
        client = context.bot_data["ai_client"]
        model = _chat_model(settings)
        day = _day_key(settings)
        for user_id in _authorized_users(settings):
            try:
                morning, evening = get_checkin_times(user_id, settings)
                period = window_of(settings, morning, evening)
                if period is None:
                    continue
                if checkin_sent_today(user_id, day, period):
                    continue
                if not can_send(user_id, day, settings.proactive_max_daily):
                    continue
                chat_id = reminders.get_chat_for_user(user_id)
                if chat_id is None:
                    continue
                if period == "morning":
                    message = await build_morning_message(
                        client, model, user_id, settings,
                        session_id=f"tg-proactive-{user_id}",
                    )
                else:
                    message = await build_evening_message(
                        client, model, user_id, settings,
                        session_id=f"tg-proactive-{user_id}",
                    )
                if not message:
                    continue
                if await send_proactive(context, chat_id, message):
                    mark_checkin_sent(user_id, day, period)
                    increment_daily_count(user_id, day, message)
            except Exception as exc:  # noqa: BLE001 - never crash the job loop
                logger.warning(
                    "Check-in failed for user %d: %s", user_id, exc
                )
    except Exception as exc:  # noqa: BLE001 - never crash the job loop
        logger.error("Check-in job failed: %s", exc)


async def run_nightly_compaction(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job: compact recent raw days that exceeded the word threshold.

    Looks back COMPACTION_CATCHUP_DAYS days (not just yesterday) so a server
    that was down for a few days catches up on missed compactions when it
    comes back. generate_daily_compact is itself idempotent and re-checks
    threshold/existing compact, so rerunning is safe. Each user is wrapped
    in its own try/except so one failing user never stops the others.
    """
    try:
        settings = context.bot_data["settings"]
        if not settings.memory_enabled:
            return
        client = context.bot_data["ai_client"]
        model = _chat_model(settings)
        threshold = settings.memory_daily_compact_words
        for user_id in _authorized_users(settings):
            for offset in range(1, COMPACTION_CATCHUP_DAYS + 1):
                try:
                    day = _day_key(
                        settings, now_local(settings) - timedelta(days=offset)
                    )
                    row = memory_store.get_raw_day(user_id, day)
                    if row is None or row["raw_word_count"] < threshold:
                        continue
                    if row["compact_path"]:
                        continue
                    await summarizer.generate_daily_compact(
                        user_id,
                        day,
                        threshold,
                        client,
                        model,
                        settings.timezone,
                        session_id=f"tg-summary-{user_id}",
                        provider=settings.ai_provider,
                    )
                except Exception as exc:  # noqa: BLE001 - one user must not stop others
                    logger.warning(
                        "Nightly compaction failed for user %d day %s: %s",
                        user_id,
                        day,
                        exc,
                    )
        # Groups: same compaction for shared journal
        for group_id in _list_group_ids():
            for offset in range(1, COMPACTION_CATCHUP_DAYS + 1):
                try:
                    day = _day_key(
                        settings, now_local(settings) - timedelta(days=offset)
                    )
                    row = memory_store.get_raw_day(group_id=group_id, day=day)
                    if row is None or row["raw_word_count"] < threshold:
                        continue
                    if row["compact_path"]:
                        continue
                    await summarizer.generate_daily_compact(
                        day=day,
                        threshold=threshold,
                        client=client,
                        model=model,
                        tz_name=settings.timezone,
                        group_id=group_id,
                        session_id=f"tg-summary-group-{group_id}",
                        provider=settings.ai_provider,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Nightly compaction failed for group %d day %s: %s",
                        group_id,
                        day,
                        exc,
                    )
    except Exception as exc:  # noqa: BLE001 - never crash the job loop
        logger.error("Nightly compaction job failed: %s", exc)


async def run_weekly_summaries(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job: ensure the weekly summaries of the last few ISO weeks exist.

    Looks back WEEKLY_CATCHUP_WEEKS weeks (not just the last one) so weeks
    whose Monday job was missed while the server was down get generated on
    the next run. ensure_summary skips existing summaries and returns None
    without a model call for weeks with no journal content, so a steady
    state costs no model calls and a catch-up costs at most one per week
    with data.
    """
    try:
        settings = context.bot_data["settings"]
        if not settings.memory_enabled:
            return
        client = context.bot_data["ai_client"]
        model = _chat_model(settings)
        now = now_local(settings)
        for user_id in _authorized_users(settings):
            for offset in range(1, WEEKLY_CATCHUP_WEEKS + 1):
                try:
                    week = memory_store.week_key(
                        now - timedelta(days=7 * offset)
                    )
                    if memory_store.get_summary(user_id, "weekly", week) is not None:
                        continue
                    await summarizer.ensure_summary(
                        user_id,
                        "weekly",
                        week,
                        client,
                        model,
                        settings.timezone,
                        settings.memory_daily_compact_words,
                        session_id=f"tg-summary-{user_id}",
                        provider=settings.ai_provider,
                    )
                except Exception as exc:  # noqa: BLE001 - one user must not stop others
                    logger.warning(
                        "Weekly summary failed for user %d week %s: %s",
                        user_id,
                        week,
                        exc,
                    )
        for group_id in _list_group_ids():
            for offset in range(1, WEEKLY_CATCHUP_WEEKS + 1):
                try:
                    week = memory_store.week_key(
                        now - timedelta(days=7 * offset)
                    )
                    if memory_store.get_summary(group_id=group_id, level="weekly", period=week) is not None:
                        continue
                    await summarizer.ensure_summary(
                        level="weekly",
                        period=week,
                        client=client,
                        model=model,
                        tz_name=settings.timezone,
                        daily_threshold=settings.memory_daily_compact_words,
                        group_id=group_id,
                        session_id=f"tg-summary-group-{group_id}",
                        provider=settings.ai_provider,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Weekly summary failed for group %d week %s: %s",
                        group_id,
                        week,
                        exc,
                    )
    except Exception as exc:  # noqa: BLE001 - never crash the job loop
        logger.error("Weekly summary job failed: %s", exc)


def _recent_quarter_keys(now: datetime, count: int) -> list[str]:
    """Return the ``count`` most recent quarter keys ending at ``now``.

    Oldest first, from ``count - 1`` quarters ago up to the current quarter.
    Pure calendar arithmetic on a quarter index (year * 4 + quarter - 1): for
    now = 2026-04-15 and count=4 this returns ["2025-Q3", "2025-Q4",
    "2026-Q1", "2026-Q2"]. Used by the quarterly catch-up job so a quarter
    whose closing day was missed while the server was down is generated on
    the next run.
    """
    current = now.year * 4 + (now.month - 1) // 3
    return [
        f"{index // 4}-Q{index % 4 + 1}" for index in range(current - count + 1, current + 1)
    ]


async def run_quarterly_summaries(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job: ensure quarterly summaries of the recent quarters exist.

    No calendar guard: the job runs daily and looks back
    QUARTERLY_CATCHUP_QUARTERS quarters (current + previous), generating any
    missing quarterly summary. A quarterly summary whose child weeks are
    missing lazily ensures them (see summarizer), so a quarter whose
    April/July/October/January closing job was missed while the server was
    down is filled in on the next run instead of being lost.
    """
    try:
        settings = context.bot_data["settings"]
        if not settings.memory_enabled:
            return
        client = context.bot_data["ai_client"]
        model = _chat_model(settings)
        now = now_local(settings)
        quarters = _recent_quarter_keys(now, QUARTERLY_CATCHUP_QUARTERS)
        for user_id in _authorized_users(settings):
            for quarter in quarters:
                try:
                    if memory_store.get_summary(user_id, "quarterly", quarter) is not None:
                        continue
                    await summarizer.ensure_summary(
                        user_id,
                        "quarterly",
                        quarter,
                        client,
                        model,
                        settings.timezone,
                        settings.memory_daily_compact_words,
                        session_id=f"tg-summary-{user_id}",
                        provider=settings.ai_provider,
                    )
                except Exception as exc:  # noqa: BLE001 - one user must not stop others
                    logger.warning(
                        "Quarterly summary failed for user %d quarter %s: %s",
                        user_id,
                        quarter,
                        exc,
                    )
        for group_id in _list_group_ids():
            for quarter in quarters:
                try:
                    if memory_store.get_summary(group_id=group_id, level="quarterly", period=quarter) is not None:
                        continue
                    await summarizer.ensure_summary(
                        level="quarterly",
                        period=quarter,
                        client=client,
                        model=model,
                        tz_name=settings.timezone,
                        daily_threshold=settings.memory_daily_compact_words,
                        group_id=group_id,
                        session_id=f"tg-summary-group-{group_id}",
                        provider=settings.ai_provider,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Quarterly summary failed for group %d quarter %s: %s",
                        group_id,
                        quarter,
                        exc,
                    )
    except Exception as exc:  # noqa: BLE001 - never crash the job loop
        logger.error("Quarterly summary job failed: %s", exc)


async def run_yearly_summaries(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job: ensure yearly summaries of the recent years exist.

    No calendar guard: runs daily and looks back YEARLY_CATCHUP_YEARS years
    (current + previous), generating any missing yearly summary. A year whose
    January 1 job was missed while the server was down is filled in on the
    next run; child quarters are lazily ensured by the summarizer.
    """
    try:
        settings = context.bot_data["settings"]
        if not settings.memory_enabled:
            return
        client = context.bot_data["ai_client"]
        model = _chat_model(settings)
        now = now_local(settings)
        years = [str(now.year - i) for i in range(YEARLY_CATCHUP_YEARS)]
        for user_id in _authorized_users(settings):
            for year in years:
                try:
                    if memory_store.get_summary(user_id, "yearly", year) is not None:
                        continue
                    await summarizer.ensure_summary(
                        user_id,
                        "yearly",
                        year,
                        client,
                        model,
                        settings.timezone,
                        settings.memory_daily_compact_words,
                        session_id=f"tg-summary-{user_id}",
                        provider=settings.ai_provider,
                    )
                except Exception as exc:  # noqa: BLE001 - one user must not stop others
                    logger.warning(
                        "Yearly summary failed for user %d year %s: %s",
                        user_id,
                        year,
                        exc,
                    )
        for group_id in _list_group_ids():
            for year in years:
                try:
                    if memory_store.get_summary(group_id=group_id, level="yearly", period=year) is not None:
                        continue
                    await summarizer.ensure_summary(
                        level="yearly",
                        period=year,
                        client=client,
                        model=model,
                        tz_name=settings.timezone,
                        daily_threshold=settings.memory_daily_compact_words,
                        group_id=group_id,
                        session_id=f"tg-summary-group-{group_id}",
                        provider=settings.ai_provider,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Yearly summary failed for group %d year %s: %s",
                        group_id,
                        year,
                        exc,
                    )
    except Exception as exc:  # noqa: BLE001 - never crash the job loop
        logger.error("Yearly summary job failed: %s", exc)
