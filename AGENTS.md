# AGENTS.md — Telegram AI Bot (OpenCode Go)

Instrucciones para el agente (opencode / OpenAgents) que **desarrolla y mantiene** este proyecto.
Este archivo es para el programador humano y el agente de desarrollo. **NO es para el bot de Telegram.**

---

## ⛔ Regla de oro: separación de mundos (LEER PRIMERO)

Este proyecto usa el mismo servicio "OpenCode Go" para **dos fines completamente distintos**:

| Mundo | Quién lo lee | Qué contiene |
|-------|-------------|--------------|
| **Desarrollo** | Tú + opencode (agente que programa) | `AGENTS.md`, `README.md`, `docs/`, `.opencode/context/` |
| **Bot runtime** | Solo el bot en Docker | `SYSTEM_PROMPT` (en `.env`), tools, y datos de usuario (`DATA_DIR/projects/*.md`, `chat_history.db`, `projects.db`, `memory.db`, `memory/`) |

### Reglas absolutas

1. **El bot de Telegram NUNCA debe leer ni interpretar la documentación del proyecto** (`AGENTS.md`, `README.md`, `docs/`, `.opencode/`).
2. **NUNCA añadas una tool de lectura de archivos al bot** (ej. `read_file`, `list_directory`). Las tools del bot están cerradas y limitadas a: proyectos del usuario, recordatorios, búsqueda web, tasa del dólar, generación de imágenes, la bitácora/memoria del usuario (journal_append, memory_read, memory_recall, memory_insights, set_checkin_time) y nada más.
3. **NUNCA incluyas contenido de `AGENTS.md`, `README.md` o `docs/` en el `SYSTEM_PROMPT` o en el contexto que se envía al modelo del bot.**
4. La documentación de desarrollo vive en la raíz/`docs/` y **jamás se copia al contenedor Docker** (ver `.dockerignore`) ni al VPS (ver `deploy.yml`).
5. El bot solo persiste datos de usuario en `DATA_DIR` (por defecto `/app/data` en Docker): `projects.db`, `chat_history.db`, `projects/`, `media_usage.db`, `memory.db`, `memory/` (bitácora por usuario) y `entities.db` (entidades tracking). Nada más.

> Si una tarea de desarrollo requiere que el bot acceda a algo del proyecto, **detente y consulta con el usuario primero**.

---

## Proyecto

Bot de Telegram con IA para uso personal/familiar (Venezuela). Conversa en español vía OpenCode Go API, gestiona proyectos con memoria a largo plazo (SQLite + Markdown), recordatorios (incluidos recurrentes), búsqueda web, tasa del dólar y medios (notas de voz, análisis de fotos y generación de imágenes con Gemini).

