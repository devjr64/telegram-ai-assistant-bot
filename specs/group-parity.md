# Spec: Group Feature Parity

**Status:** Draft — reviewed 2026-08-21 (post-discovery)
**Date:** 2026-08-21
**Objective:** Make Telegram groups have virtually the same features as private chats, while maintaining strict data separation between group and private. Only `generate_image` and `set_checkin_time` remain out of groups in v1.

> **How to read this revision:** this spec was validated against the actual repo (`bot/tools.py`, `bot/reminders.py`, `bot/memory.py`, `bot/history.py`, `bot/projects.py`, `bot/handlers.py`, `bot/bot.py`, `bot/proactive.py`, `bot/auto_journal.py`, `bot/summarizer.py`). Changes from the initial draft are motivated by detected gaps and owner decisions (see §13 Assumptions).

---

## 1. Vision and Objectives

Currently the bot operates with two rigid sets: `GROUP_TOOLS` (12 tools) and `PRIVATE_TOOLS` (blocked in groups). This leaves groups without web search, dollar rate, manageable reminders, and journal/memory — the user has to switch to private chat for basic tasks.

The objective is for a group to be an **autonomous and complete space**: manage projects, reminders, entities, journal/memory, search the web, and check the dollar rate **without leaving the group**.

### Guiding Principle: Strict World Separation (Tenants)

Each `chat_id` is an isolated tenant, with no leakage:

- Private `group_id IS NULL` ↔ only the `user_id` owner.
- Group A `group_id = -100111…` ↔ only `group_members` of that group.
- Group B `group_id = -100222…` ↔ same, isolated from A and from private.

Rules:

- A reminder created in a group → delivered **in the group** and **belongs to the group** (any member can view/edit/delete).
- A journal entry created in a group → belongs to the **shared group journal** (`memory/group_-xxx/`).
- Memory queried in a group → is the **group memory**.
- Private chat reminders and journal remain intact, never appear in group and vice versa.
- `chat_history` is already isolated by `chat_id` (`bot/history.py` uses `WHERE user_id=? AND chat_id=?`) — no changes required, just don't regress.

### Deliberate Exclusion in v1

- `generate_image` remains **private only** (Gemini quota, no collaborative purpose).
- `set_checkin_time` remains **private only** (check-ins are personal; group proactive uses global schedules, not per-user in group).
- `share_project`, `unshare_project`, `delete_project`, `list_project_members` **remain private only in v1**: in groups, projects already auto-add all `group_members` when created, so sharing to invite outside the group is confusing and `delete` is destructive. Re-evaluated in v2 if needed.

---

## 2. Current State (AS-IS) — Grounded

### 2.1 Tools by Environment (Actual Repo)

Defined in `bot/tools.py:95-133`:

```
GROUP_TOOLS = {                          PRIVATE_TOOLS (blocked in group) = {
  create_project,                          list_reminders, cancel_reminder,
  update_project_status/priority/           edit_reminder, reschedule_reminder,
    description,                            postpone_reminder,
  add_project_note,                         share_project, unshare_project,
  list_projects, search_projects,          list_project_members, delete_project,
  get_project,                              web_search, dolar_rate,
  create_reminder,        # → today delivers  generate_image,
  create_entity, list_entities,            journal_append, memory_read,
  search_entities, get_entity_details,      memory_recall, memory_insights,
  add_entity_note, update_entity,           set_checkin_time
  archive_entity,
}
```

`_PROJECT_TOOLS` in `execute_tool` includes `share/unshare/list_members/delete` but the gate `if group_id is not None and name in PRIVATE_TOOLS` still blocks them — the previous spec that said "move them to GROUP_TOOLS" has not been applied yet.

### 2.2 How the Restriction is Applied

`execute_tool(name, args, user_id, group_id=None, private_chat_id=None)` in `bot/tools.py:1779`:

- If `group_id is not None and name in PRIVATE_TOOLS` → error `"This action is only available in private chats with the bot."`
- If `group_id` and tool is project-related → injects `args["_group_id"] = group_id` for `projects.py`.
- `create_reminder` in group → uses `private_chat_id` for private delivery (behavior **to be replaced**).

### 2.3 Current UX Problem

`handlers.py:_run_ai_tool_loop` always sends the full `tools=TOOL_DEFINITIONS` to the model, **without filtering**. In a group the model "sees" `web_search`/`dolar_rate` as available, tries them and receives the blocking error → confusing responses + wasted tokens.

