"""Tool/function-calling definitions and execution for the AI.

The AI model (via OpenCode Go's OpenAI-compatible API) can decide to
call these tools during a conversation. Each tool maps to a project
CRUD operation; results are returned as JSON strings so the model can
produce a natural-language reply.
"""

import base64
import datetime
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

from bot.projects import (
    create_project,
    create_group_project,
    update_project_status,
    update_project_priority,
    update_project_description,
    add_project_note,
    list_projects,
    search_projects,
    get_project_for_user,
    get_project_notes,
    read_project_files,
    is_owner,
    resolve_username,
    add_project_member,
    remove_project_member,
    list_project_members,
    list_shared_with_me,
    list_group_projects_for_user,
    get_shared_projects_in_group,
    PROJECT_STATUSES,
    PROJECT_PRIORITIES,
    ACTIVE_STATUSES,
    can_activate_urgent,
)
from bot.reminders import (
    create_reminder as create_reminder_db,
    delete_reminder,
    get_chat_for_user,
    list_pending_reminders,
    update_reminder_text,
    update_reminder_time,
    postpone_reminder as postpone_reminder_db,
    RECURRENCE_VALUES,
    RECURRENCE_LABELS,
)
from bot.memory import (
    append_raw_entry,
    day_range,
    get_daily_compact,
    get_raw_content,
    get_raw_entries,
    get_summary_content,
    list_summaries,
    week_key,
)
from bot.summarizer import days_in_week, quarters_in_year, weeks_in_quarter
from bot.proactive import set_checkin_time as set_checkin_time_store
from bot.websearch import web_search
from bot.dolar import get_dolar_info
from bot.ai_client import (
    build_client_for_provider,
    generate_image as generate_image_ai,
    OpenCodeError,
    RateLimitError,
    QuotaExhaustedError,
)
from bot.config import load_settings
from bot.timeutil import today_key
from bot import media_usage
from bot.media_usage import IMAGE_TYPE
from bot.entities import (
    create_entity,
    update_entity,
    get_entity,
    list_entities,
    search_entities,
    archive_entity,
    add_entity_note,
    get_entity_notes,
    get_entity_with_notes,
    ENTITY_TYPES,
)

# Tools that are NEVER available in group context (private-only, v1 whitelist)
PRIVATE_ONLY_TOOLS: set[str] = {
    "generate_image",
    "set_checkin_time",
    "share_project",
    "unshare_project",
    "delete_project",
    "list_project_members",
}

# Back-compat alias — keep PRIVATE_TOOLS pointing to the curated private-only set
PRIVATE_TOOLS: set[str] = set(PRIVATE_ONLY_TOOLS)

# Tools that are available in group context (scoped to group when group_id is set)
GROUP_TOOLS: set[str] = {
    "create_project",
    "update_project_status",
    "update_project_priority",
    "update_project_description",
    "add_project_note",
    "list_projects",
    "search_projects",
    "get_project",
    "create_reminder",
    "list_reminders",
    "cancel_reminder",
    "edit_reminder",
    "reschedule_reminder",
    "postpone_reminder",
    "create_entity",
    "list_entities",
    "search_entities",
    "get_entity_details",
    "add_entity_note",
    "update_entity",
    "archive_entity",
    "web_search",
    "dolar_rate",
    "journal_append",
    "memory_read",
    "memory_recall",
    "memory_insights",
}


