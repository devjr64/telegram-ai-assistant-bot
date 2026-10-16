# Development Guide — Telegram AI Bot

Guía para desarrolladores que mantienen este bot. **Solo para desarrollo** — el bot de Telegram jamás lee este archivo (ver `AGENTS.md`).

## Requisitos

- Python 3.12
- Docker + Docker Compose (para el contenedor)
- `.env` creado desde `.env.example` (con `BOT_TOKEN` y `OPENCODE_GO_API_KEY`)

## Setup local

```bash
cd telegram-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

> Para probar con el bot real necesitas un token de @BotFather y tu ID en `AUTHORIZED_USERS`.

## Dependencias (lockfile)

Las dependencias están **pineadas** para que local, CI y VPS instalen exactamente el mismo set, y un deploy no rompa el bot silenciosamente cuando pip resuelve una versión nueva.

- `requirements.in` — **dependencias directas declaradas** (el input, lo que se edita a mano).
- `requirements.txt` — **lockfile generado**: deps directas + transitivas con versiones exactas. Es lo que instalan el Dockerfile y el CI. **No se edita a mano.**
- `requirements-dev.txt` — dependencias de test (solo local/CI), también pineadas.

### Por qué

Con rangos sueltos (`>=21.0`) el contenedor se reconstruye desde cero en cada deploy y pip elige la última versión disponible en ese momento, que puede diferir de lo probado en local. Ejemplo real: al generar este lock, una resolución fresca instalaba `openai==3.1.0` mientras el entorno de desarrollo tenía `openai==2.54.0` — sin el lock, el siguiente push habría desplegado una SDK 3.x que nadie había probado. Con el lock, el deploy instala exactamente lo validado con la suite.

### Cómo regenerar el lock

Cuando quieras actualizar dependencias:

```bash
# 1) Edita requirements.in (añade/actualiza las deps directas)
# 2) Genera el lock en el mismo entorno de producción (python:3.12-slim):
docker run --rm -v "$PWD":/w -w /w python:3.12-slim \
  sh -c "pip install -r requirements.in && pip freeze" > requirements.txt