### 2.4 Reminders: Forced Private Delivery (To Be Fixed)

- `_handle_create_reminder` (`tools.py:1833`) uses `args["_chat_id"]` (injected from `private_chat_id`) or `get_chat_for_user(user_id)`.
- `check_due_reminders` delivers to `reminder["chat_id"]`.
- `/recordar` command in group (`handlers.py:1233 recordar_command_group`) requires `private_chat_id` and responds "I'll remind you via your private chat".
- **No shared group management exists**: `list/cancel/edit/reschedule/postpone` are in `PRIVATE_TOOLS` and all do `WHERE user_id = ?` (owner-gate), not `WHERE group_id = ?`.

### 2.5 Memory/Journal: Strictly Per User (To Be Fixed)

- `memory.py` has no `group_id`. Actual schema:
  - `days (user_id, day, raw_path, ...)` — `PRIMARY KEY (user_id, day)`
  - `summaries (user_id, level, period, ...)` — `PRIMARY KEY (user_id, level, period)`
- Files in `memory/{user_id}/` (raw/, days/, summaries/).
- All 4 memory tools are in `PRIVATE_TOOLS`.
- **Critical gap:** `ALTER TABLE ADD COLUMN group_id` without touching the PK collides (same `user_id, day` private and group would have the same PK). Tables must be **recreated** with a new PK.

### 2.6 Chat History: Already Correct

`bot/history.py` uses `chat_history(user_id, chat_id, role, content, created_at)` with index `idx_chat_user(user_id, chat_id, created_at)` and migration `chat_id = user_id` for legacy rows. `add_message(..., chat_id)` and `get_history(..., chat_id)` already isolate by chat. **No changes**, just maintain `chat_id = group_id` in `handle_message_group`.

### 2.7 Entities: Already Enabled in Groups (No Changes)

- `entities.py` is per `user_id`, without `group_id`. Already in `GROUP_TOOLS` since `2026-08-20-enable-entities-in-groups`.
- Decision confirmed: they remain per-user, manageable from group or private. There are no "group entities".

### 2.8 web_search and dolar_rate

- Trivial handlers (`tools.py:1921, 1930`) with no `user_id` dependency.
- They are in `PRIVATE_TOOLS` only due to historical restriction — these are quick wins without schema changes.

### 2.9 Proactive and auto_journal: Private Only Today

- `proactive.py:run_checkins` iterates `authorized_users`, reads `reminders.get_chat_for_user(user_id)` and `memory_store.get_* (user_id)`.
- `auto_journal.py:auto_journal_job` scans `chat_history WHERE user_id=? AND chat_id=user_id` (private only) every 30 min.
- `summarizer.py` generates compacts/summaries per `user_id`.
- For parity, they must also scan `group_id`s.

---

## 3. Target Data Model (TO-BE) — Revised

### 3.1 Group-Shared Reminders

**Owner decision (1+4):** the reminder **belongs to the group**, any member can view/edit/delete.

Table `reminders` (in `projects.db`): add `group_id` + `created_by` (audit), keep `chat_id` as delivery destination.

```sql
-- Idempotent migration (see §7.1)
ALTER TABLE reminders ADD COLUMN group_id INTEGER;      -- NULL = private, -100xxx = group
ALTER TABLE reminders ADD COLUMN created_by INTEGER;    -- audit, nullable for legacy
CREATE INDEX IF NOT EXISTS idx_reminders_group ON reminders(group_id, sent);
CREATE INDEX IF NOT EXISTS idx_reminders_due_group ON reminders(sent, remind_at, group_id);
```

Semantics:

- `group_id IS NULL` → private, `chat_id` = `private_chat_id` (as today).
- `group_id = <group_chat_id>` → group, `chat_id = group_id`, delivery in the group. `created_by = caller` is informational only.

Functions in `bot/reminders.py` with scope:

