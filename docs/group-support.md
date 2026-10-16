# Soporte para Grupos — Telegram AI Bot

> **⚠️ Actualizado 2026-08-22:** Este plan inicial fue **implementado y ampliado** — la paridad completa está especificada en `specs/group-parity.md` (v1: web_search, dolar_rate, recordatorios y bitácora compartidos por grupo, entrega en el grupo). Este doc se conserva por historial; la referencia vigente es el spec.

Plan de implementación para habilitar la interacción del bot en grupos de Telegram. **Solo para desarrollo** — el bot de Telegram jamás lee este archivo (ver `AGENTS.md`).

---

## 1. Resumen Ejecutivo

El bot actualmente funciona exclusivamente en chats privados. Este plan habilita la interacción en grupos de Telegram con las siguientes restricciones de privacidad:

- **En grupos**: proyectos, recordatorios, bitácora/memoria, búsqueda web y dólar — todo **compartido y aislado por `group_id`** (paridad v1, ver `specs/group-parity.md`)
- **En privado**: se accede a toda la información del usuario (igual que ahora), aislado de grupos
- **Recordatorios y bitácora en grupo**: **compartidos por grupo**, se entregan en el grupo y cualquier miembro puede gestionar; privados quedan aislados
- **Miembros no autorizados**: no pueden usar el bot en grupos

---

## 2. Requisitos

### 2.1 Funcionales

| ID | Requisito | Prioridad |
|----|-----------|-----------|
| F1 | El bot responde a mensajes naturales en grupos | Alta |
| F2 | En grupos solo se muestran proyectos compartidos entre miembros | Alta |
| F3 | Los proyectos en grupo tienen `group_id` (= `chat_id` del grupo) | Alta |
| F4 | Crear proyecto en grupo añade automáticamente a todos los miembros | Alta |
| F5 | Los proyectos en grupo se pueden editar desde chats privados | Alta |
| F6 | Recordatorios en grupo son compartidos (entrega en el grupo, visibles por todos los miembros, aislados por `group_id`) | Alta |
| F7 | Bitácora en grupo es compartida (`memory/group_-xxx/`, firmada `@usuario`) | Alta |
| F8 | Solo usuarios autorizados pueden usar el bot en grupos | Alta |
| F9 | El bot ignora a miembros no autorizados en grupos | Media |
| F10 | Respuestas en grupo son públicas | Media |

### 2.2 No funcionales

| ID | Requisito |
|----|-----------|
| NF1 | Retrocompatibilidad: chats privados funcionan igual que antes |
| NF2 | No exponer información privada de usuarios en grupos |
| NF3 | Mantener la arquitectura existente (SQLite + Markdown) |
| NF4 | Tests para nuevos flujos |

---

## 3. Diseño de Base de Datos

### 3.1 Cambio en tabla `projects`

Agregar campo `group_id` (nullable):

```sql
ALTER TABLE projects ADD COLUMN group_id INTEGER;
```

- `group_id = NULL`: proyecto privado (comportamiento actual)
- `group_id = chat_id`: proyecto creado en grupo o asociado a grupo

**Índice para queries de grupo:**
```sql
CREATE INDEX IF NOT EXISTS idx_projects_group ON projects(group_id);
```

### 3.2 Cambio en tabla `users`

Agregar campo `private_chat_id` para recordatorios desde grupo:

```sql
ALTER TABLE users ADD COLUMN private_chat_id INTEGER;
```

- Se actualiza cuando el usuario interactúa en chat privado
- Se usa para crear recordatorios cuando el usuario pide en grupo

### 3.3 Nueva tabla `group_members`

Para rastrear qué usuarios son miembros de un grupo (necesario para auto-invitar al crear proyectos):

```sql
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    first_seen_at REAL NOT NULL,
    UNIQUE (group_id, user_id)
);
```

**Índice:**
```sql
CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id);
```

### 3.4 Diagrama ER

