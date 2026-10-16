"""Auto-journaling background job: extracts events from chat history.

Periodically reviews recent chat messages and uses the AI model to extract
notable events, automatically saving them to the user's long-term memory
journal. This ensures nothing important is lost even if the in-conversation
AI forgets to call journal_append.

Also extracts entities (people, places, cases, items) and saves them to
the entity tracking system.

Design rules:
- Best-effort: failures are logged but never crash the job loop.
- Uses a lightweight model call (no tools) to extract events.
- Only processes users with recent activity (last 2 hours).
- Deduplicates by checking if similar events already exist today.
"""

import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram.ext import ContextTypes

from bot import memory as memory_store
from bot.ai_client import chat_completion, OpenCodeError, RateLimitError
from bot.config import load_settings
from bot.history import get_history
from bot.timeutil import day_key

logger = logging.getLogger(__name__)

# How far back to look for chat messages (in hours)
# 3h covers 60min interval with overlap; for 120min consider 4h
LOOKBACK_HOURS = 3

# Threshold for event-driven skip (messages in lookback window)
DEFAULT_MIN_MESSAGES = 6

# Track last run per user/group to implement skip fallback (best-effort, in-memory)
_last_run_ts: dict[str, float] = {}

# Fallback multiplier: force run after 3× interval even with few messages
FALLBACK_MULTIPLIER = 3

# System prompt for event extraction
_EXTRACT_SYSTEM_PROMPT = (
    "Eres un asistente que extrae eventos notables de conversaciones. "
    "Analiza la conversación y extrae SOLO los eventos que representen "
    "acciones, decisiones, planes, o hechos significativos del usuario.\n\n"
    "Ignora: saludos, despedidas, preguntas simples, confirmaciones vacías, "
    "y cualquier cosa que no sea un hecho concreto.\n\n"
    "Para cada evento extraído, devuelve un JSON con esta estructura:\n"
    '[{"content": "resumen del evento en español (1-2 oraciones)", '
    '"topic": "uno de: salud, finanzas, viajes, familia, trabajo, animo, '
    'pendientes, espiritualidad, ocio, aprendizaje, eventos"}]\n\n'
    "Si no hay eventos notables, devuelve: []\n"
    "Responde SOLO con el JSON, sin explicaciones adicionales."
)

# System prompt for entity extraction
_ENTITY_EXTRACT_SYSTEM_PROMPT = (
    "Eres un asistente que identifica entidades importantes en conversaciones. "
    "Analiza la conversación y extrae personas, lugares, documentos o casos "
    "que el usuario mencione como relevantes.\n\n"
    "Para cada entidad, devuelve un JSON con esta estructura:\n"
    '[{"type": "person|place|case|item", "name": "nombre de la entidad", '
    '"alias": "alias o apodo (opcional)", '
    '"metadata": {"campo": "valor"} (info adicional como teléfono, dirección, etc.)}]\n\n'
    "Ejemplos:\n"
    '- Persona: {"type": "person", "name": "María", "metadata": {"relación": "esposa"}}\n'
    '- Lugar: {"type": "place", "name": "Hospital Central", "metadata": {"dirección": "Av. Principal"}}\n'
    '- Caso: {"type": "case", "name": "Llevar documentos al banco", "metadata": {"fecha_límite": "viernes"}}\n'
    '- Documento: {"type": "item", "name": "Pasaporte", "metadata": {"estado": "pendiente"}}\n\n'
    "Si no hay entidades notables, devuelve: []\n"
    "Responde SOLO con el JSON, sin explicaciones adicionales."
)

# Valid journal topics
_VALID_TOPICS = {
    "salud", "finanzas", "viajes", "familia", "trabajo", "animo",
    "pendientes", "espiritualidad", "ocio", "aprendizaje", "eventos",
}

# Valid entity types
_VALID_ENTITY_TYPES = {"person", "place", "case", "item"}

