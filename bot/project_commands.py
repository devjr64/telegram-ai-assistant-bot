"""Telegram command handlers for project management.

All user-facing messages are in Spanish (the bot's users are in
Venezuela); the code itself is in English.
"""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.access import is_authorized
from bot.config import Settings
from bot.projects import (
    create_project,
    delete_project,
    search_projects,
    get_project_for_user,
    is_owner,
    add_project_member,
    remove_project_member,
    list_project_members,
    list_owned_projects,
    list_shared_with_me,
    list_shared_by_me,
    list_group_projects_for_user,
    resolve_username,
    get_user_profile,
    update_project_status,
    update_project_priority,
    add_project_note,
    get_project_notes,
    get_shared_projects_in_group,
    PROJECT_STATUSES,
    PROJECT_PRIORITIES,
    ACTIVE_STATUSES,
    can_activate_urgent,
)
from bot.reminders import get_chat_for_user
from bot.handlers import notify_group_project_change

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "sin_iniciar": "Sin iniciar",
    "en_proceso": "En proceso",
    "parado": "Parado",
    "completado": "Completado",
    "archivado": "Archivado",
}

_PRIORITY_LABELS = {
    "baja": "Baja",
    "media": "Media",
    "alta": "Alta",
    "urgente": "Urgente",
}

# Optional first argument of /proyectos selecting which project scope to list.
_SCOPE_FILTERS = ("todos", "mios", "dueños", "compartidos", "invitado")


def _auth_check(update: Update) -> bool:
    """Return True when the user may use the bot (purely a gate)."""
    return is_authorized(update.effective_user.id)


async def _deny(update: Update) -> None:
    """Send the polite unauthorized message."""
    await update.message.reply_text(
        "Lo siento, no estás autorizado para usar este bot."
    )


def _status_label(status: str) -> str:
    """Return the Spanish display label for a project status."""
    return _STATUS_LABELS.get(status, status)


def _priority_label(priority: str) -> str:
    """Return the Spanish display label for a project priority."""
    return _PRIORITY_LABELS.get(priority, priority)


def _normalize_status(arg: str) -> str | None:
    """Map a user-typed status to its internal DB value.

    Accepts either the internal snake_case value (``sin_iniciar``) or the
    Spanish display label (``Sin iniciar`` / ``sin iniciar``).
    """
    normalized = arg.lower().strip().replace(" ", "_")
    if normalized in PROJECT_STATUSES:
        return normalized
    for key, label in _STATUS_LABELS.items():
        if label.lower() == arg.lower().strip():
            return key
    return None


def _format_date(timestamp: float, timezone_name: str = "America/Caracas") -> str:
    """Format a Unix timestamp as dd/mm/yyyy in the given timezone."""
    if not timestamp:
        return "—"
    return datetime.fromtimestamp(
        timestamp, ZoneInfo(timezone_name)
    ).strftime("%d/%m/%Y")


def _find_project(user_id: int, name: str, group_id: int | None = None) -> dict | None:
    """Locate a project the user may access (owner or invited member).

    Delegates to ``get_project_for_user``, which tries the literal name
    and the slugified form. This is the ACL gate for every project read:
    an invited collaborator sees a project only when a membership row
    exists for them — never another user's project by ID or slug.

    When ``group_id`` is provided, only projects in that group are returned.
    When ``group_id`` is None, only private projects are returned.
    """
    return get_project_for_user(user_id, name.strip(), group_id=group_id)


def _valid_statuses_text() -> str:
    """Comma-separated list of Spanish status labels for help messages."""
    return ", ".join(_status_label(s) for s in PROJECT_STATUSES)


def _valid_priorities_text() -> str:
    """Comma-separated list of Spanish priority labels for help messages."""
    return ", ".join(_priority_label(p) for p in PROJECT_PRIORITIES)


def _owner_label(owner_id: int) -> str:
    """Return a display label for a project's owner.

    Prefers the owner's @username, falls back to their first name, and
    only then to the raw user_id — so collaborators never need to see a
    numeric Telegram ID.
    """
    profile = get_user_profile(owner_id)
    if profile and profile.get("username"):
        return f"@{profile['username']}"
    if profile and profile.get("first_name"):
        return profile["first_name"]
    return f"usuario {owner_id}"