```
┌─────────────────┐      ┌─────────────────────┐
│    projects      │      │   project_members    │
├─────────────────┤      ├─────────────────────┤
│ id (PK)         │      │ id (PK)             │
│ user_id (FK)    │──┐   │ project_id (FK)     │
│ slug            │  │   │ user_id             │
│ name            │  │   │ invited_by          │
│ status          │  │   │ created_at          │
│ description     │  │   └─────────────────────┘
│ priority        │  │
│ group_id (NEW)  │  │   ┌─────────────────────┐
│ created_at      │  │   │   group_members      │
│ updated_at      │  │   ├─────────────────────┤
│ ...             │  │   │ id (PK)             │
└─────────────────┘  │   │ group_id            │
                     │   │ user_id             │
                     │   │ first_seen_at       │
                     │   └─────────────────────┘
                     │
                     │   ┌─────────────────────┐
                     └──>│      users           │
                         ├─────────────────────┤
                         │ user_id (PK)        │
                         │ username            │
                         │ first_name          │
                         │ private_chat_id (NEW)│
                         │ updated_at          │
                         └─────────────────────┘
```

---

## 4. Cambios por Módulo

### 4.1 `bot/projects.py`

#### Nuevas funciones

```python
def get_group_projects(group_id: int) -> list[dict]:
    """Return all projects created in or associated with a group."""
    # SELECT * FROM projects WHERE group_id = ? ORDER BY updated_at DESC

def get_shared_projects_in_group(group_id: int) -> list[dict]:
    """Return projects shared among group members.
    
    Logic: projects where group_id = chat_id AND at least 2 members
    of the group are either owner or project_members.
    """
    # SELECT DISTINCT p.* FROM projects p
    # JOIN group_members gm ON gm.group_id = p.group_id
    # WHERE p.group_id = ?
    # AND (p.user_id IN (SELECT user_id FROM group_members WHERE group_id = ?)
    #      OR p.id IN (SELECT project_id FROM project_members 
    #                  WHERE user_id IN (SELECT user_id FROM group_members WHERE group_id = ?)))
    # GROUP BY p.id
    # HAVING COUNT(DISTINCT ...) >= 2

def add_group_member(group_id: int, user_id: int) -> None:
    """Record a user as a member of a group (UPSERT)."""
    # INSERT OR IGNORE INTO group_members ...

def get_group_members(group_id: int) -> list[int]:
    """Return all user_ids in a group."""
    # SELECT user_id FROM group_members WHERE group_id = ?

def create_group_project(
    owner_id: int, 
    group_id: int, 
    name: str, 
    description: str = "",
    priority: str = "media"
) -> dict | None:
    """Create a project in a group and auto-add all group members."""
    # 1. create_project(owner_id, name, description, priority)
    # 2. UPDATE projects SET group_id = ? WHERE id = ?
    # 3. Get all group members
    # 4. For each member != owner: add_project_member(project_id, member_id, owner_id)
    # 5. Return project dict
```

#### Funciones modificadas

```python
def create_project(
    user_id: int,
    name: str,
    description: str = "",
    priority: str = "media",
    group_id: int | None = None,  # NUEVO PARÁMETRO
) -> dict | None:
    """Create a project, optionally in a group context."""
    # Lógica existente + INSERT con group_id

def list_projects(user_id: int, status_filter: str | None = None) -> list[dict]:
    """List a user's projects (no change - works for private)."""
    # Sin cambios

def search_projects(user_id: int, query: str, group_id: int | None = None) -> list[dict]:
    """Search projects, optionally scoped to a group."""
    # Si group_id: filtrar por group_id
    # Si no: comportamiento actual
```

#### Función para obtener private_chat_id

```python
def get_private_chat_id(user_id: int) -> int | None:
    """Return the user's private chat ID, or None if never chatted privately."""
    # SELECT private_chat_id FROM users WHERE user_id = ?

def upsert_user(
    user_id: int, 
    username: str | None, 
    first_name: str | None,
    private_chat_id: int | None = None,  # NUEVO PARÁMETRO
) -> None:
    """Record or refresh a Telegram user's profile."""
    # Si private_chat_id no es None, actualizar el campo
```

### 4.2 `bot/handlers.py`

#### Cambios en `handle_message`

