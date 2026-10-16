# Spec: Paridad de funciones en grupos

**Estado:** Borrador — revisado 2026-08-21 (post-discovery)
**Fecha:** 2026-08-21
**Objetivo:** Que los grupos de Telegram tengan prácticamente las mismas funciones que los chats privados, manteniendo la separación estricta de datos entre grupo y privado. Solo `generate_image` y `set_checkin_time` quedan fuera de grupos en v1.

> **Cómo leer esta revisión:** este spec fue contrastado contra el repo real (`bot/tools.py`, `bot/reminders.py`, `bot/memory.py`, `bot/history.py`, `bot/projects.py`, `bot/handlers.py`, `bot/bot.py`, `bot/proactive.py`, `bot/auto_journal.py`, `bot/summarizer.py`). Los cambios respecto al borrador inicial están motivados por gaps detectados y por decisiones del owner (ver §13 Supuestos).

---

## 1. Visión y objetivos

Hoy el bot opera con dos conjuntos rígidos: `GROUP_TOOLS` (12 tools) y `PRIVATE_TOOLS` (bloqueado en grupo). Esto deja a los grupos sin búsqueda web, dólar, recordatorios gestionables y bitácora/memoria — el usuario tiene que salir al chat privado para tareas básicas.

El objetivo es que un grupo sea un espacio **autónomo y completo**: gestionar proyectos, recordatorios, entidades, bitácora/memoria, buscar en la web y consultar el dólar **sin salir del grupo**.

### Principio rector: separación estricta de mundos (tenants)

Cada `chat_id` es un tenant aislado, sin fuga:

- Privado `group_id IS NULL` ↔ solo el `user_id` dueño.
- Grupo A `group_id = -100111…` ↔ solo `group_members` de ese grupo.
- Grupo B `group_id = -100222…` ↔ idem, aislado de A y de privado.

Reglas:

- Un recordatorio creado en grupo → se entrega **en el grupo** y es **del grupo** (cualquier miembro puede ver/editar/borrar).
- Una entrada de bitácora creada en grupo → pertenece a la **bitácora compartida del grupo** (`memory/group_-xxx/`).
- La memoria consultada en grupo → es la **memoria del grupo**.
- Los recordatorios y bitácora del chat privado siguen intactos, nunca aparecen en grupo y viceversa.
- `chat_history` ya está aislado por `chat_id` (`bot/history.py` usa `WHERE user_id=? AND chat_id=?`) — no requiere cambios, solo no regresionar.

### Exclusión deliberada en v1

- `generate_image` permanece **solo en privado** (cuota Gemini, sin sentido colaborativo).
- `set_checkin_time` permanece **solo en privado** (check-ins son personales; el proactivo de grupo usa horarios globales, no per-user en grupo).
- `share_project`, `unshare_project`, `delete_project`, `list_project_members` **quedan solo en privado en v1**: en grupo los proyectos ya auto-añaden a todos los `group_members` al crearse, por lo que compartir invitar fuera del grupo es confuso y `delete` es destructivo. Se reevalúa en v2 si hace falta.

---

## 2. Estado actual (AS-IS) — grounded

### 2.1 Tools por entorno (repo real)

Definidas en `bot/tools.py:95-133`:

```
GROUP_TOOLS = {                          PRIVATE_TOOLS (bloqueadas en grupo) = {
  create_project,                          list_reminders, cancel_reminder,
  update_project_status/priority/           edit_reminder, reschedule_reminder,
    description,                            postpone_reminder,
  add_project_note,                         share_project, unshare_project,
  list_projects, search_projects,          list_project_members, delete_project,
  get_project,                              web_search, dolar_rate,
  create_reminder,        # → hoy entrega      generate_image,
  create_entity, list_entities,            journal_append, memory_read,
  search_entities, get_entity_details,      memory_recall, memory_insights,
  add_entity_note, update_entity,           set_checkin_time
  archive_entity,
}
```

`_PROJECT_TOOLS` en `execute_tool` incluye `share/unshare/list_members/delete` pero el gate `if group_id is not None and name in PRIVATE_TOOLS` los sigue bloqueando — el spec anterior que decía "moverlos a GROUP_TOOLS" no se ha aplicado aún.

### 2.2 Cómo se aplica la restricción

`execute_tool(name, args, user_id, group_id=None, private_chat_id=None)` en `bot/tools.py:1779`:

- Si `group_id is not None and name in PRIVATE_TOOLS` → error `"Esta acción solo está disponible en chats privados con el bot."`
- Si `group_id` y tool es de proyecto → inyecta `args["_group_id"] = group_id` para `projects.py`.
- `create_reminder` en grupo → usa `private_chat_id` para entrega privada (comportamiento **a reemplazar**).

### 2.3 Problema de UX actual

`handlers.py:_run_ai_tool_loop` envía siempre `tools=TOOL_DEFINITIONS` completo al modelo, **sin filtrar**. En grupo el modelo "ve" `web_search`/`dolar_rate` como disponibles, las intenta y recibe el error de bloqueo → respuestas confusas + tokens desperdiciados.

### 2.4 Recordatorios: entrega privada forzada (a corregir)

