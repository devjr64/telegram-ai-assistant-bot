"""Hierarchical memory summaries: prompt templates, helpers and generation.

The summarizer has two parts. Part 1 is the pure layer: constants,
period-expansion helpers and the prompt builder that turns child sources
(days, weeks, quarters) into an instruction prompt for the chat model — it
stays pure so it can be unit-tested without network access or a database.
Part 2 is the generation layer that calls the chat model through
bot.ai_client.chat_completion and persists through bot.memory:
generate_daily_compact, generate_weekly/quarterly/yearly_summary and the
lazy ensure_summary. Generation receives client/model/timezone as
parameters (dependency injection) so it is testable with a stubbed model
call and an isolated memory dir.

Every summary is an INDEX: a metadata line (date range, source count,
covered child units), a chronological list of dated events, the 11 fixed
domain sections, patterns/streaks and child-period pointers. Those pointers
let memory_recall navigate top-down (year → quarter → week → day) without
re-reading raw files.
"""

import logging
import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openai import AsyncOpenAI

from bot import memory as memory_store
from bot.ai_client import chat_completion, OpenCodeError, RateLimitError

logger = logging.getLogger(__name__)

# The 11 fixed domain sections every summary must contain, in this exact
# order. Keeping them as a module tuple (not embedded only in the prompt)
# makes the order a single source of truth for tests, generation and any
# future rendering.
SUMMARY_SECTIONS = (
    "Salud y ejercicio",
    "Finanzas y pagos",
    "Viajes y salidas",
    "Familia y amigos",
    "Trabajo y proyectos",
    "Estado de ánimo",
    "Pendientes y seguimientos",
    "Espiritualidad y eventos extraordinarios",
    "Ocio, descanso o disfrute",
    "Aprendizajes y estudios",
    "Eventos históricos o acontecimientos ambientales",
)

# Human label per summary level, used in headings and period_display.
LEVEL_LABELS = {
    "daily": "día",
    "weekly": "semana",
    "quarterly": "trimestre",
    "yearly": "año",
}

# The child period unit each summary level condenses: a weekly summary
# summarizes days, a quarterly one summarizes weeks, a yearly one summarizes
# quarters. Daily is absent because it is the leaf of the hierarchy (its
# detail is the raw day file itself, referenced by the day key).
CHILD_LEVELS = {
    "weekly": "día",
    "quarterly": "semana",
    "yearly": "trimestre",
}

# How verbose the model should be at each level, from leaf to overview.
_DETAIL_GUIDELINES = {
    "daily": "muy conciso (unas pocas líneas por sección)",
    "weekly": "conciso (1-2 líneas por sección)",
    "quarterly": "medio (2-4 líneas por sección)",
    "yearly": "extenso (varios párrafos por sección, con visión de conjunto)",
}

# Exact key shapes per level, mirroring memory.py's period helpers. These
# gate every expansion helper before any date arithmetic runs.
_WEEK_RE = re.compile(r"(\d{4})-W(\d{2})")
_QUARTER_RE = re.compile(r"(\d{4})-Q([1-4])")
_YEAR_RE = re.compile(r"(\d{4})")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# --- Period expansion helpers (pure, no IO) --------------------------------