```python
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages in private chats AND groups."""
    if not _authorized(update):
        await _deny(update)
        return

    chat = update.effective_chat
    user_id = update.effective_user.id

    # NUEVO: Guardar private_chat_id si es chat privado
    if chat.type == "private":
        upsert_user(user_id, update.effective_user.username, update.effective_user.first_name, private_chat_id=chat.id)

    # NUEVO: Registrar al usuario como miembro del grupo si aplica
    if chat.type in ("group", "supergroup"):
        add_group_member(chat.id, user_id)

    # ... resto de la lógica existente ...
```

#### Nuevo handler `handle_message_group`

```python
async def handle_message_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages in groups — scoped to group projects only."""
    if not _authorized(update):
        return  # Silently ignore unauthorized users in groups

    user_id = update.effective_user.id
    group_id = update.effective_chat.id
    user_text = update.message.text.strip()

    # Registrar al usuario como miembro del grupo
    add_group_member(group_id, user_id)
    upsert_user(user_id, update.effective_user.username, update.effective_user.first_name)

    # Rate limit (mismo sistema)
    if _is_rate_limited(user_id, settings):
        return

    # Construir contexto SOLO con proyectos del grupo
    group_projects = get_shared_projects_in_group(group_id)
    
    # Guardar mensaje en historial (por usuario, no por grupo)
    add_message(user_id, "user", user_text)
    history = get_history(user_id, settings.max_context_messages)

    # Obtener private_chat_id para recordatorios
    private_chat_id = get_private_chat_id(user_id)

    # Inyectar contexto del grupo en el system prompt
    system_prompt = _build_group_system_prompt(settings, group_projects, group_id, private_chat_id)
    
    # Llamar al modelo con tools filtradas para grupo
    reply_text = await _run_ai_tool_loop(
        client=client,
        settings=settings,
        messages=history,
        user_id=user_id,
        model=model,
        context=context,
        chat_id=group_id,
        group_context=group_id,  # NUEVO: indica contexto de grupo
        private_chat_id=private_chat_id,  # NUEVO: para recordatorios
    )

    # Responder públicamente
    if reply_text:
        add_message(user_id, "assistant", reply_text)
        await _reply_markdown_safe(update.message, reply_text)
```

### 4.3 `bot/tools.py`

#### Filtrado de tools por contexto (paridad v1 — ver `specs/group-parity.md`)

```python
# Grupo: paridad casi total (solo 6 privadas)
GROUP_TOOLS = {
    "list_projects", "search_projects", "get_project",
    "create_project", "add_project_note",
    "update_project_status", "update_project_priority", "update_project_description",
    "create_reminder", "list_reminders", "cancel_reminder", "edit_reminder",
    "reschedule_reminder", "postpone_reminder",  # recordatorios compartidos por group_id
    "create_entity", "list_entities", "search_entities", "get_entity_details",
    "add_entity_note", "update_entity", "archive_entity",
    "web_search", "dolar_rate",
    "journal_append", "memory_read", "memory_recall", "memory_insights",
}
PRIVATE_ONLY_TOOLS = {
    "generate_image", "set_checkin_time",  # cuota / proactivo personal
    "share_project", "unshare_project", "delete_project", "list_project_members",
}
```

#### Modificación en `execute_tool`

```python
async def execute_tool(
    name: str, 
    args: dict, 
    user_id: int,
    group_id: int | None = None,
    private_chat_id: int | None = None,  # NUEVO
) -> str:
    """Execute a tool, with group scoping when applicable."""
    
    # NUEVO: Bloquear tools privadas en contexto de grupo
    if group_id is not None and name in PRIVATE_TOOLS:
        return json.dumps({
            "ok": False,
            "error": "Esta acción solo está disponible en chats privados con el bot."
        })
    
    # NUEVO: Para create_reminder en grupo, usar private_chat_id
    if group_id is not None and name == "create_reminder":
        args["chat_id"] = private_chat_id  # Inyectar chat privado
    
    # NUEVO: Para tools de proyecto en grupo, filtrar por group_id
    if group_id is not None and name in GROUP_TOOLS:
        if name in ("list_projects", "search_projects"):
            args["_group_id"] = group_id  # Inyectar scope de grupo
    
    # ... lógica existente ...
```