def _filter_tool_definitions(group_id: int | None) -> list[dict]:
    """Return TOOL_DEFINITIONS filtered for the caller's scope.

    In private chat every tool is shown. In group context the
    PRIVATE_ONLY_TOOLS are hidden so the model never tries them —
    zero wasted tokens, zero confusing error replies.
    """
    if group_id is None:
        return TOOL_DEFINITIONS
    blocked = PRIVATE_ONLY_TOOLS
    return [d for d in TOOL_DEFINITIONS if d["function"]["name"] not in blocked]

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "create_project",
            "description": "Crea un nuevo proyecto para el usuario. Úsala cuando el usuario "
            "mencione una tarea, plan o tema recurrente que merezca su propia "
            "carpeta (por ejemplo, arreglar el carro, remodelar la casa).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Nombre del proyecto (por ejemplo, 'Arreglar el carro').",
                    },
                    "description": {
                        "type": "string",
                        "description": "Breve descripción del proyecto.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": list(PROJECT_PRIORITIES),
                        "description": "Prioridad del proyecto: baja, media, alta o urgente. "
                        "Solo puede haber hasta 3 proyectos urgentes activos.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project_status",
            "description": "Cambia el estado de un proyecto existente. Si el nuevo estado "
            "es 'parado', puedes indicar opcionalmente el motivo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug del proyecto (nombre en minúsculas y guiones).",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(PROJECT_STATUSES),
                        "description": "Nuevo estado del proyecto.",
                    },
                    "stopped_reason": {
                        "type": "string",
                        "description": "Motivo por el que se detuvo el proyecto. "
                        "Solo se guarda cuando el estado es 'parado'.",
                    },
                },
                "required": ["slug", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project_priority",
            "description": "Cambia la prioridad de un proyecto existente. Solo se permiten "
            "hasta 3 proyectos urgentes en estados activos (sin_iniciar o en_proceso).",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug del proyecto.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": list(PROJECT_PRIORITIES),
                        "description": "Nueva prioridad: baja, media, alta o urgente.",
                    },
                },
                "required": ["slug", "priority"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project_description",
            "description": "Actualiza la descripción de un proyecto existente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug del proyecto.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Nueva descripción del proyecto.",
                    },
                },
                "required": ["slug", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_project_note",
            "description": "Agrega una nota a un proyecto existente. Úsala cuando el "
            "usuario comparta nueva información, avances o detalles de un proyecto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug del proyecto.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Resumen de una línea de la nota.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Contenido completo de la nota.",
                    },
                },
                "required": ["slug", "summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_projects",
            "description": "Lista los proyectos del usuario. Úsala cuando el usuario "
            "pregunte por sus proyectos o qué está pendiente. Return the `formatted` "
            "field as-is in your reply; do not convert it into a Markdown table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status_filter": {
                        "type": "string",
                        "enum": list(PROJECT_STATUSES),
                        "description": "Estado opcional por el que filtrar.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["all", "owned", "shared_with_me", "shared_by_me"],
                        "description": "Alcance: 'all' (todos), 'owned' (solo míos), "
                        "'shared_with_me' (compartidos conmigo), 'shared_by_me' "
                        "(míos compartidos con otros). Default: 'all'.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_projects",
            "description": "Busca proyectos del usuario por nombre o descripción. "
            "Return the `formatted` field as-is in your reply; do not convert it "
            "into a Markdown table.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Término de búsqueda.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project",
            "description": "Obtiene los detalles completos de un proyecto, incluyendo "
            "sus notas y contenido Markdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug del proyecto.",
                    },
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_project",
            "description": "Comparte un proyecto del usuario con otro usuario "
            "(invitado/colaborador) usando su @username de Telegram. Solo el dueño "
            "puede compartir. Ejemplo: 'comparte el proyecto Casa con @mama'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug del proyecto (nombre en minúsculas y guiones).",
                    },
                    "username": {
                        "type": "string",
                        "description": "Username de Telegram del invitado, con o sin @ "
                        "(por ejemplo, '@mama' o 'mama').",
                    },
                },
                "required": ["slug", "username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unshare_project",
            "description": "Quita el acceso a un colaborador de un proyecto. Solo el "
            "dueño puede hacerlo. Ejemplo: 'deja de compartir el proyecto Casa con @mama'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug del proyecto.",
                    },
                    "username": {
                        "type": "string",
                        "description": "Username de Telegram del colaborador, con o sin @.",
                    },
                },
                "required": ["slug", "username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_project",
            "description": "Pide confirmación para eliminar un proyecto archivado y "
            "liberar espacio. SOLO úsala cuando el usuario pida explícitamente "
            "eliminar un proyecto archivado, y nunca de forma proactiva. La tool "
            "NO elimina directamente: devuelve confirmation_required y el bot "
            "muestra al usuario botones de confirmación; el proyecto solo se borra "
            "si el usuario confirma. Es IRREVERSIBLE (las notas y archivos se "
            "pierden para siempre) y solo el dueño puede eliminar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug del proyecto archivado a eliminar.",
                    },
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_project_members",
            "description": "Lista los colaboradores (invitados) con los que un proyecto "
            "está compartido, con su @username. Ejemplo: 'con quién está compartido el "
            "proyecto Casa'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug del proyecto.",
                    },
                },
                "required": ["slug"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_reminder",
            "description": "Schedule a reminder for the user at a future date/time. "
            "Use this when the user asks to be reminded of something later "
            "(e.g. 'recuérdame mañana llamar al mecánico', 'quiero que me "
            "recuerdes la semana que viene comprar el repuesto'). For recurring "
            "reminders (e.g. 'todos los lunes'), set recurrence accordingly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The reminder text, e.g. 'Llamar al mecánico'.",
                    },
                    "datetime": {
                        "type": "string",
                        "description": "ISO 8601 datetime in the user's timezone, "
                        "format YYYY-MM-DDTHH:MM. Use the next occurrence for "
                        "recurring reminders.",
                    },
                    "recurrence": {
                        "type": "string",
                        "enum": list(RECURRENCE_VALUES),
                        "description": "'daily' for every day, 'weekly' for the "
                        "same day every week, 'monthly' for the same day every "
                        "month, 'none' for a one-time reminder.",
                    },
                },
                "required": ["message", "datetime"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "List the user's pending reminders (unsent, including future ones). "
            "Use this when the user asks what reminders they have scheduled, "
            "or wants to see their pending reminders. Return the `formatted` "
            "field as-is in your reply; do not convert it into a Markdown table.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_reminder",
            "description": "Cancel (delete) one of the user's pending reminders by its id. "
            "Use this when the user asks to cancel, delete, or remove a specific reminder. "
            "The id comes from list_reminders or the /recordatorios command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "integer",
                        "description": "The id of the reminder to cancel.",
                    },
                },
                "required": ["reminder_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_reminder",
            "description": "Change the text of one of the user's pending reminders by its id. "
            "Use this when the user asks to edit, change, or reword an existing reminder "
            "without moving its date. The id comes from list_reminders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "integer",
                        "description": "The id of the reminder to edit.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The new reminder text.",
                    },
                },
                "required": ["reminder_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_reminder",
            "description": "Move one of the user's pending reminders to a different date/time "
            "by its id. Use this when the user asks to move, reschedule, or change when a "
            "reminder fires. The id comes from list_reminders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "integer",
                        "description": "The id of the reminder to reschedule.",
                    },
                    "datetime": {
                        "type": "string",
                        "description": "ISO 8601 datetime in the user's timezone, "
                        "format YYYY-MM-DDTHH:MM.",
                    },
                },
                "required": ["reminder_id", "datetime"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "postpone_reminder",
            "description": "Push one of the user's pending reminders forward by a number of "
            "minutes, keeping its text. Use this when the user asks to postpone, snooze, or "
            "delay a reminder (e.g. 'pospónlo 2 horas'). The id comes from list_reminders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reminder_id": {
                        "type": "integer",
                        "description": "The id of the reminder to postpone.",
                    },
                    "minutes": {
                        "type": "integer",
                        "description": "How many minutes to delay the reminder "
                        "(e.g. 30, 120, 1440 for a day).",
                    },
                },
                "required": ["reminder_id", "minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the internet for current or factual information. "
            "Use this when the user asks about recent events, news, prices, "
            "products, or any data you are not certain about and whose answer may "
            "have changed since your training. Summarize the results in Spanish "
            "and cite the source links.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query, concise and in the "
                        "user's language.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "How many results to fetch (1-10, "
                        "default 5).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dolar_rate",
            "description": "Get the current US dollar exchange rates in Venezuela: "
            "the official BCV rate and the parallel rate (USDT Binance P2P). "
            "Use this whenever the user asks about the dollar price or rate, the "
            "parallel dollar, USDT, or the BCV official rate. Reply with both "
            "rates in Spanish.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image with AI from a text description. "
            "Use this whenever the user asks you to create, draw, or generate "
            "an image (e.g. 'genera una imagen de un gato astronauta', 'dibuja "
            "un logo para mi proyecto', 'hazme una imagen de una casa moderna'). "
            "The image is sent to the user directly; reply confirming what was "
            "created.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the image to "
                        "generate, in the user's language. Include style, "
                        "colors, and composition when provided.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "journal_append",
            "description": "Guarda algo notable del día del usuario en su bitácora "
            "personal (memoria a largo plazo). Úsala cuando el usuario cuente "
            "experiencias de su vida diaria: comidas, sueño, ejercicio, viajes, "
            "salidas, pagos (luz, condominio, agua), conversaciones con otras "
            "personas, estado de ánimo, anécdotas, aprendizajes, eventos. Escribe "
            "un resumen breve en español en tus propias palabras conservando la "
            "información clave y las fechas. No la uses para tareas/proyectos "
            "(usa add_project_note) ni para recordatorios (usa create_reminder).",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "El hecho notable del día, resumido en 1-2 "
                        "frases en español.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Categoría opcional entre: salud, finanzas, "
                        "viajes, familia, trabajo, animo, pendientes, "
                        "espiritualidad, ocio, aprendizaje, eventos.",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_read",
            "description": "Lee el contenido exacto de un resumen o de un día de la "
            "bitácora del usuario. Úsala cuando memory_recall haya indicado un "
            "periodo concreto y necesites el detalle, o cuando el usuario pregunte "
            "por algo puntual de una fecha/periodo. period ejemplos: '2026-08-14' "
            "(día), '2026-W33' (semana), '2026-Q3' (trimestre), '2026' (año).",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "description": "Periodo a leer: día (YYYY-MM-DD), semana "
                        "(YYYY-Www), trimestre (YYYY-Q#) o año (YYYY).",
                    },
                    "level": {
                        "type": "string",
                        "enum": ["auto", "raw", "daily", "weekly", "quarterly", "yearly"],
                        "description": "Nivel de detalle: auto (recomendado) deduce "
                        "el nivel del formato del periodo; raw/daily leen la "
                        "bitácora cruda o el compacto de un día; weekly/quarterly/"
                        "yearly leen el resumen del periodo.",
                    },
                },
                "required": ["period"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": "Busca en la memoria a largo plazo del usuario "
            "(bitácora y resúmenes) para responder preguntas sobre su pasado: "
            "'¿cuándo fue mi último viaje a Panamá?', '¿qué comí la semana "
            "pasada?', '¿cuándo pagué la luz?'. Hace búsqueda jerárquica: "
            "primero resúmenes anuales/trimestrales/semanales y, si no "
            "encuentra, busca en los días del rango. Úsala SIEMPRE que el "
            "usuario pregunte por algo que pasó antes de esta conversación y "
            "no lo tengas en contexto. Puedes limitar date_from/date_to "
            "(formato YYYY-MM-DD) para acotar la búsqueda.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "La pregunta o tema a buscar, en español.",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Límite inferior YYYY-MM-DD",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Límite superior YYYY-MM-DD",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_insights",
            "description": "Devuelve contexto reciente de la bitácora del "
            "usuario (últimos días + resumen de la semana en curso) para "
            "detectar patrones, rachas, ausencias o pendientes. Úsala cuando "
            "el usuario pregunte por sus hábitos ('¿he hecho ejercicio esta "
            "semana?', '¿qué patrón notas?') o cuando necesites datos para "
            "recordarle algo proactivamente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Cuántos días atrás incluir "
                        "(1-30, default 7).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_checkin_time",
            "description": "Configura la hora a la que el usuario quiere "
            "recibir sus check-ins proactivos (matutino y vespertino). Úsala "
            "cuando el usuario pida cambiar el horario de sus check-ins, por "
            "ejemplo 'cambia mi check-in de la mañana a las 9' o 'adelanta "
            "mi check-in de la tarde'. La hora va en formato HH:MM (24h) en "
            "la zona horaria del usuario. Las horas por defecto vienen de la "
            "configuración; esta tool solo se usa si el usuario pide "
            "cambiarlas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["morning", "evening"],
                        "description": "Qué check-in ajustar: 'morning' "
                        "(matutino) o 'evening' (vespertino).",
                    },
                    "time": {
                        "type": "string",
                        "description": "Hora en formato HH:MM (24h), por "
                        "ejemplo '09:00' o '18:30'.",
                    },
                },
                "required": ["period", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_entity",
            "description": "Crea una nueva entidad para el usuario (persona, lugar, caso o documento). "
            "Úsala cuando el usuario quiera registrar algo para seguimiento, como un caso SIGAT, "
            "una persona de contacto, un lugar o un documento pendiente. Ejemplo: "
            "'registra el caso SIGAT con RIF y comprobante, lleva 2 meses en trámite'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": list(ENTITY_TYPES),
                        "description": "Tipo de entidad: 'person' (persona), 'place' (lugar), "
                        "'case' (caso/trámite) o 'item' (documento/objeto).",
                    },
                    "name": {
                        "type": "string",
                        "description": "Nombre de la entidad (ej. 'SIGAT', 'María', 'Hospital Central').",
                    },
                    "alias": {
                        "type": "string",
                        "description": "Alias o apodo alternativo (opcional).",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Información adicional como teléfono, dirección, estado, etc.",
                    },
                },
                "required": ["entity_type", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_entities",
            "description": "Lista las entidades del usuario (personas, lugares, casos, documentos). "
            "Úsala cuando el usuario pregunte por personas que conoce, lugares que frecuenta, "
            "o casos/pendientes que está siguiendo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": list(ENTITY_TYPES),
                        "description": "Filtrar por tipo: 'person', 'place', 'case' o 'item'. "
                        "Si se omite, muestra todos los tipos.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_entity",
            "description": "Actualiza una entidad existente (nombre, alias, metadata, estado). "
            "Úsala cuando el usuario quiera actualizar información sobre una persona, "
            "lugar o caso que ya está registrado.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "integer",
                        "description": "ID de la entidad a actualizar.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Nuevo nombre de la entidad.",
                    },
                    "alias": {
                        "type": "string",
                        "description": "Alias o apodo alternativo.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Campos extra como teléfono, dirección, relación, etc.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "archived"],
                        "description": "Nuevo estado: 'active' o 'archived'.",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entity_details",
            "description": "Obtiene los detalles completos de una entidad incluyendo sus notas "
            "de seguimiento. Úsala cuando el usuario quiera ver toda la información "
            "de una persona, lugar o caso específico.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "integer",
                        "description": "ID de la entidad.",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_entity_note",
            "description": "Añade una nota de seguimiento a una entidad. Úsala cuando el usuario "
            "cobre actualizaciones sobre un caso, persona o lugar (por ejemplo, 'me dijeron "
            "que tengo que llevar los documentos otro día').",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "integer",
                        "description": "ID de la entidad a la que añadir la nota.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Contenido de la nota (1-2 oraciones).",
                    },
                },
                "required": ["entity_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_entities",
            "description": "Busca entidades por nombre o alias. Úsala cuando el usuario pregunte "
            "por algo específico como '¿quién es María?', '¿dónde está el banco?', "
            "o '¿qué pasa con los documentos?'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Término de búsqueda (nombre, alias o parte del nombre).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "archive_entity",
            "description": "Archiva una entidad (caso, persona, lugar o documento) para ocultarla "
            "de las listas activas. Úsala cuando el usuario diga que un caso ya terminó, "
            "se resolvió o no quiere verlo más (ej. 'archiva el caso SIGAT'). Es reversible "
            "con update_entity status 'active'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "integer",
                        "description": "ID de la entidad a archivar.",
                    },
                },
                "required": ["entity_id"],
            },
        },
    },
]