# 3) Añade el header de comentario del archivo (ver requirements.txt)
# 4) Valida obligatoriamente con la suite completa:
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest --cov=bot
```

> Si una regeneración sube un major que rompe la suite, acota el rango en `requirements.in` (ej. `openai>=2.54,<3`) y regenera. El lock solo fija el resultado; la validación con tests es obligatoria antes de commitear.

## Docker SIEMPRE (recomendado para pruebas)

> ⛔ **Este proyecto se ejecuta SIEMPRE en Docker** en producción, y lo recomendado para desarrollo también es Docker: `docker compose up -d --build`. Es el mismo contenedor que corre en el VPS, así que lo que pruebas localmente es exactamente lo que se despliega.

```bash
docker compose up -d --build   # construye y arranca (lee tu .env local)
docker compose logs -f         # ver logs
docker compose up -d --build   # reiniciar tras cambios
docker compose down -v         # borrar volumen de datos (¡cuidado!)
```

> ⚠️ **El token de Telegram de desarrollo es distinto al de producción.** Tu `.env` local debe usar un `BOT_TOKEN` de **pruebas** (bot creado con @BotFather para desarrollo), jamás el token del bot en producción, que vive solo en el `.env` del VPS. Usar el token de producción en local **rompería el bot en vivo**. Los `AUTHORIZED_USERS` también pueden diferir entre entornos.

## Estructura de trabajo

- **Código del bot**: `main.py` + `bot/` — esto es lo único que va al contenedor
- **Documentación de desarrollo**: `AGENTS.md`, `README.md`, `docs/` — jamás va al contenedor
- **Datos de usuario**: `DATA_DIR` (`/app/data` en Docker) — jamás va a git

## Cómo añadir una tool al modelo

Las tools son el único puente entre el modelo y el sistema. Pasos:

1. **Definir la tool** en `TOOL_DEFINITIONS` (`bot/tools.py`):
   ```python
   {
       "type": "function",
       "function": {
           "name": "mi_tool",
           "description": "Qué hace y CUÁNDO usarla (instrucciones para el modelo).",
           "parameters": {"type": "object", "properties": {...}, "required": [...]},
       },
   },
   ```
2. **Añadir el dispatch** en `_dispatch()`:
   ```python
   if name == "mi_tool":
       resultado = await mi_funcion(args)
       return json.dumps(resultado)
   ```
3. **Implementar la lógica** en su módulo (nuevo o existente).
4. **Siempre devolver JSON string** al modelo: `{"ok": True, ...}` o `{"ok": False, "error": "..."}`. Nunca excepciones crudas.
5. **Validar valores de dominio** antes de persistir (por ejemplo, estados y prioridades de proyectos contra `PROJECT_STATUSES` / `PROJECT_PRIORITIES`); devuelve el error en JSON para que el modelo lo maneje en español.
6. **Si la tool lista contenido para el usuario** (proyectos, recordatorios, etc.), devuelve además un campo `formatted` con el texto ya en viñetas (•), siguiendo el patrón de `_format_projects_list` / `_format_reminders_list` (`bot/tools.py`). El modelo debe repetir ese texto tal cual, **sin convertirlo a tabla Markdown** (Telegram no las renderiza bien) — indica esa instrucción en la descripción de la tool.

> ⛔ **NUNCA** añadir una tool que lea archivos del proyecto (AGENTS.md, README, docs/…). Ver regla de oro en `AGENTS.md`.

> Las tools de memoria (`journal_append`, `memory_read`, `memory_recall`, `memory_insights`) se añadieron siguiendo **exactamente** este procedimiento: definición en `TOOL_DEFINITIONS`, dispatch en `_dispatch()`, lógica en `bot/memory.py` + `bot/summarizer.py`, y **siempre JSON string** de vuelta al modelo. Si añades otra tool de memoria, respeta el mismo patrón y el scope restringido a `DATA_DIR/memory/{user_id}/`.

## Cómo añadir un comando

1. Crear/editar el handler en `bot/handlers.py` (o `bot/project_commands.py` si es de proyectos).
2. Registrar el comando en `bot/bot.py` (`app.add_handler(CommandHandler("micomando", mi_handler))`).
3. Si el comando necesita IA, usar el cliente de `bot.ai_client`.
4. Validar entradas sensibles (por ejemplo, estados y prioridades de proyectos contra `PROJECT_STATUSES` / `PROJECT_PRIORITIES`) y devolver errores claros al usuario.
5. Documentar en `README.md` (tabla de comandos).

> **Convención de nombres**: los comandos expuestos a los usuarios están en español, en minúsculas y sin guiones bajos (p. ej. `/nuevoproyecto`, `/estadoproyecto`, `/prioridadproyecto`). Mantén ese patrón para nuevos comandos.

## Nota: API de python-telegram-bot v22 (botones inline)

El proyecto requiere `python-telegram-bot>=21` (instalada la v22). En v22, el
método `Message.edit_message_text()` fue **eliminado** — usarlo lanza en
runtime `AttributeError: 'Message' object has no attribute 'edit_message_text'`.
Los tests **no** lo detectan porque `tests/fakes.py` define fakes propios
(`FakeMessage`) que sí tienen ese método.

Para editar el mensaje de un botón inline usa siempre el método del propio
`CallbackQuery`:

```python
await query.edit_message_text("Texto nuevo")   # ✅ correcto (PTB v22)
# await query.message.edit_message_text(...)   # ❌ eliminado en v22
```

Los tests simulan esto en `FakeCallbackQuery.edit_message_text`
(`tests/fakes.py`), que delega en el mensaje y registra en
`query.message.edited_messages`.

## Compartición de proyectos (ACL)

Los proyectos pueden compartirse: el **dueño** vive en `projects.user_id` (fuente de verdad única) y los invitados en `project_members` (un único rol de colaborador completo). La tabla `users` guarda los perfiles Telegram (`username`, `first_name`) para resolver `@username → user_id`.

**Reglas de oro al tocar este código:**

1. **Toda lectura/escritura de un proyecto pasa por `get_project_for_user(user_id, slug)`** (devuelve el proyecto solo si eres dueño o miembro). No consultes `get_project_by_slug` para operaciones de usuario — es la puerta que evita filtrar proyectos ajenos. En `project_commands._find_project` y `tools._resolve_slug` ya está aplicado.
2. **Solo el dueño invita/remueve**: `is_owner` se verifica en el comando/tool **y** de nuevo dentro de `add_project_member`/`remove_project_member` (defensa en profundidad). Un caller nuevo nunca debe saltarse la capa de datos.
3. **Auto-invitación rechazada**: `add_project_member` devuelve error si `user_id == invited_by`; no lo permitas tampoco en la capa de UI.
4. **Las rutas de archivos usan SIEMPRE el owner**: `read_project_files(project["user_id"], ...)` — el invitado lee/escribe en la carpeta del dueño (`projects/{owner_id}/{slug}/`), nunca en la suya.
5. **El `user_id` siempre se inyecta desde el update de Telegram** en `execute_tool(name, args, user_id)` — jamás desde los argumentos del modelo.
6. **Regla de 3 urgentes contra el dueño**: si un invitado edita un proyecto compartido urgente, los pre-checks usan `project["user_id"]` (no el `user_id` que actúa).
7. **Resolver `@username`**: el usuario a invitar debe estar en la tabla `users`, que se puebla con `upsert_user` en cada update (mensaje, voz, foto y `/start`). `resolve_username` es determinista (`ORDER BY updated_at DESC LIMIT 1`) para no enrutar a un username reutilizado.

Nuevos comandos de compartición: `/compartir <proyecto> @usuario` y `/quitaracceso <proyecto> @usuario`. Nuevas tools: `share_project`, `unshare_project`, `list_project_members`.

## Eliminación de proyectos (solo archivados)

Para liberar espacio, un proyecto archivado puede **eliminarse definitivamente**. Es la única operación destructiva del bot, así que hay reglas estrictas (misma filosofía defensiva que la ACL):

1. **Solo dueño**: `is_owner` se verifica en el comando/tool **y** dentro de `delete_project()` (`bot/projects.py`) — la capa de datos rechaza a cualquier `actor_id != projects.user_id`, igual que `remove_project_member`.
2. **Solo archivado**: si el status no es `archivado`, `delete_project()` devuelve error. Un proyecto activo jamás se puede borrar directamente; hay que archivarlo antes con `/estadoproyecto <nombre> archivado`.
3. **Confirmación**: `/eliminarproyecto <nombre>` no borra nada por sí solo — renderiza botones inline **"Sí, eliminar"** (`delproj:<id>`) / **"Cancelar"** (`delproj_cancel:<id>`). El `CallbackQueryHandler` `delete_project_callback` (patrón `^delproj(_cancel)?:\d+$`) re-valida auth/owner/archivado antes de ejecutar el borrado. La eliminación pedida por **texto natural** (tool `delete_project`) pasa por la misma confirmación: `handlers._forward_tool_confirmation` detecta el `confirmation_required` devuelto por la tool y envía los mismos botones al chat.
4. **Cascada completa**: al borrar la fila de `projects`, `project_notes` y `project_members` se eliminan por FK `ON DELETE CASCADE`, y la carpeta `projects/{owner_id}/{slug}/` (overview, notes, context.json) se borra del disco con `_remove_project_dir` — protegido contra path traversal (`is_relative_to(PROJECTS_DIR)`).
5. **Tool del modelo**: `delete_project` (IA) aplica las mismas reglas y **nunca borra directo** — devuelve `confirmation_required` (con el proyecto objetivo) y `handlers._forward_tool_confirmation` se encarga de mostrar los botones; la descripción de la tool insiste en que es **irreversible** y solo debe usarse ante petición explícita del usuario.
6. **El `user_id` nunca viene de los args del modelo** ni del `callback_data` — siempre del update de Telegram.

## Cómo añadir un recordatorio

Los recordatorios se crean:
- **Automáticamente** por la IA: el modelo llama la tool `create_reminder`
- **Por comando**: `/recordar <texto>` → `parse_reminder_datetime()` extrae fecha/mensaje/recurrencia

La persistencia vive en `bot/reminders.py` (SQLite). El scheduler está en `bot/bot.py`.

**Gestión**: los recordatorios se pueden editar/posponer/cancelar de tres formas:
- **Por conversación**: tools de IA `list_reminders`, `cancel_reminder`, `edit_reminder`, `reschedule_reminder`, `postpone_reminder` (`bot/tools.py`)
- **Por comandos**: `/recordatorios` (lista con botones inline), `/editarrecordatorio`, `/posponerrecordatorio`, `/reprogramarrecordatorio`, `/cancelarrecordatorio` (`bot/handlers.py`, registrados en `bot/bot.py`)
- **Botones inline**: el `CallbackQueryHandler` `reminder_callback` (patrón `^remind_(cancel|postpone):\d+$`) maneja los taps

**Entrega con reintentos**: `check_due_reminders` (`bot/handlers.py`) reintenta envíos fallidos con backoff exponencial definido en `bot/reminders.py` (`retry_delay`, `schedule_retry`, `reset_retry`): 10s, 20s, 40s... hasta un máximo de 8h. Al editar un recordatorio (texto, fecha o posponer) se resetea el estado de reintento.

## Cómo funciona la memoria (bitácora)

La memoria a largo plazo es una **bitácora por tenant** (`user_id` o `group_id`): SQLite (`memory.db`) guarda los **metadatos** (días y resúmenes) y el contenido legible vive en Markdown bajo `DATA_DIR/memory/{user_id}/` o `memory/group_-xxx/` (`raw/` = captura cruda del día, `days/` = compactos diarios, `summaries/{weekly,quarterly,yearly}/` = resúmenes jerárquicos). Mismo patrón SQLite+MD que `projects.py`, con la misma filosofía defensiva:

1. **Aislamiento por `user_id`/`group_id`** — cada tenant tiene su carpeta `memory/{user_id}/` o `memory/group_-xxx/` y filas con `CHECK((user_id IS NOT NULL AND group_id IS NULL) OR ...)` + índices parciales; el `user_id`/`group_id` siempre se inyecta desde el update de Telegram (`execute_tool(name, args, user_id, _group_id)`), nunca de los args del modelo. La bitácora de grupo es **compartida y firmada `@usuario`**.
2. **Path traversal** — `_validate_day` / `_validate_summary_period` solo admiten el formato exacto del periodo (`YYYY-MM-DD`, `YYYY-Www`, `YYYY-Q#`, `YYYY`) y `_safe_under_memory` / `_safe_under_memory_group` resuelven cada ruta exigiéndole que quede dentro de `MEMORY_DIR` **y** dentro de la carpeta del tenant (doble guarda, igual que `_remove_project_dir` con `is_relative_to`). Si añades una función nueva que construya rutas de memoria, pásala por esas guardas.
3. **Una conexión por operación** con WAL y schema idempotente (`CREATE TABLE IF NOT EXISTS`), como el resto de módulos.
4. **Consistencia de resúmenes** — guardar un resumen de un nivel finaliza cualquier otro `current` del mismo nivel: siempre hay exactamente un `current` por nivel (el periodo en curso); los cerrados quedan `finalized` y navegables.