- `_handle_create_reminder` (`tools.py:1833`) usa `args["_chat_id"]` (inyectado desde `private_chat_id`) o `get_chat_for_user(user_id)`.
- `check_due_reminders` entrega a `reminder["chat_id"]`.
- Comando `/recordar` en grupo (`handlers.py:1233 recordar_command_group`) exige `private_chat_id` y responde "te recordaré por tu chat privado".
- **No existe gestión compartida en grupo**: `list/cancel/edit/reschedule/postpone` están en `PRIVATE_TOOLS` y todas hacen `WHERE user_id = ?` (owner-gate), no `WHERE group_id = ?`.

### 2.5 Memoria/Bitácora: estrictamente por usuario (a corregir)

- `memory.py` no tiene `group_id`. Schema real:
  - `days (user_id, day, raw_path, ...)` — `PRIMARY KEY (user_id, day)`
  - `summaries (user_id, level, period, ...)` — `PRIMARY KEY (user_id, level, period)`
- Archivos en `memory/{user_id}/` (raw/, days/, summaries/).
- Las 4 tools de memoria están en `PRIVATE_TOOLS`.
- **Gap crítico:** `ALTER TABLE ADD COLUMN group_id` sin tocar PK colisiona (mismo `user_id, day` privado y de grupo tendrían misma PK). Hay que **recrear tablas** con PK nueva.

### 2.6 Historial de chat: ya correcto

`bot/history.py` usa `chat_history(user_id, chat_id, role, content, created_at)` con índice `idx_chat_user(user_id, chat_id, created_at)` y migración `chat_id = user_id` para filas legacy. `add_message(..., chat_id)` y `get_history(..., chat_id)` ya aíslan por chat. **Sin cambios**, solo mantener el `chat_id = group_id` en `handle_message_group`.

### 2.7 Entidades: ya habilitadas en grupo (sin cambios)

- `entities.py` es por `user_id`, sin `group_id`. Ya están en `GROUP_TOOLS` desde `2026-08-20-enable-entities-in-groups`.
- Decisión confirmada: se mantienen por usuario, gestionables desde grupo o privado. No hay entidades "del grupo".

### 2.8 web_search y dolar_rate

- Handlers triviales (`tools.py:1921, 1930`) sin dependencia de `user_id`.
- Están en `PRIVATE_TOOLS` solo por restricción histórica — son quick wins sin schema.

### 2.9 Proactivo y auto_journal: solo privado hoy

- `proactive.py:run_checkins` itera `authorized_users`, lee `reminders.get_chat_for_user(user_id)` y `memory_store.get_* (user_id)`.
- `auto_journal.py:auto_journal_job` escanea `chat_history WHERE user_id=? AND chat_id=user_id` (solo privado) cada 30 min.
- `summarizer.py` genera compacts/summaries por `user_id`.
- Para paridad, deben escanear también `group_id`s.

---

## 3. Modelo de datos objetivo (TO-BE) — revisado

### 3.1 Recordatorios compartidos por grupo

**Decisión owner (1+4):** el recordatorio es **del grupo**, cualquier miembro puede ver/editar/borrar.

Tabla `reminders` (en `projects.db`): añadir `group_id` + `created_by` (auditoría), mantener `chat_id` como destino de entrega.

```sql
-- Migración idempotente (ver §7.1)
ALTER TABLE reminders ADD COLUMN group_id INTEGER;      -- NULL = privado, -100xxx = grupo
ALTER TABLE reminders ADD COLUMN created_by INTEGER;    -- auditoría, nullable para legacy
CREATE INDEX IF NOT EXISTS idx_reminders_group ON reminders(group_id, sent);
CREATE INDEX IF NOT EXISTS idx_reminders_due_group ON reminders(sent, remind_at, group_id);
```

Semántica:

- `group_id IS NULL` → privado, `chat_id` = `private_chat_id` (como hoy).
- `group_id = <group_chat_id>` → de grupo, `chat_id = group_id`, entrega en el grupo. `created_by = caller` solo informativo.

Funciones `bot/reminders.py` con scope:

- `create_reminder(user_id, chat_id, message, remind_at, recurrence, group_id=None, created_by=None)` — si `group_id` no es None, valida que `caller IN group_members`.
- `list_pending_reminders(user_id, group_id=None)` → privado: `WHERE group_id IS NULL AND user_id=? AND sent=0`; grupo: `WHERE group_id=? AND sent=0` (sin filtrar por `user_id`, ordenado por `remind_at`). Solo miembros del grupo pueden llamarla (gate en `tools.py`).
- `delete_reminder(reminder_id, user_id, group_id=None)` / `update_reminder_text/time`, `postpone_reminder_db` → en grupo validan `EXISTS (SELECT 1 FROM group_members WHERE group_id=? AND user_id=caller)` en vez de `reminder.user_id == caller`. En privado mantienen `WHERE id=? AND user_id=?`.
- `get_reminder_by_id` → variante `get_reminder_by_id_for_scope(reminder_id, caller, group_id)` que en grupo no filtra por `user_id`.
- **Entrega:** `check_due_reminders` sin cambios — ya entrega a `reminder.chat_id`; con `chat_id=group_id` suena en el grupo.
- **Comando `/recordar` en grupo:** deja de exigir `private_chat_id`; crea con `chat_id=group_id, group_id=group_id, created_by=user_id`; responde "Ok, te lo recordaré aquí en el grupo 👍".
- **Sin migración de históricos:** lo existente queda `group_id IS NULL` (privado). Lo nuevo desde el deploy es de grupo.