def _resolve_slug(user_id: int, slug: str, group_id: int | None = None) -> dict | None:
    """Resolve a possibly-raw slug to a project the user can access, or None.

    Delegates to get_project_for_user(), the ACL gate: the project is
    returned only when the user is its owner or an invited member. This
    single change scopes every existing project tool — a non-member gets
    "Proyecto no encontrado" instead of seeing another user's project.

    When ``group_id`` is provided, only projects in that group are returned.
    When ``group_id`` is None, only private projects are returned.
    """
    return get_project_for_user(user_id, slug, group_id=group_id)


def _project_not_found() -> str:
    """Return a standardized Spanish 'project not found' error."""
    return json.dumps({"ok": False, "error": "Proyecto no encontrado"})


async def _handle_create_project(args: dict, user_id: int) -> str:
    """Create a project, applying the urgent-priority limit if needed.
    
    When ``_group_id`` is provided (group context), uses ``create_group_project``
    to automatically add all group members as collaborators.
    """
    name = args["name"]
    description = args.get("description", "")
    priority = args.get("priority", "media")
    group_id = args.pop("_group_id", None)
    
    if group_id is not None:
        # Group context: create project and auto-add all group members
        result = create_group_project(user_id, group_id, name, description, priority)
    else:
        # Private context: create normal project
        result = create_project(user_id, name, description, priority)
    
    if result is None:
        return json.dumps(
            {"ok": False, "error": "Ya existe un proyecto con ese nombre"}
        )
    if isinstance(result, dict) and result.get("ok") is False:
        return json.dumps(result)
    return json.dumps({"ok": True, "project": result})


async def _handle_update_project_status(args: dict, user_id: int) -> str:
    """Update a project's status and optional stopped reason."""
    group_id = args.pop("_group_id", None)
    project = _resolve_slug(user_id, args["slug"], group_id=group_id)
    if project is None:
        return _project_not_found()
    status = args["status"]
    if status not in PROJECT_STATUSES:
        return json.dumps({"ok": False, "error": f"Estado inválido: {status}"})
    stopped_reason = args.get("stopped_reason")

    # Refuse to reactivate an urgent project beyond the 3-urgent limit.
    if (
        project["priority"] == "urgente"
        and project["status"] not in ACTIVE_STATUSES
        and status in ACTIVE_STATUSES
    ):
        can, existing = can_activate_urgent(project["user_id"], project["id"])
        if not can:
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "No se puede activar el proyecto: el dueño ya tiene 3 "
                        "proyectos urgentes activos. Baja la prioridad de uno "
                        "antes de activarlo."
                    ),
                    "existing": existing,
                },
                ensure_ascii=False,
            )

    updated = update_project_status(project["id"], status, stopped_reason)
    if not updated:
        return json.dumps({"ok": False, "error": "No se pudo actualizar el estado"})

    result_project = get_project_for_user(user_id, project["slug"], group_id=group_id)
    response: dict = {"ok": True, "project": result_project}

    # Include notification metadata for group projects
    if result_project and result_project.get("group_id"):
        response["_notify"] = {
            "type": "status",
            "project": result_project,
            "actor_id": user_id,
            "detail": f" a {status.replace('_', ' ')}",
        }

    return json.dumps(response, ensure_ascii=False)


async def _handle_update_project_priority(args: dict, user_id: int) -> str:
    """Update a project's priority, enforcing the urgent-project limit."""
    group_id = args.pop("_group_id", None)
    project = _resolve_slug(user_id, args["slug"], group_id=group_id)
    if project is None:
        return _project_not_found()
    priority = args.get("priority")
    if priority not in PROJECT_PRIORITIES:
        return json.dumps({"ok": False, "error": f"Prioridad inválida: {priority}"})
    result = update_project_priority(project["id"], priority)

    # Include notification metadata for group projects
    if result.get("ok") and project.get("group_id"):
        result["_notify"] = {
            "type": "priority",
            "project": project,
            "actor_id": user_id,
            "detail": f" a {priority}",
        }

    return json.dumps(result, ensure_ascii=False)


async def _handle_update_project_description(args: dict, user_id: int) -> str:
    """Update a project's description."""
    group_id = args.pop("_group_id", None)
    project = _resolve_slug(user_id, args["slug"], group_id=group_id)
    if project is None:
        return _project_not_found()
    updated = update_project_description(project["id"], args["description"])
    if not updated:
        return json.dumps(
            {"ok": False, "error": "No se pudo actualizar la descripción"}
        )
    return json.dumps(
        {"ok": True, "project": get_project_for_user(user_id, project["slug"], group_id=group_id)}
    )


async def _handle_add_project_note(args: dict, user_id: int) -> str:
    """Add a note to a project."""
    group_id = args.pop("_group_id", None)
    project = _resolve_slug(user_id, args["slug"], group_id=group_id)
    if project is None:
        return _project_not_found()
    note = add_project_note(
        project["id"], user_id, args["summary"], args.get("content", "")
    )
    result: dict = {"ok": note is not None, "note": note}

    # Include notification metadata for group projects
    if note and project.get("group_id"):
        result["_notify"] = {
            "type": "note",
            "project": project,
            "actor_id": user_id,
        }

    return json.dumps(result, ensure_ascii=False)


async def _handle_list_projects(args: dict, user_id: int) -> str:
    """List the user's projects (owned + shared with them), optionally filtered.
    
    When ``_group_id`` is provided (group context), only projects associated
    with that group are returned. The ``scope`` parameter controls which
    projects are listed: "all" (default), "owned", "shared_with_me",
    or "shared_by_me".
    """
    group_id = args.pop("_group_id", None)
    status_filter = args.get("status_filter")
    scope = args.get("scope", "all")
    
    if group_id is not None:
        # Group context: only show projects associated with this group
        projects = get_shared_projects_in_group(group_id)
        if status_filter:
            projects = [p for p in projects if p["status"] == status_filter]
    else:
        # Private context: show projects based on scope
        if status_filter and status_filter not in PROJECT_STATUSES:
            return json.dumps({"ok": False, "error": f"Estado inválido: {status_filter}"})

        if scope == "owned":
            projects = [{**p, "role": "owner"} for p in list_projects(user_id, status_filter)]
        elif scope == "shared_with_me":
            projects = [
                {**p, "role": "member"}
                for p in list_shared_with_me(user_id)
                if not status_filter or p["status"] == status_filter
            ]
        elif scope == "shared_by_me":
            projects = [
                {**p, "role": "owner"}
                for p in list_shared_by_me(user_id)
                if not status_filter or p["status"] == status_filter
            ]
        else:
            # Default: "all" — owned + shared + group
            projects = _list_visible_projects(user_id, status_filter)
    
    formatted = _format_projects_list(projects)
    return json.dumps(
        {"ok": True, "projects": projects, "formatted": formatted},
        ensure_ascii=False,
    )