- `create_reminder(user_id, chat_id, message, remind_at, recurrence, group_id=None, created_by=None)` — if `group_id` is not None, validates that `caller IN group_members`.
- `list_pending_reminders(user_id, group_id=None)` → private: `WHERE group_id IS NULL AND user_id=? AND sent=0`; group: `WHERE group_id=? AND sent=0` (no filtering by `user_id`, ordered by `remind_at`). Only group members can call it (gate in `tools.py`).
- `delete_reminder(reminder_id, user_id, group_id=None)` / `update_reminder_text/time`, `postpone_reminder_db` → in group validates `EXISTS (SELECT 1 FROM group_members WHERE group_id=? AND user_id=caller)` instead of `reminder.user_id == caller`. In private maintains `WHERE id=? AND user_id=?`.
- `get_reminder_by_id` → variant `get_reminder_by_id_for_scope(reminder_id, caller, group_id)` which in group does not filter by `user_id`.
- **Delivery:** `check_due_reminders` unchanged — already delivers to `reminder.chat_id`; with `chat_id=group_id` it fires in the group.
- **`/recordar` command in group:** no longer requires `private_chat_id`; creates with `chat_id=group_id, group_id=group_id, created_by=user_id`; responds "Ok, I'll remind you here in the group 👍".
- **No historical migration:** existing data stays `group_id IS NULL` (private). New data from deployment onwards is group data.

`projects.db` already exists; the migration is idempotent via `PRAGMA table_info`.

### 3.2 Group-Shared Journal/Memory

**Owner decision (2+5):** memory **shared by group**, isolated from private. Any member can add/read.

**PK problem:** `ADD COLUMN` is not enough. Tables must be recreated so `group_id` is part of uniqueness.

Adopted Option A (reuse tables with constraint, without duplicating tables):

```sql
-- Idempotent recreation (see §7.1 for script with fallback)
-- days: one row is (user_id, day) if private, or (group_id, day) if group
CREATE TABLE IF NOT EXISTS days_new (
    user_id INTEGER,                -- NULL if group row
    group_id INTEGER,               -- NULL if private row
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
    -- In SQLite this is implemented as UNIQUE(day, user_id, group_id) + CHECK; the actual
    -- composite PK is resolved in migration with CREATE TABLE ... UNIQUE(user_id, group_id, day)
);
-- Practical equivalent in migration:
-- UNIQUE(user_id, group_id, day) where one of the two is NULL; SQLite treats NULLs as distinct,
-- so CHECK is added and UNIQUE(user_id, day) WHERE group_id IS NULL is enforced
-- and UNIQUE(group_id, day) WHERE group_id IS NOT NULL via partial unique indexes.

CREATE UNIQUE INDEX IF NOT EXISTS ux_days_private ON days(user_id, day) WHERE group_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_days_group   ON days(group_id, day) WHERE group_id IS NOT NULL;

-- summaries: same approach
CREATE UNIQUE INDEX IF NOT EXISTS ux_summaries_private ON summaries(user_id, level, period) WHERE group_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_summaries_group   ON summaries(group_id, level, period) WHERE group_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_days_owner ON days(group_id, user_id, day);
CREATE INDEX IF NOT EXISTS idx_summaries_owner ON summaries(group_id, user_id, level, range_end);
```

If the team prefers **separate tables** (`group_days`, `group_summaries`), it's equivalent in isolation but duplicates queries in `summarizer.py` — left as an alternative if recreation is considered too invasive. The spec recommends **a single table with partial indexes** to avoid forking `memory.py`.

**File paths:** `memory/{owner}/` where owner is:
- `{user_id}` for private → `memory/12345/raw/2026-08-21.md`
- `group_{group_id}` for group → `memory/group_-1001234567890/raw/2026-08-21.md`

> The `group_` prefix makes the domain explicit in FS and logs, even though `group_id` is already negative.

**Guards:** new `_group_memory_dir(group_id)` + `_safe_under_memory_group(group_id, relative)` which validates `is_relative_to(MEMORY_DIR)` and `is_relative_to(group_root)` (analogous to `_safe_under_memory` for `user_id`). `_ensure_user_layout` is generalized to `_ensure_owner_layout(user_id=None, group_id=None)`.

**`memory.py` functions:** take optional `group_id`, exclusive with `user_id`:

- `append_raw_entry(user_id=None, group_id=None, day, text, ts=None, tz_name="UTC", author_username=None)`
- `get_raw_day(...)`, `get_raw_entries(...)`, `save_daily_compact(...)`, `get_summary(...)`, `save_summary(...)`, `list_summaries(...)` — all with branch `WHERE group_id IS NULL` vs `WHERE group_id = ?`.
- In group the text is saved as `- [HH:MM] @user: text` for traceability (decision 5b). In private the format `- [HH:MM] text` is maintained.

**Backward compat:** existing rows stay `group_id IS NULL` (private). No data is migrated to groups.

### 3.3 Summaries and Proactivity in Groups

**Owner decision (7):** yes, `auto_journal` and summaries for groups, with shared memory.