def _member_label(member: dict) -> str:
    """Return a display label for a project member dict.

    Prefers the @username, falls back to the first name, then to the
    raw user_id when the invitee has no profile recorded yet.
    """
    if member.get("username"):
        return f"@{member['username']}"
    if member.get("first_name"):
        return member["first_name"]
    return str(member["user_id"])


def _username_label(username: str) -> str:
    """Normalize a typed username into a display label with a leading @."""
    return f"@{username.lstrip('@')}"


def _project_listing(
    user_id: int, scope: str, status_filter: str | None
) -> list[dict]:
    """Fetch the projects for the requested /proyectos scope.

    ``scope`` is one of ``_SCOPE_FILTERS``. The status filter is applied
    uniformly (the owned scope also gets it from list_owned_projects, so
    filtering twice is harmless) to keep every listing consistent.
    
    When scope is "mios" (default), also includes group projects where
    the user is a member. When scope is "todos", includes owned,
    shared-with-me, and group projects, deduplicated by id.
    """
    if scope == "compartidos":
        projects = list_shared_by_me(user_id)
    elif scope == "invitado":
        projects = list_shared_with_me(user_id)
    elif scope == "todos":
        # All visible projects: owned + shared + group
        owned = list_owned_projects(user_id, status_filter)
        shared = list_shared_with_me(user_id)
        group = list_group_projects_for_user(user_id)
        merged: dict[int, dict] = {}
        for p in owned:
            merged[p["id"]] = {**p, "role": "owner"}
        for p in shared:
            if p["id"] not in merged:
                merged[p["id"]] = {**p, "role": "member"}
        for p in group:
            if p["id"] not in merged:
                merged[p["id"]] = {**p, "role": "group"}
        projects = list(merged.values())
    else:
        # Default: "mios" or "dueños" (alias) → owned + group
        owned = list_owned_projects(user_id, status_filter)
        group = list_group_projects_for_user(user_id)
        group_ids = {p["id"] for p in group}
        # Merge, deduplicate by id (owned wins, but mark group projects)
        merged: dict[int, dict] = {}
        for p in owned:
            if p["id"] in group_ids:
                merged[p["id"]] = {**p, "role": "group"}
            else:
                merged[p["id"]] = p
        for p in group:
            if p["id"] not in merged:
                merged[p["id"]] = {**p, "role": "group"}
        projects = list(merged.values())
    
    if status_filter:
        projects = [p for p in projects if p["status"] == status_filter]
    return projects


def _listing_header(scope: str, status_filter: str | None) -> str:
    """Return the Markdown header line for a /proyectos listing."""
    headers = {
        "todos": "*Todos tus proyectos*",
        "mios": "*Tus proyectos*",
        "compartidos": "*Proyectos compartidos por mí*",
        "invitado": "*Proyectos compartidos conmigo*",
    }
    header = headers.get(scope, "*Tus proyectos*")
    if status_filter:
        header += f" ({_status_label(status_filter)})"
    return header


def _empty_projects_text(scope: str, status_filter: str | None) -> str:
    """Return the Spanish empty-state message for a /proyectos listing."""
    if scope == "compartidos":
        return "No tienes proyectos compartidos."
    if scope == "invitado":
        return "No te han compartido proyectos."
    suffix = ""
    if status_filter:
        suffix = f" en estado {_status_label(status_filter)}"
    if scope == "todos":
        return (
            f"No tienes ningún proyecto{suffix}. "
            "Crea uno con /nuevoproyecto <nombre>."
        )
    return (
        f"Aún no tienes proyectos{suffix}. "
        "Crea uno con /nuevoproyecto <nombre>."
    )