def _list_visible_projects(
    user_id: int, status_filter: str | None = None
) -> list[dict]:
    """Merge owned, shared-with-me, and group projects, deduplicated by project id.

    Owned projects come from list_projects() (scoped to projects.user_id),
    shared ones from list_shared_with_me() (membership rows), and group
    projects from list_group_projects_for_user() (projects where the user
    is a member or owner in any group). Each project carries a ``role`` key
    — "owner", "member", or "group" — so formatters can annotate them.
    On a duplicate id, the owned entry wins.
    """
    owned = [
        {**project, "role": "owner"}
        for project in list_projects(user_id, status_filter)
    ]
    shared = [
        {**project, "role": "member"}
        for project in list_shared_with_me(user_id)
        if not status_filter or project["status"] == status_filter
    ]
    group = [
        {**project, "role": "group"}
        for project in list_group_projects_for_user(user_id)
        if not status_filter or project["status"] == status_filter
    ]
    merged: dict[int, dict] = {}
    for project in owned + shared + group:
        merged[project["id"]] = project
    return list(merged.values())


async def _handle_search_projects(args: dict, user_id: int) -> str:
    """Search projects by name or description.
    
    When ``_group_id`` is provided (group context), only projects associated
    with that group are searched.
    """
    group_id = args.pop("_group_id", None)
    query = args["query"]
    
    if group_id is not None:
        # Group context: search only within group projects
        all_group_projects = get_shared_projects_in_group(group_id)
        pattern = query.lower()
        results = [
            p for p in all_group_projects
            if pattern in p["name"].lower() or pattern in (p.get("description") or "").lower()
        ]
    else:
        # Private context: search all visible projects
        results = search_projects(user_id, query)
    
    formatted = _format_projects_list(results)
    return json.dumps(
        {"ok": True, "projects": results, "formatted": formatted},
        ensure_ascii=False,
    )


async def _handle_get_project(args: dict, user_id: int) -> str:
    """Get full project details, notes and Markdown files."""
    group_id = args.pop("_group_id", None)
    project = _resolve_slug(user_id, args["slug"], group_id=group_id)
    if project is None:
        return _project_not_found()
    notes = get_project_notes(project["id"])
    # Files live under the owner's directory; for a member, project["user_id"]
    # is the owner (not the caller), so the path always resolves correctly.
    files = read_project_files(project["user_id"], project["slug"])
    return json.dumps(
        {"ok": True, "project": project, "notes": notes, "files": files}
    )


async def _handle_share_project(args: dict, user_id: int) -> str:
    """Share a project with another user by @username; owner only."""
    group_id = args.pop("_group_id", None)
    project = _resolve_slug(user_id, args["slug"], group_id=group_id)
    if project is None:
        return _project_not_found()
    if not is_owner(user_id, project["id"]):
        return json.dumps(
            {"ok": False, "error": "Solo el dueño del proyecto puede compartirlo"}
        )
    guest_id = resolve_username(args["username"])
    if guest_id is None:
        return json.dumps(
            {
                "ok": False,
                "error": "Usuario no encontrado. El invitado debe haber "
                "hablado conmigo al menos una vez.",
            }
        )
    if guest_id == user_id:
        return json.dumps(
            {"ok": False, "error": "No puedes compartir un proyecto contigo mismo"}
        )
    result = add_project_member(project["id"], guest_id, user_id)
    return json.dumps(result, ensure_ascii=False)


async def _handle_unshare_project(args: dict, user_id: int) -> str:
    """Revoke a collaborator's access to a project; owner only."""
    group_id = args.pop("_group_id", None)
    project = _resolve_slug(user_id, args["slug"], group_id=group_id)
    if project is None:
        return _project_not_found()
    if not is_owner(user_id, project["id"]):
        return json.dumps(
            {"ok": False, "error": "Solo el dueño del proyecto puede quitar acceso"}
        )
    guest_id = resolve_username(args["username"])
    if guest_id is None:
        return json.dumps(
            {
                "ok": False,
                "error": "Usuario no encontrado. El invitado debe haber "
                "hablado conmigo al menos una vez.",
            }
        )
    if guest_id == project["user_id"]:
        return json.dumps(
            {"ok": False, "error": "No puedes quitar el acceso al dueño del proyecto"}
        )
    removed = remove_project_member(project["id"], guest_id, user_id)
    if not removed.get("ok"):
        return json.dumps(removed, ensure_ascii=False)
    return json.dumps({"ok": True})


async def _handle_list_project_members(args: dict, user_id: int) -> str:
    """List a project's collaborators (invitees) with their @username."""
    group_id = args.pop("_group_id", None)
    project = _resolve_slug(user_id, args["slug"], group_id=group_id)
    if project is None:
        return _project_not_found()
    members = list_project_members(project["id"])
    lines = [
        f"• @{member['username']}"
        if member.get("username")
        else f"• {member.get('first_name') or member['user_id']}"
        for member in members
    ]
    formatted = "\n".join(lines) if lines else "Este proyecto no tiene colaboradores."
    return json.dumps(
        {
            "ok": True,
            "members": members,
            "formatted": formatted,
            "project": {"name": project["name"]},
        },
        ensure_ascii=False,
    )


async def _handle_delete_project(args: dict, user_id: int) -> str:
    """Request confirmation to delete an archived project; owner only.

    Deletion is destructive and irreversible, so the tool never deletes
    directly: it returns a payload flagging ``confirmation_required`` and
    the handlers layer renders the inline confirm/cancel buttons. The
    actual deletion happens in ``delete_project_callback`` (project_commands)
    after the user taps "Sí, eliminar", re-validating owner and archived
    status. Ownership and the archived status are still checked here at
    the UI layer so only a valid target ever reaches the confirmation
    step. The user_id always comes from the Telegram update (never from
    the model args), per the ACL rules.
    """
    group_id = args.pop("_group_id", None)
    project = _resolve_slug(user_id, args["slug"], group_id=group_id)
    if project is None:
        return _project_not_found()
    if not is_owner(user_id, project["id"]):
        return json.dumps(
            {"ok": False, "error": "Solo el dueño del proyecto puede eliminarlo"},
            ensure_ascii=False,
        )
    if project["status"] != "archivado":
        return json.dumps(
            {
                "ok": False,
                "error": "Solo puedes eliminar un proyecto archivado. "
                "Archívalo primero con update_project_status.",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": False,
            "confirmation_required": True,
            "project": {
                "id": project["id"],
                "name": project["name"],
                "slug": project["slug"],
            },
            "error": "Se requiere confirmación del usuario antes de eliminar el proyecto.",
        },
        ensure_ascii=False,
    )


# Valid journal topic prefixes (soft-validated: an unknown topic is dropped).
_JOURNAL_TOPICS = frozenset(
    {
        "salud",
        "finanzas",
        "viajes",
        "familia",
        "trabajo",
        "animo",
        "pendientes",
        "espiritualidad",
        "ocio",
        "aprendizaje",
        "eventos",
    }
)


def _guess_memory_level(period: str) -> str | None:
    """Guess the journal level from a period key's shape, or None.

    Mirrors memory.py's period formats: day YYYY-MM-DD, ISO week
    YYYY-Www, quarter YYYY-Q#, year YYYY. This is a shape-only check;
    memory.py re-validates calendar validity when the content is read.
    """
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
        return "day"
    if re.fullmatch(r"\d{4}-W\d{2}", period):
        return "weekly"
    if re.fullmatch(r"\d{4}-Q[1-4]", period):
        return "quarterly"
    if re.fullmatch(r"\d{4}", period):
        return "yearly"
    return None


async def _handle_journal_append(args: dict, user_id: int) -> str:
    """Append one entry to the user's long-term memory journal for today.

    In group context writes to group's shared journal (group_{id}/) with
    author tag ``@username: text`` for traceability.
    """
    content = (args.get("content") or "").strip()
    if not content:
        return json.dumps(
            {"ok": False, "error": "El contenido no puede estar vacío"}
        )
    topic = (args.get("topic") or "").strip()
    if topic and topic not in _JOURNAL_TOPICS:
        topic = ""
    group_id = args.pop("_group_id", None)
    author = args.pop("_author_username", None)
    settings = load_settings()
    day = _media_day(settings)
    text = f"[{topic}] {content}" if topic else content
    if group_id is not None:
        result = append_raw_entry(
            group_id=group_id, day=day, text=text, ts=time.time(), tz_name=settings.timezone, author_username=author
        )
        msg = "Guardado en la bitácora del grupo."
    else:
        result = append_raw_entry(
            user_id, day, text, ts=time.time(), tz_name=settings.timezone
        )
        msg = "Guardado en tu bitácora del día."
    if result is None:
        return json.dumps({"ok": False, "error": "No se pudo guardar en la bitácora"})
    return json.dumps(
        {
            "ok": True,
            "day": day,
            "entry_count": result["entry_count"],
            "word_count": result["raw_word_count"],
            "message": msg,
        },
        ensure_ascii=False,
    )


