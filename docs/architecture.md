# Architecture — Telegram AI Bot

Documentación técnica del proyecto. **Solo para desarrollo** — el bot de Telegram jamás lee este archivo (ver `AGENTS.md`).

## Vista general

Bot de Telegram con IA para uso personal/familiar. Usa la API de **OpenCode Go** (compatible OpenAI) para conversar en español, con un bucle de *function calling* que permite al modelo invocar herramientas del sistema.

## Componentes

### `main.py` — Entry point
Configura logging, valida `Settings`, instancia `Application` de python-telegram-bot, registra handlers y arranca el long-polling.

### `bot/config.py` — Configuración
`Settings` es un `@dataclass(frozen=True)` construido desde variables de entorno. **Fail-secure**: valida que existan las variables requeridas (`BOT_TOKEN` y la key del proveedor activo) y lanza `ValueError` si falta alguna.

Proveedores:
- `opencode` (default) → `OPENCODE_GO_API_KEY`, `OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1`
- `gemini` → `GEMINI_API_KEY`, `GEMINI_BASE_URL` (endpoint OpenAI-compatible)

### `bot/access.py` — Control de acceso
Middleware que rechaza cualquier update de un `user_id` no listado en `AUTHORIZED_USERS`. Sin `AUTHORIZED_USERS` → nadie accede.

### `bot/ai_client.py` — Cliente IA
Wrapper sobre `AsyncOpenAI` con:
- Retry con backoff exponencial (429 y errores 5xx)
- `chat_completion_with_tools` → soporta *tool calls*, y devuelve `{"kind": "tool_call" | "text", ...}`
- Manejo de `reasoning_content` (DeepSeek thinking) y `thought_signature` (Gemini) que deben reenviarse al API en el mensaje del assistant cuando hay tool call
- Helpers multimodales: `transcribe_audio`, `describe_image` (Gemini vision) y `generate_image` (Gemini image)

### `bot/handlers.py` — Comandos + chat + loop de tools
- Comandos: `/iniciar`, `/ayuda`, `/info`, `/reiniciar`, `/dolar`, `/imagina`, los de recordatorios (`/recordar`, `/recordatorios`, `/cancelarrecordatorio`, `/editarrecordatorio`, `/posponerrecordatorio`, `/reprogramarrecordatorio`) y los de proyectos (ver `project_commands.py`)
- `handle_message`: guarda el mensaje en historial, construye el contexto **consciente del tiempo** (30 mensajes por defecto vía `get_history_with_timestamps` → `_format_history_for_model` prefija `[YYYY-MM-DD HH:MM]` en `TIMEZONE`), filtra `TOOL_DEFINITIONS` por `group_id`, llama al modelo con tools, y si hay tool call ejecuta el **tool loop** (máx. `MAX_TOOL_ROUNDS` rondas, default 12) hasta obtener respuesta de texto. `handle_voice`/`handle_photo` usan el mismo flujo (transcripción/descripción → historial con timestamp → tool loop).
- `handle_message_group`: handler para mensajes en grupos. Paridad v1: proyectos + recordatorios/bitácora/memoria/web_search/dólar compartidos por `group_id` (solo `generate_image`/`set_checkin_time` y `share/unshare/delete/list_members` quedan privados); inyecta `_group_id` y `_author_username` y también usa historial con timestamps.
- `handle_chat_member`: detecta cuando el bot es removido de un grupo y borra todo lo del grupo (`cleanup_group_data`)
- `handle_voice`: descarga la nota de voz, la convierte con ffmpeg y la transcribe con Gemini
- `handle_photo`: descarga la foto y la analiza con visión de Gemini (en memoria, se elimina al terminar)
- `reminder_callback`: maneja los botones inline de recordatorios (cancelar / posponer 1h)