#### Nueva tool `create_group_project`

```python
{
    "type": "function",
    "function": {
        "name": "create_group_project",
        "description": (
            "Crear un proyecto nuevo en el grupo actual. Los miembros del grupo "
            "se añaden automáticamente como colaboradores. Solo usar cuando el "
            "usuario pide crear un proyecto EN EL GRupo (no en privado)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Nombre del proyecto"},
                "description": {"type": "string", "description": "Descripción del proyecto"},
                "priority": {"type": "string", "enum": ["baja", "media", "alta", "urgente"]},
            },
            "required": ["name"],
        },
    },
}
```

### 4.4 `bot/project_commands.py`

#### Modificar comandos para contexto de grupo

```python
async def proyectos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /proyectos — list projects (private or group scoped)."""
    if not _authorized(update):
        await _deny(update)
        return

    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type in ("group", "supergroup"):
        # En grupo: solo proyectos compartidos en este grupo
        projects = get_shared_projects_in_group(chat.id)
        scope_label = "en este grupo"
    else:
        # En privado: todos los proyectos del usuario
        projects = list_projects(user_id)
        scope_label = "tuyos"

    if not projects:
        await update.message.reply_text(
            f"No tienes proyectos {scope_label}."
        )
        return

    # ... formatear y responder ...
```

### 4.5 `bot/bot.py`

#### Registro de handlers actualizado

```python
def create_application() -> Application:
    # ... existente ...

    # Comandos en privado (como antes)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ayuda", help_command))
    # ...

    # Comandos en grupo (NUEVO - mismos handlers)
    # Los comandos funcionan en ambos contextos por defecto

    # Texto en privado
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_message
        )
    )

    # Texto en grupo (NUEVO)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            handle_message_group
        )
    )

    # Voz y fotos: privado solamente (sin cambios)
    app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.PRIVATE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
```

### 4.6 `bot/config.py`

#### Nuevas variables de entorno

```python
@dataclass(frozen=True)
class Settings:
    # ... existente ...
    
    # NUEVO: Comportamiento en grupos
    group_response_mode: str = field(
        default_factory=lambda: os.environ.get("GROUP_RESPONSE_MODE", "public")
    )  # "public" | "silent" (ignorar no-autorizados)
    
    group_auto_add_members: bool = field(
        default_factory=lambda: _parse_bool(os.environ.get("GROUP_AUTO_ADD_MEMBERS", "true"))
    )  # Auto-añadir miembros al crear proyecto
```

### 4.7 `bot/reminders.py`

#### Modificación en `create_reminder`

```python
def create_reminder(
    user_id: int,
    chat_id: int,  # Ahora puede ser private_chat_id cuando se pide desde grupo
    message: str,
    remind_at: float,
    recurrence: str = "none",
) -> dict:
    """Create a reminder.
    
    When called from a group context, chat_id should be the user's
    private_chat_id (not the group chat_id).
    """
    # Lógica existente - sin cambios necesarios
    # El chat_id ya se inyecta desde execute_tool con el valor correcto
```

---

## 5. Flujo de Datos

### 5.1 Mensaje en grupo: "lista los proyectos"

```
1. Usuario envía "lista los proyectos" en grupo
2. handle_message_group() recibe el update
3. Valida autorización (AUTHORIZED_USERS)
4. Registra usuario como miembro del grupo (group_members)
5. Obtiene proyectos compartidos del grupo (get_shared_projects_in_group)
6. Construye system prompt con contexto del grupo
7. Llama al modelo con historial del usuario + contexto del grupo
8. Modelo genera respuesta
9. Responde públicamente en el grupo
```

### 5.2 Crear proyecto en grupo: "crea un proyecto para arreglar el carro"

```
1. Usuario envía "crea un proyecto para arreglar el carro" en grupo
2. handle_message_group() procesa el mensaje
3. Modelo decide llamar create_group_project
4. create_group_project():
   a. Crea el proyecto con owner_id = usuario
   b. Asigna group_id = chat_id del grupo
   c. Obtiene todos los miembros del grupo
   d. Añade cada miembro como collaborator (project_members)
5. Responde confirmando la creación
```

