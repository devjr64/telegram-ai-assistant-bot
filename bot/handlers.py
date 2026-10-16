"""Core Telegram command handlers and AI chat logic.

User-facing messages are in Spanish (the bot's users are in Venezuela);
the code itself is in English.
"""

import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.timeutil import today_key
from bot.access import is_authorized
from bot.ai_client import (
    chat_completion_with_tools,
    parse_reminder_datetime,
    build_client_for_provider,
    OpenCodeError,
    RateLimitError,
)
from bot.history import add_message, get_history, get_history_with_timestamps, clear_history
from bot.projects import (
    upsert_user,
    add_group_member,
    get_shared_projects_in_group,
    get_private_chat_id,
    cleanup_group_data,
    get_project_owner_name,
)
from bot.reminders import (
    upsert_user_chat,
    get_reminder_by_id,
    create_reminder,
    get_due_reminders,
    mark_reminder_sent,
    next_occurrence,
    reschedule_reminder,
    schedule_retry,
    reset_retry,
    list_pending_reminders,
    delete_reminder,
    update_reminder_text,
    update_reminder_time,
    postpone_reminder,
    RECURRENCE_VALUES,
    RECURRENCE_LABELS,
)
from bot.tools import TOOL_DEFINITIONS, execute_tool, _filter_tool_definitions
from bot.dolar import get_dolar_info, format_bs
from bot.ai_client import (
    transcribe_audio,
    describe_image,
    generate_image,
)
from bot.media import (
    download_file,
    ogg_to_wav,
    resize_image,
    encode_base64,
)
from bot import media_usage
from bot.media_usage import IMAGE_TYPE, AUDIO_TYPE

logger = logging.getLogger(__name__)

# Per-user rate limiting: {user_id: last_request_timestamp}
_rate_limit_tracker: dict[int, float] = {}

# Bot start time for /info
_start_time = time.time()


def _authorized(update: Update) -> bool:
    """Return True when the user may use the bot."""
    return is_authorized(update.effective_user.id)