### `bot/project_commands.py` — Comandos de proyectos
Handlers de `/proyectos` (con filtros `todos`/`mios`/`dueños`/`compartidos`/`invitado`), `/nuevoproyecto`, `/proyecto` (muestra dueño y compartidos), `/estadoproyecto`, `/prioridadproyecto`, `/agregarnota`, `/buscarproyecto`, `/compartir`, `/quitaracceso` y `/eliminarproyecto` (con confirmación por botones inline `delproj:<id>` / `delproj_cancel:<id>`). Sobre `bot/projects.py`. La compartición usa ACL: `_find_project` delega en `get_project_for_user`, y `/compartir`/`/quitaracceso` exigen `is_owner` y notifican proactivamente al invitado (vía `get_chat_for_user`). La eliminación exige ser dueño y que el proyecto esté `archivado`; el borrado efectivo ocurre en el callback tras re-validar esas mismas puertas. Los comandos de cambio de estado/prioridad/nota envían notificaciones a grupos cuando el proyecto es de grupo (vía `notify_group_project_change`).

### `bot/projects.py` — Persistencia de proyectos
CRUD en SQLite (`projects.db`) + espejo legible en Markdown (`projects/{owner_id}/{slug}/overview.md`, `notes/{id}.md`, `context.json`). El `context.json` es el resumen compacto que la tool `get_project` devuelve al modelo.

Además de `projects` y `project_notes`, el schema incluye:
- `project_members` — ACL por objeto: `(project_id → projects.id CASCADE, user_id, invited_by, created_at, UNIQUE(project_id, user_id))`. El dueño vive en `projects.user_id` (fuente de verdad única); esta tabla solo guarda invitados con un único rol de colaborador completo.
- `users` — perfiles Telegram `(user_id PK, username, first_name, private_chat_id, updated_at)` para resolver `@username → user_id` (poblada en cada update). `private_chat_id` se almacena para entregar recordatorios desde contexto de grupo.
- `group_members` — membresía de grupos: `(group_id, user_id, first_seen_at, UNIQUE(group_id, user_id))`. Registra qué usuarios han interactuado en cada grupo.

Núcleo de autorización: `get_project_for_user` (dueño o miembro — sin filtración por ID), `is_owner`, `add_project_member`/`remove_project_member` (exigen ser dueño en la capa de datos, rechazan auto-invitación), `list_project_members`, `list_owned_projects`, `list_shared_with_me`, `list_shared_by_me`, `resolve_username` (determinista), `upsert_user`, `get_user_profile`. `delete_project(project_id, actor_id)` elimina **solo proyectos archivados y solo del dueño** (defensa en profundidad en la capa de datos, igual que `remove_project_member`): borra la fila (cascada FK a `project_notes`/`project_members`) y elimina la carpeta Markdown vía `_remove_project_dir` (guardado contra path traversal con `is_relative_to(PROJECTS_DIR)`). `search_projects` y `list_projects` incluyen proyectos compartidos. La regla de los 3 urgentes siempre cuenta contra el dueño del proyecto.

**Soporte de grupos**: los proyectos pueden asociarse a grupos de Telegram mediante el campo `group_id`. Funciones clave:
- `add_group_member(group_id, user_id)` — registra un usuario como miembro de un grupo
- `get_group_members(group_id)` — retorna todos los miembros de un grupo
- `get_shared_projects_in_group(group_id)` — retorna proyectos asociados al grupo
- `get_private_chat_id(user_id)` — retorna el chat privado del usuario (para recordatorios)
- `create_group_project(owner_id, group_id, name, ...)` — crea un proyecto en grupo y añade automáticamente a todos los miembros como colaboradores
- `migrate_group_projects_to_private(group_id)` — migra proyectos a privado cuando el bot es removido de un grupo
- `get_group_project_notify_targets(project_id, exclude_user_id)` — retorna user_ids a notificar cuando cambia un proyecto de grupo
- `get_project_owner_name(user_id)` — retorna nombre de display del dueño (para notificaciones)