- `auto_journal_job` every 30 min: in addition to iterating `authorized_users` with `chat_id=user_id`, iterates `SELECT DISTINCT group_id FROM group_members` and for each `group_id` does `_get_recent_messages(group_id, group_id)` → extracts events/entities → `_save_events_to_journal(group_id=...)` / `_save_entities` (entities remain per `user_id` of the author, not duplicated to group).
- `summarizer.py`: `generate_daily_compact`, `generate_weekly/quarterly/yearly_summary`, `ensure_summary` accept `group_id` (owner). `collect_period_sources(group_id=...)` reads from `memory/group_-xxx/`.
- `proactive.py`: new jobs `run_group_summaries` (or extend existing ones) that iterate groups; `run_checkins` **does not** apply to groups (check-ins are personal by definition — maintained). Group summaries are not proactively sent to the group in v1 (they remain available via `memory_read/recall`); optional sending to `group_id` is left for v2 if desired to notify "weekly group summary ready".
- **Cost:** 1 model call per group every 30 min (auto_journal) + 1 call per week/quarter/year with content. Scaling with N groups increases cost linearly — documented in §9.

### 3.4 Entities: No Changes (Confirmed)

They remain per `user_id`. Already work in groups. Not modeled as "group entities".

---

## 4. User Stories

> **UX Principle (AGENTS.md):** primary path is **natural chat**, not commands. Commands are for testing/security. The bot ALWAYS executes a tool.

### EP-01: Web Search and Dollar in Group

**US-01.1 — Search the web from a group**
> "search how to prune a lemon tree" → `web_search`, responds in the group with summary + sources.

**US-01.2 — Check the dollar rate from a group**
> "what's the dollar rate" → `dolar_rate`, responds in the group (BCV + Binance).

### EP-02: Shared Group Reminders

**US-02.1 — Create via natural language (primary path)**
> "remind everyone that tomorrow there's a meeting at 4pm" → `create_reminder` with `group_id` as `chat_id`; confirms "here in the group".

**US-02.2 — Create via command (secondary)**
> `/recordar pay the electricity bill on the 28th` in group → `chat_id=group_id, group_id=group_id`.

**US-02.3 — List group reminders (shared)**
> "what reminders do we have pending" or `/recordatorios` in group → `list_pending_reminders(group_id=X)` returns **all group reminders**, not just the caller's; Cancel/Postpone buttons work on any of them (any member can act).

**US-02.4 — Shared cancel/edit**
> "cancel the meeting one" / "postpone it to Friday" → `cancel/edit/reschedule/postpone` with current `group_id`; validate group membership, not ownership.

### EP-03: Shared Journal/Memory

**US-03.1 — Add a group entry**
> "note in the journal: today we finished the trip budget" → `journal_append` in group writes `- [HH:MM] @user: today we finished...` in `memory/group_-xxx/`.

**US-03.2 — Query group memory**
> "when was the last planning meeting?" → `memory_recall/read/insights` with `group_id` search only in group memory.

**US-03.3 — Guaranteed separation**
> Private `IS NULL` vs group `=X`; no query mixes scopes. `chat_history` already isolates by `chat_id`.

### EP-04: Entities (Already Available)

**US-04.1 — User entities from group**
> Creating a `case/person` from a group remains associated with me, visible in group and in private.

### EP-05: Projects (Already Available, Not Expanded in v1)

**US-05.1 — Group project management**
> Create/list/update/note already work with `group_id`. `delete/share/unshare/list_members` are **not** enabled in groups in v1 (used in private).

---

## 5. Functions by Environment (Target v1 Matrix)