async def _handle_memory_read(args: dict, user_id: int) -> str:
    """Read a stored day or summary from the user's journal (model-free).

    Returns exactly what is stored: the daily compact preferred for a day
    (raw log as fallback) and the Markdown summary for weekly/quarterly/
    yearly periods. Never generates content — a missing result carries a
    ``note`` so the model can decide whether to answer or explore another
    period. user_id comes from the Telegram update, never the model args.
    """
    period = (args.get("period") or "").strip()
    if not period:
        return json.dumps({"ok": False, "error": "El periodo es obligatorio"})
    level = (args.get("level") or "auto").strip()
    if level == "auto":
        level = _guess_memory_level(period)
        if level is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "No pude identificar el periodo: usa un día (YYYY-MM-DD), "
                        "semana (YYYY-Www), trimestre (YYYY-Q#) o año (YYYY)"
                    ),
                },
                ensure_ascii=False,
            )

    group_id = args.pop("_group_id", None)
    args.pop("_author_username", None)
    try:
        if level == "day":
            if group_id is not None:
                content = get_daily_compact(group_id=group_id, day=period)
                if not content.strip():
                    content = get_raw_content(group_id=group_id, day=period)
            else:
                content = get_daily_compact(user_id, period)
                if not content.strip():
                    content = get_raw_content(user_id, period)
            note = "No hay entradas para ese día"
        elif level == "raw":
            if group_id is not None:
                content = get_raw_content(group_id=group_id, day=period)
            else:
                content = get_raw_content(user_id, period)
            note = "No hay entradas para ese día"
        elif level == "daily":
            if group_id is not None:
                content = get_daily_compact(group_id=group_id, day=period)
            else:
                content = get_daily_compact(user_id, period)
            note = "No hay entradas para ese día"
        elif level in ("weekly", "quarterly", "yearly"):
            if group_id is not None:
                content = get_summary_content(group_id=group_id, level=level, period=period)
            else:
                content = get_summary_content(user_id, level, period)
            note = "Todavía no existe un resumen para ese periodo"
        else:
            return json.dumps({"ok": False, "error": f"Nivel desconocido: {level}"})
    except ValueError:
        return json.dumps(
            {"ok": False, "error": f"Periodo inválido: {period}"},
            ensure_ascii=False,
        )

    payload = {"ok": True, "level": level, "period": period, "content": content}
    if not content.strip():
        payload["note"] = note
    return json.dumps(payload, ensure_ascii=False)


# Spanish stopwords for recall keyword scoring. The set deliberately includes
# both accented and unaccented variants of common words (ultimo/último…) and
# question words, so queries like "último viaje a Panamá" reduce to the
# meaningful token "panamá" and match summaries written either way.
_RECALL_STOPWORDS = frozenset(
    {
        "el", "la", "los", "las", "de", "del", "que", "y", "a", "en", "un",
        "una", "con", "por", "para", "mi", "mis", "tu", "se", "su", "me",
        "te", "le", "lo", "es", "fue", "hace", "cuanto", "cuándo", "cuando",
        "como", "cual", "donde", "hay", "tiene", "viaje", "viajar", "ultimo",
        "última", "ultima", "vez", "dia", "día", "semana", "año", "meses",
        "recuerdas", "recuerda", "recuerdame", "busque", "buscar", "paso",
        "pasó", "comio", "comió",
    }
)


def _keyword_hits(query: str, text: str) -> int:
    """Return how many meaningful query tokens appear in ``text``.

    Pure and deterministic: the query is lower-cased, split into word tokens,
    filtered through _RECALL_STOPWORDS (keeping tokens of length >= 4) and each
    surviving token is searched as a case-insensitive substring of ``text``.
    A query like "pagué la luz" scores against content mentioning either
    "pagué" or "luz" without requiring an exact phrase match.
    """
    tokens = {
        token
        for token in re.findall(r"\w+", query.lower())
        if token not in _RECALL_STOPWORDS and len(token) >= 4
    }
    if not tokens:
        return 0
    haystack = text.lower()
    return sum(1 for token in tokens if token in haystack)


def _validated_day_arg(value: object) -> str | None:
    """Return ``value`` as YYYY-MM-DD when valid, else None.

    The model may hallucinate a date format, so a malformed range bound is
    silently dropped rather than failing the whole recall.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value.strip()).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _child_periods(level: str, period: str) -> list[str]:
    """Return the child period keys of ``period`` for the next level down.

    Pointers let the model navigate top-down (year → quarter → week → day)
    by calling memory_read on a child period when it needs the exact detail.
    A malformed stored period yields [] instead of raising, so one bad row
    never aborts a recall.
    """
    expanders = {
        "yearly": quarters_in_year,
        "quarterly": weeks_in_quarter,
        "weekly": days_in_week,
    }
    expand = expanders.get(level)
    if expand is None:
        return []
    try:
        return expand(period)
    except ValueError:
        return []


def _range_epoch_bounds(
    date_from: str | None, date_to: str | None, tz: ZoneInfo
) -> tuple[float | None, float | None]:
    """Convert a day range to epoch [start, end), or (None, None) when open.

    A summary overlaps the range when its stored [range_start, range_end]
    intersects these bounds; an open range (no bounds) matches everything.
    """
    if date_from is None and date_to is None:
        return None, None
    start = day_range(date_from, tz)[0] if date_from else None
    end = day_range(date_to, tz)[1] if date_to else None
    return start, end


def _summary_overlaps(
    summary: dict, start_epoch: float | None, end_epoch: float | None
) -> bool:
    """True when the summary's stored range intersects [start, end)."""
    if start_epoch is not None and summary["range_end"] < start_epoch:
        return False
    if end_epoch is not None and summary["range_start"] > end_epoch:
        return False
    return True


def _recall_from_summaries(
    user_id: int | None = None,
    query: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    tz: ZoneInfo | None = None,
    group_id: int | None = None,
) -> list[dict]:
    """Score the top-down summary levels and return one best match per level.

    With a date range, candidates are the summaries overlapping it; without
    one, only the most recent summary of each level (last by range_end) is
    scored — the summaries are indexes, so recall stays cheap by design. The
    best-scoring summary of each level with at least one keyword hit becomes
    a match carrying its content and child-period pointers for navigation.
    """
    start_epoch, end_epoch = _range_epoch_bounds(date_from, date_to, tz)
    matches: list[dict] = []
    for level in ("yearly", "quarterly", "weekly"):
        if group_id is not None:
            summaries = list_summaries(group_id=group_id, level=level)
        else:
            summaries = list_summaries(user_id, level)
        if start_epoch is None and end_epoch is None:
            candidates = summaries[-1:] if summaries else []
        else:
            candidates = [
                s for s in summaries if _summary_overlaps(s, start_epoch, end_epoch)
            ]
        scored: list[tuple[int, dict, str]] = []
        for summary in candidates:
            if group_id is not None:
                content = get_summary_content(group_id=group_id, level=level, period=summary["period"])
            else:
                content = get_summary_content(user_id, level, summary["period"])
            if content.strip():
                scored.append((_keyword_hits(query, content), summary, content))
        if not scored:
            continue
        best_score, best_summary, best_content = max(
            scored, key=lambda item: item[0]
        )
        if best_score <= 0:
            continue
        matches.append(
            {
                "level": level,
                "period": best_summary["period"],
                "content": best_content,
                "score": best_score,
                "pointer": _child_periods(level, best_summary["period"]),
            }
        )
    return matches


def _recall_from_raw(
    user_id: int | None = None,
    query: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    today: datetime.date | None = None,
    group_id: int | None = None,
) -> list[dict]:
    """Search the raw day journal within the range; best 3 days by hits.

    Fallback of memory_recall when no summary matches: days are keyword-scored
    on their raw content and the top 3 are returned as {level: "raw"} matches.
    Without an explicit range, the default window is the last 60 days up to
    today so a recent fact the summarizer has not reached is still findable.
    """
    if date_from is None:
        date_from = (today - datetime.timedelta(days=59)).strftime("%Y-%m-%d")
    if date_to is None:
        date_to = today.strftime("%Y-%m-%d")
    scored: list[tuple[int, dict]] = []
    if group_id is not None:
        entries = get_raw_entries(group_id=group_id, day_from=date_from, day_to=date_to)
    else:
        entries = get_raw_entries(user_id, date_from, date_to)
    for day in entries:
        hits = _keyword_hits(query, day["content"])
        if hits > 0:
            scored.append((hits, day))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        {"level": "raw", "period": day["day"], "content": day["content"], "score": hits}
        for hits, day in scored[:3]
    ]