def _format_project_line(
    project: dict, settings: Settings, scope: str
) -> str:
    """Format one project bullet for the /proyectos listing."""
    base = (
        f"• *{project['name']}* — {_priority_label(project['priority'])} · "
        f"{_status_label(project['status'])}"
    )
    if scope == "compartidos":
        count = project.get("member_count", 0)
        base += f" · compartido con {count}"
    elif scope == "invitado":
        owner_id = project.get("owner_id", project["user_id"])
        base += f" (de {_owner_label(owner_id)})"
    elif scope == "todos":
        role = project.get("role", "owner")
        if role == "member":
            owner_id = project.get("owner_id", project["user_id"])
            base += f" (de {_owner_label(owner_id)})"
        elif role == "group":
            base += " (grupo)"
    elif scope == "mios":
        role = project.get("role", "owner")
        if role == "group":
            base += " (grupo)"
    return (
        f"{base}\n"
        f"  _Último cambio de estado: {_format_date(project['status_changed_at'], settings.timezone)}_"
    )


async def proyectos_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /proyectos [dueños|compartidos|invitado] [estado] — list projects.
    
    In groups, only shows projects associated with that group.
    In private chats, shows all user's projects.
    """
    if not _auth_check(update):
        await _deny(update)
        return

    user_id = update.effective_user.id
    settings = context.bot_data["settings"]
    chat = update.effective_chat
    args = context.args or []

    logger.info("proyectos_command: chat_type=%s, chat_id=%s, user_id=%s", chat.type, chat.id, user_id)

    # Group context: only show projects associated with this group
    if chat.type in ("group", "supergroup"):
        projects = get_shared_projects_in_group(chat.id)
        logger.info("Group context: found %d projects for group_id=%s", len(projects), chat.id)
        if not projects:
            await update.message.reply_text(
                "No hay proyectos asociados a este grupo."
            )
            return

        lines = ["Proyectos del grupo:\n"]
        for project in projects:
            lines.append(_format_project_line(project, settings, "grupo"))
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.MARKDOWN
        )
        return

    # Private context: original logic
    scope = "mios"
    status_args = args
    if args and args[0] in _SCOPE_FILTERS:
        scope = args[0]
        status_args = args[1:]

    status_filter: str | None = None
    if status_args:
        # Join tokens so multi-word states work: "en proceso" -> "en_proceso"
        status_filter = _normalize_status(" ".join(status_args))
        if status_filter is None:
            await update.message.reply_text(
                f"Estado no reconocido. Válidos: {_valid_statuses_text()}"
            )
            return

    projects = _project_listing(user_id, scope, status_filter)

    if not projects:
        await update.message.reply_text(_empty_projects_text(scope, status_filter))
        return

    lines = [_listing_header(scope, status_filter) + "\n"]
    for project in projects:
        lines.append(_format_project_line(project, settings, scope))
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


async def nuevoproyecto_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /nuevoproyecto <nombre> — create a project.

    In groups, automatically creates a group project with all group members
    as collaborators. In private chats, creates a private project.
    """
    if not _auth_check(update):
        await _deny(update)
        return

    name = " ".join(context.args or []).strip()
    if not name:
        await update.message.reply_text(
            "Uso: /nuevoproyecto <nombre del proyecto>"
        )
        return

    user_id = update.effective_user.id
    chat = update.effective_chat

    # Group context: create project with group_id and auto-add members
    if chat.type in ("group", "supergroup"):
        from bot.projects import create_group_project
        project = create_group_project(user_id, chat.id, name)
    else:
        project = create_project(user_id, name)

    if isinstance(project, dict) and project.get("ok") is False:
        text = f"❌ No se pudo crear el proyecto: {project['error']}"
        existing = project.get("existing", [])
        if existing:
            names = "\n".join(f"• {p['name']}" for p in existing)
            text += f"\n\nProyectos urgentes actuales:\n{names}"
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    if project is None:
        await update.message.reply_text(
            f"Ya existe un proyecto llamado *{name}*. "
            "Usa /proyecto <nombre> para verlo.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        f"✅ Proyecto *{project['name']}* creado.\n\n"
        f"Para agregar notas: /agregarnota {project['slug']} <texto>\n"
        f"Para cambiar estado: /estadoproyecto {project['slug']} <estado>\n"
        f"Para cambiar prioridad: /prioridadproyecto {project['slug']} <prioridad>",
        parse_mode=ParseMode.MARKDOWN,
    )