**Tools de memoria y sus contratos** (todas devuelven JSON string; definidas en `bot/tools.py`, lógica en `bot/memory.py`/`bot/summarizer.py`):

| Tool | Contrato |
|------|----------|
| `journal_append(content, topic)` | Guarda en la bitácora del día (`raw/YYYY-MM-DD.md`) con prefijo `[topic]` opcional; topic desconocido se descarta (validación suave); devuelve `{ok, day, entry_count, word_count}` |
| `memory_read(period, level=auto)` | Lee un día/resumen concreto; `auto` deduce el nivel del formato del periodo; día → compacto diario preferido, raw como fallback; devuelve `{ok, level, period, content}` (o `note` si no hay nada) |
| `memory_recall(query, date_from?, date_to?)` | Búsqueda top-down: puntúa los resúmenes anual→trimestre→semana (1 mejor por nivel, con `pointer` a los hijos); si ninguno coincide, cae a los días crudos del rango (top 3, ventana por defecto 60 días); devuelve `{ok, matches, hint/note}` |
| `memory_insights(days=7)` | Últimos `days` días crudos + resumen de la semana en curso; alimenta preguntas de hábitos y el motor proactivo; devuelve `{ok, days, week, week_summary}` |
| `set_checkin_time(period, time)` | Ajusta las horas de check-in del usuario en `user_proactive_settings` (tabla en `projects.db`): `period` ∈ `morning`/`evening`, `time` en `HH:MM` (24h); valida y devuelve `{ok, period, time, morning, evening}` o un error en español (**solo privado**; bloqueado en grupo) |