| Category | Tool | Private | Group | Notes |
|----------|------|:-------:|:-----:|-------|
| Projects | create_project | ✅ | ✅ | Already |
| | update_project_status/priority/description | ✅ | ✅ | Already |
| | add_project_note | ✅ | ✅ | Already |
| | list_projects | ✅ | ✅ | Already (group scope) |
| | search_projects | ✅ | ✅ | Already |
| | get_project | ✅ | ✅ | Already |
| | delete_project | ✅ | ❌ | **Remains private in v1** |
| | share_project | ✅ | ❌ | Remains private |
| | unshare_project | ✅ | ❌ | Remains private |
| | list_project_members | ✅ | ❌ | Remains private |
| Reminders | create_reminder | ✅ (priv) | ✅ (group) | Delivers in group, shared |
| | list_reminders | ✅ | ✅ | **Move** + `group_id`, shared |
| | cancel_reminder | ✅ | ✅ | **Move** + group ACL |
| | edit_reminder | ✅ | ✅ | **Move** + group ACL |
| | reschedule_reminder | ✅ | ✅ | **Move** + group ACL |
| | postpone_reminder | ✅ | ✅ | **Move** + group ACL |
| Entities | create_entity, list_entities, … | ✅ | ✅ | Already (per user) |
| Journal | journal_append | ✅ | ✅ | **Move** + `group_id`, with author |
| Memory | memory_read | ✅ | ✅ | **Move** + `group_id` |
| | memory_recall | ✅ | ✅ | **Move** + `group_id` |
| | memory_insights | ✅ | ✅ | **Move** + `group_id` |
| | set_checkin_time | ✅ | ❌ | Remains private |
| Web | web_search | ✅ | ✅ | **Move** |
| | dolar_rate | ✅ | ✅ | **Move** |
| Images | generate_image | ✅ | ❌ | Remains private |
| History | chat_history | ✅ | ✅ | Already isolated by `chat_id` |
| Proactive | check-ins | ✅ | ❌ | Personal |
| | auto_journal + summaries | ✅ | ✅ | **New for groups** |

**Resulting sets (curated whitelist, not blind blacklist):**
```python
PRIVATE_ONLY_TOOLS = {"generate_image", "set_checkin_time",
                      "share_project", "unshare_project", "delete_project", "list_project_members"}
GROUP_TOOLS = ALL_TOOL_NAMES - PRIVATE_ONLY_TOOLS  # in practice, explicit subset from above
```
In `execute_tool`, `PRIVATE_ONLY_TOOLS` is blocked when `group_id is not None`; the rest is allowed. If in v2 you want to enable `share/delete` in groups, it's removing them from `PRIVATE_ONLY_TOOLS`.

---

## 6. UX Improvements: Definition Filtering by Environment

**Problem:** `handlers.py:_run_ai_tool_loop` sends the full `TOOL_DEFINITIONS` even in groups → the model tries `web_search` and fails.

**Solution:** filter before calling the model.

```python
def _filter_tool_definitions(group_id: int | None) -> list[dict]:
    if group_id is None:
        return TOOL_DEFINITIONS
    blocked = PRIVATE_ONLY_TOOLS
    return [d for d in TOOL_DEFINITIONS if d["function"]["name"] not in blocked]

# In _run_ai_tool_loop, before chat_completion_with_tools:
tools_for_model = _filter_tool_definitions(group_id)
result = await chat_completion_with_tools(..., tools=tools_for_model)
```

Zero wasted tokens, zero failed attempts.

### 6.1 Updated Group System Prompt

`--- GROUP CONTEXT ---` block in `handlers.py`:

- Reminders you create here will be delivered **in this group** and are **shared** (any member can manage them).
- The journal and memory you use here are **group's** (`group_-xxx`), shared and isolated from your private. Each entry is signed `@user`.
- Entities are personal (yours), manageable from here or from private.
- Web search and dollar rate work here; images only in private.

---

## 7. Technical Architecture

### 7.1 Schema Migration

Idempotent, protected by `PRAGMA table_info` and `CREATE ... IF NOT EXISTS`.

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

**`memory.db` — `days` and `summaries` (recreation):**
```python
# Detect if group_id is missing
cols_days = {r["name"] for r in conn.execute("PRAGMA table_info(days)")}
if "group_id" not in cols_days:
    # 1. Rename old table
    conn.execute("ALTER TABLE days RENAME TO days_old")
    # 2. Create new one with group_id + partial indexes
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
    # 3. Copy existing data (all private)
    conn.execute("""
        INSERT INTO days (user_id, group_id, day, raw_path, raw_word_count, entry_count,
                          compact_path, compact_word_count, range_start, range_end, created_at, updated_at)
        SELECT user_id, NULL, day, raw_path, raw_word_count, entry_count,
               compact_path, compact_word_count, range_start, range_end, created_at, updated_at
        FROM days_old
    """)
    conn.execute("DROP TABLE days_old")
# Same for summaries with ux_summaries_private/group + idx_summaries_owner
```

**Backward compat:** existing rows → `group_id IS NULL` (private). Code that filters `IS NULL` still sees them.

### 7.2 Changes by Module

#### `bot/tools.py`