`projects.db` ya existe; la migración es idempotente vía `PRAGMA table_info`.

### 3.2 Bitácora/Memoria compartida por grupo

**Decisión owner (2+5):** memoria **compartida por grupo**, aislada de la privada. Cualquier miembro puede añadir/leer.

**Problema de PK:** no basta `ADD COLUMN`. Hay que recrear tablas para que `group_id` forme parte de la unicidad.

Opción A adoptada (reutilizar tablas con constraint, sin duplicar tablas):

```sql
-- Recreación idempotente (ver §7.1 para script con fallback)
-- days: una fila es (user_id, day) si privado, o (group_id, day) si de grupo
CREATE TABLE IF NOT EXISTS days_new (
    user_id INTEGER,                -- NULL si es fila de grupo
    group_id INTEGER,               -- NULL si es fila privada
    day TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    raw_word_count INTEGER NOT NULL DEFAULT 0,
    entry_count INTEGER NOT NULL DEFAULT 0,
    compact_path TEXT,
    compact_word_count INTEGER NOT NULL DEFAULT 0,
    range_start REAL NOT NULL,
    range_end REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK ((user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL)),
    PRIMARY KEY (day, COALESCE(user_id, group_id), COALESCE(group_id, 0))
    -- En SQLite se implementa como UNIQUE(day, user_id, group_id) + CHECK; la PK compuesta real
    -- se resuelve en la migración con CREATE TABLE ... UNIQUE(user_id, group_id, day)
);
-- Equivalente práctico en migración:
-- UNIQUE(user_id, group_id, day) donde uno de los dos es NULL; SQLite trata NULLs como distintos,
-- por lo que se añade CHECK y se fuerza UNIQUE(user_id, day) WHERE group_id IS NULL
-- y UNIQUE(group_id, day) WHERE group_id IS NOT NULL vía índices únicos parciales.

CREATE UNIQUE INDEX IF NOT EXISTS ux_days_private ON days(user_id, day) WHERE group_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_days_group   ON days(group_id, day) WHERE group_id IS NOT NULL;

-- summaries: idem
CREATE UNIQUE INDEX IF NOT EXISTS ux_summaries_private ON summaries(user_id, level, period) WHERE group_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_summaries_group   ON summaries(group_id, level, period) WHERE group_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_days_owner ON days(group_id, user_id, day);
CREATE INDEX IF NOT EXISTS idx_summaries_owner ON summaries(group_id, user_id, level, range_end);
```

Si el equipo prefiere **tablas separadas** (`group_days`, `group_summaries`), es equivalente en aislamiento pero duplica queries en `summarizer.py` — se deja como alternativa si la recreación se considera demasiado invasiva. El spec recomienda **una sola tabla con índices parciales** para no bifurcar `memory.py`.

**Rutas de archivos:** `memory/{owner}/` donde owner es:
- `{user_id}` para privado → `memory/12345/raw/2026-08-21.md`
- `group_{group_id}` para grupo → `memory/group_-1001234567890/raw/2026-08-21.md`

> Prefijo `group_` hace explícito el dominio en FS y logs, aunque `group_id` ya sea negativo.

**Guards:** nuevo `_group_memory_dir(group_id)` + `_safe_under_memory_group(group_id, relative)` que valida `is_relative_to(MEMORY_DIR)` y `is_relative_to(group_root)` (análogo a `_safe_under_memory` para `user_id`). `_ensure_user_layout` se generaliza a `_ensure_owner_layout(user_id=None, group_id=None)`.

**Funciones `memory.py`:** toman `group_id` opcional, exclusivo con `user_id`:

- `append_raw_entry(user_id=None, group_id=None, day, text, ts=None, tz_name="UTC", author_username=None)`
- `get_raw_day(...)`, `get_raw_entries(...)`, `save_daily_compact(...)`, `get_summary(...)`, `save_summary(...)`, `list_summaries(...)` — todos con branch `WHERE group_id IS NULL` vs `WHERE group_id = ?`.
- En grupo el texto se guarda como `- [HH:MM] @usuario: texto` para trazabilidad (decisión 5b). En privado se mantiene `- [HH:MM] texto`.

**Backward compat:** filas existentes quedan `group_id IS NULL` (privado). No se migran datos a grupos.

### 3.3 Resúmenes y proactividad en grupo

**Decisión owner (7):** sí, `auto_journal` y resúmenes para grupos, con memoria compartida.