**Generación de resúmenes**: la compacción nocturna, el semanal, el trimestral y el anual corren como jobs en `bot/bot.py` → `bot/proactive.py` y son **escáneres de catch-up**: cada vez que corren aseguran *todo* lo que falte en su ventana de recuperación (últimos 3 días, últimas 8 semanas, últimos 4 trimestres, año actual + anterior), de modo que si el server estuvo apagado cuando cerraba un periodo, el siguiente arranque se pone al día solo (sin guardas de calendario: trimestral/anual corren a diario y son no-op cuando no falta nada). La **generación perezosa** (`summarizer.ensure_summary`) complementa el catch-up: regenera bajo demanda si el recall no encuentra, es idempotente y no llama al modelo para periodos vacíos, así que el costo estable es ~0. El motor proactivo (`bot/proactive.py`) genera check-ins con el modelo de chat: `run_checkins` (job periódico cada 15 min) resuelve por usuario su ventana — horas propias en `user_proactive_settings` o los defaults `PROACTIVE_MORNING_TIME`/`PROACTIVE_EVENING_TIME` —, respeta los marcadores `morning_sent_at`/`evening_sent_at` (una ventana por día) y `PROACTIVE_MAX_DAILY` (tope diario en `proactive_log`), y entrega best-effort con backoff.