1. Define `PRIVATE_ONLY_TOOLS = {"generate_image","set_checkin_time","share_project","unshare_project","delete_project","list_project_members"}` and helper `_filter_tool_definitions`.
2. `execute_tool`: `if group_id is not None and name in PRIVATE_ONLY_TOOLS` → error. For `create_reminder` in group inject `args["_chat_id"]=group_id`, `args["_group_id"]=group_id`, `args["_author_username"]=resolve_username(user_id)`; for `journal_append`/`memory_*` inject `args["_group_id"]`; for projects inject `args["_group_id"]` as today.
3. Reminder/memory handlers read `args["_group_id"]` and pass it to `reminders.py`/`memory.py` with group membership validation.
4. `_dispatch` without signature change, but handlers use `_group_id` and `_author_username` when applicable.

#### `bot/reminders.py`

- Signatures: `create_reminder(user_id, chat_id, message, remind_at, recurrence="none", group_id=None, created_by=None)`, `list_pending_reminders(caller_id, group_id=None)`, `delete_reminder(reminder_id, caller_id, group_id=None)`, `update_reminder_text/time`, `postpone_reminder_db`, `get_reminder_by_id_for_scope`.
- Branches: `group_id is None` → `WHERE group_id IS NULL AND user_id=?`; `group_id is not None` → `WHERE group_id=?` + `AND EXISTS (SELECT 1 FROM group_members WHERE group_id=? AND user_id=caller)`.
- Helpers: `cancel_group_reminders(group_id)` → `DELETE FROM reminders WHERE group_id=?` (deletion, see §7.5), `list_group_reminders(group_id)`.
- New indexes already created in migration.

#### `bot/memory.py`

- New helpers `_group_memory_dir(group_id)`, `_safe_under_memory_group`, `_ensure_owner_layout(user_id=None, group_id=None)`, `_owner_dir(user_id, group_id)`.
- CRUD with `group_id`: `append_raw_entry(..., group_id=None, author_username=None)` writes to `memory/group_-xxx/raw/YYYY-MM-DD.md` with `- [HH:MM] @user: text` when `group_id` is present; otherwise maintains private format.
- `get_raw_day`, `get_raw_entries`, `get_summary`, `save_summary`, `list_summaries` with filtering `IS NULL` vs `= ?` and corresponding paths.
- `count_words`, `_validate_day`, `_validate_summary_period` unchanged.

#### `bot/handlers.py`

- `_run_ai_tool_loop`: filter `TOOL_DEFINITIONS` via `_filter_tool_definitions(group_id)` and prompt §6.1.
- `recordar_command_group`: no longer requires `private_chat_id`; creates `chat_id=group_id, group_id=group_id`; responds "I'll remind you here in the group".
- `recordatorios_command`: detects `chat.type in ("group","supergroup")` → lists by `group_id`, otherwise by `user_id`.
- `reminder_callback` (buttons `remind_cancel/postpone`): resolves `group_id = query.message.chat.id if chat.type != "private" else None` and validates membership before `delete/postpone`.
- `handle_message_group`: already registers `add_group_member`; maintains `add_message(... chat_id=group_id)` and `get_history(..., chat_id=group_id)`.

#### `bot/proactive.py` + `bot/auto_journal.py` + `bot/summarizer.py`

- `auto_journal_job`: iterates `authorized_users` for private + `SELECT DISTINCT group_id FROM group_members` for groups; for each group uses `chat_id=group_id` and `group_id` as owner.
- `summarizer.collect_period_sources(group_id=...)` and `generate_*` with `group_id` param; `ensure_summary` same.
- `proactive.run_checkins` remains private only; `run_group_summaries` is added or existing jobs are extended to iterate groups (no proactive sending to group in v1).

#### `bot/bot.py`

- Register `recordatorios_command` with group filter (or have the handler detect `chat.type` internally); `reminder_callback` already covers both scopes.
- `create_application` registers additional `auto_journal` jobs for groups if separated.

### 7.3 Updated AI Flow

1. Message in group → `handle_message_group` registers `group_members`, saves to `chat_history` with `chat_id=group_id`, filters tools.
2. Model receives only group-allowed tools.
3. Reminder/memory tool executes with `group_id` → operates on shared scope.
4. Response is published in the group and saved to `chat_history` with `chat_id=group_id`.

### 7.4 Security