def days_in_week(week_key: str) -> list[str]:
    """Return the 7 day keys (YYYY-MM-DD, Monday..Sunday) of ISO ``week_key``.

    Expands "2026-W33" into its calendar days so a weekly summary can label
    its metadata and sources with concrete dates. ``date.fromisocalendar``
    both validates the week number (raising ValueError for 00, >53 or a
    malformed string) and gives the Monday anchor.
    """
    match = _WEEK_RE.fullmatch(week_key)
    if not match:
        raise ValueError(f"Invalid week: {week_key!r} (expected YYYY-Www)")
    year, week_no = int(match.group(1)), int(match.group(2))
    try:
        monday = date.fromisocalendar(year, week_no, 1)
    except ValueError as exc:
        raise ValueError(f"Invalid week: {week_key!r}") from exc
    return [
        (monday + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(7)
    ]


def weeks_in_quarter(quarter_key: str) -> list[str]:
    """Return the ISO week keys (YYYY-Www) contained in ``quarter_key``.

    Scans every calendar day of the quarter and collects the distinct ISO
    weeks in ascending order. A quarter spans 13 or 14 ISO weeks depending
    on the year and weekday alignment, so the count is computed, never
    assumed.
    """
    match = _QUARTER_RE.fullmatch(quarter_key)
    if not match:
        raise ValueError(f"Invalid quarter: {quarter_key!r} (expected YYYY-Qq)")
    year, quarter = int(match.group(1)), int(match.group(2))
    start = date(year, (quarter - 1) * 3 + 1, 1)
    end = date(year + 1, 1, 1) if quarter == 4 else date(year, (quarter - 1) * 3 + 4, 1)
    weeks: list[str] = []
    seen: set[str] = set()
    day = start
    while day < end:
        iso_year, iso_week, _ = day.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        if key not in seen:
            seen.add(key)
            weeks.append(key)
        day += timedelta(days=1)
    return weeks


def quarters_in_year(year_key: str) -> list[str]:
    """Return the 4 quarter keys (YYYY-Q1..Q4) of ``year_key``."""
    if not isinstance(year_key, str) or not _YEAR_RE.fullmatch(year_key):
        raise ValueError(f"Invalid year: {year_key!r} (expected YYYY)")
    return [f"{year_key}-Q{quarter}" for quarter in range(1, 5)]


def _validate_day_key(day: str) -> None:
    """Raise ValueError unless ``day`` is a calendar-valid YYYY-MM-DD.

    Mirrors memory.py's _validate_day: the regex only admits the exact
    zero-padded shape and ``date()`` rejects impossible dates, so a crafted
    period can never slip past the expansion helpers.
    """
    if not isinstance(day, str) or not _DAY_RE.fullmatch(day):
        raise ValueError(f"Invalid day: {day!r} (expected YYYY-MM-DD)")
    try:
        date(int(day[:4]), int(day[5:7]), int(day[8:10]))
    except ValueError as exc:
        raise ValueError(f"Invalid day: {day!r}") from exc


def clean_model_output(text: str) -> str:
    """Strip whitespace and remove a surrounding markdown code fence.

    Chat models often wrap their answer in ```markdown … ``` fences, and a
    fence around a whole summary file would break rendering; stripping it
    here keeps the stored Markdown clean. Only removes the fence when the
    text is fully wrapped (opening and closing), leaving arbitrary inline
    backticks untouched.
    """
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        first_line, _, rest = cleaned.partition("\n")
        language = first_line.strip("`").strip().lower()
        if language in ("", "markdown", "md"):
            return rest.rstrip("`").strip()
    return cleaned


def period_display(level: str, period: str) -> str:
    """Return a human label for a period, e.g. "semana 2026-W33"."""
    if level not in LEVEL_LABELS:
        raise ValueError(f"Unknown summary level: {level!r}")
    return f"{LEVEL_LABELS[level]} {period}"


# --- Prompt builder (pure, no IO) -------------------------------------------


def build_summary_prompt(
    level: str,
    period: str,
    sources: list[dict] | str,
    level_label: str | None = None,
) -> str:
    """Build the chat-model prompt that summarizes ``period`` from ``sources``.

    Pure and deterministic: the prompt embeds the fixed summary structure
    (metadata, dated-events index, the 11 SUMMARY_SECTIONS in order,
    patterns/streaks and child-period pointers) plus the source texts
    labeled by their period keys, so the model needs no external state.
    ``sources`` is a list of {"period", "content"} dicts — the level's child
    units (days for weekly, weeks for quarterly, quarters for yearly); for
    daily it may also be a plain string (the raw day content), normalized to
    a single source dict. ``level_label`` overrides the default LEVEL_LABELS
    entry for ad-hoc labels.
    """
    if level not in LEVEL_LABELS:
        raise ValueError(f"Unknown summary level: {level!r}")
    label = level_label or LEVEL_LABELS[level]
    source_list = _normalize_sources(level, period, sources)
    child_keys = _child_periods(level, period)
    start, end = _period_range(level, period)
    sections = "\n".join(f"## {name}" for name in SUMMARY_SECTIONS)
    child_bullets = "\n".join(f"- {key}" for key in child_keys)
    source_blocks = "\n\n".join(
        f"--- Fuente: {src['period']} ---\n{src['content']}" for src in source_list
    )
    return (
        "Eres el motor de memoria de un asistente personal. A partir de las "
        "fuentes de abajo, escribe el resumen de "
        f"{label} {period} en español, en Markdown, con EXACTAMENTE esta "
        "estructura (conserva los encabezados):\n\n"
        f"# Resumen {label} {period}\n"
        f"Metadatos: rango {start} al {end} · "
        f"{_plural(len(source_list), 'fuente', 'fuentes')} · "
        f"{_plural(len(child_keys), 'unidad cubierta', 'unidades cubiertas')}\n"
        "## Eventos con fecha\n"
        "(lista cronológica de eventos con fecha concreta; cada uno lleva su "
        "fecha YYYY-MM-DD — es el índice que permite navegar al detalle)\n"
        f"{sections}\n"
        "## Patrones y rachas\n"
        "(hábitos, rachas, ausencias y cambios notables del periodo)\n"
        "## Referencias al nivel hijo\n"
        f"{child_bullets}\n\n"
        f"{_prompt_rules(level)}"
        "Fuentes:\n"
        f"{source_blocks}"
    )


def _plural(count: int, singular: str, plural: str) -> str:
    """Return ``f"{count} {singular}"`` or ``f"{count} {plural}"`` by count.

    Keeps the metadata line grammatically correct for single-source days
    ("1 fuente", "1 unidad cubierta") without complicating the template.
    """
    return f"{count} {singular if count == 1 else plural}"


def _prompt_rules(level: str) -> str:
    """Return the "Reglas" instruction block for a summary prompt.

    Extracted from build_summary_prompt so the builder stays small and the
    rules are independently testable. ``level`` only selects the detail
    guideline; the rest of the block is level-agnostic.
    """
    return (
        "Reglas:\n"
        f"- Las 11 secciones van SIEMPRE en este orden: "
        f"{', '.join(SUMMARY_SECTIONS)}.\n"
        "- Cada sección incluye el contenido real que haya en las fuentes; "
        "si no hay nada, escribe exactamente \"No hubo novedades\".\n"
        "- En \"Eventos con fecha\" solo incluye eventos con fecha concreta y "
        "escribe esa fecha como YYYY-MM-DD para que se pueda localizar "
        "después.\n"
        "- En \"Referencias al nivel hijo\" reproduce la lista anterior; son "
        "los enlaces de navegación al detalle.\n"
        f"- Nivel de detalle: {_DETAIL_GUIDELINES[level]}.\n"
        "- NO inventes hechos ni fechas que no aparezcan en las fuentes.\n"
        "- Escribe todo en español, con frases cortas y naturales.\n\n"
    )


def _normalize_sources(
    level: str, period: str, sources: list[dict] | str
) -> list[dict]:
    """Return a list of {"period", "content"} source dicts.

    Daily accepts a plain string (the raw day content) and normalizes it to
    a single source dict; every other level expects a list of dicts. A
    missing "period" falls back to the summary's own ``period``, and empty
    content is kept so the prompt still labels the covered unit.
    """
    if level == "daily" and isinstance(sources, str):
        return [{"period": period, "content": sources}]
    result: list[dict] = []
    for source in sources:
        if not isinstance(source, dict) or "content" not in source:
            raise ValueError(f"Invalid source entry: {source!r}")
        result.append(
            {
                "period": source.get("period") or period,
                "content": source.get("content", ""),
            }
        )
    return result


def _child_periods(level: str, period: str) -> list[str]:
    """Return the child period keys of ``period`` at ``level``, ascending.

    Weekly → the 7 days of the ISO week; quarterly → the ISO weeks of the
    quarter; yearly → the 4 quarters; daily → the day itself (its detail is
    the raw file, the leaf of the hierarchy). Raises ValueError on a
    malformed period for the level.
    """
    if level == "daily":
        _validate_day_key(period)
        return [period]
    if level == "weekly":
        return days_in_week(period)
    if level == "quarterly":
        return weeks_in_quarter(period)
    if level == "yearly":
        return quarters_in_year(period)
    raise ValueError(f"Unknown summary level: {level!r}")


def _period_range(level: str, period: str) -> tuple[str, str]:
    """Return the (start, end) YYYY-MM-DD dates spanned by ``period``.

    Uses the same expansion helpers as _child_periods so the metadata range
    always agrees with the child pointers listed in the summary.
    """
    if level == "daily":
        _validate_day_key(period)
        return period, period
    if level == "weekly":
        days = days_in_week(period)
        return days[0], days[-1]
    if level == "quarterly":
        weeks = weeks_in_quarter(period)
        start = days_in_week(weeks[0])[0]
        end = days_in_week(weeks[-1])[-1]
        return start, end
    if level == "yearly":
        quarters_in_year(period)
        return f"{period}-01-01", f"{period}-12-31"
    raise ValueError(f"Unknown summary level: {level!r}")


# --- Hierarchical generation (calls the chat model, persists via memory) ----


def _timezone(tz_name: str) -> ZoneInfo:
    """Resolve ``tz_name`` to a ZoneInfo, falling back to UTC on error.

    memory.py's range helpers need a ZoneInfo object, and a bad TIMEZONE
    value must never abort a summarization job — the fallback mirrors
    memory.py's own handling of unknown zone names.
    """
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r, falling back to UTC", tz_name)
        return ZoneInfo("UTC")


async def _call_model(
    client: AsyncOpenAI,
    model: str,
    prompt: str,
    system_prompt: str = "",
    session_id: str | None = None,
    provider: str = "opencode",
) -> str | None:
    """Ask the chat model to produce a summary; return cleaned text or None.

    Every API failure (OpenCodeError and its RateLimitError subclass) is
    caught here so a summarization job never sees an exception — a
    transient outage simply yields None and the caller skips persistence,
    leaving the summary to be regenerated lazily later. The single
    user-turn prompt from build_summary_prompt is wrapped in the message
    shape chat_completion expects.
    """
    try:
        text = await chat_completion(
            client=client,
            model=model,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            session_id=session_id,
            provider=provider,
        )
    except (OpenCodeError, RateLimitError) as exc:
        logger.warning(
            "Model call failed for a summary (%s): %s",
            type(exc).__name__,
            exc,
        )
        return None
    return clean_model_output(text)


def _day_source(user_id: int | None = None, day: str = "", group_id: int | None = None) -> str:
    """Return the best source text of ``day`` for a weekly summary.

    The daily compact is preferred because the model already distilled it;
    the raw journal is the fallback for days below the compaction
    threshold. Returns '' when the day has no content at all.
    """
    if group_id is not None:
        compact = memory_store.get_daily_compact(group_id=group_id, day=day)
        if compact:
            return compact
        return memory_store.get_raw_content(group_id=group_id, day=day)
    compact = memory_store.get_daily_compact(user_id, day)
    if compact:
        return compact
    return memory_store.get_raw_content(user_id, day)


def _child_summaries(level: str, period: str) -> list[tuple[str, str]]:
    """Return (child_key, child_level) pairs of ``period`` at ``level``.

    Quarterly → (week, "weekly") for each ISO week of the quarter; yearly →
    (quarter, "quarterly") for each quarter of the year. Both the source
    collector and the lazy-ensure loop enumerate children through this
    helper so they can never disagree on which child periods exist.
    """
    if level == "quarterly":
        return [(week, "weekly") for week in weeks_in_quarter(period)]
    if level == "yearly":
        return [(quarter, "quarterly") for quarter in quarters_in_year(period)]
    raise ValueError(f"Unknown summary level: {level!r}")


def collect_period_sources(
    user_id: int | None = None,
    level: str = "",
    period: str = "",
    tz_name: str = "UTC",
    group_id: int | None = None,
) -> list[dict]:
    """Return [{period, content}] of ``period``'s children that have content.

    Weekly reads each day's compact (preferred) then raw file; quarterly
    and yearly read the already-stored child summary files, which the
    generate_* callers ensure exist first via ensure_summary. Empty
    children are skipped so the prompt only carries real signal. ``tz_name``
    is accepted for interface uniformity; day expansion is calendar-based
    and does not need it today.
    """
    if level == "weekly":
        sources: list[dict] = []
        for day in days_in_week(period):
            content = _day_source(user_id, day, group_id=group_id)
            if content:
                sources.append({"period": day, "content": content})
        return sources
    if group_id is not None:
        existing = {
            (row["level"], row["period"])
            for row in memory_store.list_summaries(group_id=group_id)
        }
    else:
        existing = {
            (row["level"], row["period"])
            for row in memory_store.list_summaries(user_id)
        }
    sources = []
    for child, child_level in _child_summaries(level, period):
        if (child_level, child) not in existing:
            continue
        if group_id is not None:
            content = memory_store.get_summary_content(group_id=group_id, level=child_level, period=child)
        else:
            content = memory_store.get_summary_content(user_id, child_level, child)
        if content:
            sources.append({"period": child, "content": content})
    return sources


async def _summarize_period(
    user_id: int | None = None,
    level: str = "",
    period: str = "",
    sources: list[dict] | None = None,
    start: float = 0,
    end: float = 0,
    client: AsyncOpenAI | None = None,
    model: str = "",
    group_id: int | None = None,
    session_id: str | None = None,
    provider: str = "opencode",
) -> str | None:
    """Build, call and persist a summary once sources and range are known.

    Shared tail of generate_weekly/quarterly/yearly: the three levels only
    differ in how they collect sources and compute their epoch range, so
    the model call, save and None-on-failure behavior live here. Returns
    None (and writes nothing) when there is no source content or the model
    call fails.
    """
    if not sources:
        logger.info(
            "No source content for user %d %s %s; skipping",
            user_id,
            level,
            period,
        )
        return None
    prompt = build_summary_prompt(level, period, sources)
    content = await _call_model(
        client, model, prompt,
        session_id=session_id, provider=provider,
    )
    if content is None:
        return None
    if group_id is not None:
        saved = memory_store.save_summary(group_id=group_id, level=level, period=period, content=content, range_start=start, range_end=end)
    else:
        saved = memory_store.save_summary(
            user_id, level, period, content, start, end
        )
    if saved is None:
        return None
    return content


async def generate_daily_compact(
    user_id: int | None = None,
    day: str | None = None,
    threshold: int | None = None,
    client: AsyncOpenAI | None = None,
    model: str | None = None,
    tz_name: str = "UTC",
    group_id: int | None = None,
    session_id: str | None = None,
    provider: str = "opencode",
) -> str | None:
    """Compact a raw day into days/YYYY-MM-DD.md via the chat model.

    Runs overnight for days whose raw journal exceeds ``threshold`` words;
    smaller days are skipped because they are not worth a model call.
    Idempotent: when a compact already exists and its file is non-empty it
    is returned untouched, so re-running the nightly job never pays the API
    cost twice. On success the compact is persisted through
    memory.save_daily_compact and its text returned; on API failure None is
    returned and the raw day remains the only source (recall then falls
    back to it).
    """
    if group_id is not None:
        row = memory_store.get_daily_compact_metadata(group_id=group_id, day=day)
    else:
        row = memory_store.get_daily_compact_metadata(user_id, day)
    if row is None or row["raw_word_count"] < threshold:
        logger.info(
            "Skipping daily compact for user %d day %s (%d raw words < %d)",
            user_id,
            day,
            row["raw_word_count"] if row else 0,
            threshold,
        )
        return None
    if row["compact_path"]:
        if group_id is not None:
            existing = memory_store.get_daily_compact(group_id=group_id, day=day)
        else:
            existing = memory_store.get_daily_compact(user_id, day)
        if existing:
            logger.info(
                "Daily compact for user %d day %s already exists; skipping",
                user_id,
                day,
            )
            return existing
    if group_id is not None:
        raw = memory_store.get_raw_content(group_id=group_id, day=day)
    else:
        raw = memory_store.get_raw_content(user_id, day)
    if not raw.strip():
        logger.info(
            "Skipping daily compact for user %d day %s: no raw content",
            user_id,
            day,
        )
        return None
    prompt = build_summary_prompt("daily", day, raw)
    content = await _call_model(
        client, model, prompt,
        session_id=session_id, provider=provider,
    )
    if content is None:
        return None
    if group_id is not None:
        if memory_store.save_daily_compact(group_id=group_id, day=day, content=content) is None:
            return None
    elif memory_store.save_daily_compact(user_id, day, content) is None:
        return None
    return content


async def generate_weekly_summary(
    user_id: int | None = None,
    week: str = "",
    client: AsyncOpenAI | None = None,
    model: str = "",
    tz_name: str = "UTC",
    group_id: int | None = None,
    session_id: str | None = None,
    provider: str = "opencode",
) -> str | None:
    """Generate and persist the weekly summary of ``week``; return its text.

    Sources are the ISO week's days (compact first, raw as fallback). The
    summary is written with the week's [start, end) epoch range so recall
    can locate it by timestamp. Returns None when no day has content or the
    model call fails, leaving the week without a summary (ensure_summary
    can rebuild it lazily).
    """
    sources = collect_period_sources(user_id, "weekly", week, tz_name, group_id=group_id)
    start, end = memory_store.week_range(week, _timezone(tz_name))
    return await _summarize_period(
        user_id, "weekly", week, sources, start, end, client, model,
        group_id=group_id, session_id=session_id, provider=provider,
    )


async def generate_quarterly_summary(
    user_id: int | None = None,
    quarter: str = "",
    client: AsyncOpenAI | None = None,
    model: str = "",
    tz_name: str = "UTC",
    group_id: int | None = None,
    session_id: str | None = None,
    provider: str = "opencode",
) -> str | None:
    """Generate and persist the quarterly summary of ``quarter``.

    Sources are the quarter's ISO weeks. Any week without a stored weekly
    summary is generated lazily first (via ensure_summary), so a missing
    child means "not built yet" rather than "empty week" and a quarter never
    silently drops covered weeks. Returns None when no week yields content
    or the model call fails.
    """
    for child, child_level in _child_summaries("quarterly", quarter):
        await ensure_summary(
            user_id, child_level, child, client, model, tz_name,
            group_id=group_id, session_id=session_id, provider=provider,
        )
    sources = collect_period_sources(user_id, "quarterly", quarter, tz_name, group_id=group_id)
    start, end = memory_store.quarter_range(quarter, _timezone(tz_name))
    return await _summarize_period(
        user_id, "quarterly", quarter, sources, start, end, client, model,
        group_id=group_id, session_id=session_id, provider=provider,
    )


async def generate_yearly_summary(
    user_id: int | None = None,
    year: str = "",
    client: AsyncOpenAI | None = None,
    model: str = "",
    tz_name: str = "UTC",
    group_id: int | None = None,
    session_id: str | None = None,
    provider: str = "opencode",
) -> str | None:
    """Generate and persist the yearly summary of ``year``.

    Sources are the year's 4 quarters; quarterly summaries are ensured
    lazily first (exactly as generate_quarterly_summary ensures weeks), so
    the year condenses the full hierarchy. Returns None when no quarter has
    content or the model call fails.
    """
    for child, child_level in _child_summaries("yearly", year):
        await ensure_summary(
            user_id, child_level, child, client, model, tz_name,
            group_id=group_id, session_id=session_id, provider=provider,
        )
    sources = collect_period_sources(user_id, "yearly", year, tz_name, group_id=group_id)
    start, end = memory_store.year_range(year, _timezone(tz_name))
    return await _summarize_period(
        user_id, "yearly", year, sources, start, end, client, model,
        group_id=group_id, session_id=session_id, provider=provider,
    )


async def ensure_summary(
    user_id: int | None = None,
    level: str = "",
    period: str = "",
    client: AsyncOpenAI | None = None,
    model: str = "",
    tz_name: str = "UTC",
    daily_threshold: int = 600,
    group_id: int | None = None,
    session_id: str | None = None,
    provider: str = "opencode",
) -> dict | None:
    """Return the (level, period) summary row, generating it if missing.

    Lazy generation: when the summary already exists with non-empty content
    the row is returned unchanged and no model call is made, so recall and
    the quarterly/yearly builders can rely on it without regenerating
    children. Otherwise the matching generate_* runs and the freshly
    persisted row is returned; None when generation failed (API error or no
    source content). Daily compacts are out of scope here — they are
    handled by generate_daily_compact; ``daily_threshold`` is reserved for
    a future lazy daily-compaction path and unused today.
    """
    existing = memory_store.get_summary(user_id, level, period)
    if existing and memory_store.get_summary_content(
        user_id, level, period
    ).strip():
        return existing
    if level == "weekly":
        content = await generate_weekly_summary(
            user_id, period, client, model, tz_name,
            group_id=group_id, session_id=session_id, provider=provider,
        )
    elif level == "quarterly":
        content = await generate_quarterly_summary(
            user_id, period, client, model, tz_name,
            group_id=group_id, session_id=session_id, provider=provider,
        )
    elif level == "yearly":
        content = await generate_yearly_summary(
            user_id, period, client, model, tz_name,
            group_id=group_id, session_id=session_id, provider=provider,
        )
    else:
        raise ValueError(f"Unknown summary level: {level!r}")
    if content is None:
        return None
    return memory_store.get_summary(user_id, level, period)