### 5.3 Recordatorio en grupo: "recuérdame llamar al registro en 3 días"

```
1. Usuario envía "recuérdame llamar al registro en 3 días" en grupo
2. handle_message_group() detecta intención + inyecta group_id
3. Modelo llama create_reminder con group_id
4. execute_tool() crea con group_id (aislado por grupo, entrega en el grupo)
5. Responde: "Ok, te recordaré aquí en el grupo a las..."
6. check_due_reminders entrega en group_id (cualquiera puede gestionarlo)
```

### 5.4 Editar proyecto desde privado

```
1. En chat privado, usuario dice "cambia el estado de Arreglar Carro a en proceso"
2. handle_message() procesa (sin cambios)
3. Modelo llama update_project_status
4. Actualiza el proyecto (independiente de group_id)
5. El cambio se refleja tanto en privado como en grupo
```

---

## 6. Casos de Uso

### CU-01: Listar proyectos en grupo

**Actor**: Usuario autorizado en grupo
**Precondición**: Proyectos compartidos existen
**Flujo**:
1. Usuario escribe "lista los proyectos" o "/proyectos"
2. Bot responde con proyectos compartidos en ese grupo
3. No muestra proyectos privados de ningún miembro

### CU-02: Crear proyecto en grupo

**Actor**: Usuario autorizado en grupo
**Precondición**: Ninguno
**Flujo**:
1. Usuario escribe "crea un proyecto llamado X"
2. Bot crea el proyecto con el usuario como dueño
3. Bot añade automáticamente a todos los miembros del grupo como colaboradores
4. Bot asigna `group_id = chat_id`
5. Bot confirma la creación

### CU-03: Crear recordatorio en grupo

**Actor**: Usuario autorizado en grupo
**Precondición**: Usuario tiene chat privado con el bot
**Flujo**:
1. Usuario escribe "recuérdame algo en 3 días" en grupo
2. Bot crea el recordatorio usando el `private_chat_id` del usuario
3. Bot responde: "Ok, te recordaré por tu chat privado"
4. El recordatorio se entrega en el chat privado del usuario (no en el grupo)

### CU-04: Editar proyecto desde privado

**Actor**: Usuario autorizado
**Precondición**: Proyecto existe (puede ser de grupo)
**Flujo**:
1. En chat privado, usuario pide editar el proyecto
2. Bot permite la edición (ACL normal)
3. Cambios se reflejan en grupo y privado

---

## 7. Restricciones de Privacidad

### 7.1 Reglas estrictas

| Regla | Implementación |
|-------|----------------|
| No exponer proyectos privados en grupo | `get_shared_projects_in_group()` solo retorna proyectos con `group_id` |
| Recordatorios aislados por grupo | `reminders.group_id` (entrega en el grupo, `list_pending_reminders(..., group_id)`) |
| Bitácora aislada por grupo | `memory.db` con `group_id` + `memory/group_-xxx/` (firmada `@usuario`) |
| No mostrar historial de otros usuarios | Historial siempre por `user_id`+`chat_id`, tenant por `group_id` |
| Auto-añadir al crear proyecto de grupo | `create_group_project` añade `group_members` |
| Gestionar recordatorios en grupo | Cualquier miembro puede ver/editar/borrar (shared) |

### 7.2 Escenarios protegidos

1. **Ana pide "muestra los proyectos de Luis"**: Bot responde "Solo puedes ver proyectos que te hayan compartido"
2. **Luis pide "recuérdame algo" en grupo**: Bot crea el recordatorio para Luis (con su `private_chat_id`) y responde "Ok, te recordaré por tu chat privado"
3. **Miembro no autorizado intenta usar el bot**: Bot ignora silenciosamente
4. **Usuario pide "muestra mi historial" en grupo**: Bot responde "El historial es privado"
5. **Usuario pide "cancela mis recordatorios" en grupo**: Bot responde "Esta acción solo está disponible en chats privados con el bot"
5. **Usuario pide "cancela mis recordatorios" en grupo**: Bot responde "Esta acción solo está disponible en chats privados con el bot"

---

## 8. Plan de Implementación