### `bot/reminders.py` — Recordatorios
SQLite (`projects.db` tabla `reminders` con `group_id`/`created_by`). Soporta one-time y recurrentes (`daily`/`weekly`/`monthly`) y **modo compartido por grupo**: `group_id IS NOT NULL` entrega en el grupo y `list_pending_reminders(..., group_id)` aísla por tenant. El scheduler de PTB dispara los recordatorios y reprograma la próxima ocurrencia. Si un envío falla, se reintenta con backoff exponencial (10s → 8h) usando las columnas `attempts`/`next_retry_at`; editar o posponer un recordatorio resetea ese estado.

### `bot/history.py` — Historial de chat
SQLite (`chat_history.db`), mensajes por usuario con orden y ventana de contexto. Cada fila guarda `created_at` (epoch). `get_history()` devuelve ventana reciente `ORDER BY created_at DESC LIMIT max*2` revertida a orden cronológico; `get_history_with_timestamps()` expone además `created_at` para que `handlers._format_history_for_model` prefije cada turno como `[YYYY-MM-DD HH:MM] contenido` en `TIMEZONE` del usuario (fallback UTC). Así el modelo ve el **hueco** (ej. 10 días de silencio) y no asume que el último mensaje fue ayer.

### `bot/memory.py` — Persistencia de la bitácora (memoria a largo plazo)
SQLite (`memory.db`) + Markdown por usuario **y por grupo** (`group_id`). `memory.db` guarda **solo metadatos** (rutas, conteos de palabras, rangos de fechas, estado `current`/`finalized`); el contenido legible vive en `memory/{user_id}/raw|days|summaries/` o `memory/group_-xxx/...` para grupos (firmas `@usuario`). **Aislamiento por `user_id`/`group_id`**: cada tenant tiene su carpeta (`_owner_dir` / `_group_memory_dir`) — misma filosofía que `projects.py`. **Protección contra path traversal**: `_validate_day` / `_validate_summary_period` solo admiten el formato exacto del periodo (`YYYY-MM-DD`, `YYYY-Www`, `YYYY-Q#`, `YYYY`) antes de construir rutas, y `_safe_under_memory` (+ `_safe_under_memory_group`) resuelve cada ruta exigiéndole que quede dentro de `MEMORY_DIR` **y** dentro de la carpeta del tenant.

Tablas:
- `days` — PK `(user_id, day)`; `raw_path`, `raw_word_count`, `entry_count`, `compact_path`, `compact_word_count`, `range_start`, `range_end`, timestamps. Un día empieza solo con `raw` y gana `compact` al compactarse; la compacción nocturna reescribe solo `compact_*`, dejando intactas las estadísticas crudas.
- `summaries` — PK `(user_id, level, period)`; `content_path`, `word_count`, `range_start`, `range_end`, `status`. Regla de consistencia: al guardar un resumen de un nivel, cualquier otro `current` del mismo nivel pasa a `finalized` — siempre hay exactamente **un `current` por nivel** (el periodo que aún se escribe; los cerrados quedan `finalized` y navegables).

API principal: `append_raw_entry` (append `- [HH:MM] texto`), `get_raw_entries` (rango por días), `save_daily_compact`, `save_summary`, `get_summary`, `get_summary_content`, `list_summaries`, `finalize_summary`, `find_summary_covering` (para el recall top-down). Una conexión por operación, WAL y schema idempotente (patrón de `projects.py`/`history.py`).

### `bot/summarizer.py` — Resúmenes jerárquicos
Genera los resúmenes con el **mismo modelo del chat** (`ai_client.chat_completion`). Cada resumen es un **índice navegable**: línea de metadatos (rango, nº de fuentes), "Eventos con fecha" (cronológico, con fechas `YYYY-MM-DD`), las **11 secciones de dominio** en orden fijo (`SUMMARY_SECTIONS`: salud y ejercicio; finanzas y pagos; viajes y salidas; familia y amigos; trabajo y proyectos; estado de ánimo; pendientes y seguimientos; espiritualidad y eventos extraordinarios; ocio, descanso o disfrute; aprendizajes y estudios; eventos históricos o acontecimientos ambientales), "Patrones y rachas" y "Referencias al nivel hijo" (punteros a los periodos hijos — la base del recall top-down).