- **Producción:** [@<bot-produccion>](https://t.me/bot-produccion-ejemplo)
- **Pruebas:** [@<bot-pruebas>](https://t.me/bot-pruebas-ejemplo) — solo se usa para pruebas locales, NUNCA para producción
- **Repo GitHub:** `github.com/<usuario>/<repo>` (privado)
- **Deploy:** automático vía GitHub Actions al VPS (Docker)

> ⚠️ **Regla de tokens**: el bot de producción (`@<bot-produccion>`) y el de pruebas (`@<bot-pruebas>`) usan tokens diferentes. **NUNCA** uses el token de producción para pruebas locales. El bot de pruebas NO corre 24/7 — solo se levanta cuando se hacen pruebas manuales.

## Arquitectura

```
main.py                      → entry point (Python 3.12, python-telegram-bot)
bot/
├── config.py                → Settings dataclass (variables .env, fail-secure)
├── access.py                → control de acceso (solo AUTHORIZED_USERS)
├── ai_client.py             → cliente OpenAI-compatible (opencode / gemini)
├── handlers.py              → comandos + chat + loop de tools + handlers de grupo
├── project_commands.py      → comandos de proyectos (/proyectos, /nuevoproyecto,
│                              /compartir, /quitaracceso…)
│                              Scope filters: todos, mios, dueños, compartidos, invitado
│                              Notificaciones de cambios en proyectos de grupo
├── projects.py              → CRUD proyectos SQLite + Markdown + compartición
│                              + soporte de grupos (ACL: project_members, users,
│                              group_members, get_project_for_user, get_shared_projects_in_group)
├── reminders.py             → recordatorios SQLite (one-time + recurrentes)
├── history.py               → historial de chat SQLite (PERMANENTE, sin pruning)
├── tools.py                 → definiciones + ejecución de tools del modelo
│                              (GROUP_TOOLS, PRIVATE_ONLY_TOOLS para scope de grupo)
│                              list_projects soporta scope: all, owned, shared_with_me, shared_by_me
│                              Tools de grupo generan _notify metadata para notificaciones
├── auto_journal.py          → auto-guardado de eventos y entidades (60min + 1 call, threshold 6)
├── entities.py              → tracking de entidades (personas, lugares, casos, documentos)
├── memory.py                → bitácora/journal (SQLite + Markdown, resúmenes jerárquicos)
├── summarizer.py            → generación de resúmenes (daily, weekly, quarterly, yearly)
├── proactive.py             → check-ins matutinos/vespertinos + jobs de resumen
├── dolar.py                 → tasas dólar (BCV oficial + USDT Binance P2P)
├── websearch.py             → búsqueda web DuckDuckGo (ddgs, sin API key)
├── media.py                 → procesado de voz (ffmpeg) e imágenes
├── media_usage.py           → límites diarios de generación de medios
└── bot.py                   → wiring de python-telegram-bot (handlers privado + grupo)

Dockerfile / docker-compose.yml
.github/workflows/deploy.yml → CI/CD: rsync + docker compose en el VPS
.env.example / .gitignore
```

### Flujo de IA con herramientas (clave)

1. Usuario envía mensaje → el bot envía historial + definiciones de tools al modelo
2. El modelo decide: responder directamente o llamar una tool
3. Si llama una tool, `tools.py` la ejecuta y devuelve el resultado JSON al modelo
4. El modelo produce la respuesta final en español

**Añadir una tool nueva**: definición en `TOOL_DEFINITIONS` (tools.py) + handler en `_dispatch()` + lógica en su módulo. Las tools son el ÚNICO puente entre el modelo y el sistema — mantenerlas limitadas y seguras.

### UX: lenguaje natural sobre comandos

El objetivo final del bot es que el usuario interactúe por **chat natural** (lenguaje natural), no por comandos. El usuario final es personal/familiar, no técnico.

- **Preferir siempre la interacción por chat** sobre los comandos (`/proyectos`, `/recordar`, etc.). Si se añade una función nueva, el objetivo es que funcione conversando, no exigiendo un comando.
- Los comandos existen para: **testeo**, **implementación**, **decisiones técnicas** y **seguridad** (ej. botones de confirmación para acciones destructivas). Son una herramienta de desarrollo, no el objetivo de UX.
- La seguridad no se relaja: el bot SIEMPRE debe llamar a una tool para realizar sus acciones. La diferencia es que el modelo decide cuándo llamar la tool a partir del lenguaje natural del usuario, no porque el usuario escriba un comando.
- Al diseñar una feature nueva, pensar primero: *"¿cómo lo pediría el usuario en una conversación?"* y hacer que el modelo lo resuelva vía tools. El comando equivalente es secundario u opcional.

### Compartición de proyectos (ACL) — reglas de seguridad

Los proyectos pueden compartirse con usuarios autorizados. **Reglas no negociables** (ver `docs/development.md` → "Compartición de proyectos (ACL)"):

1. **Toda lectura/escritura de proyectos pasa por `get_project_for_user(user_id, slug)`** — dueño o miembro; jamás consultar por `user_id` ajeno ni permitir filtrar proyectos por ID.
2. **Solo el dueño invita/remueve** — `is_owner` se verifica en la capa de UI **y** dentro de `add_project_member`/`remove_project_member`.
3. **Auto-invitación rechazada** — un usuario no puede añadirse solo a un proyecto ajeno.
4. **Rutas de archivos SIEMPRE con el owner** — el invitado lee/escribe en `projects/{owner_id}/{slug}/`.
5. **El `user_id` siempre se inyecta desde el update de Telegram** en `execute_tool(name, args, user_id)` — nunca desde los args del modelo.
6. **Regla de 3 urgentes contra el dueño** — incluso cuando edita un invitado.

### Soporte de grupos — reglas de seguridad (paridad v1 — ver `specs/group-parity.md`)

Los grupos de Telegram permiten colaboración completa con aislamiento estricto por `chat_id`:

1. **Proyectos de grupo**: un proyecto con `group_id` es visible en ese grupo. Al crearlo en grupo, automáticamente se añaden todos los miembros como colaboradores.
2. **Paridad en grupo**: `web_search`, `dolar_rate`, recordatorios y bitácora/memoria son **compartidos por grupo** (`group_id` tenant). Solo `generate_image` y `set_checkin_time` (y `share/unshare/delete/list_members`) quedan privados.
3. **Recordatorios en grupo**: se crean con `group_id` y se **entregan en el grupo**; cualquier miembro puede ver/editar/borrar (`group_id` aislado, `created_by` registra autor). El comando `/recordar` y `list_reminders` etc. funcionan en grupo.
4. **Bitácora en grupo**: `memory/group_-xxx/` compartida, entradas firmadas `@usuario` (`append_raw_entry(..., group_id, author_username)`), consultable vía `journal_append`/`memory_*` en grupo.
5. **Notificaciones de grupo**: cuando cambia el estado, prioridad o se agrega una nota a un proyecto de grupo, se envía una notificación breve al grupo (best-effort, nunca rompe la operación).
6. **Disolución de grupo**: si el bot es removido (`kicked/left`), se **borra todo** del grupo (`cleanup_group_data`: reminders, `days/summaries`, `memory/group_-xxx/`, `chat_history`, proyectos desasociados).
7. **`group_members`**: tabla que registra qué usuarios han interactuado en cada grupo. Se usa al crear proyectos de grupo para auto-añadir miembros y para descubrir grupos en jobs.

## Estándares de código

- **Python 3.12** con type hints en todas las firmas (`def foo(x: int) -> str:`)
- **Async nativo** (`async def`, `await`) — nunca bloquear el event loop; tareas CPU/IO bloqueantes en `asyncio.to_thread`
- **Docstrings** en los módulos y funciones públicas (explicar el *porqué*, no el *qué*)
- **Errores**: manejo explícito en los bordes; las tools SIEMPRE devuelven JSON de string al modelo (`{"ok": ...}`), nunca excepciones crudas
- **Logging** con `logging.getLogger(__name__)` (nunca `print`)
- **SQLite**: una conexión por operación con `with closing(get_connection())`; schema idempotente (`CREATE TABLE IF NOT EXISTS`)
- **Configuración** solo vía `Settings` (config.py) — nunca hardcodear valores sensibles
- **Funciones pequeñas** (< ~50 líneas), composición sobre herencia, sin estado global mutable
- **Ejecutar siempre en Docker** (`docker compose up -d --build`) — nunca `python main.py` directo

## Tests (pytest)

La suite de tests vive en `tests/` (unitarios + integración con objetos falsos de Telegram, sin red). **Antes de commitear cambios de código, ejecutar la suite y confirmar que pasa:**

```bash
# Dependencias de test (solo una vez por .venv)
.venv/bin/pip install -r requirements-dev.txt

# Ejecutar todos los tests con cobertura
.venv/bin/python -m pytest --cov=bot
```

- `--cov=bot` mide el % del código de `bot/` ejecutado por los tests (reporte por módulo).
- En CI corre `pytest --cov=bot --cov-fail-under=80` — cobertura <80% bloquea el deploy.
- Los tests NO requieren red ni tokens reales (todo mockeado) y usan BD SQLite en `tmp_path`.
- Si añades una feature nueva, añade tests para ella (happy path + edge + error).

## Seguridad

- **Nunca** committear `.env`, claves, tokens o `.private/` (ver `.gitignore`)
- El bot es **fail-secure**: sin `AUTHORIZED_USERS` nadie accede
- Contenedor **no-root** (`botuser`, UID 999), sin puertos expuestos (long-polling saliente)
- Rate limiting (3s/mensaje/usuario) y contexto limitado (30 mensajes) protegen el presupuesto de Go
- El `.env` del VPS jamás se toca en deploys (excluido del rsync)
- El repo es privado en GitHub

## Commits

Conventional Commits, en inglés, descriptivos:

```
feat: add new command
fix: correct rate limit handling
docs: update architecture
ci: adjust deploy workflow
chore: bump dependency
refactor: extract dolar module
```

## Docker SIEMPRE (desarrollo y producción)

> ⛔ **Este proyecto se ejecuta SIEMPRE en Docker.** Nunca corras el bot directamente con `python main.py` en tu máquina.

- **Desarrollo/pruebas locales**: `docker compose up -d --build` (lee tu `.env` local, datos en el volumen `bot_data`). Es el mismo contenedor que corre en el VPS: lo que pruebas localmente es exactamente lo que se despliega.
- **Producción**: mismo `docker compose up -d --build` en el VPS vía CI/CD.
- Para ver logs: `docker compose logs -f`. Para reiniciar: `docker compose up -d --build`. Para limpiar volumen: `docker compose down -v && docker compose up -d --build`.

### ⚠️ Tokens de Telegram: desarrollo ≠ producción

- Tu `.env` **local** usa un `BOT_TOKEN` de **pruebas** (bot creado con @BotFather solo para desarrollo).
- El **VPS** tiene su propio `.env` con el `BOT_TOKEN` de **producción** (`@<bot-produccion>`). Ese `.env` del servidor **jamás se toca** desde aquí: los deploys no lo sincronizan (excluido del rsync).
- **NUNCA** copies el `.env` de producción a local ni viceversa, y **nunca** uses el token de producción para pruebas locales — romperías el bot en vivo.
- Los IDs en `AUTHORIZED_USERS` pueden diferir entre entornos: el bot de pruebas solo responde a los IDs listados en tu `.env` local.

## Deploy (no push sin aprobación explícita del usuario)

- `git push origin main` → GitHub Actions → rsync al VPS → `docker compose up -d --build`
- **Antes de hacer push, pedir siempre aprobación explícita al usuario.**
- El deploy NO lleva: `.env`, bases de datos, `.venv`, `.opencode/`, `AGENTS.md`, `docs/`, `projects/` (ver `.dockerignore` y `deploy.yml`)

## Datos persistentes (volumen Docker `bot_data` → `/app/data`)

```
/app/data/
├── chat_history.db          ← historial de conversaciones (PERMANENTE, sin pruning, aislado por chat_id)
├── projects.db              ← metadatos de proyectos (+ reminders con group_id + proactive_log)
├── projects/{user_id}/{slug}/  ← contenido de proyectos (overview.md, notes/, context.json)
├── media_usage.db           ← límites de medios
├── memory.db                ← metadatos de la bitácora (días y resúmenes, con group_id)
├── memory/{user_id}/        ← bitácora por usuario (raw/, days/, summaries/)
├── memory/group_-xxx/       ← bitácora compartida por grupo (aislada, firmada @usuario)
└── entities.db              ← entidades tracking (personas, lugares, casos, documentos)
```

Estos archivos son **datos de usuario del bot** — no confundir con la documentación del proyecto.

## Pruebas automatizadas con MCP de Telegram

Las pruebas automatizadas con el MCP de Telegram **siempre** se hacen contra el bot de pruebas `@<bot-pruebas>`, **NUNCA** contra el de producción.

### Flujo de pruebas

1. **Levantar el bot de pruebas**: `docker compose up -d --build` (usa el `BOT_TOKEN` de pruebas de tu `.env` local)
2. **Ejecutar pruebas MCP**: usar el Telegram MCP para enviar mensajes al bot `@<bot-pruebas>`
3. **Verificar respuestas**: revisar que el bot responde correctamente en privado y en grupos
4. **Apagar el bot**: `docker compose down` (el bot de pruebas NO debe correr 24/7)

### Reglas de pruebas

- **Siempre** usar el chat_id del bot de pruebas (`@<bot-pruebas>`), nunca el de producción
- El bot de pruebas solo responde a los `AUTHORIZED_USERS` configurados en tu `.env` local
- Después de las pruebas, **apagar el contenedor** — el bot de pruebas no debe estar corriendo permanentemente
- Los datos de pruebas se guardan en el volumen Docker `bot_data` — se pueden limpiar con `docker compose down -v`

### IDs de referencia

| Bot | Username | Uso |
|-----|----------|-----|
| Producción | `@<bot-produccion>` | Bot en vivo, 24/7 |
| Pruebas | `@<bot-pruebas>` | Solo pruebas locales, bajo demanda |
