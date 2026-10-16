from dataclasses import dataclass, field
import os
from dotenv import load_dotenv

load_dotenv()


_DEFAULT_SYSTEM_PROMPT = (
    "Eres un asistente personal útil y conciso. Responde siempre en español "
    "con un tono cercano y claro.\n\n"
    "Puedes gestionar los proyectos del usuario. Cada proyecto tiene:\n"
    "- Estados: Sin iniciar (sin_iniciar), En proceso (en_proceso), Parado (parado), "
    "Completado (completado), Archivado (archivado).\n"
    "- Prioridades: Baja (baja), Media (media), Alta (alta), Urgente (urgente).\n\n"
    "Reglas importantes:\n"
    "- Solo puede haber hasta 3 proyectos urgentes en estados activos "
    "(Sin iniciar o En proceso). Si el usuario intenta crear o dejar un cuarto "
    "proyecto urgente activo, explica el límite y lista los 3 proyectos urgentes actuales.\n"
    "- El motivo de parada es opcional cuando el estado es Parado.\n\n"
    "Los proyectos se pueden compartir con otros usuarios autorizados usando su "
    "@username de Telegram, por ejemplo: \"comparte el proyecto Casa con @mama\", "
    "\"con quién está compartido el proyecto Casa\" o \"deja de compartir el proyecto "
    "Casa con @mama\". El usuario solo puede ver sus propios proyectos y los que le "
    "han compartido; únicamente el dueño de un proyecto puede compartirlo o quitarle "
    "el acceso a un colaborador.\n\n"
    "Los proyectos archivados pueden eliminarse definitivamente para liberar espacio. "
    "Solo el dueño puede eliminar un proyecto, y solo si está archivado. "
    "La eliminación es irreversible (se borran las notas y los archivos del proyecto). "
    "La herramienta delete_project solo debe usarse cuando el usuario lo pida "
    "explícitamente; al llamarla el bot mostrará una confirmación con botones y "
    "el proyecto solo se eliminará si el usuario confirma.\n\n"
    "El usuario puede pedirte todo por texto natural, por ejemplo: "
    "\"crea un proyecto llamado Reparar bici con prioridad alta\", "
    "\"muéstrame mis proyectos urgentes\", "
    "\"marca Remodelación como en proceso\", "
    "\"cambia la prioridad de Tesis a urgente\", "
    "\"agrega una nota al proyecto Carro: pedí presupuesto\", "
    "\"busca proyectos de casa\" o "
    "\"recuérdame llamar al mecánico mañana a las 10am\".\n\n"
    "Cuando el usuario mencione proyectos, tareas, recordatorios o cualquier "
    "petición de acción, usa las herramientas disponibles para ejecutarla directamente. "
    "Cuando necesites información actual o datos que no conozcas con certeza, "
    "usa la herramienta web_search y cita las fuentes. "
    "Si el usuario pide crear, dibujar o generar una imagen, usa la herramienta "
    "generate_image y confirma qué imagen creaste.\n\n"
    "Importante: nunca uses tablas Markdown en tus respuestas (Telegram no las "
    "muestra bien). Para listar recordatorios o proyectos usa listas simples "
    "con viñetas (•). Cuando una herramienta devuelva un campo 'formatted', "
    "repítelo tal cual sin convertirlo a tabla.\n\n"
    "El proveedor y el modelo de IA están fijados por configuración y NO se "
    "pueden cambiar ni por comando ni pidiéndolo en la conversación. Si el "
    "usuario pide cambiar de modelo o proveedor, responde amablemente que no "
    "es posible y que se usa el modelo configurado.\n\n"
    "El bot lleva una memoria a largo plazo del usuario (una bitácora personal "
    "de su día a día). El bot GUARDA AUTOMÁTICAMENTE eventos importantes de "
    "cada conversación en tu bitácora — no necesitas pedirle que guarde algo. "
    "También puedes usar journal_append para guardar cosas específicas que "
    "quieras asegurar. Para tareas o proyectos usa add_project_note; para "
    "recordatorios usa create_reminder.\n\n"
    "Cuando el usuario pregunte por algo que ocurrió antes de esta "
    "conversación ('¿cuándo fue mi último viaje a Panamá?', '¿qué comí la "
    "semana pasada?', '¿cuándo pagué la luz?', '¿cómo estuve de ánimo en "
    "abril?'), usa la herramienta memory_recall para buscar en su memoria. "
    "Si necesitas el detalle exacto de un periodo concreto que la búsqueda "
    "indique, usa memory_read. Si el usuario pregunta por sus hábitos o "
    "patrones ('¿he hecho ejercicio esta semana?', '¿qué notas de mi rutina?'), "
    "usa memory_insights. La memoria es por usuario y nunca "
    "inventes hechos: si no encuentras algo, dilo con honestidad y ofrece "
    "guardarlo cuando el usuario te lo cuente.\n\n"
    "El bot también es proactivo: puede enviar check-ins por la mañana y por "
    "la tarde y recordatorios basados en los patrones de la bitácora (rachas "
    "de ejercicio, pagos pendientes, ausencias). Responde a esos mensajes "
    "con naturalidad y sigue usando las herramientas de memoria cuando sea "
    "relevante. El usuario puede ajustar la hora de sus check-ins por "
    "lenguaje natural (por ejemplo, 'cambia mi check-in de la mañana a las "
    "9' o 'adelanta mi check-in de la tarde'); si lo pide, usa la "
    "herramienta set_checkin_time con la hora en formato HH:MM (24h) en su "
    "zona horaria. Todo esto se maneja por lenguaje natural, sin comandos "
    "especiales.\n\n"
    "El bot también trackea entidades importantes: personas, lugares, "
    "documentos y casos pendientes. Cuando el usuario mencione una persona "
    "('mi jefe me dijo X'), un lugar ('la cita es en Y'), un documento "
    "('tengo que llevar los papeles') o un caso ('me agendaron una cita'), "
    "usa las tools de entities (list_entities, update_entity, "
    "get_entity_details, add_entity_note, search_entities) para registrarlo "
    "y dar seguimiento. Cada entidad puede tener notas de seguimiento para "
    "rastrear evoluciones."
)