- `build_summary_prompt` — plantilla pura y determinista (sin IO) que embebe la estructura y las fuentes etiquetadas.
- `generate_daily_compact` — compacta un día crudo a `days/YYYY-MM-DD.md` solo si supera `MEMORY_DAILY_COMPACT_WORDS`; idempotente (si el compacto ya existe no paga otra llamada al modelo).
- `generate_weekly/quarterly/yearly_summary` — cada nivel asegura primero a sus hijos (`ensure_summary`) y lee los resúmenes hijos ya almacenados (semana ← días, trimestre ← semanas, año ← trimestres).
- `ensure_summary` — **generación perezosa**: devuelve el resumen si existe; si no, lo genera bajo demanda (lo usan el recall y los jobs trimestral/anual).
- Fallos del modelo → `None` (se registran y se reintentan perezosamente); `clean_model_output` quita fences markdown.

### `bot/proactive.py` — Motor proactivo y jobs de memoria
Envíos salientes best-effort: check-in matutino (saludo + `Hoy es YYYY-MM-DD (weekday) — zona TZ` + ayer + semana + `Última entrada: YYYY-MM-DD (hace N días)` cuando los últimos 7 días están vacíos + recordatorios próximos), cierre vespertino (`Hoy es ...` + `Hoy:` o `Última entrada antes de hoy` cuando hoy vacío + semana) y los jobs de compacción/resúmenes. Así los check-ins son **conscientes del tiempo**: si el usuario estuvo 10 días en silencio, el prompt ya lleva `Hoy es 2026-09-03` y `Última entrada: 2026-08-23 (hace 10 días)` para que el modelo no salude como si fuera ayer. Helpers `_hoy_line`, `_fallback_last_entry` (escanea 7..30 días) y `_last_entry_before_today` (1..30 días) son best-effort, truncan con `_CONTENT_TRUNCATE_CHARS` y nunca lanzan. `proactive_log` (en `projects.db`) lleva el **tope diario por usuario** (`PROACTIVE_MAX_DAILY`) y los **marcadores de ventana** `morning_sent_at`/`evening_sent_at` (cada ventana se envía como mucho una vez al día); `can_send` con `max_daily <= 0` es fail-closed. `user_proactive_settings` guarda **horas de check-in por usuario** (NULL → defaults globales); `get_checkin_times` resuelve por usuario y `set_checkin_time` valida `HH:MM` (la tool `set_checkin_time` del modelo escribe aquí). `window_of(settings, morning_time, evening_time)` define la ventana de 3 horas desde la hora correspondiente como guarda. `send_proactive` reintenta hasta 3 veces con backoff exponencial (10s, 20s) y cae a texto plano si el Markdown del modelo es inválido; sin cola persistente (a diferencia de `reminders`). Drivers: `run_checkins` (job periódico cada 15 min que resuelve la ventana de cada usuario con sus propias horas, respeta marcadores y tope), `run_nightly_compaction`, `run_weekly_summaries`, `run_quarterly_summaries`, `run_yearly_summaries` — cada usuario va en su propio `try/except` para que un fallo no detenga a los demás. El módulo no importa `bot.tools`/`bot.handlers` (evita un ciclo de imports).

### `bot/tools.py` — Definición y dispatch de tools
`TOOL_DEFINITIONS` es la lista que se envía al modelo. `_filter_tool_definitions(group_id)` oculta `PRIVATE_ONLY_TOOLS` en grupos; `_dispatch()` enruta cada tool call a su lógica. **Todas las tools devuelven un JSON string** (`{"ok": True/False, ...}`) — nunca excepciones crudas.

Tools actuales: `create_project`, `update_project_status`, `update_project_priority`, `update_project_description`, `add_project_note`, `list_projects` (con parámetro `scope`: all, owned, shared_with_me, shared_by_me), `search_projects`, `get_project`, `share_project`, `unshare_project`, `list_project_members`, `delete_project`, `create_reminder`, `list_reminders`, `cancel_reminder`, `edit_reminder`, `reschedule_reminder`, `postpone_reminder`, `web_search`, `dolar_rate`, `generate_image`, `journal_append`, `memory_read`, `memory_recall`, `memory_insights`, `set_checkin_time`.