# Combined prompt (1 call vs 2) — returns {"events": [...], "entities": [...]}
_COMBINED_EXTRACT_PROMPT = (
    "Eres un asistente que extrae eventos y entidades de conversaciones. "
    "Analiza la conversación y devuelve SOLO un JSON con dos claves:\n\n"
    '{"events": [{"content": "resumen 1-2 oraciones en español", "topic": "salud|finanzas|viajes|familia|trabajo|animo|pendientes|espiritualidad|ocio|aprendizaje|eventos"}], '
    '"entities": [{"type": "person|place|case|item", "name": "nombre", "alias": "apodo opcional", "metadata": {"campo": "valor"}}]}\n\n'
    "Reglas eventos: ignora saludos/despedidas/preguntas simples. Solo hechos/acciones/decisiones/planes significativos. Si no hay, events=[]\n"
    "Reglas entidades: solo personas/lugares/documentos/casos relevantes mencionados. Si no hay, entities=[]\n"
    "Responde SOLO con el JSON, sin explicaciones ni markdown."
)


def _get_recent_messages_for_group(group_id: int, hours: int = LOOKBACK_HOURS) -> list[dict]:
    """Get recent messages from a group chat (any author)."""
    cutoff = time.time() - (hours * 3600)
    from bot.history import _get_connection
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat_history "
            "WHERE chat_id = ? AND created_at > ? "
            "ORDER BY created_at ASC",
            (group_id, cutoff),
        ).fetchall()
    return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows]


def _get_recent_messages(user_id: int, chat_id: int, hours: int = LOOKBACK_HOURS) -> list[dict]:
    """Get recent chat messages from the last N hours.

    Queries the chat_history database for messages newer than `hours` ago.
    Returns messages in chronological order.
    """
    cutoff = time.time() - (hours * 3600)
    from bot.history import _get_connection

    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM chat_history "
            "WHERE user_id = ? AND chat_id = ? AND created_at > ? "
            "ORDER BY created_at ASC",
            (user_id, chat_id, cutoff),
        ).fetchall()

    return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows]


def _format_conversation_for_extraction(messages: list[dict]) -> list[dict]:
    """Format messages into a conversation for the extraction model."""
    formatted = []
    for msg in messages:
        formatted.append({
            "role": msg["role"],
            "content": msg["content"],
        })
    return formatted