- `auto_journal_job` cada 30 min: además de iterar `authorized_users` con `chat_id=user_id`, itera `SELECT DISTINCT group_id FROM group_members` y para cada `group_id` hace `_get_recent_messages(group_id, group_id)` → extrae eventos/entidades → `_save_events_to_journal(group_id=...)` / `_save_entities` (entidades siguen por `user_id` del autor, no se duplican a grupo).
- `summarizer.py`: `generate_daily_compact`, `generate_weekly/quarterly/yearly_summary`, `ensure_summary` aceptan `group_id` (owner). `collect_period_sources(group_id=...)` lee desde `memory/group_-xxx/`.
- `proactive.py`: nuevos jobs `run_group_summaries` (o ampliar los existentes) que iteren grupos; `run_checkins` **no** aplica a grupos (check-ins son personales por definición — se mantiene). Los resúmenes de grupo no se envían proactivamente al grupo en v1 (solo quedan disponibles vía `memory_read/recall`); el envío opcional al `group_id` se deja para v2 si se desea notificar "resumen semanal del grupo listo".
- **Coste:** 1 call modelo por grupo cada 30 min (auto_journal) + 1 call por semana/trimestre/año con contenido. Escalar con N grupos aumenta coste lineal — se documenta en §9.

### 3.4 Entidades: sin cambios (confirmado)

Siguen por `user_id`. Ya funcionan en grupo. No se modelan como "del grupo".

---

## 4. Historias de usuario

> **Principio UX (AGENTS.md):** vía principal **chat natural**, no comandos. Los comandos son para testeo/seguridad. El bot SIEMPRE ejecuta una tool.

### EP-01: Búsqueda web y dólar en grupo

**US-01.1 — Buscar en la web desde un grupo**
> "busca cómo podar un limonero" → `web_search`, responde en el grupo con resumen + fuentes.

**US-01.2 — Consultar el dólar desde un grupo**
> "a cómo está el dólar" → `dolar_rate`, responde en el grupo (BCV + Binance).

### EP-02: Recordatorios compartidos de grupo

**US-02.1 — Crear por lenguaje natural (vía principal)**
> "recuérdame a todos que mañana hay reunión a las 4pm" → `create_reminder` con `group_id` como `chat_id`; confirma "aquí en el grupo".

**US-02.2 — Crear por comando (secundario)**
> `/recordar pagar la luz el 28` en grupo → `chat_id=group_id, group_id=group_id`.

**US-02.3 — Listar recordatorios del grupo (compartidos)**
> "qué recordatorios tenemos pendientes" o `/recordatorios` en grupo → `list_pending_reminders(group_id=X)` devuelve **todos los del grupo**, no solo los del caller; botones Cancelar/Posponer operan sobre cualquiera (cualquier miembro puede accionar).

**US-02.4 — Cancelar/editar compartido**
> "cancela el de la reunión" / "pospónlo al viernes" → `cancel/edit/reschedule/postpone` con `group_id` actual; validan pertenencia al grupo, no ownership.

### EP-03: Bitácora/Memoria compartida

**US-03.1 — Añadir entrada grupal**
> "anota en la bitácora: hoy terminamos el presupuesto del viaje" → `journal_append` en grupo escribe `- [HH:MM] @usuario: hoy terminamos...` en `memory/group_-xxx/`.

**US-03.2 — Consultar memoria grupal**
> "¿cuándo fue la última reunión de planeación?" → `memory_recall/read/insights` con `group_id` buscan solo en memoria del grupo.

**US-03.3 — Separación garantizada**
> Privado `IS NULL` vs grupo `=X`; ningún query mezcla scopes. `chat_history` ya aísla por `chat_id`.

### EP-04: Entidades (ya disponible)

**US-04.1 — Entidades por usuario desde grupo**
> Crear `case/person` desde grupo queda asociado a mí, visible en grupo y en privado.

### EP-05: Proyectos (ya disponible, sin ampliar en v1)

**US-05.1 — Gestión de proyectos de grupo**
> Crear/listar/actualizar/nota ya funcionan con `group_id`. `delete/share/unshare/list_members` **no** se habilitan en grupo en v1 (se usan en privado).

---

## 5. Funciones por entorno (matriz objetivo v1)

| Categoría | Tool | Privado | Grupo | Notas |
|-----------|------|:-------:|:-----:|-------|
| Proyectos | create_project | ✅ | ✅ | Ya |
| | update_project_status/priority/description | ✅ | ✅ | Ya |
| | add_project_note | ✅ | ✅ | Ya |
| | list_projects | ✅ | ✅ | Ya (scope group) |
| | search_projects | ✅ | ✅ | Ya |
| | get_project | ✅ | ✅ | Ya |
| | delete_project | ✅ | ❌ | **Queda privado en v1** |
| | share_project | ✅ | ❌ | Queda privado |
| | unshare_project | ✅ | ❌ | Queda privado |
| | list_project_members | ✅ | ❌ | Queda privado |
| Recordatorios | create_reminder | ✅ (priv) | ✅ (grupo) | Entrega en grupo, shared |
| | list_reminders | ✅ | ✅ | **Mover** + `group_id`, shared |
| | cancel_reminder | ✅ | ✅ | **Mover** + ACL grupo |
| | edit_reminder | ✅ | ✅ | **Mover** + ACL grupo |
| | reschedule_reminder | ✅ | ✅ | **Mover** + ACL grupo |
| | postpone_reminder | ✅ | ✅ | **Mover** + ACL grupo |
| Entidades | create_entity, list_entities, … | ✅ | ✅ | Ya (por usuario) |
| Bitácora | journal_append | ✅ | ✅ | **Mover** + `group_id`, con autor |
| Memoria | memory_read | ✅ | ✅ | **Mover** + `group_id` |
| | memory_recall | ✅ | ✅ | **Mover** + `group_id` |
| | memory_insights | ✅ | ✅ | **Mover** + `group_id` |
| | set_checkin_time | ✅ | ❌ | Queda privado |
| Web | web_search | ✅ | ✅ | **Mover** |
| | dolar_rate | ✅ | ✅ | **Mover** |
| Imágenes | generate_image | ✅ | ❌ | Permanece privado |
| Historial | chat_history | ✅ | ✅ | Ya aislado por `chat_id` |
| Proactivo | check-ins | ✅ | ❌ | Personal |
| | auto_journal + summaries | ✅ | ✅ | **Nuevo para grupos** |