- `user_id` and `group_id` always from `update.effective_user.id` / `update.effective_chat.id`, never from model args.
- Isolation: `group_id IS NULL` ↔ private, `group_id = X` ↔ group X. Partial indexes guarantee uniqueness.
- Rate limiting by `user_id` (3s) is maintained; group flood is mitigated by the same gate. Reminder delivery is best-effort.
- Membership validation: every group operation does `EXISTS (SELECT 1 FROM group_members WHERE group_id=? AND user_id=caller)` before reading/writing.

### 7.5 Group Dissolution (Cleanup) — Total Deletion

**Owner decision (8):** when the bot is removed (`handle_chat_member` with `status in ("kicked","left")`) **everything** from the group is deleted to avoid retaining data without permission.

```python
def cleanup_group_data(group_id: int):
    with get_projects_conn() as conn:
        conn.execute("DELETE FROM reminders WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM projects WHERE group_id=?", (group_id,))  # or migrate to private then delete dirs per policy
        conn.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
    with get_memory_conn() as conn:
        conn.execute("DELETE FROM days WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM summaries WHERE group_id=?", (group_id,))
    with get_history_conn() as conn:
        conn.execute("DELETE FROM chat_history WHERE chat_id=?", (group_id,))
    shutil.rmtree(MEMORY_DIR / f"group_{group_id}", ignore_errors=True)
    # projects dirs: if choosing to delete, also rmtree each project dir of the group
```

Note: for projects, today `migrate_group_projects_to_private` does `UPDATE group_id=NULL` and `DELETE FROM project_members` + `rmtree` of the dir. With the new total deletion policy, that `UPDATE` is replaced by `DELETE`. If you want to preserve group projects as private owner projects, you'd maintain the `UPDATE`; the current decision is **delete**.

---

## 8. Testing Strategy

### 8.1 Schema and Migration