async def _handle_memory_recall(args: dict, user_id: int) -> str:
    """Search the user's journal top-down: summaries first, raw days as fallback.

    Recall mirrors the memory hierarchy: yearly/quarterly/weekly summaries are
    keyword-scored first (they are indexes over the raw journal), and when none
    matches the search falls back to the raw day files within the requested
    range. Only the user's own memory is read — user_id always comes from the
    Telegram update, never from the model args, keeping per-user isolation.
    """
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps(
            {"ok": False, "error": "La búsqueda necesita un query"},
            ensure_ascii=False,
        )
    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    date_from = _validated_day_arg(args.get("date_from"))
    date_to = _validated_day_arg(args.get("date_to"))

    group_id = args.pop("_group_id", None)
    args.pop("_author_username", None)
    matches = _recall_from_summaries(user_id, query, date_from, date_to, tz, group_id=group_id)
    if matches:
        return json.dumps(
            {
                "ok": True,
                "matches": matches,
                "hint": (
                    "Si necesitas el detalle exacto, usa memory_read con "
                    "el periodo indicado."
                ),
            },
            ensure_ascii=False,
        )

    today = datetime.datetime.now(tz).date()
    raw_matches = _recall_from_raw(user_id, query, date_from, date_to, today, group_id=group_id)
    if raw_matches:
        return json.dumps({"ok": True, "matches": raw_matches}, ensure_ascii=False)

    return json.dumps(
        {
            "ok": True,
            "matches": [],
            "note": (
                "No encontré nada en la bitácora para esa búsqueda. "
                "Si ocurrió recientemente, cuéntamelo y lo guardo."
            ),
        },
        ensure_ascii=False,
    )


async def _handle_memory_insights(args: dict, user_id: int) -> str:
    """Return recent raw days plus the current week's summary for context.

    Feeds the proactive engine and habit questions ("¿he hecho ejercicio esta
    semana?"): the last ``days`` raw days (each with content and word counts)
    plus the current ISO week key and its summary, which may be empty when the
    weekly summary has not been generated yet. Purely journal-based — pending
    reminders belong to another module and are deliberately not mixed in here.
    """
    settings = load_settings()
    tz = ZoneInfo(settings.timezone)
    try:
        days_back = int(args.get("days") or 7)
    except (TypeError, ValueError):
        days_back = 7
    days_back = min(max(days_back, 1), 30)

    group_id = args.pop("_group_id", None)
    args.pop("_author_username", None)
    today = _media_day(settings)
    week = week_key(datetime.datetime.now(tz))
    day_from = (
        datetime.date.fromisoformat(today) - datetime.timedelta(days=days_back - 1)
    ).strftime("%Y-%m-%d")
    if group_id is not None:
        entries_src = get_raw_entries(group_id=group_id, day_from=day_from, day_to=today)
        week_summary = get_summary_content(group_id=group_id, level="weekly", period=week)
    else:
        entries_src = get_raw_entries(user_id, day_from, today)
        week_summary = get_summary_content(user_id, "weekly", week)
    days = [
        {
            "day": entry["day"],
            "word_count": entry["raw_word_count"],
            "entry_count": entry["entry_count"],
            "content": entry["content"],
        }
        for entry in entries_src
    ]
    return json.dumps(
        {
            "ok": True,
            "days": days,
            "week": week,
            "week_summary": week_summary,
        },
        ensure_ascii=False,
    )


async def _handle_set_checkin_time(args: dict, user_id: int) -> str:
    """Set the user's morning/evening check-in time, or an error JSON.

    The user_id always comes from the Telegram update (never from model
    args). The proactive module validates the period and the HH:MM format
    and returns a Spanish error the model can relay; on success the
    response echoes the current times so the model can confirm the change.
    """
    period = (args.get("period") or "").strip().lower()
    hhmm = (args.get("time") or "").strip()
    return json.dumps(set_checkin_time_store(user_id, period, hhmm), ensure_ascii=False)


async def execute_tool(
    name: str,
    args: dict,
    user_id: int,
    group_id: int | None = None,
    private_chat_id: int | None = None,
) -> str:
    """Execute a tool and return a JSON result string for the AI.

    Any unexpected exception is logged with its full detail and converted
    into a generic Spanish error, so the model never sees internal error
    text (file paths, SQLite/SDK internals, class names) that could leak
    to the user. Known validation failures are handled inside each handler
    and return their own friendly JSON; this catch-all is the last safety
    net before the tool loop.

    When ``group_id`` is set (group context), tools are validated against
    GROUP_TOOLS/PRIVATE_TOOLS restrictions, and ``private_chat_id`` is
    injected for tools that need to deliver to the user's private chat.
    """
    # Block private-only tools in group context (curated whitelist)
    if group_id is not None and name in PRIVATE_ONLY_TOOLS:
        return json.dumps(
            {
                "ok": False,
                "error": "Esta acción solo está disponible en chats privados con el bot.",
            },
            ensure_ascii=False,
        )

    # For reminders in group: deliver in group, mark as shared (group_id)
    _REMINDER_TOOLS = {
        "create_reminder",
        "list_reminders",
        "cancel_reminder",
        "edit_reminder",
        "reschedule_reminder",
        "postpone_reminder",
    }
    if group_id is not None and name in _REMINDER_TOOLS:
        # All reminder tools get group scope; create also gets chat_id=group_id
        args["_group_id"] = group_id
        if name == "create_reminder":
            args["_chat_id"] = group_id
            # Track who created it (audit)
            args["_created_by"] = user_id
            # Resolve display name for potential future autor tagging
            try:
                from bot.projects import get_user_profile as _get_profile

                prof = _get_profile(user_id)
                # Prefer username, fallback to first_name, then id
                if prof and prof.get("username"):
                    args["_author_username"] = prof["username"]
                elif prof and prof.get("first_name"):
                    args["_author_username"] = prof["first_name"]
                else:
                    args["_author_username"] = str(user_id)
            except Exception:
                args["_author_username"] = str(user_id)

    # For memory/journal tools in group: inject group scope + author for traceability
    _MEMORY_TOOLS = {"journal_append", "memory_read", "memory_recall", "memory_insights"}
    if group_id is not None and name in _MEMORY_TOOLS:
        args["_group_id"] = group_id
        if name == "journal_append":
            try:
                from bot.projects import get_user_profile as _get_profile2

                prof = _get_profile2(user_id)
                if prof and prof.get("username"):
                    args["_author_username"] = prof["username"]
                elif prof and prof.get("first_name"):
                    args["_author_username"] = prof["first_name"]
                else:
                    args["_author_username"] = str(user_id)
            except Exception:
                args["_author_username"] = str(user_id)

    # For project tools in group, inject group scope
    _PROJECT_TOOLS = {
        "create_project",
        "update_project_status",
        "update_project_priority",
        "update_project_description",
        "add_project_note",
        "list_projects",
        "search_projects",
        "get_project",
        "share_project",
        "unshare_project",
        "list_project_members",
        "delete_project",
    }
    if group_id is not None and name in _PROJECT_TOOLS:
        logger.info("Injecting group_id=%s for %s", group_id, name)
        args["_group_id"] = group_id

    try:
        return await _dispatch(name, args, user_id)
    except Exception as exc:  # noqa: BLE001 - never crash the tool loop
        logger.error("Tool %r failed for user %d: %s", name, user_id, exc)
        return json.dumps(
            {
                "ok": False,
                "error": "No pude completar la acción. Inténtalo de nuevo.",
            },
            ensure_ascii=False,
        )


async def _handle_create_reminder(args: dict, user_id: int) -> str:
    """Schedule a reminder for the user from the model's parsed datetime."""
    group_id = args.pop("_group_id", None)
    created_by = args.pop("_created_by", None)
    # Use _chat_id if provided (from group context), otherwise get from user's chat
    chat_id = args.pop("_chat_id", None) or get_chat_for_user(user_id)
    if chat_id is None:
        return json.dumps(
            {"ok": False, "error": "no chat associated with this user"}
        )
    dt_str = args.get("datetime")
    if not dt_str:
        return json.dumps(
            {"ok": False, "error": "datetime is required"}
        )
    remind_at = _parse_iso_datetime(dt_str)
    if remind_at is None:
        return json.dumps(
            {"ok": False, "error": f"could not parse datetime: {dt_str}"}
        )
    recurrence = args.get("recurrence", "none")
    if recurrence not in RECURRENCE_VALUES:
        recurrence = "none"
    # Group scope: chat_id already equals group_id, pass group_id
    if group_id is not None:
        reminder = create_reminder_db(
            user_id, chat_id, args["message"], remind_at, recurrence, group_id=group_id, created_by=created_by or user_id
        )
    else:
        reminder = create_reminder_db(
            user_id, chat_id, args["message"], remind_at, recurrence
        )
    return json.dumps({"ok": True, "reminder": reminder})