> ⛔ **Regla de oro (recordatorio)**: la memoria es **dato de tenant** (`DATA_DIR/memory/{user_id}/` o `group_-xxx/`), no documentación del proyecto. Las tools de memoria **jamás** leen archivos del proyecto (AGENTS.md, README, docs/…) — su único alcance es la carpeta de memoria del tenant que habla. No añadas nunca una tool que lea archivos del proyecto para "enriquecer" la memoria.

> El `SYSTEM_PROMPT` por defecto (`bot/config.py`) ya incluye las instrucciones de memoria y proactividad (cuándo usar `journal_append`, `memory_recall`, `memory_read`, `memory_insights`, `set_checkin_time` y cómo responder a los check-ins). Si el `.env` del VPS define un `SYSTEM_PROMPT` viejo, ese texto **anula** el default y desactiva esas reglas: edita a mano `~/telegram-bot/.env` en el servidor y borra/actualiza la línea `SYSTEM_PROMPT` (los deploys no tocan ese archivo).

## Cómo añadir un nuevo proveedor de IA

1. Añadir las variables de entorno en `bot/config.py` (`Settings`).
2. Crear el builder del cliente en `bot/ai_client.py` (patrón `build_client_for_provider`).
3. Añadir la opción en la validación de `load_settings()`.