**Conjuntos resultantes (whitelist curada, no blacklist ciega):**
```python
PRIVATE_ONLY_TOOLS = {"generate_image", "set_checkin_time",
                      "share_project", "unshare_project", "delete_project", "list_project_members"}
GROUP_TOOLS = ALL_TOOL_NAMES - PRIVATE_ONLY_TOOLS  # en la práctica, subset explícito de arriba
```
En `execute_tool` se bloquea `PRIVATE_ONLY_TOOLS` cuando `group_id is not None`; el resto se permite. Si en v2 se quiere habilitar `share/delete` en grupo, es quitarlos de `PRIVATE_ONLY_TOOLS`.

---

## 6. Mejoras de UX: filtrado de definiciones por entorno

**Problema:** `handlers.py:_run_ai_tool_loop` manda `TOOL_DEFINITIONS` completo incluso en grupo → el modelo intenta `web_search` y falla.

**Solución:** filtrar antes de llamar al modelo.

```python
def _filter_tool_definitions(group_id: int | None) -> list[dict]:
    if group_id is None:
        return TOOL_DEFINITIONS
    blocked = PRIVATE_ONLY_TOOLS
    return [d for d in TOOL_DEFINITIONS if d["function"]["name"] not in blocked]

# En _run_ai_tool_loop, antes de chat_completion_with_tools:
tools_for_model = _filter_tool_definitions(group_id)
result = await chat_completion_with_tools(..., tools=tools_for_model)
```

Cero tokens desperdiciados, cero intentos fallidos.

### 6.1 Prompt de sistema de grupo actualizado

Bloque `--- CONTEXTO DE GRUPO ---` en `handlers.py`:

- Los recordatorios que crees aquí se entregarán **en este grupo** y son **compartidos** (cualquier miembro puede gestionarlos).
- La bitácora y memoria que uses aquí son **del grupo** (`group_-xxx`), compartidas y aisladas de tu privado. Cada entrada queda firmada `@usuario`.
- Las entidades son personales (tuyas), gestionables desde aquí o desde privado.
- Búsqueda web y dólar funcionan aquí; imágenes solo en privado.

---

## 7. Arquitectura técnica

### 7.1 Migración de schema

Idempotente, protegida por `PRAGMA table_info` y `CREATE ... IF NOT EXISTS`.

**`projects.db` — `reminders`:**
```python
cols = {r["name"] for r in conn.execute("PRAGMA table_info(reminders)")}
if "group_id" not in cols:
    conn.execute("ALTER TABLE reminders ADD COLUMN group_id INTEGER")
if "created_by" not in cols:
    conn.execute("ALTER TABLE reminders ADD COLUMN created_by INTEGER")
conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_group ON reminders(group_id, sent)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_reminders_due_group ON reminders(sent, remind_at, group_id)")
```

**`memory.db` — `days` y `summaries` (recreación):**
```python
# Detecta si falta group_id
cols_days = {r["name"] for r in conn.execute("PRAGMA table_info(days)")}
if "group_id" not in cols_days:
    # 1. Renombrar tabla vieja
    conn.execute("ALTER TABLE days RENAME TO days_old")
    # 2. Crear nueva con group_id + índices parciales
    conn.execute("""
        CREATE TABLE days (
            user_id INTEGER,
            group_id INTEGER,
            day TEXT NOT NULL,
            raw_path TEXT NOT NULL,
            raw_word_count INTEGER NOT NULL DEFAULT 0,
            entry_count INTEGER NOT NULL DEFAULT 0,
            compact_path TEXT,
            compact_word_count INTEGER NOT NULL DEFAULT 0,
            range_start REAL NOT NULL,
            range_end REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            CHECK ((user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL))
        )
    """)
    conn.execute("CREATE UNIQUE INDEX ux_days_private ON days(user_id, day) WHERE group_id IS NULL")
    conn.execute("CREATE UNIQUE INDEX ux_days_group ON days(group_id, day) WHERE group_id IS NOT NULL")
    conn.execute("CREATE INDEX idx_days_owner ON days(group_id, user_id, day)")
    # 3. Copiar datos existentes (todos privados)
    conn.execute("""
        INSERT INTO days (user_id, group_id, day, raw_path, raw_word_count, entry_count,
                          compact_path, compact_word_count, range_start, range_end, created_at, updated_at)
        SELECT user_id, NULL, day, raw_path, raw_word_count, entry_count,
               compact_path, compact_word_count, range_start, range_end, created_at, updated_at
        FROM days_old
    """)
    conn.execute("DROP TABLE days_old")
# Idem para summaries con ux_summaries_private/group + idx_summaries_owner
```

