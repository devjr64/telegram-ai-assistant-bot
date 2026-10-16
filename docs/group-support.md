# Group Support — Telegram AI Bot

> **⚠️ Updated 2026-08-22:** This initial plan was **implemented and expanded** — full parity is specified in `specs/group-parity.md` (v1: web_search, dolar_rate, reminders and journal shared per group, delivery in the group). This doc is kept for history; the current reference is the spec.

Implementation plan to enable bot interaction in Telegram groups. **For development only** — the Telegram bot never reads this file (see `AGENTS.md`).

---

## 1. Executive Summary

The bot currently works exclusively in private chats. This plan enables interaction in Telegram groups with the following privacy restrictions:

- **In groups**: projects, reminders, journal/memory, web search and dollar rate — all **shared and isolated by `group_id`** (v1 parity, see `specs/group-parity.md`)
- **In private**: all user information is accessible (same as now), isolated from groups
- **Reminders and journal in groups**: **shared per group**, delivered in the group and manageable by any member; private ones remain isolated
- **Unauthorized members**: cannot use the bot in groups

---

## 2. Requirements

### 2.1 Functional

| ID | Requirement | Priority |
|----|-------------|----------|
| F1 | Bot responds to natural messages in groups | High |
| F2 | In groups, only projects shared among members are shown | High |
| F3 | Group projects have `group_id` (= group's `chat_id`) | High |
| F4 | Creating a project in a group automatically adds all members | High |
| F5 | Group projects can be edited from private chats | High |
| F6 | Group reminders are shared (delivery in the group, visible to all members, isolated by `group_id`) | High |
| F7 | Group journal is shared (`memory/group_-xxx/`, signed `@user`) | High |
| F8 | Only authorized users can use the bot in groups | High |
| F9 | Bot ignores unauthorized members in groups | Medium |
| F10 | Group responses are public | Medium |

### 2.2 Non-functional

| ID | Requirement |
|----|-------------|
| NF1 | Backward compatibility: private chats work the same as before |
| NF2 | Do not expose private user information in groups |
| NF3 | Maintain existing architecture (SQLite + Markdown) |
| NF4 | Tests for new flows |

---

## 3. Database Design

### 3.1 Change in `projects` table

Add `group_id` field (nullable):

```sql
ALTER TABLE projects ADD COLUMN group_id INTEGER;
```

- `group_id = NULL`: private project (current behavior)
- `group_id = chat_id`: project created in or associated with a group

**Index for group queries:**
```sql
CREATE INDEX IF NOT EXISTS idx_projects_group ON projects(group_id);
```

### 3.2 Change in `users` table

Add `private_chat_id` field for reminders from groups:

```sql
ALTER TABLE users ADD COLUMN private_chat_id INTEGER;
```

- Updated when the user interacts in a private chat
- Used to create reminders when the user requests from a group

### 3.3 New `group_members` table

To track which users are members of a group (needed to auto-invite when creating projects):

```sql
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    first_seen_at REAL NOT NULL,
    UNIQUE (group_id, user_id)
);
```

**Index:**
```sql
CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id);
```

### 3.4 ER Diagram

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

## 4. Changes per Module

### 4.1 `bot/projects.py`

#### New functions

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

#### Modified functions

```python
def create_project(
    user_id: int,
    name: str,
    description: str = "",
    priority: str = "media",
    group_id: int | None = None,  # NEW PARAMETER
) -> dict | None:
    """Create a project, optionally in a group context."""
    # Existing logic + INSERT with group_id

def list_projects(user_id: int, status_filter: str | None = None) -> list[dict]:
    """List a user's projects (no change - works for private)."""
    # No changes

def search_projects(user_id: int, query: str, group_id: int | None = None) -> list[dict]:
    """Search projects, optionally scoped to a group."""
    # If group_id: filter by group_id
    # If not: current behavior
```

#### Function to get private_chat_id

```python
def get_private_chat_id(user_id: int) -> int | None:
    """Return the user's private chat ID, or None if never chatted privately."""
    # SELECT private_chat_id FROM users WHERE user_id = ?

def upsert_user(
    user_id: int, 
    username: str | None, 
    first_name: str | None,
    private_chat_id: int | None = None,  # NEW PARAMETER
) -> None:
    """Record or refresh a Telegram user's profile."""
    # If private_chat_id is not None, update the field
```

### 4.2 `bot/handlers.py`

#### Changes in `handle_message`

```python
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages in private chats AND groups."""
    if not _authorized(update):
        await _deny(update)
        return

    chat = update.effective_chat
    user_id = update.effective_user.id

    # NEW: Save private_chat_id if it's a private chat
    if chat.type == "private":
        upsert_user(user_id, update.effective_user.username, update.effective_user.first_name, private_chat_id=chat.id)

    # NEW: Register the user as a group member if applicable
    if chat.type in ("group", "supergroup"):
        add_group_member(chat.id, user_id)

    # ... rest of existing logic ...
```

#### New handler `handle_message_group`

```python
async def handle_message_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages in groups — scoped to group projects only."""
    if not _authorized(update):
        return  # Silently ignore unauthorized users in groups

    user_id = update.effective_user.id
    group_id = update.effective_chat.id
    user_text = update.message.text.strip()

    # Register the user as a group member
    add_group_member(group_id, user_id)
    upsert_user(user_id, update.effective_user.username, update.effective_user.first_name)

    # Rate limit (same system)
    if _is_rate_limited(user_id, settings):
        return

    # Build context ONLY with group projects
    group_projects = get_shared_projects_in_group(group_id)
    
    # Save message in history (per user, not per group)
    add_message(user_id, "user", user_text)
    history = get_history(user_id, settings.max_context_messages)

    # Get private_chat_id for reminders
    private_chat_id = get_private_chat_id(user_id)

    # Inject group context into the system prompt
    system_prompt = _build_group_system_prompt(settings, group_projects, group_id, private_chat_id)
    
    # Call the model with tools filtered for groups
    reply_text = await _run_ai_tool_loop(
        client=client,
        settings=settings,
        messages=history,
        user_id=user_id,
        model=model,
        context=context,
        chat_id=group_id,
        group_context=group_id,  # NEW: indicates group context
        private_chat_id=private_chat_id,  # NEW: for reminders
    )

    # Respond publicly
    if reply_text:
        add_message(user_id, "assistant", reply_text)
        await _reply_markdown_safe(update.message, reply_text)
```

### 4.3 `bot/tools.py`

#### Tool filtering by context (v1 parity — see `specs/group-parity.md`)

```python
# Group: nearly full parity (only 6 private)
GROUP_TOOLS = {
    "list_projects", "search_projects", "get_project",
    "create_project", "add_project_note",
    "update_project_status", "update_project_priority", "update_project_description",
    "create_reminder", "list_reminders", "cancel_reminder", "edit_reminder",
    "reschedule_reminder", "postpone_reminder",  # reminders shared per group_id
    "create_entity", "list_entities", "search_entities", "get_entity_details",
    "add_entity_note", "update_entity", "archive_entity",
    "web_search", "dolar_rate",
    "journal_append", "memory_read", "memory_recall", "memory_insights",
}
PRIVATE_ONLY_TOOLS = {
    "generate_image", "set_checkin_time",  # personal quota / proactive
    "share_project", "unshare_project", "delete_project", "list_project_members",
}
```

#### Modification in `execute_tool`

```python
async def execute_tool(
    name: str, 
    args: dict, 
    user_id: int,
    group_id: int | None = None,
    private_chat_id: int | None = None,  # NEW
) -> str:
    """Execute a tool, with group scoping when applicable."""
    
    # NEW: Block private tools in group context
    if group_id is not None and name in PRIVATE_TOOLS:
        return json.dumps({
            "ok": False,
            "error": "This action is only available in private chats with the bot."
        })
    
    # NEW: For create_reminder in groups, use private_chat_id
    if group_id is not None and name == "create_reminder":
        args["chat_id"] = private_chat_id  # Inject private chat
    
    # NEW: For project tools in groups, filter by group_id
    if group_id is not None and name in GROUP_TOOLS:
        if name in ("list_projects", "search_projects"):
            args["_group_id"] = group_id  # Inject group scope
    
    # ... existing logic ...
```

#### New tool `create_group_project`

```python
{
    "type": "function",
    "function": {
        "name": "create_group_project",
        "description": (
            "Create a new project in the current group. Group members "
            "are automatically added as collaborators. Only use when the "
            "user asks to create a project IN THE GROUP (not in private)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name"},
                "description": {"type": "string", "description": "Project description"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
            },
            "required": ["name"],
        },
    },
}
```

### 4.4 `bot/project_commands.py`

#### Modify commands for group context

```python
async def proyectos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /proyectos — list projects (private or group scoped)."""
    if not _authorized(update):
        await _deny(update)
        return

    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type in ("group", "supergroup"):
        # In group: only shared projects in this group
        projects = get_shared_projects_in_group(chat.id)
        scope_label = "in this group"
    else:
        # In private: all user's projects
        projects = list_projects(user_id)
        scope_label = "yours"

    if not projects:
        await update.message.reply_text(
            f"You don't have projects {scope_label}."
        )
        return

    # ... format and respond ...
```

### 4.5 `bot/bot.py`

#### Updated handler registration

```python
def create_application() -> Application:
    # ... existing ...

    # Commands in private (as before)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ayuda", help_command))
    # ...

    # Commands in group (NEW - same handlers)
    # Commands work in both contexts by default

    # Text in private
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_message
        )
    )

    # Text in group (NEW)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            handle_message_group
        )
    )

    # Voice and photos: private only (no changes)
    app.add_handler(MessageHandler(filters.VOICE & filters.ChatType.PRIVATE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_photo))
```

### 4.6 `bot/config.py`

#### New environment variables

```python
@dataclass(frozen=True)
class Settings:
    # ... existing ...
    
    # NEW: Group behavior
    group_response_mode: str = field(
        default_factory=lambda: os.environ.get("GROUP_RESPONSE_MODE", "public")
    )  # "public" | "silent" (ignore unauthorized)
    
    group_auto_add_members: bool = field(
        default_factory=lambda: _parse_bool(os.environ.get("GROUP_AUTO_ADD_MEMBERS", "true"))
    )  # Auto-add members when creating project
```

### 4.7 `bot/reminders.py`

#### Modification in `create_reminder`

```python
def create_reminder(
    user_id: int,
    chat_id: int,  # Now can be private_chat_id when requested from a group
    message: str,
    remind_at: float,
    recurrence: str = "none",
) -> dict:
    """Create a reminder.
    
    When called from a group context, chat_id should be the user's
    private_chat_id (not the group chat_id).
    """
    # Existing logic - no changes needed
    # chat_id is already injected from execute_tool with the correct value
```

---

## 5. Data Flow

### 5.1 Message in group: "list the projects"

```
1. User sends "list the projects" in a group
2. handle_message_group() receives the update
3. Validates authorization (AUTHORIZED_USERS)
4. Registers user as group member (group_members)
5. Gets shared projects from the group (get_shared_projects_in_group)
6. Builds system prompt with group context
7. Calls the model with user history + group context
8. Model generates response
9. Responds publicly in the group
```

### 5.2 Create project in group: "create a project to fix the car"

```
1. User sends "create a project to fix the car" in a group
2. handle_message_group() processes the message
3. Model decides to call create_group_project
4. create_group_project():
   a. Creates the project with owner_id = user
   b. Sets group_id = group's chat_id
   c. Gets all group members
   d. Adds each member as a collaborator (project_members)
5. Responds confirming the creation
```

### 5.3 Reminder in group: "remind me to call the registry in 3 days"

```
1. User sends "remind me to call the registry in 3 days" in a group
2. handle_message_group() detects intent + injects group_id
3. Model calls create_reminder with group_id
4. execute_tool() creates with group_id (isolated per group, delivered in the group)
5. Responds: "Ok, I'll remind you here in the group at..."
6. check_due_reminders delivers to group_id (anyone can manage it)
```

### 5.4 Edit project from private chat

```
1. In private chat, user says "change the status of Fix Car to in progress"
2. handle_message() processes (no changes)
3. Model calls update_project_status
4. Updates the project (independent of group_id)
5. The change is reflected both in private chat and in the group
```

---

## 6. Use Cases

### UC-01: List projects in group

**Actor**: Authorized user in group
**Precondition**: Shared projects exist
**Flow**:
1. User types "list the projects" or "/proyectos"
2. Bot responds with shared projects in that group
3. Does not show private projects from any member

### UC-02: Create project in group

**Actor**: Authorized user in group
**Precondition**: None
**Flow**:
1. User types "create a project called X"
2. Bot creates the project with the user as owner
3. Bot automatically adds all group members as collaborators
4. Bot sets `group_id = chat_id`
5. Bot confirms the creation

### UC-03: Create reminder in group

**Actor**: Authorized user in group
**Precondition**: User has a private chat with the bot
**Flow**:
1. User types "remind me of something in 3 days" in a group
2. Bot creates the reminder using the user's `private_chat_id`
3. Bot responds: "Ok, I'll remind you in your private chat"
4. The reminder is delivered in the user's private chat (not in the group)

### UC-04: Edit project from private chat

**Actor**: Authorized user
**Precondition**: Project exists (may be a group project)
**Flow**:
1. In private chat, user asks to edit the project
2. Bot allows the edit (normal ACL)
3. Changes are reflected in group and private chat

---

## 7. Privacy Restrictions

### 7.1 Strict rules

| Rule | Implementation |
|------|----------------|
| Do not expose private projects in groups | `get_shared_projects_in_group()` only returns projects with `group_id` |
| Reminders isolated per group | `reminders.group_id` (delivery in the group, `list_pending_reminders(..., group_id)`) |
| Journal isolated per group | `memory.db` with `group_id` + `memory/group_-xxx/` (signed `@user`) |
| Do not show other users' history | History always per `user_id`+`chat_id`, tenant by `group_id` |
| Auto-add when creating group project | `create_group_project` adds `group_members` |
| Manage reminders in group | Any member can view/edit/delete (shared) |

### 7.2 Protected scenarios

1. **Ana asks "show me Luis's projects"**: Bot responds "You can only see projects that have been shared with you"
2. **Luis asks "remind me of something" in a group**: Bot creates the reminder for Luis (with his `private_chat_id`) and responds "Ok, I'll remind you in your private chat"
3. **Unauthorized member tries to use the bot**: Bot silently ignores
4. **User asks "show my history" in a group**: Bot responds "History is private"
5. **User asks "cancel my reminders" in a group**: Bot responds "This action is only available in private chats with the bot"
5. **User asks "cancel my reminders" in a group**: Bot responds "This action is only available in private chats with the bot"

---

## 8. Implementation Plan

### Phase 1: Database (1-2 hours)

- [ ] Add `group_id` column to `projects` table
- [ ] Add `private_chat_id` column to `users` table
- [ ] Create `group_members` table
- [ ] Create necessary indexes
- [ ] Idempotent migration (schema)

### Phase 2: Project logic (2-3 hours)

- [ ] Implement `get_group_projects()`
- [ ] Implement `get_shared_projects_in_group()`
- [ ] Implement `add_group_member()` / `get_group_members()`
- [ ] Implement `create_group_project()`
- [ ] Implement `get_private_chat_id()`
- [ ] Modify `create_project()` to accept `group_id`
- [ ] Modify `upsert_user()` to accept `private_chat_id`
- [ ] Modify `search_projects()` for group scope

### Phase 3: Group handlers (2-3 hours)

- [ ] Create `handle_message_group()`
- [ ] Modify `handle_message()` to save `private_chat_id`
- [ ] Modify `bot.py` to register group handlers
- [ ] Add `ChatType.GROUP` / `ChatType.SUPERGROUP` filters
- [ ] Modify commands for group context (`/proyectos`, `/proyecto`, etc.)
- [ ] Implement group dissolution detection (`handle_chat_member`)

### Phase 4: Tools and scope (1-2 hours)

- [ ] Define `GROUP_TOOLS` and `PRIVATE_TOOLS`
- [ ] Modify `execute_tool()` to accept `group_id`
- [ ] Create `create_group_project` tool
- [ ] Update `TOOL_DEFINITIONS`

### Phase 5: Configuration (30 min)

- [ ] Add `GROUP_RESPONSE_MODE` to `Settings`
- [ ] Add `GROUP_AUTO_ADD_MEMBERS` to `Settings`

### Phase 6: Tests (2-3 hours)

- [ ] Tests for `get_shared_projects_in_group()`
- [ ] Tests for `create_group_project()`
- [ ] Tests for `handle_message_group()`
- [ ] Privacy tests (reminders blocked in groups)
- [ ] Authorization tests (unauthorized users ignored)

### Phase 7: Documentation (1 hour)

- [ ] Update `README.md` with groups section
- [ ] Update `architecture.md`
- [ ] Update `AGENTS.md`

**Estimated total: 10-15 hours**

---

## 9. Testing

### 9.1 Unit tests

```python
# tests/test_group_projects.py

def test_get_shared_projects_in_group():
    """Shared projects are filtered by group_id."""
    # Create private and group projects
    # Verify that get_shared_projects_in_group only returns group ones

def test_create_group_project_auto_adds_members():
    """When creating a project in a group, all members are added."""
    # Create group with 3 members
    # Create project in group
    # Verify that all 3 members are project_members

def test_tools_blocked_in_group():
    """Private tools don't work in group context."""
    # Execute list_reminders with group_id
    # Verify it returns an error

def test_create_reminder_uses_private_chat_id():
    """create_reminder in groups uses private_chat_id."""
    # Execute create_reminder with group_id and private_chat_id
    # Verify the reminder is created with private_chat_id
```

### 9.2 Integration tests

```python
# tests/test_group_handlers.py

def test_handle_message_group_scopes_projects():
    """In groups, only group projects are visible."""
    # Simulate message in group
    # Verify that only group projects are included in the context

def test_unauthorized_user_ignored_in_group():
    """Unauthorized users are ignored in groups."""
    # Simulate message from unauthorized user
    # Verify it is not processed

def test_reminder_in_group_uses_private_chat():
    """Reminder in group is sent to private chat."""
    # Simulate "remind me of something" message in group
    # Verify the reminder is created with private_chat_id
    # Verify the response is "Ok, I'll remind you in your private chat"
```

### 9.3 Manual testing

1. Create a test group with 2-3 authorized users
2. Test `/proyectos` in group
3. Create a project in group using natural language
4. Verify that reminders are created correctly in groups (with private_chat_id)
5. Verify that the reminder is delivered to the user's private chat
6. Edit a project from private chat and verify changes in group
7. Test with an unauthorized user (should be ignored)
8. Test group dissolution (projects become private)

---

## 10. Risks and Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Data leakage between users | Low | High | Strict privacy tests, verified ACL |
| Complexity in SQL queries | Medium | Medium | Simple queries, proper indexes |
| Broken backward compatibility | Low | High | `group_id` nullable, existing private tests |
| Performance in large groups | Low | Low | Query limits, indexes |

---

## 11. Design Decisions

| Decision | Discarded alternative | Reason |
|----------|----------------------|--------|
| `group_id` in `projects` | New `group_projects` table | Simpler, fewer JOINs, backward compatible |
| `group_members` separate from `project_members` | Reuse `project_members` | Different semantics: group vs project |
| Private tools blocked in groups | Redirect to private | Cleaner than sending user to another chat |
| Public responses in groups | Private responses | Transparency in collaborative work |
| Auto-add members when creating project | Manual invitation | Simplifies group flows |

---

## 12. Group Dissolution Rules

When a group is dissolved or the bot is removed from the group:

1. **Group projects become private**: `group_id = NULL` for all group projects
2. **Owner keeps the project**: the `user_id` (owner) does not change
3. **Collaborators lose access**: rows in `project_members` are deleted for those users
4. **The project remains editable**: the owner can edit it from their private chat
5. **The owner can re-share**: if the group is re-created or there is a new group, they can share the project again

**Implementation**: Detect group dissolution (`MyChatMember` event with `status = "kicked"` or `"left"`) and execute the migration.

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
        # 1. Get all group projects
        projects = conn.execute(
            "SELECT id, user_id FROM projects WHERE group_id = ?", (group_id,)
        ).fetchall()
        
        # 2. For each project:
        for project in projects:
            # a. Remove group_id
            conn.execute(
                "UPDATE projects SET group_id = NULL WHERE id = ?", (project["id"],)
            )
            # b. Delete collaborators (no longer meaningful)
            conn.execute(
                "DELETE FROM project_members WHERE project_id = ?", (project["id"],)
            )
        
        # 3. Delete group membership records
        conn.execute(
            "DELETE FROM group_members WHERE group_id = ?", (group_id,)
        )
        conn.commit()
```

---

## 13. Future

- [x] Support for `/recordar` in groups (reminders delivered to private chat)
- [x] Group project change notifications (status, priority, notes)
- [ ] Granular permissions per group (read-only, full edit)
- [ ] Usage statistics per group