async def proyecto_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /proyecto <nombre> — show project details."""
    if not _auth_check(update):
        await _deny(update)
        return

    name = " ".join(context.args or []).strip()
    if not name:
        await update.message.reply_text("Uso: /proyecto <nombre del proyecto>")
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None
    project = _find_project(user_id, name, group_id=group_id)

    if project is None:
        await update.message.reply_text(
            f"No encontré el proyecto *{name}*. "
            "Usa /proyectos para ver tus proyectos.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    notes = get_project_notes(project["id"])
    settings = context.bot_data["settings"]
    lines = [
        f"*{project['name']}*\n",
        f"Prioridad: {_priority_label(project['priority'])}",
        f"Estado: {_status_label(project['status'])}",
        f"Creado: {_format_date(project['created_at'], settings.timezone)}",
        f"Último cambio de estado: {_format_date(project['status_changed_at'], settings.timezone)}",
    ]
    if project["user_id"] != user_id:
        lines.append(f"Dueño: {_owner_label(project['user_id'])}")
    else:
        members = list_project_members(project["id"])
        if members:
            names = ", ".join(_member_label(m) for m in members)
            lines.append(f"Compartido con: {names}")
    if project["status"] == "parado" and project.get("stopped_reason"):
        lines.append(f"Razón de parada: {project['stopped_reason']}")
    lines.append(f"Notas: {len(notes)}")

    if project["description"]:
        lines.append(f"\n*Descripción:* {project['description']}")

    if notes:
        lines.append("\n*Notas recientes:*")
        for note in notes[-5:]:
            lines.append(f"• {note['summary']}")

    lines.append(
        f"\n_Comandos: /agregarnota {project['slug']} <texto> · "
        f"/estadoproyecto {project['slug']} <estado> · "
        f"/prioridadproyecto {project['slug']} <prioridad>_"
    )
    if project["status"] == "archivado":
        lines.append(
            "\n🗑️ Este proyecto está archivado. Puedes eliminarlo con "
            "/eliminarproyecto <nombre>."
        )
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


def _parse_share_args(args: list[str]) -> tuple[str, str] | None:
    """Split /compartir (or /quitaracceso) args into (project_name, username).

    The project name is everything except the last token and the last
    token is the @username, e.g. "/compartir Proyecto X @mama" ->
    ("Proyecto X", "@mama"). Returns None when there is no username.
    """
    if len(args) < 2:
        return None
    return " ".join(args[:-1]).strip(), args[-1]


async def _notify_shared_project(
    context: ContextTypes.DEFAULT_TYPE, guest_id: int, project_name: str
) -> None:
    """Proactively notify a new collaborator about a shared project.

    Best-effort: a failure to reach the invitee (no recorded chat,
    Telegram error, network hiccup) must never break a sharing that
    already succeeded, so exceptions are swallowed and logged.
    """
    chat_id = get_chat_for_user(guest_id)
    if chat_id is None:
        return
    try:
        await context.bot.send_message(
            chat_id,
            f"Te compartieron el proyecto *{project_name}*. "
            "Usa /proyectos para verlo.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:  # noqa: BLE001 - best-effort notification, never break sharing
        logger.exception(
            "No se pudo notificar al invitado %s del proyecto %s",
            guest_id,
            project_name,
        )


async def compartir_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /compartir <proyecto> @usuario — share a project with a user.

    Only the project owner may share (checked via ``is_owner`` before
    any membership change), the invitee must be a known user, and the
    owner cannot share a project with themselves.
    """
    if not _auth_check(update):
        await _deny(update)
        return

    parsed = _parse_share_args(context.args or [])
    if parsed is None:
        await update.message.reply_text(
            "Uso: /compartir <nombre del proyecto> @usuario"
        )
        return
    name, username = parsed

    user_id = update.effective_user.id
    chat = update.effective_chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None
    project = _find_project(user_id, name, group_id=group_id)
    if project is None:
        await update.message.reply_text(
            f"No encontré el proyecto *{name}*. "
            "Usa /proyectos para ver tus proyectos.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not is_owner(user_id, project["id"]):
        await update.message.reply_text(
            f"Solo el dueño del proyecto *{project['name']}* puede compartirlo.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    guest_id = resolve_username(username)
    if guest_id is None:
        await update.message.reply_text(
            "No conozco a ese usuario. "
            "El invitado debe haber hablado conmigo al menos una vez."
        )
        return

    if guest_id == user_id:
        await update.message.reply_text(
            "No puedes compartir un proyecto contigo mismo."
        )
        return

    result = add_project_member(project["id"], guest_id, user_id)
    if not result.get("ok"):
        await update.message.reply_text(
            f"❌ {result.get('error', 'No se pudo compartir el proyecto.')}"
        )
        return

    await update.message.reply_text(
        f"✅ Proyecto *{project['name']}* compartido con "
        f"{_username_label(username)}.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _notify_shared_project(context, guest_id, project["name"])


async def quitaracceso_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /quitaracceso <proyecto> @usuario — revoke a collaborator.

    Only the project owner may revoke access, and the owner's own access
    can never be removed (they are not in project_members by design).
    """
    if not _auth_check(update):
        await _deny(update)
        return

    parsed = _parse_share_args(context.args or [])
    if parsed is None:
        await update.message.reply_text(
            "Uso: /quitaracceso <nombre del proyecto> @usuario"
        )
        return
    name, username = parsed

    user_id = update.effective_user.id
    chat = update.effective_chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None
    project = _find_project(user_id, name, group_id=group_id)
    if project is None:
        await update.message.reply_text(
            f"No encontré el proyecto *{name}*. "
            "Usa /proyectos para ver tus proyectos.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not is_owner(user_id, project["id"]):
        await update.message.reply_text(
            f"Solo el dueño del proyecto *{project['name']}* "
            "puede quitar el acceso.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    guest_id = resolve_username(username)
    if guest_id is None:
        await update.message.reply_text("No conozco a ese usuario.")
        return

    if guest_id == project["user_id"]:
        await update.message.reply_text(
            "No puedes quitar el acceso al dueño del proyecto."
        )
        return

    removed = remove_project_member(project["id"], guest_id, user_id)
    if not removed.get("ok"):
        await update.message.reply_text(
            f"❌ {removed.get('error', 'No se pudo quitar el acceso.')}"
        )
        return

    await update.message.reply_text(
        f"✅ Se quitó el acceso a {_username_label(username)} del proyecto "
        f"*{project['name']}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


def _parse_status_change_args(
    args: list[str],
) -> tuple[str, str, str | None] | None:
    """Split /estadoproyecto args into (project_name, status, reason).

    The status is the first token (or two-token phrase like "en proceso" /
    "sin iniciar") that matches a valid status; everything before is the
    project name, everything after is the optional reason. Returns None
    when no valid status is found.
    """
    for i, arg in enumerate(args):
        # Try a two-word phrase first ("en proceso"), then the single token.
        if len(args) > i + 1:
            phrase_status = _normalize_status(" ".join(args[i : i + 2]))
        else:
            phrase_status = None
        if phrase_status is not None:
            name = " ".join(args[:i]).strip()
            reason = " ".join(args[i + 2 :]).strip() or None
            return name, phrase_status, reason
        single_status = _normalize_status(arg)
        if single_status is not None:
            name = " ".join(args[:i]).strip()
            reason = " ".join(args[i + 1 :]).strip() or None
            return name, single_status, reason
    return None


async def estadoproyecto_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /estadoproyecto <nombre> <estado> [razón] — update status."""
    if not _auth_check(update):
        await _deny(update)
        return

    parsed = _parse_status_change_args(context.args or [])
    if parsed is None:
        await update.message.reply_text(
            f"Uso: /estadoproyecto <nombre> <estado> [razón]\n"
            f"Estados válidos: {_valid_statuses_text()}"
        )
        return

    name, new_status, reason = parsed
    if not name:
        await update.message.reply_text(
            f"Uso: /estadoproyecto <nombre> <estado> [razón]\n"
            f"Estados válidos: {_valid_statuses_text()}"
        )
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None
    project = _find_project(user_id, name, group_id=group_id)
    if project is None:
        await update.message.reply_text(
            f"No encontré el proyecto *{name}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Refuse to reactivate an urgent project beyond the 3-urgent limit.
    if (
        project["priority"] == "urgente"
        and project["status"] not in ACTIVE_STATUSES
        and new_status in ACTIVE_STATUSES
    ):
        can, existing = can_activate_urgent(project["user_id"], project["id"])
        if not can:
            names = "\n".join(f"• {p['name']}" for p in existing)
            await update.message.reply_text(
                f"❌ No puedo activar *{project['name']}*: el dueño ya tiene 3 "
                f"proyectos urgentes activos.\n\nProyectos urgentes actuales:\n"
                f"{names}\n\n"
                "Baja la prioridad de uno con /prioridadproyecto.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    updated = update_project_status(project["id"], new_status, stopped_reason=reason)
    if not updated:
        await update.message.reply_text(
            f"❌ No se pudo actualizar el estado de *{project['name']}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    text = (
        f"✅ Estado de *{project['name']}* actualizado a "
        f"{_status_label(new_status)}."
    )
    if new_status == "parado" and reason:
        text += f"\nRazón: _{reason}_"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    # Notify group members about the change
    await notify_group_project_change(
        context, project, "status", user_id,
        detail=f" a {_status_label(new_status)}",
    )


async def prioridadproyecto_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /prioridadproyecto <nombre> <prioridad> — update priority."""
    if not _auth_check(update):
        await _deny(update)
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            f"Uso: /prioridadproyecto <nombre> <prioridad>\n"
            f"Prioridades válidas: {_valid_priorities_text()}"
        )
        return

    priority = args[-1].lower().strip()
    name = " ".join(args[:-1]).strip()

    if priority not in PROJECT_PRIORITIES:
        await update.message.reply_text(
            f"Prioridad inválida. Válidas: {_valid_priorities_text()}"
        )
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None
    project = _find_project(user_id, name, group_id=group_id)
    if project is None:
        await update.message.reply_text(
            f"No encontré el proyecto *{name}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    result = update_project_priority(project["id"], priority)
    if result.get("ok") is False:
        text = f"❌ {result['error']}"
        existing = result.get("existing", [])
        if existing:
            names = "\n".join(f"• {p['name']}" for p in existing)
            text += f"\n\nProyectos urgentes actuales:\n{names}"
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    updated = result["project"]
    await update.message.reply_text(
        f"✅ Prioridad de *{updated['name']}* actualizada a "
        f"{_priority_label(updated['priority'])}.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Notify group members about the change
    await notify_group_project_change(
        context, project, "priority", user_id,
        detail=f" a {_priority_label(updated['priority'])}",
    )


async def agregarnota_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /agregarnota <proyecto> <texto> — add a note to a project."""
    if not _auth_check(update):
        await _deny(update)
        return

    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text(
            "Uso: /agregarnota <nombre del proyecto> <texto de la nota>"
        )
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None

    # Find the longest project-name prefix so multi-word names work:
    # "/agregarnota Arreglar el carro comprar repuesto" -> project
    # "Arreglar el carro", note "comprar repuesto".
    tokens = text.split(" ")
    project = None
    name = ""
    note_text = ""
    for i in range(len(tokens) - 1, 0, -1):
        candidate = " ".join(tokens[:i])
        project = _find_project(user_id, candidate, group_id=group_id)
        if project is not None:
            name = candidate
            note_text = " ".join(tokens[i:])
            break

    if project is None:
        await update.message.reply_text(
            f"No encontré el proyecto. Escribe la nota así:\n"
            "/agregarnota <nombre del proyecto> <texto de la nota>"
        )
        return

    summary = note_text[:80] + ("..." if len(note_text) > 80 else "")
    add_project_note(project["id"], user_id, summary, content=note_text)
    await update.message.reply_text(
        f"📝 Nota agregada al proyecto *{project['name']}*.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Notify group members about the new note
    await notify_group_project_change(context, project, "note", user_id)


async def buscarproyecto_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /buscarproyecto <término> — search projects."""
    if not _auth_check(update):
        await _deny(update)
        return

    query = " ".join(context.args or []).strip()
    if not query:
        await update.message.reply_text(
            "Uso: /buscarproyecto <término de búsqueda>"
        )
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None
    results = search_projects(user_id, query, group_id=group_id)

    if not results:
        await update.message.reply_text(
            f"No encontré proyectos que coincidan con *{query}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f'*Resultados para "{query}":*\n']
    for p in results:
        lines.append(
            f"• *{p['name']}* — {_priority_label(p['priority'])} · "
            f"{_status_label(p['status'])}"
        )
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


async def eliminarproyecto_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /eliminarproyecto <nombre> — ask to delete an archived project.

    Only the owner may delete, and only when the project is already
    archived. Nothing is deleted here: the command renders a confirmation
    keyboard ("Sí, eliminar" / "Cancelar") and ``delete_project_callback``
    performs the deletion after re-checking the same gates (auth, owner,
    archived), so a fat-fingered command cannot destroy data by itself.
    """
    if not _auth_check(update):
        await _deny(update)
        return

    name = " ".join(context.args or []).strip()
    if not name:
        await update.message.reply_text(
            "Uso: /eliminarproyecto <nombre del proyecto>"
        )
        return

    user_id = update.effective_user.id
    chat = update.effective_chat
    group_id = chat.id if chat.type in ("group", "supergroup") else None
    project = _find_project(user_id, name, group_id=group_id)
    if project is None:
        await update.message.reply_text(
            f"No encontré el proyecto *{name}*. "
            "Usa /proyectos para ver tus proyectos.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not is_owner(user_id, project["id"]):
        await update.message.reply_text(
            f"Solo el dueño del proyecto *{name}* puede eliminarlo.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if project["status"] != "archivado":
        await update.message.reply_text(
            f"El proyecto *{name}* no está archivado. "
            "Solo puedes eliminar proyectos archivados. "
            f"Archívalo primero con /estadoproyecto {name} archivado.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🗑️ Sí, eliminar", callback_data=f"delproj:{project['id']}"
                ),
                InlineKeyboardButton(
                    "Cancelar", callback_data=f"delproj_cancel:{project['id']}"
                ),
            ]
        ]
    )
    await update.message.reply_text(
        f"⚠️ ¿Eliminar definitivamente el proyecto archivado *{project['name']}*?\n\n"
        "Se borrarán sus notas y archivos. Esta acción no se puede deshacer.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


async def delete_project_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline button taps on /eliminarproyecto: confirm or cancel.

    Mirrors ``handlers.reminder_callback``: callback data is user-controlled,
    so it is parsed defensively with ``partition`` and the id is validated as
    an int before any action. The actor's ``user_id`` always comes from the
    Telegram update, never from the callback data. The cancel branch only
    answers/edits the message; the confirm branch delegates to
    ``delete_project``, which re-validates ownership and the archived status
    at the data layer.
    """
    query = update.callback_query
    data = query.data or ""
    prefix, _, id_str = data.partition(":")
    try:
        project_id = int(id_str)
    except ValueError:
        await query.answer(text="Botón no válido.", show_alert=True)
        return

    if not _auth_check(update):
        await query.answer(text="No autorizado.", show_alert=True)
        return

    if prefix == "delproj_cancel":
        await query.answer(text="Cancelado")
        if query.message:
            await query.edit_message_text(
                "Cancelado. Tu proyecto sigue archivado ✅"
            )
        return

    if prefix != "delproj":
        await query.answer(text="Acción desconocida.", show_alert=True)
        return

    user_id = update.effective_user.id
    result = delete_project(project_id, user_id)
    if not result.get("ok"):
        await query.answer(
            text=result.get("error", "No se pudo eliminar."), show_alert=True
        )
        return

    await query.answer(text="Proyecto eliminado")
    if query.message:
        await query.edit_message_text(
            "Ok, proyecto eliminado ✅"
        )