**Backward compat:** filas existentes → `group_id IS NULL` (privado). Código que filtra `IS NULL` las sigue viendo.

### 7.2 Cambios por módulo

#### `bot/tools.py`

1. Definir `PRIVATE_ONLY_TOOLS = {"generate_image","set_checkin_time","share_project","unshare_project","delete_project","list_project_members"}` y helper `_filter_tool_definitions`.
2. `execute_tool`: `if group_id is not None and name in PRIVATE_ONLY_TOOLS` → error. Para `create_reminder` en grupo inyectar `args["_chat_id"]=group_id`, `args["_group_id"]=group_id`, `args["_author_username"]=resolve_username(user_id)`; para `journal_append`/`memory_*` inyectar `args["_group_id"]`; para proyectos inyectar `args["_group_id"]` como hoy.
3. Handlers de recordatorios/memoria leen `args["_group_id"]` y lo pasan a `reminders.py`/`memory.py` con validación de pertenencia al grupo.
4. `_dispatch` sin cambio de firma, pero handlers usan `_group_id` y `_author_username` cuando aplica.

#### `bot/reminders.py`

- Firmas: `create_reminder(user_id, chat_id, message, remind_at, recurrence="none", group_id=None, created_by=None)`, `list_pending_reminders(caller_id, group_id=None)`, `delete_reminder(reminder_id, caller_id, group_id=None)`, `update_reminder_text/time`, `postpone_reminder_db`, `get_reminder_by_id_for_scope`.
- Branches: `group_id is None` → `WHERE group_id IS NULL AND user_id=?`; `group_id is not None` → `WHERE group_id=?` + `AND EXISTS (SELECT 1 FROM group_members WHERE group_id=? AND user_id=caller)`.
- Helpers: `cancel_group_reminders(group_id)` → `DELETE FROM reminders WHERE group_id=?` (borrado, ver §7.5), `list_group_reminders(group_id)`.
- Índices nuevos ya creados en migración.

#### `bot/memory.py`

- Nuevos helpers `_group_memory_dir(group_id)`, `_safe_under_memory_group`, `_ensure_owner_layout(user_id=None, group_id=None)`, `_owner_dir(user_id, group_id)`.
- CRUD con `group_id`: `append_raw_entry(..., group_id=None, author_username=None)` escribe en `memory/group_-xxx/raw/YYYY-MM-DD.md` con `- [HH:MM] @user: text` cuando `group_id` está presente; si no, mantiene formato privado.
- `get_raw_day`, `get_raw_entries`, `get_summary`, `save_summary`, `list_summaries` con filtrado `IS NULL` vs `= ?` y paths correspondientes.
- `count_words`, `_validate_day`, `_validate_summary_period` sin cambios.

#### `bot/handlers.py`

- `_run_ai_tool_loop`: filtrar `TOOL_DEFINITIONS` vía `_filter_tool_definitions(group_id)` y prompt §6.1.
- `recordar_command_group`: no exige `private_chat_id`; crea `chat_id=group_id, group_id=group_id`; responde "te lo recordaré aquí en el grupo".
- `recordatorios_command`: detecta `chat.type in ("group","supergroup")` → lista por `group_id`, si no por `user_id`.
- `reminder_callback` (botones `remind_cancel/postpone`): resuelve `group_id = query.message.chat.id if chat.type != "private" else None` y valida pertenencia antes de `delete/postpone`.
- `handle_message_group`: ya registra `add_group_member`; mantiene `add_message(... chat_id=group_id)` y `get_history(..., chat_id=group_id)`.

#### `bot/proactive.py` + `bot/auto_journal.py` + `bot/summarizer.py`

- `auto_journal_job`: itera `authorized_users` para privado + `SELECT DISTINCT group_id FROM group_members` para grupos; para cada grupo usa `chat_id=group_id` y `group_id` como owner.
- `summarizer.collect_period_sources(group_id=...)` y `generate_*` con `group_id` param; `ensure_summary` idem.
- `proactive.run_checkins` sigue solo privado; se añade `run_group_summaries` o se amplían los jobs existentes para iterar grupos (sin envío proactivo al grupo en v1).

#### `bot/bot.py`

- Registrar `recordatorios_command` con filtro de grupo (o hacer que el handler detecte `chat.type` internamente); `reminder_callback` ya cubre ambos scopes.
- `create_application` registra jobs adicionales de `auto_journal` para grupos si se separan.

### 7.3 Flujo de IA actualizado

1. Mensaje en grupo → `handle_message_group` registra `group_members`, guarda en `chat_history` con `chat_id=group_id`, filtra tools.
2. Modelo recibe solo tools permitidas en grupo.
3. Tool de recordatorio/memoria se ejecuta con `group_id` → opera sobre scope compartido.
4. Respuesta se publica en el grupo y se guarda en `chat_history` con `chat_id=group_id`.