### Fase 1: Base de datos (1-2 horas)

- [ ] Agregar columna `group_id` a tabla `projects`
- [ ] Agregar columna `private_chat_id` a tabla `users`
- [ ] Crear tabla `group_members`
- [ ] Crear índices necesarios
- [ ] Migración idempotente (schema)

### Fase 2: Lógica de proyectos (2-3 horas)

- [ ] Implementar `get_group_projects()`
- [ ] Implementar `get_shared_projects_in_group()`
- [ ] Implementar `add_group_member()` / `get_group_members()`
- [ ] Implementar `create_group_project()`
- [ ] Implementar `get_private_chat_id()`
- [ ] Modificar `create_project()` para aceptar `group_id`
- [ ] Modificar `upsert_user()` para aceptar `private_chat_id`
- [ ] Modificar `search_projects()` para scope de grupo

### Fase 3: Handlers de grupo (2-3 horas)

- [ ] Crear `handle_message_group()`
- [ ] Modificar `handle_message()` para guardar `private_chat_id`
- [ ] Modificar `bot.py` para registrar handlers de grupo
- [ ] Agregar filtros `ChatType.GROUP` / `ChatType.SUPERGROUP`
- [ ] Modificar comandos para contexto de grupo (`/proyectos`, `/proyecto`, etc.)
- [ ] Implementar detección de disolución de grupo (`handle_chat_member`)

### Fase 4: Tools y scope (1-2 horas)

- [ ] Definir `GROUP_TOOLS` y `PRIVATE_TOOLS`
- [ ] Modificar `execute_tool()` para aceptar `group_id`
- [ ] Crear tool `create_group_project`
- [ ] Actualizar `TOOL_DEFINITIONS`

### Fase 5: Configuración (30 min)

- [ ] Agregar `GROUP_RESPONSE_MODE` a `Settings`
- [ ] Agregar `GROUP_AUTO_ADD_MEMBERS` a `Settings`

### Fase 6: Tests (2-3 horas)

- [ ] Tests para `get_shared_projects_in_group()`
- [ ] Tests para `create_group_project()`
- [ ] Tests para `handle_message_group()`
- [ ] Tests de privacidad (recordatorios bloqueados en grupo)
- [ ] Tests de autorización (usuarios no autorizados ignorados)

### Fase 7: Documentación (1 hora)

- [ ] Actualizar `README.md` con sección de grupos
- [ ] Actualizar `architecture.md`
- [ ] Actualizar `AGENTS.md`

**Total estimado: 10-15 horas**

---

## 9. Testing

### 9.1 Tests unitarios

```python
# tests/test_group_projects.py

def test_get_shared_projects_in_group():
    """Proyectos compartidos se filtran por group_id."""
    # Crear proyectos privados y de grupo
    # Verificar que get_shared_projects_in_group solo retorna los de grupo

def test_create_group_project_auto_adds_members():
    """Al crear proyecto en grupo, todos los miembros se añaden."""
    # Crear grupo con 3 miembros
    # Crear proyecto en grupo
    # Verificar que los 3 miembros son project_members

def test_tools_blocked_in_group():
    """Tools privadas no funcionan en contexto de grupo."""
    # Ejecutar list_reminders con group_id
    # Verificar que devuelve error

def test_create_reminder_uses_private_chat_id():
    """create_reminder en grupo usa private_chat_id."""
    # Ejecutar create_reminder con group_id y private_chat_id
    # Verificar que el recordatorio se crea con private_chat_id
```

### 9.2 Tests de integración

```python
# tests/test_group_handlers.py

def test_handle_message_group_scopes_projects():
    """En grupo, solo se ven proyectos del grupo."""
    # Simular mensaje en grupo
    # Verificar que solo se incluyen proyectos del grupo en el contexto

def test_unauthorized_user_ignored_in_group():
    """Usuarios no autorizados son ignorados en grupo."""
    # Simular mensaje de usuario no autorizado
    # Verificar que no se procesa

def test_reminder_in_group_uses_private_chat():
    """Recordatorio en grupo se envía al chat privado."""
    # Simular mensaje "recuérdame algo" en grupo
    # Verificar que el recordatorio se crea con private_chat_id
    # Verificar que la respuesta es "Ok, te recordaré por tu chat privado"
```