def _parse_bool(raw: str | None) -> bool:
    """Parse a boolean env var leniently.

    Treats "1", "true", "yes", "on" (case-insensitive) as True and
    everything else as False. Missing/empty input falls back to False so a
    malformed variable can never silently enable a feature.
    """
    if not raw:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_authorized_users(raw: str | None) -> frozenset[int]:
    """Parse a comma-separated list of Telegram user IDs into a frozenset.

    Missing or empty input yields an empty set (fail-secure: nobody is
    authorized). Malformed entries — non-numeric or non-positive — are
    skipped so a typo can never grant access to an unintended user.
    """
    if not raw:
        return frozenset()
    user_ids: set[int] = set()
    for token in raw.split(","):
        value = token.strip()
        if not value:
            continue
        try:
            user_id = int(value)
        except ValueError:
            continue
        if user_id > 0:
            user_ids.add(user_id)
    return frozenset(user_ids)


@dataclass(frozen=True)
class Settings:
    """Application settings loaded from environment variables."""

    bot_token: str = field(default_factory=lambda: os.environ["BOT_TOKEN"])
    authorized_users: frozenset[int] = field(
        default_factory=lambda: _parse_authorized_users(
            os.environ.get("AUTHORIZED_USERS")
        )
    )
    opencode_api_key: str = field(
        default_factory=lambda: os.environ.get("OPENCODE_GO_API_KEY", "")
    )
    opencode_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "OPENCODE_BASE_URL", "https://opencode.ai/zen/go/v1"
        )
    )
    model: str = field(
        default_factory=lambda: os.environ.get("MODEL", "mimo-v2.5")
    )
    ai_provider: str = field(
        default_factory=lambda: os.environ.get("AI_PROVIDER", "opencode")
    )
    gemini_api_key: str = field(
        default_factory=lambda: os.environ.get("GEMINI_API_KEY", "")
    )
    gemini_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "GEMINI_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        )
    )
    gemini_model: str = field(
        default_factory=lambda: os.environ.get(
            "GEMINI_MODEL", "gemini-3.1-flash-lite"
        )
    )
    gemini_media_model: str = field(
        default_factory=lambda: os.environ.get(
            "GEMINI_MEDIA_MODEL", "gemini-3.6-flash"
        )
    )
    gemini_image_model: str = field(
        default_factory=lambda: os.environ.get(
            "GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"
        )
    )
    media_image_daily_limit: int = field(
        default_factory=lambda: int(
            os.environ.get("MEDIA_IMAGE_DAILY_LIMIT", "30")
        )
    )
    media_audio_daily_limit: int = field(
        default_factory=lambda: int(
            os.environ.get("MEDIA_AUDIO_DAILY_LIMIT", "30")
        )
    )
    max_context_messages: int = field(
        default_factory=lambda: int(
            os.environ.get("MAX_CONTEXT_MESSAGES", "30")
        )
    )
    rate_limit_seconds: float = field(
        default_factory=lambda: float(
            os.environ.get("RATE_LIMIT_SECONDS", "5")
        )
    )
    max_tool_rounds: int = field(
        default_factory=lambda: int(os.environ.get("MAX_TOOL_ROUNDS", "12"))
    )
    system_prompt: str = field(
        default_factory=lambda: os.environ.get(
            "SYSTEM_PROMPT",
            _DEFAULT_SYSTEM_PROMPT,
        )
    )
    timezone: str = field(
        default_factory=lambda: os.environ.get("TIMEZONE", "America/Caracas")
    )
    memory_enabled: bool = field(
        default_factory=lambda: _parse_bool(os.environ.get("MEMORY_ENABLED", "true"))
    )
    memory_daily_compact_words: int = field(
        default_factory=lambda: int(
            os.environ.get("MEMORY_DAILY_COMPACT_WORDS", "1200")
        )
    )
    proactive_enabled: bool = field(
        default_factory=lambda: _parse_bool(os.environ.get("PROACTIVE_ENABLED", "true"))
    )
    proactive_morning_time: str = field(
        default_factory=lambda: os.environ.get("PROACTIVE_MORNING_TIME", "08:00")
    )
    proactive_evening_time: str = field(
        default_factory=lambda: os.environ.get("PROACTIVE_EVENING_TIME", "20:00")
    )
    proactive_max_daily: int = field(
        default_factory=lambda: int(os.environ.get("PROACTIVE_MAX_DAILY", "3"))
    )
    log_level: str = field(
        default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO")
    )
    # Group support settings
    group_response_mode: str = field(
        default_factory=lambda: os.environ.get("GROUP_RESPONSE_MODE", "public")
    )
    group_auto_add_members: bool = field(
        default_factory=lambda: _parse_bool(os.environ.get("GROUP_AUTO_ADD_MEMBERS", "true"))
    )
    # Auto-journal tuning (60min default, ~75% token saving vs 30min)
    auto_journal_interval: int = field(
        default_factory=lambda: int(os.environ.get("AUTO_JOURNAL_INTERVAL", "3600"))
    )
    auto_journal_min_messages: int = field(
        default_factory=lambda: int(os.environ.get("AUTO_JOURNAL_MIN_MESSAGES", "6"))
    )


def load_settings() -> Settings:
    """Validate and return application settings."""
    provider = os.environ.get("AI_PROVIDER", "opencode").strip().lower()
    if provider not in ("opencode", "gemini"):
        raise ValueError(
            f"AI_PROVIDER inválido: '{provider}'. Usa 'opencode' o 'gemini'."
        )

    missing: list[str] = []
    if not os.environ.get("BOT_TOKEN"):
        missing.append("BOT_TOKEN")
    if provider == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            missing.append("GEMINI_API_KEY")
    elif not os.environ.get("OPENCODE_GO_API_KEY"):
        missing.append("OPENCODE_GO_API_KEY")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Did you create a .env file from .env.example?"
        )

    return Settings()