- Idempotent `reminders` migration (2 runs don't duplicate columns/indexes); legacy rows stay `group_id IS NULL`.
- `memory` migration recreates tables without losing private rows; partial indexes `ux_*` exist; running 2x doesn't lose data.

### 8.2 Shared Reminders

- `create_reminder(..., group_id=-1001)` → `chat_id=-1001, group_id=-1001, created_by=caller`.
- `list_pending_reminders(caller, group_id=-1001)` → only `group_id=-1001`, includes those created by other members.
- `list_pending_reminders(caller, group_id=None)` → only private `IS NULL`.
- `cancel_reminder(id, caller_A, group_id=-1001)` where `id` was created by B → **ok** if A is a group member; if A is not a member → rejection.
- `check_due_reminders` delivers to `chat_id=-1001` (group).

### 8.3 Shared Memory

- `append_raw_entry(group_id=-1001, author_username="pepe")` → `memory/group_-1001/raw/YYYY-MM-DD.md` with `- [HH:MM] @pepe: text`.
- `recall` with `group_id` only searches in `memory/group_-1001/`; private doesn't leak.
- Paths don't collide: `memory/123` vs `memory/group_-1001`.

### 8.4 Tools by Environment

- `execute_tool("web_search", ..., group_id=-1001)` → ok.
- `execute_tool("dolar_rate", ..., group_id=-1001)` → ok.
- `execute_tool("journal_append", ..., group_id=-1001)` → ok (writes to group).
- `execute_tool("generate_image", ..., group_id=-1001)` → private error.
- `execute_tool("share_project", ..., group_id=-1001)` → private error (out in v1).
- `tests/test_group_support.py::TestGroupToolsScope` updated.

### 8.5 Handlers

- `/recordar` in group creates with `chat_id=group_id`.
- `/recordatorios` in group lists only from the group, any member sees all.
- `remind_cancel/postpone` buttons with group scope validate membership.
- `TOOL_DEFINITIONS` filtered in group excludes `PRIVATE_ONLY_TOOLS`.

### 8.6 Coverage

- `pytest --cov=bot --cov-fail-under=80` must pass; add tests for everything migrated + private regression.

---

## 9. Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Private→group leakage | **Critical** | `IS NULL` vs `= ?` + `EXISTS group_members` + isolation tests. |
| Model cost × N groups (`auto_journal` 1 call/30min/group + summaries) | Medium-High | Phase: v1 activates `auto_journal` for groups but with `LOOKBACK_HOURS` and dedup; monitor `TELEGRAM_BOT` usage; allow `MEMORY_ENABLED` off per group if needed. |
| Irreversible deletion when leaving group | Medium | This is the requested decision (8); log `cleanup_group_data` + document that there's no recovery without backup. |
| PK collision when migrating `memory.db` | High | Migration with `days_old` + partial indexes, tested in `tmp_path` with legacy rows. |
| Confusion "is this from the group or private?" | Medium | Prompt §6.1 + "here in the group" confirmations + `@user` signature. |
| Rate limit in active groups | Low | Gate by `user_id` (3s) already exists; flood from N different members is not blocked — accepted. |
| Group memory contamination with private | Medium | `journal_append` in group always uses `group_id`, never `user_id`. |

---

## 10. Implementation Phases

### Phase 1 — Quick Wins: `web_search` and `dolar_rate` in Group
**No schema.** Move from `PRIVATE_TOOLS` → `GROUP_TOOLS`, filter `TOOL_DEFINITIONS`, update prompt §6.1, tests. **Deliverable is isolatable.**

### Phase 2 — Shared Reminders
`reminders` migration (`group_id`, `created_by`, indexes), rewrite `reminders.py` with group ACL (any-member), `tools.py` `group_id` injection, `handlers.py` commands+callbacks with scope, `check_due_reminders` already delivers in group, `cleanup_group_data` for reminders. Tests §8.2.

### Phase 3 — Shared Journal/Memory
**Most invasive.** `memory.db` migration with recreation + partial indexes, `_owner_dir`/`_safe_under_memory_group`, `append_raw_entry` with author, `tools`/`handlers` for `journal_append` + `memory_*` with `group_id`, `auto_journal` + `summarizer`/`proactive` iterating groups. Tests §8.3. **Blocks Phase 4.**

### Phase 4 — Polish and Docs
Update `AGENTS.md` (groups), `docs/group-support.md`, document ownership, review `SYSTEM_PROMPT` only if it mentions obsolete restrictions (without putting development docs in the prompt).

> **Recommended order:** Phase 1 can go to prod alone. Phase 2 and 3 are dependent (Phase 3 assumes `group_id` plumbing from Phase 2). Do not enable `share/delete` in groups in v1.

---

## 11. Acceptance Criteria

- [ ] In group, `web_search` and `dolar_rate` work and respond in the group.
- [ ] In group, `/recordar` creates a reminder that fires **in the group** and is visible/editable by any member.
- [ ] In group, `/recordatorios` lists **all** reminders for the `group_id` (not just mine) with buttons that any member can act on.
- [ ] In group, `journal_append` writes `- [HH:MM] @user: text` in shared `memory/group_-xxx/`.
- [ ] In group, `memory_read/recall/insights` search only in group memory.
- [ ] My private reminders/journal don't appear in any group; group A's don't appear in group B or in private.
- [ ] `generate_image` and `set_checkin_time` remain private only; `share/unshare/delete/list_members` also remain private in v1.
- [ ] The model does not receive `PRIVATE_ONLY_TOOLS` in group context.
- [ ] `pytest --cov=bot --cov-fail-under=80` passes.
- [ ] When removing the bot from the group, `reminders`, `days/summaries`, `chat_history`, and `memory/group_-xxx/` for that `group_id` are deleted.
- [ ] `auto_journal` generates entries for active groups.

---

## 12. Out of Scope

- **Proactive check-ins in groups** (morning/evening) — personal, don't apply.
- **Group entities** — remain per user.
- **Push notification of group summary to the group** — the summary remains available via `memory_read/recall`; push is left for v2.
- **Historical migration** — old reminders/journal entries are not converted to groups.
- **Project share/delete in group** — out of v1 (see §5).

---

## 13. Assumptions and Decisions Made (For Traceability)

- **Shared reminders (any-member ACL):** `group_id` is the gate, not `user_id`. `created_by` is audit only.
- **Shared memory with new PK:** it is assumed that recreating `days`/`summaries` with nullable `group_id` + partial unique indexes is acceptable (requires migration window with backup). Separate tables alternative is documented but not adopted.
- **Author in group journal:** format `- [HH:MM] @username: text` (if no username, `first_name` or `user_id`).
- **Cleanup = total deletion:** `chat_history`, `reminders`, `days/summaries`, `memory/group_-xxx/`, and `group_members` are deleted on `kicked/left`. Group projects are also deleted (not migrated to private). If you prefer to preserve projects as private, change `cleanup_group_data` to `UPDATE group_id=NULL`.
- **`auto_journal` cost for groups:** 1 call/group/30min is accepted. If N groups grows, sampling or per-group disabling will be evaluated.
- **History already isolated:** `history.py` is not touched beyond not breaking `chat_id` scoping.