> El proveedor y el modelo son **fijos por configuración** (`.env`) y no se
> cambian desde el chat: no hay comando ni tool para ello (ver `bot/handlers.py`).

## Testing

### Suite automatizada (pytest)

Hay una suite de tests en `tests/` (unitarios + integración con objetos falsos de Telegram, sin red):

```bash
# 1) Instalar dependencias de test (solo una vez por .venv)
.venv/bin/pip install -r requirements-dev.txt

# 2) Ejecutar los tests con cobertura
.venv/bin/python -m pytest --cov=bot
```

Si ya tienes el entorno virtual activado (`source .venv/bin/activate`), basta con `python -m pytest --cov=bot`.

- `--cov=bot` mide qué porcentaje del código del paquete `bot/` fue ejecutado por los tests y muestra un reporte por módulo (ej. `access 100%`, `handlers 46%`). Sin el flag, pytest solo corre los tests.
- En CI se usa `pytest --cov=bot --cov-fail-under=80`: si la cobertura baja del 80%, el workflow falla y bloquea el deploy.
- Corre en CI en cada push (job `test` del workflow).
- Todos los externos (httpx, ddgs, IA, Telegram) están mockeados — no requiere red ni tokens.
- Los tests son deterministas y aislados (BD SQLite en `tmp_path` por test).

### Smoke test del wiring del bot (`bot/bot.py`)

> **Implementado** en `tests/test_bot.py` (7 tests).

`bot/bot.py` (`create_application`) registra ~25 handlers y 6 jobs programados y es el único punto crítico del arranque: un typo en el nombre de un handler o un job mal registrado rompe el bot al arrancar. El smoke test llama `create_application()` con settings de prueba (sin red ni tokens) y verifica:

- Que existan los `CommandHandler` / `MessageHandler` / `CallbackQueryHandler` esperados (comandos y patrones de callback correctos).
- Que `bot_data["settings"]`, `bot_data["provider"]` y `bot_data["ai_client"]` estén poblados.
- Que los jobs estén en la job queue con nombres/intervalos correctos (`prune_stale_chats` 600s, `check_due_reminders` 20s, `memory_*` a sus horas, `run_checkins` 900s).
- Que con `MEMORY_ENABLED=false` / `PROACTIVE_ENABLED=false` los jobs correspondientes NO se registren (gates de configuración).

Gracias a este test, `bot/bot.py` pasó de ~34% a 100% de cobertura.

### Pruebas manuales end-to-end

Para validar el flujo completo real (Telegram + IA + tools), prueba directamente contra el bot de desarrollo:

1. Levanta el bot: `docker compose up -d --build`
2. Envía mensajes reales desde tu cuenta autorizada: chat normal, `/dolar`, `/proyectos`, `/nuevoproyecto`, `/recordar`, `/recordatorios`, notas de voz, `/imagina`
3. Verifica respuestas y revisa logs: `docker compose logs -f`

> No hay pruebas E2E automatizadas con un bot cliente simulado — las pruebas manuales directas son la forma soportada de validar el flujo real.

## Deploy a producción

```bash
git add .
git commit -m "feat: mi cambio"
git push origin main   # ← pedir aprobación del usuario antes
```

El CI/CD despliega automáticamente al VPS. **Nunca hacer push sin aprobación explícita del usuario.**

## Checklist antes de un commit

- [ ] Sin secrets ni datos de usuario (revisar `git status` / `git diff`)
- [ ] Conventional commit en inglés
- [ ] No se modificó `README.md` ni `docs/` accidentalmente con contenido del bot
- [ ] Si añadiste una tool: devuelve JSON, no lee archivos del proyecto
- [ ] Si tocaste memoria: las tools solo operan en `DATA_DIR/memory/{user_id}/`; no documentes comandos nuevos (la bitácora es 100% lenguaje natural)
- [ ] La documentación de desarrollo no se copió al contenedor (`.dockerignore` intacto)