### 9.3 Pruebas manuales

1. Crear grupo de prueba con 2-3 usuarios autorizados
2. Probar `/proyectos` en grupo
3. Crear proyecto en grupo con lenguaje natural
4. Verificar que recordatorios se crean correctamente en grupo (con private_chat_id)
5. Verificar que el recordatorio llega al chat privado del usuario
6. Editar proyecto desde privado y verificar cambios en grupo
7. Probar con usuario no autorizado (debe ser ignorado)
8. Probar disolución de grupo (proyectos quedan privados)

---

## 10. Riesgos y Mitigaciones

| Riesgo | Probabilidad | Impacto | Mitigación |
|--------|--------------|---------|------------|
| Filtración de datos entre usuarios | Baja | Alto | Tests de privacidad estrictos, ACL verificada |
| Complejidad en queries SQL | Media | Medio | Queries simples, índices adecuados |
| Retrocompatibilidad rota | Baja | Alto | `group_id` nullable, tests de privado existentes |
| Performance en grupos grandes | Baja | Bajo | Límites en queries, índices |

---

## 11. Decisiones de Diseño

| Decisión | Alternativa descartada | Razón |
|----------|----------------------|-------|
| `group_id` en `projects` | Nueva tabla `group_projects` | Más simple, menos JOINs, retrocompatible |
| `group_members` separada de `project_members` | Reutilizar `project_members` | Diferente semántica: grupo vs proyecto |
| Tools privadas bloqueadas en grupo | Redirigir a privado | Más limpio que enviar al usuario a otro chat |
| Respuestas públicas en grupo | Respuestas privadas | Transparencia en trabajo colaborativo |
| Auto-añadir miembros al crear proyecto | Invitación manual | Simplifica flujos de grupo |

---

## 12. Reglas de Disolución de Grupo

Cuando un grupo es disuelto o el bot es removido del grupo:

1. **Proyectos del grupo quedan privados**: `group_id = NULL` para todos los proyectos del grupo
2. **Dueño conserva el proyecto**: el `user_id` (owner) no cambia
3. **Colaboradores pierden acceso**: las filas en `project_members` se eliminan para esos usuarios
4. **El proyecto sigue siendo editable**: el dueño puede editarlo desde su chat privado
5. **El dueño puede re-compartir**: si el grupo se re-crea o hay un nuevo grupo, puede compartir el proyecto nuevamente

**Implementación**: Detectar la disolución del grupo (evento `MyChatMember` con `status = "kicked"` o `left`) y ejecutar la migración.

```python
async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle bot removal from a group — migrate projects to private."""
    my_member = update.my_chat_member
    if my_member.new_chat_member.status in ("kicked", "left"):
        group_id = my_member.chat.id
        migrate_group_projects_to_private(group_id)
```

```python
def migrate_group_projects_to_private(group_id: int) -> None:
    """Convert group projects to private (owned by creator)."""
    with _get_connection() as conn:
        # 1. Obtener todos los proyectos del grupo
        projects = conn.execute(
            "SELECT id, user_id FROM projects WHERE group_id = ?", (group_id,)
        ).fetchall()
        
        # 2. Para cada proyecto:
        for project in projects:
            # a. Quitar group_id
            conn.execute(
                "UPDATE projects SET group_id = NULL WHERE id = ?", (project["id"],)
            )
            # b. Eliminar colaboradores (ya no tiene sentido)
            conn.execute(
                "DELETE FROM project_members WHERE project_id = ?", (project["id"],)
            )
        
        # 3. Eliminar registros de membresía del grupo
        conn.execute(
            "DELETE FROM group_members WHERE group_id = ?", (group_id,)
        )
        conn.commit()
```

---

## 13. Futuro

- [x] Soporte para `/recordar` en grupo (recordatorios se entregan al chat privado)
- [x] Notificaciones de cambios de proyecto en grupo (estado, prioridad, notas)
- [ ] Permisos granulares por grupo (solo lectura, edición completa)
- [ ] Estadísticas de uso por grupo