`GROUP_TOOLS` incluye paridad v1 (web_search, dolar_rate, recordatorios y memoria compartidos); solo `PRIVATE_ONLY_TOOLS={generate_image, set_checkin_time, share/unshare/delete/list_members}` quedan bloqueadas en grupo y `group_id`/`author` se inyectan desde el handler.

> Las tools que listan contenido al usuario (`list_projects`, `search_projects`, `list_reminders`, `list_project_members`) devuelven además un campo `formatted` con el texto ya formateado en viñetas; el modelo debe repetirlo tal cual para evitar tablas Markdown, que Telegram renderiza mal.

> **Scope de las tools de proyectos (ACL)**: `_resolve_slug` delega en `get_project_for_user`, por lo que una tool opera sobre un proyecto solo si el usuario es dueño o miembro (un no-miembro recibe "Proyecto no encontrado"). `share_project`/`unshare_project` exigen ser dueño; `delete_project` exige ser dueño **y** que el proyecto esté `archivado` (la tool se documenta como irreversible y de uso solo ante petición explícita del usuario). El `user_id` siempre se inyecta desde el update de Telegram en `execute_tool(name, args, user_id)` — nunca proviene de los argumentos del modelo.

> **Scope de las tools de memoria (bitácora)**: operan únicamente sobre `DATA_DIR/memory/{user_id}/` del usuario que habla — jamás leen archivos del proyecto (AGENTS.md, README, docs/…) ni los datos de otro usuario. `journal_append` guarda en la bitácora del día; `memory_recall` busca top-down (resúmenes → días crudos); `memory_read` lee un periodo exacto; `memory_insights` devuelve el contexto reciente (últimos días + semana en curso) para patrones y proactividad; `set_checkin_time` ajusta las horas de check-in del usuario (por defecto usa los globales `PROACTIVE_*`). El `user_id` siempre se inyecta desde el update de Telegram.

> **Soporte de grupos (ACL de grupo)**: en contexto de grupo (`group_id` no None), `_filter_tool_definitions` oculta `PRIVATE_ONLY_TOOLS` y `execute_tool` valida el scope. Solo `generate_image`/`set_checkin_time`/compartir quedan bloqueadas; `create/list/cancel/edit/reschedule/postpone_reminder`, `journal_append`/`memory_*`, `web_search`/`dolar_rate` son **compartidas por `group_id`** con inyección de `_group_id`/`_author_username` desde el handler (aislamiento estricto por `chat_id`).

### `bot/dolar.py` — Tasa del dólar
Consulta BCV oficial + USDT Binance P2P.

### `bot/websearch.py` — Búsqueda web
DuckDuckGo vía librería `ddgs` (sin API key), en `asyncio.to_thread`.

### `bot/media.py` — Procesado de medios
Conversión de voz OGG→WAV (ffmpeg), redimensionado de imágenes.

### `bot/media_usage.py` — Límites de medios
Contador diario por usuario (imágenes y audios) en `media_usage.db`.

### `bot/bot.py` — Wiring
Crea la `Application`, registra handlers, comandos, el scheduler de recordatorios y los jobs de memoria/proactividad (`_register_memory_jobs` — ver "Jobs programados").

## Flujo de IA con herramientas

```
Usuario ──> update.message ──> access.py (¿autorizado?) ──> handlers.py
   │
   ├─ comandos ──> handlers específicos (sin IA o con IA según comando)
   │
   └─ chat ──> guardar en history ──> construir contexto (últimos N mensajes)
                  │
                  ▼
        ai_client.chat_completion_with_tools(model, system_prompt, history, TOOL_DEFINITIONS)
                  │
        ┌─────────┴──────────┐
        │ kind=text          │ kind=tool_call
        │ responder          │ → tools.execute_tool(name, args, user_id)
        │ (enviar a Telegram)│ → resultado JSON al modelo
        │                    │ → repetir (máx. MAX_TOOL_ROUNDS) hasta kind=text
        └────────────────────────────────────┘
```