def _active_provider(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Return the currently active AI provider (opencode | gemini)."""
    settings: Settings = context.bot_data["settings"]
    return context.bot_data.get("provider", settings.ai_provider)


def _active_model(settings: Settings, provider: str) -> str:
    """Return the model name for the given provider."""
    if provider == "gemini":
        return settings.gemini_model
    return settings.model


def _is_rate_limited(user_id: int, settings: Settings) -> bool:
    """Return True when the user must wait before another request.

    Also records the current timestamp when the request is allowed, so
    the shared tracker works for text, voice, and photo handlers.
    """
    now = time.time()
    last_request = _rate_limit_tracker.get(user_id, 0)
    if now - last_request < settings.rate_limit_seconds:
        return True
    _rate_limit_tracker[user_id] = now
    return False


def _format_history_for_model(
    history_with_ts: list[dict], settings: Settings
) -> list[dict[str, str]]:
    """Return history with per-message local timestamps for the model.

    Each entry ``{role, content, created_at}`` becomes
    ``{role, content: "[YYYY-MM-DD HH:MM] content"}`` in the user's
    timezone (``settings.timezone``), so the model can infer gaps like
    10 days of silence. Falls back to UTC on unknown timezone. Pure
    function — no IO, only formatting.
    """
    if not history_with_ts:
        return []
    # Resolve timezone once (fallback to UTC if misconfigured)
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    formatted: list[dict[str, str]] = []
    for msg in history_with_ts:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        ts = msg.get("created_at")
        if ts is not None:
            try:
                local = datetime.fromtimestamp(float(ts), tz)
                prefix = local.strftime("%Y-%m-%d %H:%M")
                content = f"[{prefix}] {content}"
            except Exception:
                # On bad timestamp, keep original content (never break the loop)
                pass
        formatted.append({"role": role, "content": content})
    return formatted


def _media_day(settings: Settings) -> str:
    """Return today's date key (YYYY-MM-DD) in the bot's timezone."""
    return today_key(settings)


def _media_processor(context: ContextTypes.DEFAULT_TYPE):
    """Return the (client, model) for Gemini media processing.

    Media is always handled by Gemini (it supports audio + vision even
    when the chat provider is opencode). Returns (None, None) when the
    GEMINI_API_KEY is not configured.
    """
    settings: Settings = context.bot_data["settings"]
    client = build_client_for_provider(settings, "gemini")
    if client is None:
        return None, None
    return client, settings.gemini_media_model


def _media_limit(settings: Settings, media_type: str) -> int:
    """Return the daily cap for a media type (0 = disabled)."""
    if media_type == IMAGE_TYPE:
        return settings.media_image_daily_limit
    return settings.media_audio_daily_limit


def _usage_note(
    user_id: int, media_type: str, settings: Settings
) -> str:
    """Return a short 'X of Y left today' note when the cap is near."""
    limit = _media_limit(settings, media_type)
    if limit <= 0:
        return ""
    remaining = media_usage.remaining(
        user_id, media_type, _media_day(settings), limit
    )
    if remaining <= 3:
        label = "imágenes" if media_type == IMAGE_TYPE else "notas de voz"
        return f"\n\n_Te quedan {remaining} {label} hoy de {limit}._"
    return ""


def _escape_markdown(text: str) -> str:
    """Escape characters that would break Telegram's legacy Markdown."""
    for char in ("_", "*", "`", "["):
        text = text.replace(char, f"\\{char}")
    return text


async def _reply_markdown_safe(message, text: str, parse_mode=ParseMode.MARKDOWN) -> None:
    """Reply with Markdown, falling back to plain text on malformed entities.

    The AI model sometimes emits unbalanced ``*``/``_`` which Telegram
    rejects with BadRequest ("can't find end of the entity"). Retrying
    without parse_mode guarantees the user still receives the answer.
    """
    try:
        await message.reply_text(text, parse_mode=parse_mode)
    except BadRequest:
        await message.reply_text(text)


async def _send_markdown_safe(send, chat_id: int, text: str) -> None:
    """Send a message with Markdown, falling back to plain text.

    ``send`` is an async callable ``(chat_id, text, **kwargs) -> ...``.
    Used by proactive messages (reminders) whose text may
    embed user content with malformed Markdown.
    """
    try:
        await send(chat_id, text, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        await send(chat_id, text)


async def _deny(update: Update) -> None:
    """Send the polite unauthorized message."""
    await update.message.reply_text(
        "Lo siento, no estás autorizado para usar este bot."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — welcome message."""
    if not _authorized(update):
        await _deny(update)
        return

    # Record the profile so @username project sharing can resolve this user,
    # even if they only ever use commands (never free-text/voice/photo).
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)

    welcome = (
        "¡Hola! Soy tu asistente personal con IA, potenciado por OpenCode Go.\n\n"
        "Puedo conversar contigo y también *llevar el registro de tus proyectos* "
        "para no perder el hilo.\n\n"
        "*Comandos:*\n"
        "/ayuda — Ver todos los comandos\n"
        "/proyectos — Ver tus proyectos\n"
        "/nuevoproyecto <nombre> — Crear un proyecto\n"
        "/reiniciar — Limpiar la conversación\n\n"
        "Cuéntame lo que necesites y me encargo."
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN)


_HELP_TEXT = (
    "*Comandos disponibles:*\n\n"
    "*General:*\n"
    "/iniciar — Mensaje de bienvenida\n"
    "/ayuda — Esta lista de comandos\n"
    "/info — Estado del bot, modelo y tiempo activo\n"
    "/reiniciar — Limpiar la conversación actual\n"
    "/dolar — Ver la tasa del dólar hoy (oficial BCV y paralela USDT)\n"
    "/imagina <descripción> — Generar una imagen con IA\n\n"
    "*Proyectos:*\n"
    "/proyectos — Ver tus proyectos\n"
    "/proyectos <scope> — Filtrar: todos, mios, compartidos, invitado\n"
    "/proyectos <estado> — Ver proyectos por estado (sin iniciar, en proceso, parado, completado, archivado)\n"
    "/nuevoproyecto <nombre> — Crear un proyecto\n"
    "/proyecto <nombre> — Ver detalles de un proyecto\n"
    "/estadoproyecto <nombre> <estado> [razón] — Cambiar el estado\n"
    "/prioridadproyecto <nombre> <prioridad> — Cambiar la prioridad (baja, media, alta, urgente)\n"
    "/agregarnota <proyecto> <texto> — Agregar una nota\n"
    "/buscarproyecto <término> — Buscar proyectos\n"
    "/eliminarproyecto <nombre> — Eliminar un proyecto archivado (libera espacio)\n\n"
    "*Recordatorios:*\n"
    "/recordar <texto y cuándo> — Crear un recordatorio\n"
    "/recordatorios — Ver tus recordatorios activos (con botones)\n"
    "/cancelarrecordatorio <id> — Cancelar un recordatorio\n"
    "/editarrecordatorio <id> <nuevo texto> — Editar el mensaje\n"
    "/posponerrecordatorio <id> <duración> — Posponer (ej. 30m, 2h, 1d)\n"
    "/reprogramarrecordatorio <id> <fecha> — Cambiar la fecha y hora\n\n"
    "*Multimedia:*\n"
    "Envíame una 🎤 nota de voz y la transcribo, o una 🖼️ foto y la analizo.\n\n"
    "💡 *Todo lo anterior también se puede hacer por texto natural.* "
    "Solo escríbeme lo que necesitas, por ejemplo: “crea un proyecto llamado Reparar bici”, "
    "“muéstrame mis proyectos urgentes” o “recuérdame llamar mañana a las 10am”."
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ayuda — list all commands."""
    if not _authorized(update):
        await _deny(update)
        return

    await update.message.reply_text(_HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /info — show bot status."""
    if not _authorized(update):
        await _deny(update)
        return

    settings: Settings = context.bot_data["settings"]
    provider = _active_provider(context)
    model = _active_model(settings, provider)
    uptime_seconds = time.time() - _start_time
    hours, remainder = divmod(int(uptime_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    text = (
        f"*Estado del bot*\n"
        f"Proveedor: `{provider}`\n"
        f"Modelo: `{model}`\n"
        f"Contexto: {settings.max_context_messages} mensajes\n"
        f"Límite de mensajes: {settings.rate_limit_seconds}s\n"
        f"Tiempo activo: {uptime_str}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def reiniciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /reiniciar — clear conversation history."""
    if not _authorized(update):
        await _deny(update)
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    clear_history(user_id, chat_id=chat_id)
    await update.message.reply_text(
        "Conversación limpiada. ¡Empezamos de cero!"
    )


async def dolar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /dolar — show official BCV and parallel (USDT) rates."""
    if not _authorized(update):
        await _deny(update)
        return

    settings: Settings = context.bot_data["settings"]

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    info = await get_dolar_info()
    if not info.get("ok"):
        logger.warning("dolar rates unavailable: %s", info.get("error"))
        await update.message.reply_text(
            "Lo siento, no pude obtener la tasa del dólar ahora mismo. "
            "Inténtalo en un momento."
        )
        return

    updated = datetime.fromtimestamp(
        info["actualizado"], ZoneInfo(settings.timezone)
    ).strftime("%d/%m/%Y %H:%M")
    text = (
        "💵 *Tasa del dólar hoy*\n\n"
        f"🏛️ *Oficial (BCV):* {format_bs(info['oficial'])}\n"
        f"📈 *Paralela (USDT/Binance):* {format_bs(info['paralela'])}\n\n"
        f"_Brecha: {info['brecha_porcentaje']}% · Actualizado {updated}_"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages — send to AI, run tools, and reply."""
    if not _authorized(update):
        await _deny(update)
        return

    settings: Settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    user_text = update.message.text.strip()
    chat = update.effective_chat

    # Remember which chat this user talks from (needed for proactive messages)
    upsert_user_chat(user_id, chat.id)

    # Keep the users table fresh so @username project sharing can resolve this user
    # Also save private_chat_id when in private chat
    user = update.effective_user
    private_chat_id = chat.id if chat.type == "private" else None
    upsert_user(user_id, user.username, user.first_name, private_chat_id=private_chat_id)

    # Rate limit check
    if _is_rate_limited(user_id, settings):
        remaining = settings.rate_limit_seconds - (
            time.time() - _rate_limit_tracker.get(user_id, 0)
        )
        await update.message.reply_text(
            f"Espera {remaining:.0f}s antes de enviar otro mensaje."
        )
        return

    # Store user's message
    add_message(user_id, "user", user_text, chat_id=chat.id)

    # Show typing indicator
    await context.bot.send_chat_action(
        chat_id=chat.id, action="typing"
    )

    # Get conversation history and call AI (with tool support) — timestamps
    # let the model infer gaps (e.g. 10 days of silence).
    client = context.bot_data["ai_client"]
    provider = _active_provider(context)
    model = _active_model(settings, provider)
    history = _format_history_for_model(
        get_history_with_timestamps(user_id, settings.max_context_messages, chat_id=chat.id),
        settings,
    )

    try:
        reply_text = await _run_ai_tool_loop(
            client=client,
            settings=settings,
            messages=history,
            user_id=user_id,
            model=model,
            context=context,
            chat_id=chat.id,
            session_id=f"tg-user-{user_id}",
        )
    except RateLimitError:
        logger.error("OpenCode Go rate limit hit for user %d", user_id)
        await update.message.reply_text(
            "El servicio de IA está ocupado ahora mismo. Inténtalo en un momento."
        )
        return
    except OpenCodeError as exc:
        logger.error("AI request failed for user %d: %s", user_id, exc)
        await update.message.reply_text(
            "Lo siento, tuve un problema procesando eso. Inténtalo de nuevo."
        )
        return

    # Store AI's response and reply
    if reply_text:
        add_message(user_id, "assistant", reply_text, chat_id=chat.id)
        await _reply_markdown_safe(update.message, reply_text)


async def handle_message_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages in groups — scoped to group projects only.

    In groups, the bot only shows projects associated with the group,
    and private tools (reminders, memory) are restricted. Reminders
    can be created but are delivered to the user's private chat.
    """
    if not _authorized(update):
        return  # Silently ignore unauthorized users in groups

    settings: Settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    group_id = update.effective_chat.id
    user_text = update.message.text.strip()

    # Register user as member of this group
    add_group_member(group_id, user_id)

    # Keep the users table fresh (without private_chat_id from group)
    user = update.effective_user
    upsert_user(user_id, user.username, user.first_name)

    # Rate limit check
    if _is_rate_limited(user_id, settings):
        return  # Silently ignore rate-limited users in groups

    # Get user's private chat ID for reminders
    private_chat_id = get_private_chat_id(user_id)

    # Store user's message in group-scoped history
    add_message(user_id, "user", user_text, chat_id=group_id)

    # Show typing indicator
    await context.bot.send_chat_action(
        chat_id=group_id, action="typing"
    )

    # Get conversation history and call AI with group context — timestamps
    # let the model infer gaps.
    client = context.bot_data["ai_client"]
    provider = _active_provider(context)
    model = _active_model(settings, provider)
    history = _format_history_for_model(
        get_history_with_timestamps(user_id, settings.max_context_messages, chat_id=group_id),
        settings,
    )

    try:
        reply_text = await _run_ai_tool_loop(
            client=client,
            settings=settings,
            messages=history,
            user_id=user_id,
            model=model,
            context=context,
            chat_id=group_id,
            group_id=group_id,
            private_chat_id=private_chat_id,
            session_id=f"tg-user-{user_id}",
        )
    except RateLimitError:
        logger.error("OpenCode Go rate limit hit for user %d in group %d", user_id, group_id)
        return  # Don't spam groups with error messages
    except OpenCodeError as exc:
        logger.error("AI request failed for user %d in group %d: %s", user_id, group_id, exc)
        return

    # Store AI's response and reply publicly in the group
    if reply_text:
        add_message(user_id, "assistant", reply_text, chat_id=group_id)
        await _reply_markdown_safe(update.message, reply_text)


async def _run_ai_tool_loop(
    client,
    settings: Settings,
    messages: list[dict[str, str]],
    user_id: int,
    model: str,
    context: ContextTypes.DEFAULT_TYPE | None = None,
    chat_id: int | None = None,
    group_id: int | None = None,
    private_chat_id: int | None = None,
    session_id: str | None = None,
) -> str:
    """Run the AI chat loop, executing any tool calls the model requests.

    Keeps the conversation messages passed to the model in sync with the
    full thread. Caps the tool-call rounds at ``settings.max_tool_rounds``
    (default 12, configurable via MAX_TOOL_ROUNDS) to avoid runaway loops:
    each round is one API call, so the cap is the safety valve between "do
    enough work in one message" and "the model is stuck, stop paying".

    When a tool returns ``media`` (e.g. a generated image), the media is
    sent to the user through ``context`` and the model receives only a
    short confirmation string instead of the heavy base64 payload.

    When ``group_id`` is set, the loop operates in group mode: tools are
    scoped to group projects, and ``private_chat_id`` is used for
    delivering reminders to the user's private chat.

    ``session_id`` is passed to the AI API for routing/caching (OpenCode Go).
    """
    working_messages = list(messages)
    provider = settings.ai_provider

    # Tell the model the current date/time so it can interpret
    # relative requests like "mañana" or "la semana que viene".
    now_local = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d %H:%M:%S")
    system_prompt = (
        f"{settings.system_prompt}\n\n"
        f"Fecha y hora actual del usuario: {now_local} "
        f"(zona horaria {settings.timezone})."
    )

    # Add group context instructions if in a group
    if group_id is not None:
        system_prompt += (
            "\n\n--- CONTEXTO DE GRUPO ---"
            "\nEstás en un grupo de Telegram. REGLAS ESTRICTAS DE AISLAMIENTO:"
            "\n1. Solo puedes ver y modificar proyectos que pertenezcan a ESTE "
            "grupo. Los proyectos privados de los usuarios NUNCA existen aquí."
            "\n2. Si un usuario menciona un proyecto, SOLO busca entre los "
            "proyectos de este grupo. Nunca asumas que un proyecto privado "
            "existe en el grupo."
            "\n3. Los recordatorios que crees aquí se entregan EN ESTE GRUPO y "
            "son COMPARTIDOS: cualquier miembro puede verlos, editarlos o borrarlos."
            "\n4. La bitácora y memoria que uses aquí son DEL GRUPO (memoria "
            "compartida, aislada de la privada). Cada entrada queda firmada "
            "@usuario y la memoria consultada es solo la del grupo."
            "\n5. Las entidades/casos (personas, lugares, casos como SIGAT, "
            "documentos) siguen siendo personales (tuyas), gestionables desde "
            "aquí o desde privado."
            "\n6. Búsqueda web y dólar funcionan aquí; imágenes solo en privado."
        )

    # Filter tools for group scope so model never sees private-only tools
    tools_for_model = _filter_tool_definitions(group_id)

    for _ in range(settings.max_tool_rounds):
        result = await chat_completion_with_tools(
            client=client,
            model=model,
            system_prompt=system_prompt,
            messages=working_messages,
            tools=tools_for_model,
            session_id=session_id,
            provider=provider,
        )

        if result["kind"] == "text":
            return result["content"]

        # Tool call requested — execute and feed the result back
        tool_name = result["name"]
        tool_args = result["arguments"]
        logger.info(
            "Tool call for user %d: %s(%s)", user_id, tool_name, tool_args
        )

        tool_result = await execute_tool(
            tool_name, tool_args, user_id,
            group_id=group_id,
            private_chat_id=private_chat_id,
        )
        # Dispatch group notifications before stripping metadata
        await _dispatch_tool_notifications(tool_result, context)
        # Strip _notify metadata so the AI never sees it
        tool_result = _strip_notify_metadata(tool_result)
        tool_feedback = await _forward_tool_media(
            tool_result, context, chat_id, settings, user_id
        )
        confirmation_feedback = await _forward_tool_confirmation(
            tool_result, context, chat_id
        )
        if confirmation_feedback is not None:
            # Confirmation buttons were sent to the user. Stop the tool loop
            # immediately — the user sees the buttons and decides; the model
            # should not produce additional text.
            return ""

        # Echo back any reasoning content (DeepSeek thinking mode) so the
        # provider accepts the continued conversation.
        reasoning = result.get("reasoning_content")
        tool_message: dict = {
            "role": "assistant",
            "content": result.get("content"),
            "tool_calls": [
                {
                    "id": result["call_id"],
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args),
                    },
                }
            ],
        }
        if reasoning:
            tool_message["reasoning_content"] = reasoning
        # Echo back the Gemini thought signature so the provider accepts
        # the continued conversation.
        thought_signature = result.get("thought_signature")
        if thought_signature:
            tool_message["tool_calls"][0]["extra_content"] = {
                "google": {"thought_signature": thought_signature}
            }
        working_messages.append(tool_message)
        working_messages.append(
            {
                "role": "tool",
                "tool_call_id": result["call_id"],
                "content": tool_feedback,
            }
        )

    # Safety: too many tool rounds, return the last plain answer
    return (
        "Hice demasiadas acciones seguidas y me perdí. "
        "¿Prefieres que lo haga en partes más pequeñas?"
    )


async def _forward_tool_media(
    tool_result: str,
    context: ContextTypes.DEFAULT_TYPE | None,
    chat_id: int | None,
    settings: Settings,
    user_id: int,
) -> str:
    """Send media returned by a tool to the user; return AI feedback.

    When ``tool_result`` is a JSON object with a ``media`` field (as
    produced by the ``generate_image`` tool), the media is forwarded to
    the Telegram chat and the model gets a short confirmation. Otherwise
    the original result string is returned unchanged.
    """
    try:
        payload = json.loads(tool_result)
    except (json.JSONDecodeError, TypeError):
        return tool_result

    media = payload.get("media")
    if not media or media.get("type") != "image":
        return tool_result

    if context is None or chat_id is None:
        return json.dumps(
            {"ok": False, "error": "no chat to deliver generated image"}
        )

    import base64 as _b64

    try:
        image_bytes = _b64.b64decode(media["data_b64"])
    except (TypeError, ValueError) as exc:
        logger.warning("Could not decode tool media for user %d: %s", user_id, exc)
        return json.dumps({"ok": False, "error": "imagen corrupta"})

    usage_note = _usage_note(user_id, IMAGE_TYPE, settings)
    caption = f"🎨 {_escape_markdown(media.get('prompt', ''))}{usage_note}"
    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=image_bytes,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:  # noqa: BLE001 - never crash the tool loop
        logger.warning("Failed to send generated image for user %d: %s", user_id, exc)
        return json.dumps(
            {"ok": False, "error": "no pude enviar la imagen generada"}
        )

    return json.dumps(
        {
            "ok": True,
            "result": "Imagen generada y enviada al usuario.",
        }
    )


async def _forward_tool_confirmation(
    tool_result: str,
    context: ContextTypes.DEFAULT_TYPE | None,
    chat_id: int | None,
) -> str | None:
    """Ask the user to confirm a destructive tool via inline buttons.

    Tools that require confirmation (currently ``delete_project``) return a
    JSON payload with ``confirmation_required`` plus the target object. The
    confirmation buttons are sent to the user here — the same buttons and
    ``callback_data`` as the /eliminarproyecto command — and the model
    receives a short message telling it to wait for the user's tap instead
    of the original result. Returns None when no confirmation is required.
    """
    try:
        payload = json.loads(tool_result)
    except (json.JSONDecodeError, TypeError):
        return None

    if not payload.get("confirmation_required"):
        return None

    project = payload.get("project") or {}
    project_id = project.get("id")
    project_name = project.get("name")
    if context is None or chat_id is None or project_id is None:
        return json.dumps(
            {"ok": False, "error": "no chat to confirm destructive action"}
        )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑️ Sí, eliminar", callback_data=f"delproj:{project_id}"
                ),
                InlineKeyboardButton(
                    "Cancelar", callback_data=f"delproj_cancel:{project_id}"
                ),
            ]
        ]
    )
    safe_name = _escape_markdown(project_name or "el proyecto")
    try:
        await context.bot.send_message(
            chat_id,
            f"⚠️ ¿Eliminar definitivamente el proyecto *{safe_name}*?\n\n"
            "Se borrarán sus notas y archivos. Esta acción no se puede deshacer.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
    except Exception as exc:  # noqa: BLE001 - never crash the tool loop
        logger.warning(
            "Failed to send confirmation for project %s: %s", project_id, exc
        )
        return json.dumps(
            {"ok": False, "error": "no pude mostrar la confirmación"}
        )

    return json.dumps(
        {
            "ok": True,
            "confirmation_required": True,
            "result": (
                "Se mostró al usuario una confirmación con botones para "
                "eliminar el proyecto. Indica al usuario que confirme "
                "tocando 'Sí, eliminar' en los botones que aparecieron."
            ),
        },
        ensure_ascii=False,
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice notes: transcribe with Gemini, then continue the chat."""
    if not _authorized(update):
        await _deny(update)
        return

    settings: Settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    upsert_user_chat(user_id, update.effective_chat.id)
    # Keep the users table fresh so @username project sharing can resolve this user
    user = update.effective_user
    upsert_user(user_id, user.username, user.first_name)

    if _is_rate_limited(user_id, settings):
        await update.message.reply_text(
            "Espera un momento antes de enviar otra nota de voz."
        )
        return

    media_client, media_model = _media_processor(context)
    if media_client is None:
        await update.message.reply_text(
            "Para escuchar notas de voz necesito `GEMINI_API_KEY` en el `.env`.\n"
            "Consíguela gratis en https://aistudio.google.com/ y reinicia el bot.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    day = _media_day(settings)
    limit = _media_limit(settings, AUDIO_TYPE)
    if not media_usage.try_increment(user_id, AUDIO_TYPE, day, limit):
        label = "notas de voz" if limit != 1 else "nota de voz"
        await update.message.reply_text(
            f"Has alcanzado el máximo de {label} hoy ({limit}). "
            "El límite se reinicia mañana."
        )
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    try:
        raw = await download_file(context.bot, update.message.voice.file_id)
        wav = ogg_to_wav(raw)
        audio_b64 = encode_base64(wav)
    except Exception as exc:
        media_usage.refund_usage(user_id, AUDIO_TYPE, day)
        logger.warning("Voice download/convert failed for user %d: %s", user_id, exc)
        await update.message.reply_text(
            "No pude leer la nota de voz. Asegúrate de que ffmpeg esté "
            "instalado e inténtalo de nuevo."
        )
        return

    try:
        transcript = await transcribe_audio(
            client=media_client,
            model=media_model,
            audio_b64=audio_b64,
            session_id=f"tg-media-{user_id}",
            provider="gemini",
        )
    except (OpenCodeError, RateLimitError) as exc:
        media_usage.refund_usage(user_id, AUDIO_TYPE, day)
        logger.error("Transcription failed for user %d: %s", user_id, exc)
        await update.message.reply_text(
            "No pude transcribir la nota de voz. Inténtalo de nuevo."
        )
        return

    # The slot was reserved by try_increment; the transcript is now counted.

    # Feed the transcript into the normal conversation so the AI can act
    # on it (create projects, reminders, etc.) and reply naturally.
    chat_id = update.effective_chat.id
    add_message(user_id, "user", transcript, chat_id=chat_id)
    history = _format_history_for_model(
        get_history_with_timestamps(user_id, settings.max_context_messages, chat_id=chat_id),
        settings,
    )
    provider = _active_provider(context)
    model = _active_model(settings, provider)
    try:
        ai_reply = await _run_ai_tool_loop(
            client=context.bot_data["ai_client"],
            settings=settings,
            messages=history,
            user_id=user_id,
            model=model,
            context=context,
            chat_id=chat_id,
            session_id=f"tg-user-{user_id}",
        )
    except (OpenCodeError, RateLimitError) as exc:
        logger.error("AI reply after voice failed for user %d: %s", user_id, exc)
        ai_reply = ""
    if ai_reply:
        add_message(user_id, "assistant", ai_reply, chat_id=chat_id)

    usage_note = _usage_note(user_id, AUDIO_TYPE, settings)
    transcript_line = (
        f"🎤 *Transcripción:* {_escape_markdown(transcript)}\n\n"
    )
    await _reply_markdown_safe(
        update.message,
        f"{transcript_line}{ai_reply}{usage_note}",
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos: analyze with Gemini vision, then continue the chat."""
    if not _authorized(update):
        await _deny(update)
        return

    settings: Settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    upsert_user_chat(user_id, update.effective_chat.id)
    # Keep the users table fresh so @username project sharing can resolve this user
    user = update.effective_user
    upsert_user(user_id, user.username, user.first_name)

    if _is_rate_limited(user_id, settings):
        await update.message.reply_text(
            "Espera un momento antes de enviar otra foto."
        )
        return

    media_client, media_model = _media_processor(context)
    if media_client is None:
        await update.message.reply_text(
            "Para analizar fotos necesito `GEMINI_API_KEY` en el `.env`.\n"
            "Consíguela gratis en https://aistudio.google.com/ y reinicia el bot.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    day = _media_day(settings)
    limit = _media_limit(settings, IMAGE_TYPE)
    if not media_usage.try_increment(user_id, IMAGE_TYPE, day, limit):
        label = "imágenes" if limit != 1 else "imagen"
        await update.message.reply_text(
            f"Has alcanzado el máximo de {label} hoy ({limit}). "
            "El límite se reinicia mañana."
        )
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    # Telegram sends several resolutions; the last one is the largest.
    photo = update.message.photo[-1]
    caption = update.message.caption or ""

    try:
        raw = await download_file(context.bot, photo.file_id)
        jpeg = resize_image(raw)
        image_b64 = encode_base64(jpeg)
    except Exception as exc:
        media_usage.refund_usage(user_id, IMAGE_TYPE, day)
        logger.warning("Photo download/resize failed for user %d: %s", user_id, exc)
        await update.message.reply_text(
            "No pude leer la foto. Inténtalo de nuevo."
        )
        return

    question = (
        "El usuario envió esta foto. Describe lo que ves y responde de "
        "forma útil."
        + (f" Su mensaje: {caption}" if caption else "")
    )
    try:
        description = await describe_image(
            client=media_client,
            model=media_model,
            image_b64=image_b64,
            question=question,
            session_id=f"tg-media-{user_id}",
            provider="gemini",
        )
    except (OpenCodeError, RateLimitError) as exc:
        media_usage.refund_usage(user_id, IMAGE_TYPE, day)
        logger.error("Vision analysis failed for user %d: %s", user_id, exc)
        await update.message.reply_text(
            "No pude analizar la foto. Inténtalo de nuevo."
        )
        return

    # The slot was reserved by try_increment; the analysis is now counted.

    # Make the description part of the conversation so the chat AI can
    # reference it naturally.
    chat_id = update.effective_chat.id
    add_message(
        user_id,
        "user",
        f"[El usuario envió una foto. Descripción: {description}]",
        chat_id=chat_id,
    )
    history = _format_history_for_model(
        get_history_with_timestamps(user_id, settings.max_context_messages, chat_id=chat_id),
        settings,
    )
    provider = _active_provider(context)
    model = _active_model(settings, provider)
    try:
        ai_reply = await _run_ai_tool_loop(
            client=context.bot_data["ai_client"],
            settings=settings,
            messages=history,
            user_id=user_id,
            model=model,
            context=context,
            chat_id=chat_id,
            session_id=f"tg-user-{user_id}",
        )
    except (OpenCodeError, RateLimitError) as exc:
        logger.error("AI reply after photo failed for user %d: %s", user_id, exc)
        ai_reply = description
    if ai_reply:
        add_message(user_id, "assistant", ai_reply, chat_id=chat_id)

    usage_note = _usage_note(user_id, IMAGE_TYPE, settings)
    await _reply_markdown_safe(update.message, f"🖼️ {ai_reply}{usage_note}")


async def imagina_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /imagina <prompt> — generate an image with Gemini."""
    if not _authorized(update):
        await _deny(update)
        return

    settings: Settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    upsert_user_chat(user_id, update.effective_chat.id)

    prompt = " ".join(context.args or []).strip()
    if not prompt:
        await update.message.reply_text(
            "Uso: /imagina <descripción de la imagen>\n"
            "Ejemplo: /imagina un gato astronauta en la luna, estilo pixel art"
        )
        return

    if _is_rate_limited(user_id, settings):
        await update.message.reply_text(
            "Espera un momento antes de generar otra imagen."
        )
        return

    media_client, _ = _media_processor(context)
    if media_client is None:
        await update.message.reply_text(
            "Para generar imágenes necesito `GEMINI_API_KEY` en el `.env`.\n"
            "Consíguela gratis en https://aistudio.google.com/ y reinicia el bot.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    day = _media_day(settings)
    limit = _media_limit(settings, IMAGE_TYPE)
    if not media_usage.try_increment(user_id, IMAGE_TYPE, day, limit):
        label = "imágenes" if limit != 1 else "imagen"
        await update.message.reply_text(
            f"Has alcanzado el máximo de {label} hoy ({limit}). "
            "El límite se reinicia mañana."
        )
        return

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="upload_photo"
    )

    try:
        image_bytes = await generate_image(
            client=media_client,
            model=settings.gemini_image_model,
            prompt=prompt,
            session_id=f"tg-image-{user_id}",
            provider="gemini",
        )
    except (OpenCodeError, RateLimitError) as exc:
        media_usage.refund_usage(user_id, IMAGE_TYPE, day)
        logger.error("Image generation failed for user %d: %s", user_id, exc)
        await update.message.reply_text(
            "No pude generar la imagen. Inténtalo de nuevo."
        )
        return

    # The slot was reserved by try_increment; the generated image is now counted.

    usage_note = _usage_note(user_id, IMAGE_TYPE, settings)
    caption = f"🎨 {_escape_markdown(prompt)}{usage_note}"
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=image_bytes,
        caption=caption,
        parse_mode=ParseMode.MARKDOWN,
    )


# --- Group project change notifications ---------------------------------------


async def notify_group_project_change(
    context: ContextTypes.DEFAULT_TYPE,
    project: dict,
    change_type: str,
    actor_id: int,
    detail: str = "",
) -> None:
    """Send a notification to the group about a project change.

    Best-effort: failures are logged but never break the caller.
    Only sends for group projects (group_id is not None).
    """
    from bot.projects import (
        get_group_project_notify_targets,
        get_project_owner_name,
    )

    if not project.get("group_id"):
        return

    targets = get_group_project_notify_targets(
        project["id"], exclude_user_id=actor_id
    )
    if not targets:
        return

    actor_name = get_project_owner_name(actor_id)
    project_name = _escape_markdown(project["name"])

    notifications = {
        "status": f"📋 *{project_name}* cambió de estado{detail} — por {actor_name}",
        "priority": f"🔴 *{project_name}* cambió de prioridad{detail} — por {actor_name}",
        "note": f"📝 Nueva nota en *{project_name}* — por {actor_name}",
    }
    text = notifications.get(change_type)
    if not text:
        return

    group_id = project["group_id"]
    try:
        await context.bot.send_message(
            chat_id=group_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:  # noqa: BLE001 - best-effort notification
        logger.warning(
            "Failed to notify group %d about project %s change: %s",
            group_id,
            project.get("slug", "?"),
            change_type,
        )


def _strip_notify_metadata(tool_result: str) -> str:
    """Remove _notify key from tool result JSON before AI sees it.

    The _notify key is used to pass notification metadata from tool
    handlers to the dispatch layer. The AI model should never see it.
    """
    try:
        payload = json.loads(tool_result)
        if "_notify" in payload:
            del payload["_notify"]
            return json.dumps(payload, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
    return tool_result


async def _dispatch_tool_notifications(
    tool_result: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Check if a tool result includes notification metadata and dispatch it.

    Called after execute_tool() in the AI tool loop. If the tool returned
    a _notify payload (for group project changes), dispatches a notification
    to the group chat.
    """
    try:
        payload = json.loads(tool_result)
    except (json.JSONDecodeError, TypeError):
        return

    notify = payload.get("_notify")
    if not notify or context is None:
        return

    project = notify.get("project")
    if not project or not project.get("group_id"):
        return

    await notify_group_project_change(
        context,
        project,
        notify["type"],
        notify["actor_id"],
        detail=notify.get("detail", ""),
    )


# --- Reminder commands -------------------------------------------------------


async def recordar_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /recordar <text> — schedule a reminder via AI date parsing."""
    if not _authorized(update):
        await _deny(update)
        return

    settings: Settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text(
            "Uso: /recordar <qué quieres que te recuerde y cuándo>\n"
            "Ejemplo: /recordar llamar al mecánico el próximo lunes a las 10am"
        )
        return

    client = context.bot_data["ai_client"]
    provider = _active_provider(context)
    model = _active_model(settings, provider)
    now_iso = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%dT%H:%M")

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    parsed = await parse_reminder_datetime(
        client=client,
        model=model,
        text=text,
        now_iso=now_iso,
        timezone_name=settings.timezone,
        session_id=f"tg-user-{user_id}",
        provider=provider,
    )

    if parsed is None or not parsed.get("datetime"):
        await update.message.reply_text(
            "No pude interpretar cuándo quieres el recordatorio. "
            "Puedes decirme algo como: */recordar llamar al mecánico el próximo lunes a las 10am*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    message = parsed.get("message") or text
    dt_str = parsed["datetime"]
    remind_at = _parse_local_dt(dt_str, settings.timezone)
    if remind_at is None:
        await update.message.reply_text(
            f"No pude interpretar la fecha *{dt_str}*. Intenta con otro formato.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if remind_at < time.time():
        await update.message.reply_text(
            "Esa fecha ya pasó. El recordatorio debe ser en el futuro."
        )
        return

    recurrence = parsed.get("recurrence", "none")
    if recurrence not in RECURRENCE_VALUES:
        recurrence = "none"

    create_reminder(
        user_id, update.effective_chat.id, message, remind_at, recurrence
    )
    human = datetime.fromtimestamp(remind_at, ZoneInfo(settings.timezone)).strftime(
        "%d/%m/%Y a las %H:%M"
    )
    recurrence_line = (
        f"\n🔁 {RECURRENCE_LABELS[recurrence]}" if recurrence != "none" else ""
    )
    await update.message.reply_text(
        f"✅ Recordatorio programado:\n"
        f"*{message}*\n"
        f"📅 {human}"
        f"{recurrence_line}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def recordar_command_group(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /recordar in groups — create reminder delivered to private chat.

    Mirrors recordar_command but uses private_chat_id for delivery
    and confirms briefly in the group.
    """
    if not _authorized(update):
        return  # Silently ignore in groups

    settings: Settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    group_id = update.effective_chat.id
    text = " ".join(context.args or []).strip()

    if not text:
        await update.message.reply_text(
            "Uso: /recordar <qué quieres que te recuerde y cuándo>\n"
            "Ejemplo: /recordar llamar al mecánico el próximo lunes a las 10am"
        )
        return

    # Ensure group membership recorded
    from bot.projects import add_group_member

    add_group_member(group_id, user_id)

    # Use AI to parse datetime (same as private)
    client = context.bot_data["ai_client"]
    provider = _active_provider(context)
    model = _active_model(settings, provider)
    now_iso = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%dT%H:%M")

    await context.bot.send_chat_action(
        chat_id=group_id, action="typing"
    )

    parsed = await parse_reminder_datetime(
        client=client,
        model=model,
        text=text,
        now_iso=now_iso,
        timezone_name=settings.timezone,
        session_id=f"tg-user-{user_id}",
        provider=provider,
    )

    if parsed is None or not parsed.get("datetime"):
        await update.message.reply_text(
            "No pude interpretar cuándo quieres el recordatorio. "
            "Puedes decirme algo como: */recordar llamar al mecánico el próximo lunes a las 10am*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    message = parsed.get("message") or text
    dt_str = parsed["datetime"]
    remind_at = _parse_local_dt(dt_str, settings.timezone)
    if remind_at is None:
        await update.message.reply_text(
            f"No pude interpretar la fecha *{dt_str}*. Intenta con otro formato.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if remind_at < time.time():
        await update.message.reply_text(
            "Esa fecha ya pasó. El recordatorio debe ser en el futuro."
        )
        return

    recurrence = parsed.get("recurrence", "none")
    if recurrence not in RECURRENCE_VALUES:
        recurrence = "none"

    # Create reminder delivered IN the group (shared)
    create_reminder(
        user_id, group_id, message, remind_at, recurrence, group_id=group_id, created_by=user_id
    )

    await update.message.reply_text(
        "Ok, te lo recordaré aquí en el grupo 👍"
    )


async def recordatorios_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /recordatorios — list pending reminders (scoped)."""
    if not _authorized(update):
        await _deny(update)
        return

    settings: Settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    chat = update.effective_chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None
    # In group, list shared group reminders; any member sees all
    reminders = list_pending_reminders(user_id, group_id=group_id)

    if not reminders:
        msg = "No hay recordatorios pendientes en este grupo." if group_id else "No tienes recordatorios pendientes."
        await update.message.reply_text(msg)
        return

    lines = ["*Recordatorios pendientes:*\n"]
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for r in reminders:
        human = datetime.fromtimestamp(
            r["remind_at"], ZoneInfo(settings.timezone)
        ).strftime("%d/%m/%Y %H:%M")
        recurrence = (
            f" · 🔁 {RECURRENCE_LABELS.get(r['recurrence'], r['recurrence'])}"
            if r.get("recurrence", "none") != "none"
            else ""
        )
        lines.append(f"`{r['id']}` — *{r['message']}* — {human}{recurrence}")
        # One row per reminder so each row's buttons carry that reminder id
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    "🔕 Cancelar", callback_data=f"remind_cancel:{r['id']}"
                ),
                InlineKeyboardButton(
                    "⏰ Posponer 1h", callback_data=f"remind_postpone:{r['id']}"
                ),
            ]
        )
    lines.append("\n_Cancela uno con /cancelarrecordatorio <id>_")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button taps on /recordatorios: cancel or postpone 1h."""
    query = update.callback_query

    # Parse action and id from "remind_cancel:<id>" / "remind_postpone:<id>".
    # CallbackQuery.data is user-controlled, so the format is validated
    # defensively even though the handler pattern already matches it.
    data = query.data or ""
    prefix, _, id_str = data.partition(":")
    try:
        reminder_id = int(id_str)
    except ValueError:
        await query.answer(text="Botón no válido.", show_alert=True)
        return

    if not _authorized(update):
        await query.answer(text="No autorizado.", show_alert=True)
        return

    user_id = update.effective_user.id
    # Resolve scope from message chat (group vs private) — defensive for fakes
    chat = getattr(query.message, "chat", None) if query.message else None
    chat_type = getattr(chat, "type", "private") if chat else "private"
    group_id = getattr(chat, "id", None) if chat_type in ("group", "supergroup") else None
    # In group, validate membership and allow any member to act
    if group_id is not None:
        from bot.reminders import get_reminder_by_id_for_scope

        reminder = get_reminder_by_id_for_scope(reminder_id, user_id, group_id=group_id)
    else:
        reminder = get_reminder_by_id(reminder_id, user_id)
    if reminder is None:
        await query.answer(text="Ese recordatorio ya no existe.", show_alert=True)
        return

    if prefix == "remind_cancel":
        delete_reminder(reminder_id, user_id, group_id=group_id)
        await query.answer(text="Recordatorio cancelado.")
        if query.message:
            await query.edit_message_text(
                f"~~{reminder['message']}~~ — ❌ cancelado"
            )
    elif prefix == "remind_postpone":
        postpone_reminder(reminder_id, user_id, 3600, group_id=group_id)
        await query.answer(text="Pospuesto 1 hora.")
        if query.message:
            await query.edit_message_text(
                f"{reminder['message']} — ⏰ pospuesto 1h"
            )
    else:
        await query.answer(text="Acción desconocida.", show_alert=True)


async def cancelarrecordatorio_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /cancelarrecordatorio <id> — cancel a pending reminder."""
    if not _authorized(update):
        await _deny(update)
        return

    user_id = update.effective_user.id
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Uso: /cancelarrecordatorio <id>\n"
            "Ve tus recordatorios con /recordatorios"
        )
        return

    try:
        reminder_id = int(args[0])
    except ValueError:
        await update.message.reply_text("El ID debe ser un número.")
        return

    group_id = update.effective_chat.id if update.effective_chat.type in ("group", "supergroup") else None
    if delete_reminder(reminder_id, user_id, group_id=group_id):
        await update.message.reply_text(f"Recordatorio #{reminder_id} cancelado.")
    else:
        await update.message.reply_text(
            "No encontré ese recordatorio (o ya fue enviado)."
        )


def _parse_reminder_id(args: list[str]) -> int | None:
    """Return the numeric reminder id from command args, or None."""
    try:
        return int(args[0])
    except (ValueError, IndexError):
        return None


# Seconds per unit, used by _parse_duration for compact and Spanish forms
_DURATION_SECONDS = {
    "m": 60,
    "min": 60,
    "mins": 60,
    "minuto": 60,
    "minutos": 60,
    "h": 3600,
    "hora": 3600,
    "horas": 3600,
    "d": 86400,
    "dia": 86400,
    "dias": 86400,
    "día": 86400,
    "días": 86400,
    "semana": 604800,
    "semanas": 604800,
}


def _parse_duration(duration_str: str) -> float | None:
    """Parse a human duration like '30m', '2 horas', '1 día' to seconds.

    Accepts compact forms (30m, 2h, 1d) and Spanish words with optional
    spaces and mixed casing. Returns None when unparseable.
    """
    text = duration_str.strip().lower()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([a-záéíóú]+)$", text)
    if not match:
        return None
    seconds = _DURATION_SECONDS.get(match.group(2))
    if seconds is None:
        return None
    return float(match.group(1)) * seconds


def _human_duration(seconds: float) -> str:
    """Format seconds as a friendly Spanish duration ('1 hora 30 minutos')."""
    total = int(round(seconds))
    parts = []
    for size, singular, plural in (
        (604800, "semana", "semanas"),
        (86400, "día", "días"),
        (3600, "hora", "horas"),
        (60, "minuto", "minutos"),
    ):
        value, total = divmod(total, size)
        if value == 1:
            parts.append(f"{value} {singular}")
        elif value > 1:
            parts.append(f"{value} {plural}")
    if total:
        parts.append(f"{total} segundos")
    return " ".join(parts) if parts else "0 segundos"


def _format_reminder_time(remind_at: float, timezone_name: str) -> str:
    """Format an epoch timestamp as a friendly local datetime."""
    return datetime.fromtimestamp(remind_at, ZoneInfo(timezone_name)).strftime(
        "%d/%m/%Y a las %H:%M"
    )


async def editarrecordatorio_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /editarrecordatorio <id> <nuevo texto> — change a reminder's message."""
    if not _authorized(update):
        await _deny(update)
        return

    user_id = update.effective_user.id
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: /editarrecordatorio <id> <nuevo texto>\n"
            "Ve tus recordatorios con /recordatorios"
        )
        return

    reminder_id = _parse_reminder_id(args)
    if reminder_id is None:
        await update.message.reply_text("El ID debe ser un número.")
        return

    new_text = " ".join(args[1:]).strip()
    if not new_text:
        await update.message.reply_text("El nuevo texto no puede estar vacío.")
        return

    group_id = update.effective_chat.id if update.effective_chat.type in ("group", "supergroup") else None
    if update_reminder_text(reminder_id, user_id, new_text, group_id=group_id):
        await update.message.reply_text(
            f"✏️ Recordatorio #{reminder_id} actualizado:\n*{new_text}*",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "No encontré ese recordatorio (o no es tuyo)."
        )


async def posponerrecordatorio_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /posponerrecordatorio <id> <duración> — delay a pending reminder."""
    if not _authorized(update):
        await _deny(update)
        return

    user_id = update.effective_user.id
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: /posponerrecordatorio <id> <duración>\n"
            "Ejemplos: 30m, 2h, 1d, '30 minutos', '2 horas'"
        )
        return

    reminder_id = _parse_reminder_id(args)
    if reminder_id is None:
        await update.message.reply_text("El ID debe ser un número.")
        return

    seconds = _parse_duration(" ".join(args[1:]))
    if seconds is None:
        await update.message.reply_text(
            "No entendí la duración. Usa algo como: 30m, 2h, 1d, '30 minutos', '2 horas'"
        )
        return

    group_id = update.effective_chat.id if update.effective_chat.type in ("group", "supergroup") else None
    if not postpone_reminder(reminder_id, user_id, seconds, group_id=group_id):
        await update.message.reply_text(
            "No encontré ese recordatorio (o no es tuyo)."
        )
        return

    # Fetch for display — scoped
    if group_id is not None:
        from bot.reminders import get_reminder_by_id_for_scope

        reminder = get_reminder_by_id_for_scope(reminder_id, user_id, group_id=group_id)
    else:
        reminder = get_reminder_by_id(reminder_id, user_id)
    settings: Settings = context.bot_data["settings"]
    new_at = _format_reminder_time(reminder["remind_at"], settings.timezone)
    await update.message.reply_text(
        f"⏰ Recordatorio #{reminder_id} pospuesto {_human_duration(seconds)}\n"
        f"📅 {new_at}"
    )


async def reprogramarrecordatorio_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /reprogramarrecordatorio <id> <fecha> — reschedule a reminder."""
    if not _authorized(update):
        await _deny(update)
        return

    settings: Settings = context.bot_data["settings"]
    user_id = update.effective_user.id
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: /reprogramarrecordatorio <id> <fecha>\n"
            "Ejemplo: /reprogramarrecordatorio 12 mañana a las 10am"
        )
        return

    reminder_id = _parse_reminder_id(args)
    if reminder_id is None:
        await update.message.reply_text("El ID debe ser un número.")
        return

    date_text = " ".join(args[1:]).strip()
    client = context.bot_data["ai_client"]
    provider = _active_provider(context)
    model = _active_model(settings, provider)
    now_iso = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%dT%H:%M")

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    parsed = await parse_reminder_datetime(
        client=client,
        model=model,
        text=date_text,
        now_iso=now_iso,
        timezone_name=settings.timezone,
    )

    if parsed is None or not parsed.get("datetime"):
        await update.message.reply_text(
            "No pude interpretar la fecha. Intenta algo como: "
            "*mañana a las 10am*, *viernes 3pm*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    remind_at = _parse_local_dt(parsed["datetime"], settings.timezone)
    if remind_at is None:
        await update.message.reply_text(
            f"No pude interpretar la fecha *{parsed['datetime']}*. Intenta con otro formato.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if remind_at < time.time():
        await update.message.reply_text(
            "Esa fecha ya pasó. El recordatorio debe ser en el futuro."
        )
        return

    group_id = update.effective_chat.id if update.effective_chat.type in ("group", "supergroup") else None
    if not update_reminder_time(reminder_id, user_id, remind_at, group_id=group_id):
        await update.message.reply_text(
            "No encontré ese recordatorio (o no es tuyo)."
        )
        return

    if group_id is not None:
        from bot.reminders import get_reminder_by_id_for_scope

        reminder = get_reminder_by_id_for_scope(reminder_id, user_id, group_id=group_id)
    else:
        reminder = get_reminder_by_id(reminder_id, user_id)
    message = parsed.get("message") or (
        reminder["message"] if reminder else "Recordatorio"
    )
    human = _format_reminder_time(remind_at, settings.timezone)
    await update.message.reply_text(
        f"📅 Recordatorio #{reminder_id} reprogramado:\n"
        f"*{message}*\n"
        f"📅 {human}",
        parse_mode=ParseMode.MARKDOWN,
    )


# --- Scheduled jobs ----------------------------------------------------------


async def check_due_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: send any reminders whose time has come."""
    due = get_due_reminders()
    for reminder in due:
        try:
            await _send_markdown_safe(
                context.bot.send_message,
                reminder["chat_id"],
                f"🔔 *Recordatorio:* {reminder['message']}",
            )

            # Delivery succeeded: clear any previous backoff state so the
            # reminder gets a fresh retry window if it later fails again.
            reset_retry(reminder["id"])

            if reminder["recurrence"] != "none":
                next_at = next_occurrence(
                    reminder["remind_at"], reminder["recurrence"]
                )
                if next_at is not None:
                    reschedule_reminder(reminder["id"], next_at)
                    logger.info(
                        "Rescheduled recurring reminder %d (next %.0f)",
                        reminder["id"],
                        next_at,
                    )
                    continue

            mark_reminder_sent(reminder["id"])
            logger.info("Sent reminder %d to user %d", reminder["id"], reminder["user_id"])
        except Exception as exc:
            # Delivery failed: schedule an exponential-backoff retry so the
            # reminder is not lost but stops hammering the chat while down.
            schedule_retry(reminder["id"])
            logger.warning(
                "Reminder %d delivery failed, retry scheduled: %s",
                reminder["id"],
                exc,
            )


def _parse_local_dt(dt_str: str, timezone_name: str) -> float | None:
    """Parse "YYYY-MM-DDTHH:MM" (naive) to epoch in the given timezone."""
    try:
        naive = datetime.fromisoformat(dt_str)
        aware = naive.replace(tzinfo=ZoneInfo(timezone_name))
        return aware.timestamp()
    except Exception:
        return None


# --- Group lifecycle handlers ----------------------------------------------


async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle bot removal from a group — migrate projects to private.

    When the bot is kicked or leaves a group, all projects associated
    with that group are converted to private (group_id set to NULL)
    and collaborators are removed. The project owner retains access.
    """
    my_member = update.my_chat_member
    if my_member is None:
        return

    # Check if bot was removed (kicked or left)
    new_status = my_member.new_chat_member.status
    if new_status not in ("kicked", "left"):
        return

    group_id = my_member.chat.id
    logger.info("Bot removed from group %d (%s), cleaning up group data", group_id, my_member.chat.title)
    from bot.projects import cleanup_group_data

    cleanup_group_data(group_id)