### 7.4 Seguridad

- `user_id` y `group_id` siempre desde `update.effective_user.id` / `update.effective_chat.id`, nunca de args del modelo.
- Aislamiento: `group_id IS NULL` ↔ privado, `group_id = X` ↔ grupo X. Índices parciales garantizan unicidad.
- Rate limiting por `user_id` (3s) se mantiene; flood de grupo se mitiga por el mismo gate. Entrega de recordatorios best-effort.
- Validación de pertenencia: toda operación de grupo hace `EXISTS (SELECT 1 FROM group_members WHERE group_id=? AND user_id=caller)` antes de leer/escribir.

### 7.5 Disolución de grupo (cleanup) — borrado total

**Decisión owner (8):** al remover el bot (`handle_chat_member` con `status in ("kicked","left")`) se **borra todo** lo del grupo para no retener datos sin permiso.

```python
def cleanup_group_data(group_id: int):
    with get_projects_conn() as conn:
        conn.execute("DELETE FROM reminders WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM projects WHERE group_id=?", (group_id,))  # o migrar a privado y luego borrar dirs según política
        conn.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
    with get_memory_conn() as conn:
        conn.execute("DELETE FROM days WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM summaries WHERE group_id=?", (group_id,))
    with get_history_conn() as conn:
        conn.execute("DELETE FROM chat_history WHERE chat_id=?", (group_id,))
    shutil.rmtree(MEMORY_DIR / f"group_{group_id}", ignore_errors=True)
    # projects dirs: si se opta por borrar, también rmtree de cada project dir del grupo
```

Nota: para proyectos, hoy `migrate_group_projects_to_private` hace `UPDATE group_id=NULL` y `DELETE FROM project_members` + `rmtree` del dir. Con la nueva política de borrado total, ese `UPDATE` se reemplaza por `DELETE`. Si se quiere preservar proyectos del grupo como privados del owner, habría que mantener el `UPDATE`; la decisión actual es **borrar**.

---

## 8. Estrategia de pruebas

### 8.1 Schema y migración

- Migración `reminders` idempotente (2 corridas no duplican columnas/índices); filas legacy quedan `group_id IS NULL`.
- Migración `memory` recrea tablas sin perder filas privadas; índices parciales `ux_*` existen; correr 2x no pierde datos.

### 8.2 Recordatorios compartidos

- `create_reminder(..., group_id=-1001)` → `chat_id=-1001, group_id=-1001, created_by=caller`.
- `list_pending_reminders(caller, group_id=-1001)` → solo `group_id=-1001`, incluye los creados por otros miembros.
- `list_pending_reminders(caller, group_id=None)` → solo privados `IS NULL`.
- `cancel_reminder(id, caller_A, group_id=-1001)` donde `id` fue creado por B → **ok** si A es miembro del grupo; si A no es miembro → rechazo.
- `check_due_reminders` entrega a `chat_id=-1001` (grupo).

### 8.3 Memoria compartida

- `append_raw_entry(group_id=-1001, author_username="pepe")` → `memory/group_-1001/raw/YYYY-MM-DD.md` con `- [HH:MM] @pepe: texto`.
- `recall` con `group_id` solo busca en `memory/group_-1001/`; privado no fuga.
- Rutas no colisionan: `memory/123` vs `memory/group_-1001`.

### 8.4 Tools por entorno

- `execute_tool("web_search", ..., group_id=-1001)` → ok.
- `execute_tool("dolar_rate", ..., group_id=-1001)` → ok.
- `execute_tool("journal_append", ..., group_id=-1001)` → ok (escribe en grupo).
- `execute_tool("generate_image", ..., group_id=-1001)` → error privado.
- `execute_tool("share_project", ..., group_id=-1001)` → error privado (queda fuera en v1).
- `tests/test_group_support.py::TestGroupToolsScope` actualizado.

### 8.5 Handlers

- `/recordar` en grupo crea con `chat_id=group_id`.
- `/recordatorios` en grupo lista solo del grupo, cualquier miembro ve todos.
- Botones `remind_cancel/postpone` con scope de grupo validan pertenencia.
- `TOOL_DEFINITIONS` filtrado en grupo excluye `PRIVATE_ONLY_TOOLS`.

### 8.6 Cobertura

- `pytest --cov=bot --cov-fail-under=80` debe pasar; añadir tests para todo lo migrado + regresión privado.

---

## 9. Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|--------|---------|------------|
| Fuga privado→grupo | **Crítico** | `IS NULL` vs `= ?` + `EXISTS group_members` + tests de aislamiento. |
| Coste modelo × N grupos (`auto_journal` 1 call/30min/grupo + summaries) | Medio-Alto | Fasear: v1 activa `auto_journal` para grupos pero con `LOOKBACK_HOURS` y dedup; monitorizar `TELEGRAM_BOT` usage; permitir `MEMORY_ENABLED` off por grupo si hace falta. |
| Borrado irreversible al salir del grupo | Medio | Es la decisión pedida (8); loggear `cleanup_group_data` + documentar que no hay recovery sin backup. |
| Colisión PK al migrar `memory.db` | Alto | Migración con `days_old` + índices parciales, testeada en `tmp_path` con filas legacy. |
| Confusión "¿esto es del grupo o privado?" | Medio | Prompt §6.1 + confirmaciones "aquí en el grupo" + firma `@usuario`. |
| Rate limit en grupos activos | Bajo | Gate por `user_id` (3s) ya existe; flood de N miembros distintos no se bloquea — aceptado. |
| Contaminación memoria grupo con privado | Medio | `journal_append` en grupo siempre `group_id`, nunca `user_id`. |