Las tools de memoria (`journal_append`, `memory_read`, `memory_recall`, `memory_insights`) están cableadas exactamente igual que el resto: definición en `TOOL_DEFINITIONS` + dispatch en `_dispatch()` + lógica en `bot/memory.py`/`bot/summarizer.py`, y devuelven JSON string. Durante la conversación el modelo llama `journal_append` cuando el usuario cuenta algo notable de su día a día; cuando pregunta por su pasado ("¿cuándo fue mi último viaje a Panamá?") llama `memory_recall` (y `memory_read` para el detalle de un periodo concreto). Los datos viven bajo `DATA_DIR/memory/{user_id}/` — dato de usuario, no documentación del proyecto.

## Jobs programados (job_queue)

| Job | Cuándo | Qué hace | Requiere |
|-----|--------|----------|----------|
| `memory_nightly_compaction` | 00:15 diario | Compacta los **últimos 3 días** sobre `MEMORY_DAILY_COMPACT_WORDS` a `days/YYYY-MM-DD.md` (usuarios + grupos) | `MEMORY_ENABLED` |
| `memory_weekly_summaries` | 00:30 diario | Asegura los resúmenes de las **últimas 8 semanas** ISO (`summaries/weekly/YYYY-Www.md`, usuarios + grupos) | `MEMORY_ENABLED` |
| `memory_quarterly_summaries` | 00:45 diario | Asegura los resúmenes de los **últimos 4 trimestres** (asegura antes las semanas vía `ensure_summary`, usuarios + grupos) | `MEMORY_ENABLED` |
| `memory_yearly_summaries` | 01:00 diario | Asegura los resúmenes del **año actual y anterior** (asegura antes los trimestres, usuarios + grupos) | `MEMORY_ENABLED` |
| `memory_checkins` | Cada 15 min | Resuelve por usuario su ventana (horas propias o `PROACTIVE_*`) y envía el check-in matutino/vespertino si toca; marcadores `morning_sent_at`/`evening_sent_at` evitan duplicados | `MEMORY_ENABLED` + `PROACTIVE_ENABLED` |
| `auto_journal` | Cada `AUTO_JOURNAL_INTERVAL` (60min default, 75% ahorro vs 30min) | Extrae eventos+entidades en 1 call (`_COMBINED_EXTRACT_PROMPT`) con threshold `<6 msgs` skip y fallback `interval×3`, sobre `LOOKBACK_HOURS=3` | `MEMORY_ENABLED` |

**Catch-up tras downtime**: los jobs de compacción y resúmenes son **escáneres de recuperación**, no eventos de un solo día — cada vez que corren miran hacia atrás una ventana acotada (3 días, 8 semanas, 4 trimestres, 2 años) y generan **todo lo que falte**. Si el server estuvo apagado cuando cerraba un periodo (p. ej. el lunes del semanal, el 1º de abril del trimestral o el 1º de enero del anual), el siguiente arranque se pone al día solo, sin intervención. `ensure_summary` es idempotente (salta lo ya generado) y devuelve `None` sin llamar al modelo cuando un periodo no tiene contenido, así que el costo en estado estable es ~0 llamadas y un catch-up cuesta a lo sumo 1 llamada por periodo faltante con datos. Para periodos más viejos que la ventana queda la **generación perezosa**: `memory_recall`/`memory_read` (o el nivel padre) los generan bajo demanda, y el raw nunca se borra, así que no hay pérdida posible. Los check-ins perdidos no se reenvían (un "buenos días" de un día pasado no tiene sentido).

Cada job procesa los usuarios en su propio `try/except`: un fallo de un usuario (API, filesystem) nunca detiene al resto. Los jobs se registran en `bot/bot.py` → `_register_memory_jobs`, gobernados por `MEMORY_ENABLED` / `PROACTIVE_ENABLED`.

## Persistencia