def _parse_events(response: str) -> list[dict]:
    """Parse the model's JSON response into a list of events.

    Handles various formats the model might return (wrapped in markdown
    code blocks, extra text, etc.).
    """
    # Try to extract JSON from the response
    text = response.strip()

    # Remove markdown code blocks if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (``` markers)
        lines = [l for l in lines[1:] if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        events = json.loads(text)
    except json.JSONDecodeError:
        # Try to find a JSON array in the text
        start = text.find("[")
        end = text.rfind("]") + 1
        if start != -1 and end > start:
            try:
                events = json.loads(text[start:end])
            except json.JSONDecodeError:
                logger.warning("Failed to parse events JSON: %s", text[:200])
                return []
        else:
            logger.warning("No JSON array found in response: %s", text[:200])
            return []

    if not isinstance(events, list):
        return []

    # Validate and clean events
    valid_events = []
    for event in events:
        if not isinstance(event, dict):
            continue
        content = (event.get("content") or "").strip()
        topic = (event.get("topic") or "").strip().lower()
        if not content:
            continue
        # Validate topic, default to empty if invalid
        if topic not in _VALID_TOPICS:
            topic = ""
        valid_events.append({"content": content, "topic": topic})

    return valid_events


def _parse_entities(response: str) -> list[dict]:
    """Parse the model's JSON response into a list of entities."""
    text = response.strip()

    # Remove markdown code blocks if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines[1:] if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        entities = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]") + 1
        if start != -1 and end > start:
            try:
                entities = json.loads(text[start:end])
            except json.JSONDecodeError:
                logger.warning("Failed to parse entities JSON: %s", text[:200])
                return []
        else:
            logger.warning("No JSON array found in response: %s", text[:200])
            return []

    if not isinstance(entities, list):
        return []

    valid_entities = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_type = (entity.get("type") or "").strip().lower()
        name = (entity.get("name") or "").strip()
        if not name or entity_type not in _VALID_ENTITY_TYPES:
            continue
        alias = (entity.get("alias") or "").strip() or None
        metadata = entity.get("metadata") or {}
        valid_entities.append({
            "type": entity_type,
            "name": name,
            "alias": alias,
            "metadata": metadata,
        })

    return valid_entities


async def _extract_entities_from_messages(
    messages: list[dict],
) -> list[dict]:
    """Use the AI model to extract entities from messages."""
    settings = load_settings()
    from bot.ai_client import _build_client
    client = _build_client(settings)

    conversation = _format_conversation_for_extraction(messages)

    try:
        response = await chat_completion(
            client=client,
            model=settings.model,
            system_prompt=_ENTITY_EXTRACT_SYSTEM_PROMPT,
            messages=conversation,
            session_id="tg-auto-journal",
            provider=settings.ai_provider,
        )
        return _parse_entities(response)
    except (OpenCodeError, RateLimitError) as exc:
        logger.error("Failed to extract entities: %s", exc)
        return []
    except Exception as exc:
        logger.error("Unexpected error extracting entities: %s", exc)
        return []


def _parse_combined(response: str) -> tuple[list[dict], list[dict]]:
    """Parse combined JSON into (events, entities)."""
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines[1:] if not l.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            try:
                data = json.loads(text[start:end])
            except json.JSONDecodeError:
                logger.warning("Failed to parse combined JSON: %s", text[:300])
                return [], []
        else:
            logger.warning("No JSON object found in combined response: %s", text[:300])
            return [], []
    if not isinstance(data, dict):
        return [], []
    # Parse events/entities via existing validators
    events_raw = json.dumps(data.get("events", []))
    entities_raw = json.dumps(data.get("entities", []))
    return _parse_events(events_raw), _parse_entities(entities_raw)


async def _extract_combined_from_messages(
    messages: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Single model call for events+entities (saves 50% tokens)."""
    settings = load_settings()
    from bot.ai_client import _build_client

    client = _build_client(settings)
    conversation = _format_conversation_for_extraction(messages)
    try:
        response = await chat_completion(
            client=client,
            model=settings.model,
            system_prompt=_COMBINED_EXTRACT_PROMPT,
            messages=conversation,
            session_id="tg-auto-journal",
            provider=settings.ai_provider,
        )
        return _parse_combined(response)
    except (OpenCodeError, RateLimitError) as exc:
        logger.error("Failed to extract combined: %s", exc)
        return [], []
    except Exception as exc:
        logger.error("Unexpected error extracting combined: %s", exc)
        return [], []


def _should_skip(key: str, msg_count: int, min_messages: int, interval: int = 3600) -> bool:
    """Event-driven skip: skip if too few messages and fallback not due.
    
    Fallback is interval × FALLBACK_MULTIPLIER (e.g. 60min → 3h).
    """
    try:
        mm = int(min_messages)
    except Exception:
        mm = DEFAULT_MIN_MESSAGES
    if msg_count >= mm:
        return False
    try:
        iv = int(interval)
    except Exception:
        iv = 3600
    last = _last_run_ts.get(key, 0)
    if time.time() - last >= iv * FALLBACK_MULTIPLIER:
        return False
    return True


def _save_entities(
    user_id: int,
    entities: list[dict],
) -> int:
    """Save extracted entities to the tracking system.

    Returns the number of entities successfully saved.
    """
    from bot.entities import create_entity

    saved = 0
    for entity in entities:
        result = create_entity(
            user_id=user_id,
            entity_type=entity["type"],
            name=entity["name"],
            alias=entity.get("alias"),
            metadata=entity.get("metadata"),
        )
        if result is not None:
            saved += 1
        else:
            logger.warning(
                "Failed to save entity for user %d: %s", user_id, entity["name"]
            )

    return saved


async def _extract_events_from_messages(
    messages: list[dict],
) -> list[dict]:
    """Use the AI model to extract notable events from messages."""
    settings = load_settings()
    client = settings._get_client() if hasattr(settings, '_get_client') else None

    # Build client if not available
    from bot.ai_client import _build_client
    if client is None:
        client = _build_client(settings)

    conversation = _format_conversation_for_extraction(messages)

    try:
        response = await chat_completion(
            client=client,
            model=settings.model,
            system_prompt=_EXTRACT_SYSTEM_PROMPT,
            messages=conversation,
            session_id="tg-auto-journal",
            provider=settings.ai_provider,
        )
        return _parse_events(response)
    except (OpenCodeError, RateLimitError) as exc:
        logger.error("Failed to extract events: %s", exc)
        return []
    except Exception as exc:
        logger.error("Unexpected error extracting events: %s", exc)
        return []


def _save_events_to_journal(
    user_id: int | None = None,
    events: list[dict] | None = None,
    tz_name: str = "UTC",
    group_id: int | None = None,
) -> int:
    """Save extracted events to the journal (user or group).

    Returns the number of events successfully saved.
    """
    if events is None:
        events = []
    settings = load_settings()
    day = day_key(datetime.now(ZoneInfo(tz_name)))
    saved = 0

    for event in events:
        content = event["content"]
        topic = event.get("topic", "")
        text = f"[{topic}] {content}" if topic else content
        if group_id is not None:
            result = memory_store.append_raw_entry(
                group_id=group_id,
                day=day,
                text=text,
                tz_name=tz_name,
                author_username=None,
            )
            target = f"group {group_id}"
        else:
            result = memory_store.append_raw_entry(
                user_id=user_id,
                day=day,
                text=text,
                tz_name=tz_name,
            )
            target = f"user {user_id}"
        if result is not None:
            saved += 1
        else:
            logger.warning(
                "Failed to save event for %s: %s", target, content[:50]
            )

    return saved


async def auto_journal_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Background job: extract events and entities from recent chat.

    Runs every AUTO_JOURNAL_INTERVAL (60min default). Reviews the last 3 hours
    of chat history for each authorized user and:
    1. Extracts events+entities in ONE model call (saves 50%)
    2. Event-driven skip: <6 messages → skip unless 3h fallback due

    Design: best-effort, never crashes the loop.
    """
    settings = context.bot_data["settings"]

    if not settings.memory_enabled:
        return

    try:
        min_msgs = int(getattr(settings, "auto_journal_min_messages", DEFAULT_MIN_MESSAGES))
    except Exception:
        min_msgs = DEFAULT_MIN_MESSAGES
    try:
        interval = int(getattr(settings, "auto_journal_interval", 3600))
    except Exception:
        interval = 3600

    for user_id in settings.authorized_users:
        try:
            messages = _get_recent_messages(user_id, user_id, LOOKBACK_HOURS)

            if not messages:
                continue
            key = f"user:{user_id}"
            if _should_skip(key, len(messages), min_msgs, interval):
                continue

            # Single combined call
            events, entities = await _extract_combined_from_messages(messages)
            _last_run_ts[key] = time.time()

            if events:
                saved = _save_events_to_journal(user_id, events, settings.timezone)
                if saved > 0:
                    logger.info(
                        "Auto-journal: saved %d events for user %d", saved, user_id
                    )

            if entities:
                saved_entities = _save_entities(user_id, entities)
                if saved_entities > 0:
                    logger.info(
                        "Auto-journal: saved %d entities for user %d",
                        saved_entities,
                        user_id,
                    )

        except Exception as exc:
            logger.error(
                "Auto-journal failed for user %d: %s", user_id, exc
            )

    # Process groups as well (shared journal) — events only, 1 call/group
    try:
        from bot.projects import _get_connection as _get_projects_conn
        with _get_projects_conn() as conn:
            rows = conn.execute("SELECT DISTINCT group_id FROM group_members").fetchall()
            group_ids = [r[0] for r in rows]
    except Exception as exc:
        logger.warning("Failed to list groups for auto-journal: %s", exc)
        group_ids = []
    for group_id in group_ids:
        try:
            messages = _get_recent_messages_for_group(group_id, LOOKBACK_HOURS)
            if not messages:
                continue
            key = f"group:{group_id}"
            if _should_skip(key, len(messages), min_msgs, interval):
                continue
            # For groups we still use combined extractor but ignore entities (v1)
            events, _ = await _extract_combined_from_messages(messages)
            _last_run_ts[key] = time.time()
            if events:
                saved = _save_events_to_journal(events=events, tz_name=settings.timezone, group_id=group_id)
                if saved > 0:
                    logger.info("Auto-journal: saved %d events for group %d", saved, group_id)
        except Exception as exc:
            logger.error("Auto-journal failed for group %d: %s", group_id, exc)
