import logging
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    Defaults,
    MessageHandler,
    filters,
)

from bot.config import load_settings
from bot.ai_client import _build_client
from bot.handlers import (
    start_command,
    help_command,
    info_command,
    reiniciar_command,
    dolar_command,
    handle_message,
    handle_message_group,
    handle_chat_member,
    handle_voice,
    handle_photo,
    imagina_command,
    recordar_command,
    recordar_command_group,
    recordatorios_command,
    cancelarrecordatorio_command,
    editarrecordatorio_command,
    posponerrecordatorio_command,
    reprogramarrecordatorio_command,
    reminder_callback,
    check_due_reminders,
)
from bot.project_commands import (
    proyectos_command,
    nuevoproyecto_command,
    proyecto_command,
    estadoproyecto_command,
    prioridadproyecto_command,
    agregarnota_command,
    buscarproyecto_command,
    eliminarproyecto_command,
    compartir_command,
    quitaracceso_command,
    delete_project_callback,
)
from bot.proactive import (
    run_checkins,
    run_nightly_compaction,
    run_weekly_summaries,
    run_quarterly_summaries,
    run_yearly_summaries,
)
from bot.auto_journal import auto_journal_job

logger = logging.getLogger(__name__)


def create_application() -> Application:
    """Build and return the PTB Application with all handlers wired."""
    settings = load_settings()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
    )
    # Suppress noisy PTB logs at INFO level
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Build the PTB application with the configured timezone
    app = (
        Application.builder()
        .token(settings.bot_token)
        .defaults(Defaults(tzinfo=ZoneInfo(settings.timezone)))
        .build()
    )

    # Shared state accessible by all handlers
    app.bot_data["settings"] = settings
    app.bot_data["provider"] = settings.ai_provider
    app.bot_data["ai_client"] = _build_client(settings)

    # Command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("iniciar", start_command))
    app.add_handler(CommandHandler("ayuda", help_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("reiniciar", reiniciar_command))
    app.add_handler(CommandHandler("dolar", dolar_command))

    # Project command handlers
    app.add_handler(CommandHandler("proyectos", proyectos_command))
    app.add_handler(CommandHandler("nuevoproyecto", nuevoproyecto_command))
    app.add_handler(CommandHandler("proyecto", proyecto_command))
    app.add_handler(CommandHandler("estadoproyecto", estadoproyecto_command))
    app.add_handler(CommandHandler("prioridadproyecto", prioridadproyecto_command))
    app.add_handler(CommandHandler("agregarnota", agregarnota_command))
    app.add_handler(CommandHandler("buscarproyecto", buscarproyecto_command))
    app.add_handler(CommandHandler("eliminarproyecto", eliminarproyecto_command))
    app.add_handler(CommandHandler("compartir", compartir_command))
    app.add_handler(CommandHandler("quitaracceso", quitaracceso_command))

    # Reminder command handlers
    app.add_handler(CommandHandler("recordar", recordar_command))
    # /recordar in groups — delivers to private chat
    app.add_handler(
        CommandHandler(
            "recordar",
            recordar_command_group,
            filters.ChatType.GROUP | filters.ChatType.SUPERGROUP,
        )
    )
    app.add_handler(CommandHandler("recordatorios", recordatorios_command))
    app.add_handler(CommandHandler("cancelarrecordatorio", cancelarrecordatorio_command))
    app.add_handler(CommandHandler("editarrecordatorio", editarrecordatorio_command))
    app.add_handler(CommandHandler("posponerrecordatorio", posponerrecordatorio_command))
    app.add_handler(CommandHandler("reprogramarrecordatorio", reprogramarrecordatorio_command))
    # Inline buttons on /recordatorios: cancel or postpone a reminder
    app.add_handler(
        CallbackQueryHandler(reminder_callback, pattern=r"^remind_(cancel|postpone):\d+$")
    )
    # Inline buttons on /eliminarproyecto: confirm or cancel deletion
    app.add_handler(
        CallbackQueryHandler(delete_project_callback, pattern=r"^delproj(_cancel)?:\d+$")
    )

    # Text message handlers — separate private and group
    # Private chats: full functionality
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_message,
        )
    )
    # Group chats: scoped to group projects, restricted tools
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND
            & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            handle_message_group,
        )
    )

    # Media handlers — process voice notes and photos via Gemini (private only)
    app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.PRIVATE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
    app.add_handler(CommandHandler("imagina", imagina_command))

    # Group lifecycle: migrate projects when bot is removed
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Periodic job: deliver due reminders every 20 seconds
    app.job_queue.run_repeating(
        check_due_reminders,
        interval=20,
        first=10,
    )

    _register_memory_jobs(app, settings)

    logger.info(
        "Bot configured — model=%s, tz=%s, context=%d msgs, rate_limit=%.1fs",
        settings.model,
        settings.timezone,
        settings.max_context_messages,
        settings.rate_limit_seconds,
    )

    return app


def _register_memory_jobs(app, settings) -> None:
    """Register the memory/proactive background jobs guarded by Settings.

    The nightly compaction and the weekly/quarterly/yearly summary jobs
    only run when MEMORY_ENABLED; the check-in driver also requires
    PROACTIVE_ENABLED. All summary jobs are scheduled DAILY at a fixed time:
    each is a catch-up scanner that looks back a bounded window (3 days /
    8 weeks / 4 quarters / 2 years) and generates anything missing, so a
    server that was down when a period closed self-heals on the next run
    instead of waiting for the period's nominal closing day. The check-in
    driver runs every 15 minutes and resolves each user's own window (per-
    user times from the chat, global PROACTIVE_* as defaults), so custom
    times need no per-user scheduling.
    """
    if not settings.memory_enabled:
        return

    app.job_queue.run_daily(
        run_nightly_compaction, time=dt_time(0, 15), name="memory_nightly_compaction"
    )
    app.job_queue.run_daily(
        run_weekly_summaries, time=dt_time(0, 30), name="memory_weekly_summaries"
    )
    app.job_queue.run_daily(
        run_quarterly_summaries, time=dt_time(0, 45), name="memory_quarterly_summaries"
    )
    app.job_queue.run_daily(
        run_yearly_summaries, time=dt_time(1, 0), name="memory_yearly_summaries"
    )

    # Auto-journal: extract events from chat history (60min + threshold, ~75% saving)
    interval = int(getattr(settings, "auto_journal_interval", 3600))
    app.job_queue.run_repeating(
        auto_journal_job,
        interval=interval,
        first=120,  # First run 2 minutes after bot starts
        name="auto_journal",
    )

    if not settings.proactive_enabled:
        return

    app.job_queue.run_repeating(
        run_checkins,
        interval=900,
        first=60,
        name="memory_checkins",
    )