| Dato | Archivo | Ruta (Docker) |
|------|---------|---------------|
| Historial de chat | `chat_history.db` | `/app/data/chat_history.db` |
| Proyectos (metadatos + membresía + perfiles + grupos), recordatorios, `proactive_log` y `user_proactive_settings` | `projects.db` | `/app/data/projects.db` |
| Proyectos (contenido) | `projects/{owner_id}/{slug}/` | `/app/data/projects/` |
| Bitácora (metadatos de días y resúmenes) | `memory.db` | `/app/data/memory.db` |
| Bitácora (contenido Markdown) | `memory/{user_id}/…` + `memory/group_-xxx/…` | `/app/data/memory/` |
| Límites de medios | `media_usage.db` | `/app/data/media_usage.db` |

Tablas de `projects.db` relacionadas con grupos:
- `projects.group_id` — nullable; proyectos con `group_id` son visibles en ese grupo
- `users.private_chat_id` — chat privado del usuario (legacy, ya no usado para entrega; recordatorios de grupo se entregan en el grupo)
- `group_members` — `(group_id, user_id)` — usuarios que han interactuado en cada grupo

Tablas de `memory.db` / `reminders` con soporte de grupo:
- `memory.db:days(user_id nullable, group_id nullable, CHECK)` + índices parciales `ux_days_private/group`
- `memory.db:summaries` idem
- `reminders.group_id` / `created_by` — recordatorios compartidos por grupo

Layout de la bitácora bajo `DATA_DIR/memory/{user_id}/`:

```
memory/{user_id}/
├── raw/YYYY-MM-DD.md        ← captura cruda del día (append en vivo, nunca se borra)
├── days/YYYY-MM-DD.md       ← compacto diario (solo si superó MEMORY_DAILY_COMPACT_WORDS)
└── summaries/
    ├── weekly/YYYY-Www.md
    ├── quarterly/YYYY-Q#.md
    └── yearly/YYYY.md
```

`DATA_DIR` se inyecta en Docker (`/app/data`, volumen `bot_data`). El contenedor **no** contiene código de desarrollo ni documentación.

## Despliegue

1. `git push origin main` → GitHub Actions (`.github/workflows/deploy.yml`)
2. rsync al VPS (excluye `.env`, DBs, `.venv`, `.opencode/`, `AGENTS.md`, `docs/`, `projects/`)
3. `docker compose up -d --build`
4. `docker image prune -f`

El bot usa long-polling saliente → no necesita puertos expuestos ni dominio.

## Decisiones clave

| Decisión | Razón |
|----------|-------|
| SQLite + Markdown espejo | Simple, sin servidor, contenido legible/editable a mano |
| Function calling como único puente | El modelo nunca toca el sistema directamente; solo via tools aprobadas |
| Compartir proyectos con ACL + rol único de colaborador | Patrón estándar tipo Google Drive/GitHub: dueño en `projects.user_id`, invitados en `project_members`; una sola puerta `get_project_for_user`; solo el dueño invita/remueve; invitación inmediata con notificación |
| Eliminar solo proyectos archivados y solo el dueño | Operación irreversible: se exige estado `archivado` para evitar destruir accidentalmente un proyecto activo; `is_owner` se re-valida en la capa de datos (defensa en profundidad) y la UI pide confirmación con botones inline |
| Proveedor configurable (opencode/gemini) | Fallback de coste/cuota: gemini gratis para medios e imágenes |
| Fail-secure en acceso | Sin `AUTHORIZED_USERS` nadie entra (protege presupuesto y privacidad) |
| Bitácora SQLite + MD por usuario (misma filosofía que proyectos) | La memoria es dato de usuario: metadatos en SQLite para recall rápido, contenido legible en Markdown aislado por `user_id`, con guardas de path traversal en cada ruta |
| Resúmenes como índices navegables (semana → trimestre → año) | Un resumen es un índice (secciones + eventos con fecha + punteros al nivel hijo): responder sobre el pasado es bajar por punteros, no releer todo el historial |
| Contenedor no-root y sin puertos | Menor superficie de ataque |