async def _handle_list_reminders(args: dict, user_id: int) -> str:
    """List pending reminders with a formatted bullet list (scoped)."""
    group_id = args.pop("_group_id", None)
    # Pop author if injected (unused for listing)
    args.pop("_author_username", None)
    reminders_list = list_pending_reminders(user_id, group_id=group_id)
    formatted = _format_reminders_list(reminders_list)
    return json.dumps(
        {"ok": True, "reminders": reminders_list, "formatted": formatted},
        ensure_ascii=False,
    )


async def _handle_cancel_reminder(args: dict, user_id: int) -> str:
    """Cancel a pending reminder by id (scoped)."""
    group_id = args.pop("_group_id", None)
    args.pop("_author_username", None)
    deleted = delete_reminder(args["reminder_id"], user_id, group_id=group_id)
    if not deleted:
        return json.dumps({"ok": False, "error": "reminder not found or not owned by user"})
    return json.dumps({"ok": True})


async def _handle_edit_reminder(args: dict, user_id: int) -> str:
    """Change a pending reminder's message (scoped)."""
    group_id = args.pop("_group_id", None)
    args.pop("_author_username", None)
    reminder_id = args.get("reminder_id")
    message = (args.get("message") or "").strip()
    if not message:
        return json.dumps({"ok": False, "error": "message is required"})
    updated = update_reminder_text(reminder_id, user_id, message, group_id=group_id)
    if not updated:
        return json.dumps({"ok": False, "error": "reminder not found or not owned by user"})
    return json.dumps({"ok": True, "reminder": {"id": reminder_id, "message": message}})


async def _handle_reschedule_reminder(args: dict, user_id: int) -> str:
    """Move a pending reminder to a different date/time (scoped)."""
    group_id = args.pop("_group_id", None)
    args.pop("_author_username", None)
    dt_str = args.get("datetime")
    if not dt_str:
        return json.dumps({"ok": False, "error": "datetime is required"})
    remind_at = _parse_iso_datetime(dt_str)
    if remind_at is None:
        return json.dumps(
            {"ok": False, "error": f"could not parse datetime: {dt_str}"}
        )
    reminder_id = args.get("reminder_id")
    updated = update_reminder_time(reminder_id, user_id, remind_at, group_id=group_id)
    if not updated:
        return json.dumps({"ok": False, "error": "reminder not found or not owned by user"})
    return json.dumps(
        {"ok": True, "reminder": {"id": reminder_id, "remind_at": remind_at}}
    )


async def _handle_postpone_reminder(args: dict, user_id: int) -> str:
    """Delay a pending reminder by a number of minutes (scoped)."""
    group_id = args.pop("_group_id", None)
    args.pop("_author_username", None)
    reminder_id = args.get("reminder_id")
    minutes = args.get("minutes")
    if not isinstance(minutes, int) or minutes <= 0:
        return json.dumps({"ok": False, "error": "minutes must be a positive integer"})
    updated = postpone_reminder_db(reminder_id, user_id, minutes * 60, group_id=group_id)
    if not updated:
        return json.dumps({"ok": False, "error": "reminder not found or not owned by user"})
    return json.dumps({"ok": True, "minutes": minutes})


async def _handle_web_search(args: dict, user_id: int) -> str:
    """Search the web via DuckDuckGo and return a result payload."""
    result = await web_search(
        query=args.get("query", ""),
        max_results=args.get("max_results", 5),
    )
    return json.dumps(result)


async def _handle_dolar_rate(args: dict, user_id: int) -> str:
    """Return the current official + parallel dollar rates."""
    info = await get_dolar_info()
    return json.dumps(info, ensure_ascii=False)


async def _dispatch(name: str, args: dict, user_id: int) -> str:
    """Route a tool call to the matching handler, or an unknown-tool error.

    Dispatch is table-driven (``_TOOL_HANDLERS`` below) so adding a tool
    is a one-line registration instead of a new if-branch.
    """
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return json.dumps({"ok": False, "error": f"unknown tool: {name}"})
    return await handler(args, user_id)


async def _dispatch_generate_image(args: dict, user_id: int) -> str:
    """Generate an image and return a JSON result carrying the image bytes.

    The result embeds the image as base64 in ``payload.media`` so the
    caller (handlers.py) can forward it to Telegram. If media generation
    is unavailable (no Gemini key) or the daily cap is reached, it
    returns a normal error JSON instead.
    """
    settings = load_settings()
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return json.dumps({"ok": False, "error": "prompt is required"})

    client = build_client_for_provider(settings, "gemini")
    if client is None:
        return json.dumps(
            {"ok": False, "error": "generate_image requires GEMINI_API_KEY"}
        )

    limit = settings.media_image_daily_limit
    day = _media_day(settings)
    if not media_usage.try_increment(user_id, IMAGE_TYPE, day, limit):
        label = "imágenes" if limit != 1 else "imagen"
        return json.dumps(
            {
                "ok": False,
                "error": (
                    f"Has alcanzado el máximo de {label} hoy ({limit}). "
                    "El límite se reinicia mañana."
                ),
            }
        )

    try:
        image_bytes = await generate_image_ai(
            client=client,
            model=settings.gemini_image_model,
            prompt=prompt,
            session_id=f"tg-image-{user_id}",
            provider="gemini",
        )
    except QuotaExhaustedError:
        media_usage.refund_usage(user_id, IMAGE_TYPE, day)
        return json.dumps(
            {
                "ok": False,
                "quota_exhausted": True,
                "error": (
                    "La cuota gratuita diaria de generación de imágenes de "
                    "Gemini se agotó (se reinicia mañana). Puedes probar de "
                    "nuevo mañana o pedir una imagen con /imagina más tarde."
                ),
            },
            ensure_ascii=False,
        )
    except (OpenCodeError, RateLimitError) as exc:
        media_usage.refund_usage(user_id, IMAGE_TYPE, day)
        return json.dumps({"ok": False, "error": str(exc)})

    # The slot was reserved by try_increment; the generated image is now counted.
    return json.dumps(
        {
            "ok": True,
            "media": {
                "type": "image",
                "data_b64": base64.b64encode(image_bytes).decode("ascii"),
                "prompt": prompt,
            },
        },
        ensure_ascii=False,
    )


def _media_day(settings) -> str:
    """Return today's date key (YYYY-MM-DD) in the bot's timezone.

    Thin delegate to ``bot.timeutil.today_key``; kept under this name so
    call sites and tests that patch ``tools._media_day`` stay unchanged.
    """
    return today_key(settings)


# --- Entity tracking tools ---------------------------------------------------


async def _handle_create_entity(args: dict, user_id: int) -> str:
    """Create a new entity for the user.

    Validates ``entity_type`` against ENTITY_TYPES and requires a non-empty
    ``name``. ``alias`` and ``metadata`` are optional. Returns the created
    entity (or existing one on duplicate) as JSON. ``user_id`` always comes
    from the Telegram update, never from model args, preserving per-user
    isolation even when invoked from a group.
    """
    entity_type = (args.get("entity_type") or "").strip()
    name = (args.get("name") or "").strip()
    alias = (args.get("alias") or "").strip() or None
    metadata = args.get("metadata")

    if not entity_type or entity_type not in ENTITY_TYPES:
        return json.dumps(
            {"ok": False, "error": f"Tipo de entidad inválido: {entity_type or '(vacío)'}. Use: {', '.join(ENTITY_TYPES)}"},
            ensure_ascii=False,
        )
    if not name:
        return json.dumps({"ok": False, "error": "El nombre de la entidad es obligatorio"}, ensure_ascii=False)
    if metadata is not None and not isinstance(metadata, dict):
        return json.dumps({"ok": False, "error": "metadata debe ser un objeto"}, ensure_ascii=False)

    entity = create_entity(user_id, entity_type, name, alias=alias, metadata=metadata)
    if not entity:
        return json.dumps({"ok": False, "error": "No se pudo crear la entidad"}, ensure_ascii=False)

    return json.dumps({"ok": True, "entity": entity, "message": "Entidad creada correctamente."}, ensure_ascii=False)


async def _handle_list_entities(args: dict, user_id: int) -> str:
    """List entities for the user, optionally filtered by type."""
    entity_type = (args.get("entity_type") or "").strip() or None
    entities = list_entities(user_id, entity_type=entity_type)

    if not entities:
        return json.dumps(
            {"ok": True, "entities": [], "message": "No tienes entidades registradas."},
            ensure_ascii=False,
        )

    # Format for display
    formatted = []
    for e in entities:
        item = {
            "id": e["id"],
            "type": e["entity_type"],
            "name": e["name"],
            "alias": e.get("alias"),
            "status": e["status"],
        }
        if e.get("metadata"):
            item["metadata"] = e["metadata"]
        formatted.append(item)

    return json.dumps(
        {"ok": True, "entities": formatted, "count": len(formatted)},
        ensure_ascii=False,
    )