---

## 10. Fases de implementación

### Fase 1 — Quick wins: `web_search` y `dolar_rate` en grupo
**Sin schema.** Sacar de `PRIVATE_TOOLS` → `GROUP_TOOLS`, filtrar `TOOL_DEFINITIONS`, actualizar prompt §6.1, tests. **Entrega aislable.**

### Fase 2 — Recordatorios compartidos
Migración `reminders` (`group_id`, `created_by`, índices), reescribir `reminders.py` con ACL de grupo (any-member), `tools.py` inyección `group_id`, `handlers.py` comandos+callbacks con scope, `check_due_reminders` ya entrega en grupo, `cleanup_group_data` para reminders. Tests §8.2.

### Fase 3 — Bitácora/Memoria compartida
**La más invasiva.** Migración `memory.db` con recreación + índices parciales, `_owner_dir`/`_safe_under_memory_group`, `append_raw_entry` con autor, `tools`/`handlers` para `journal_append` + `memory_*` con `group_id`, `auto_journal` + `summarizer`/`proactive` iterando grupos. Tests §8.3. **Bloquea Fase 4.**

### Fase 4 — Pulido y docs
Actualizar `AGENTS.md` (grupos), `docs/group-support.md`, documentar ownership, revisar `SYSTEM_PROMPT` solo si menciona restricciones obsoletas (sin meter docs de desarrollo al prompt).

> **Orden recomendado:** Fase 1 puede ir sola a prod. Fase 2 y 3 son dependientes (Fase 3 asume `group_id` plumbing de Fase 2). No habilitar `share/delete` en grupo en v1.

---

## 11. Criterios de aceptación

- [ ] En grupo, `web_search` y `dolar_rate` funcionan y responden en el grupo.
- [ ] En grupo, `/recordar` crea recordatorio que suena **en el grupo** y es visible/editable por cualquier miembro.
- [ ] En grupo, `/recordatorios` lista **todos** los del `group_id` (no solo los míos) con botones que cualquier miembro puede accionar.
- [ ] En grupo, `journal_append` escribe `- [HH:MM] @usuario: texto` en `memory/group_-xxx/` compartida.
- [ ] En grupo, `memory_read/recall/insights` buscan solo en memoria del grupo.
- [ ] Mis recordatorios/bitácora privados no aparecen en ningún grupo; los de grupo A no aparecen en grupo B ni en privado.
- [ ] `generate_image` y `set_checkin_time` siguen solo en privado; `share/unshare/delete/list_members` también quedan privados en v1.
- [ ] El modelo no recibe `PRIVATE_ONLY_TOOLS` en contexto de grupo.
- [ ] `pytest --cov=bot --cov-fail-under=80` pasa.
- [ ] Al remover el bot del grupo, se borran `reminders`, `days/summaries`, `chat_history` y `memory/group_-xxx/` de ese `group_id`.
- [ ] `auto_journal` genera entradas para grupos activos.

---

## 12. Out of scope

- **Check-ins proactivos en grupo** (matutinos/vespertinos) — personales, no aplican.
- **Entidades de grupo** — siguen por usuario.
- **Notificación push de resumen de grupo al grupo** — el resumen queda disponible vía `memory_read/recall`; el push se deja para v2.
- **Migración de históricos** — no se convierten recordatorios/bitácoras viejas a grupos.
- **Share/delete de proyectos en grupo** — fuera de v1 (ver §5).

---

## 13. Supuestos y decisiones tomadas (para trazabilidad)

- **Recordatorios compartidos (any-member ACL):** `group_id` es el gate, no `user_id`. `created_by` es solo auditoría.
- **Memoria compartida con PK nueva:** se asume que recrear `days`/`summaries` con `group_id` nullable + índices únicos parciales es aceptable (requiere ventana de migración con backup). Alternativa de tablas separadas queda documentada pero no adoptada.
- **Autor en bitácora de grupo:** formato `- [HH:MM] @username: texto` (si no hay username, `first_name` o `user_id`).
- **Cleanup = borrado total:** `chat_history`, `reminders`, `days/summaries`, `memory/group_-xxx/` y `group_members` se eliminan al `kicked/left`. Proyectos de grupo también se eliminan (no se migran a privado). Si prefieres preservar proyectos como privados, cambia `cleanup_group_data` a `UPDATE group_id=NULL`.
- **Coste de `auto_journal` para grupos:** se acepta 1 call/grupo/30min. Si N grupos crece, se evaluará sampling o deshabilitar por grupo.
- **Historial ya aislado:** no se toca `history.py` más allá de no romper `chat_id` scoping.