async def _handle_update_entity(args: dict, user_id: int) -> str:
    """Update an entity's fields."""
    entity_id = args.get("entity_id")
    if not entity_id:
        return json.dumps({"ok": False, "error": "El ID de la entidad es obligatorio"})

    name = args.get("name")
    alias = args.get("alias")
    metadata = args.get("metadata")
    status = args.get("status")

    entity = update_entity(
        entity_id, user_id, name=name, alias=alias, metadata=metadata, status=status
    )

    if not entity:
        return json.dumps({"ok": False, "error": "Entidad no encontrada o error al actualizar"})

    return json.dumps(
        {"ok": True, "entity": entity, "message": "Entidad actualizada correctamente."},
        ensure_ascii=False,
    )


async def _handle_get_entity_details(args: dict, user_id: int) -> str:
    """Get detailed info about an entity including notes."""
    entity_id = args.get("entity_id")
    if not entity_id:
        return json.dumps({"ok": False, "error": "El ID de la entidad es obligatorio"})

    entity = get_entity_with_notes(entity_id, user_id)

    if not entity:
        return json.dumps({"ok": False, "error": "Entidad no encontrada"})

    return json.dumps(
        {"ok": True, "entity": entity},
        ensure_ascii=False,
    )


async def _handle_add_entity_note(args: dict, user_id: int) -> str:
    """Add a follow-up note to an entity."""
    entity_id = args.get("entity_id")
    content = (args.get("content") or "").strip()

    if not entity_id:
        return json.dumps({"ok": False, "error": "El ID de la entidad es obligatorio"})
    if not content:
        return json.dumps({"ok": False, "error": "El contenido de la nota es obligatorio"})

    note = add_entity_note(entity_id, user_id, content)

    if not note:
        return json.dumps({"ok": False, "error": "Entidad no encontrada o error al guardar"})

    return json.dumps(
        {"ok": True, "note": note, "message": "Nota añadida correctamente."},
        ensure_ascii=False,
    )


async def _handle_search_entities(args: dict, user_id: int) -> str:
    """Search entities by name or alias."""
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"ok": False, "error": "El término de búsqueda es obligatorio"})

    entities = search_entities(user_id, query)

    if not entities:
        return json.dumps(
            {"ok": True, "entities": [], "message": f"No se encontraron entidades para '{query}'."},
            ensure_ascii=False,
        )

    formatted = [
        {"id": e["id"], "type": e["entity_type"], "name": e["name"], "alias": e.get("alias")}
        for e in entities
    ]

    return json.dumps(
        {"ok": True, "entities": formatted, "count": len(formatted)},
        ensure_ascii=False,
    )


async def _handle_archive_entity(args: dict, user_id: int) -> str:
    """Archive an entity (soft-delete)."""
    entity_id = args.get("entity_id")
    if not entity_id:
        return json.dumps({"ok": False, "error": "El ID de la entidad es obligatorio"}, ensure_ascii=False)

    entity = archive_entity(entity_id, user_id)
    if not entity:
        return json.dumps({"ok": False, "error": "Entidad no encontrada"}, ensure_ascii=False)

    return json.dumps(
        {"ok": True, "entity": entity, "message": "Entidad archivada correctamente."},
        ensure_ascii=False,
    )


# Registry mapping tool names to their async handlers. Kept after
# _dispatch_generate_image so every handler is defined before the table
# is evaluated at import time. Adding a tool = one entry here.
_TOOL_HANDLERS: dict[str, Callable[[dict, int], Awaitable[str]]] = {
    "create_project": _handle_create_project,
    "update_project_status": _handle_update_project_status,
    "update_project_priority": _handle_update_project_priority,
    "update_project_description": _handle_update_project_description,
    "add_project_note": _handle_add_project_note,
    "list_projects": _handle_list_projects,
    "search_projects": _handle_search_projects,
    "get_project": _handle_get_project,
    "share_project": _handle_share_project,
    "unshare_project": _handle_unshare_project,
    "list_project_members": _handle_list_project_members,
    "delete_project": _handle_delete_project,
    "create_reminder": _handle_create_reminder,
    "list_reminders": _handle_list_reminders,
    "cancel_reminder": _handle_cancel_reminder,
    "edit_reminder": _handle_edit_reminder,
    "reschedule_reminder": _handle_reschedule_reminder,
    "postpone_reminder": _handle_postpone_reminder,
    "web_search": _handle_web_search,
    "dolar_rate": _handle_dolar_rate,
    "generate_image": _dispatch_generate_image,
    "journal_append": _handle_journal_append,
    "memory_read": _handle_memory_read,
    "memory_recall": _handle_memory_recall,
    "memory_insights": _handle_memory_insights,
    "set_checkin_time": _handle_set_checkin_time,
    "create_entity": _handle_create_entity,
    "list_entities": _handle_list_entities,
    "update_entity": _handle_update_entity,
    "get_entity_details": _handle_get_entity_details,
    "add_entity_note": _handle_add_entity_note,
    "search_entities": _handle_search_entities,
    "archive_entity": _handle_archive_entity,
}


def _format_reminders_list(reminders_list: list[dict]) -> str:
    """Format pending reminders as a simple bullet list for the AI.

    Returns an empty string when there are no reminders. The model is
    told to return this text verbatim instead of re-rendering it as a
    Markdown table (Telegram does not align tables well).
    """
    if not reminders_list:
        return "No tienes recordatorios pendientes."

    settings = load_settings()
    lines = [
        f"🔔 Tienes {len(reminders_list)} recordatorio(s):",
    ]
    for r in reminders_list:
        when = datetime.datetime.fromtimestamp(
            r["remind_at"], ZoneInfo(settings.timezone)
        ).strftime("%d/%m/%Y a las %H:%M")
        recurrence = r.get("recurrence", "none")
        if recurrence in RECURRENCE_LABELS and recurrence != "none":
            lines.append(
                f"• [ID {r['id']}] {r['message']} — "
                f"{RECURRENCE_LABELS[recurrence]}, próximo: {when}"
            )
        else:
            lines.append(f"• [ID {r['id']}] {r['message']} — {when}")
    return "\n".join(lines)


def _get_owner_name(user_id: int) -> str:
    """Return a display name for a project owner (shared project context).

    Prefers @username, falls back to first name, then a generic label.
    """
    from bot.projects import get_user_profile

    profile = get_user_profile(user_id)
    if profile and profile.get("username"):
        return f"@{profile['username']}"
    if profile and profile.get("first_name"):
        return profile["first_name"]
    return "desconocido"


_PROJECT_STATUS_EMOJI = {
    "sin_iniciar": "⚪",
    "en_proceso": "🟢",
    "parado": "⏸️",
    "completado": "✅",
    "archivado": "📦",
}

_PROJECT_PRIORITY_LABEL = {
    "baja": "Baja",
    "media": "Media",
    "alta": "Alta",
    "urgente": "Urgente",
}


def _format_projects_list(projects: list[dict]) -> str:
    """Format projects as a grouped bullet list for the AI.

    Groups by status with an emoji header and lists each project as a
    bullet. The model is told to return this text verbatim instead of
    re-rendering it as a Markdown table (Telegram does not align tables
    well). Returns an empty-list message when there are no projects.
    """
    if not projects:
        return "No tienes proyectos."

    lines = [f"📂 Tienes {len(projects)} proyecto(s):"]
    for status in PROJECT_STATUSES:
        group = [p for p in projects if p["status"] == status]
        if not group:
            continue
        emoji = _PROJECT_STATUS_EMOJI.get(status, "•")
        label = status.replace("_", " ").capitalize()
        lines.append("")
        lines.append(f"{emoji} {label} ({len(group)}):")
        for project in group:
            priority = _PROJECT_PRIORITY_LABEL.get(
                project.get("priority", "media"), "Media"
            )
            marker = ""
            if project.get("role") == "member":
                owner_id = project.get("owner_id", project.get("user_id"))
                owner_name = _get_owner_name(owner_id)
                marker = f" (compartido por {owner_name})"
            elif project.get("role") == "group":
                marker = " (grupo)"
            lines.append(f"• {project['name']} — Prioridad: {priority}{marker}")
    return "\n".join(lines)


def _parse_iso_datetime(dt_str: str) -> float | None:
    """Parse "YYYY-MM-DDTHH:MM" (naive, in the bot's timezone) to epoch."""
    import datetime as _dt
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from bot.config import load_settings

    try:
        tz_name = load_settings().timezone
        naive = _dt.datetime.fromisoformat(dt_str)
        aware = naive.replace(tzinfo=ZoneInfo(tz_name))
        return aware.timestamp()
    except (ValueError, ZoneInfoNotFoundError):
        return None
