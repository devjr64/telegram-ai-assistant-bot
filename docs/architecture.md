# Architecture — Telegram AI Bot

Technical project documentation. **Development only** — the Telegram bot never reads this file (see `AGENTS.md`).

## Overview

Telegram bot with AI for personal/family use. Uses the **OpenCode Go** API (OpenAI-compatible) to converse in Spanish, with a *function calling* loop that allows the model to invoke system tools.

## Components

### `main.py` — Entry point
Configures logging, validates `Settings`, instantiates python-telegram-bot `Application`, registers handlers and starts long-polling.

### `bot/config.py` — Configuration
`Settings` is a `@dataclass(frozen=True)` built from environment variables. **Fail-secure**: validates that required variables exist (`BOT_TOKEN` and the active provider's key) and raises `ValueError` if any are missing.

Providers:
- `opencode` (default) → `OPENCODE_GO_API_KEY`, `OPENCODE_BASE_URL=https://opencode.ai/zen/go/v1`
- `gemini` → `GEMINI_API_KEY`, `GEMINI_BASE_URL` (OpenAI-compatible endpoint)

### `bot/access.py` — Access control
Middleware that rejects any update from a `user_id` not listed in `AUTHORIZED_USERS`. Without `AUTHORIZED_USERS` → nobody has access.

### `bot/ai_client.py` — AI client
Wrapper over `AsyncOpenAI` with:
- Retry with exponential backoff (429 and 5xx errors)
- `chat_completion_with_tools` → supports *tool calls*, returns `{"kind": "tool_call" | "text", ...}`
- Handling of `reasoning_content` (DeepSeek thinking) and `thought_signature` (Gemini) which must be forwarded to the API in the assistant message when there is a tool call
- Multimodal helpers: `transcribe_audio`, `describe_image` (Gemini vision) and `generate_image` (Gemini image)

### `bot/handlers.py` — Commands + chat + tools loop
- Commands: `/iniciar`, `/ayuda`, `/info`, `/reiniciar`, `/dolar`, `/imagina`, reminder commands (`/recordar`, `/recordatorios`, `/cancelarrecordatorio`, `/editarrecordatorio`, `/posponerrecordatorio`, `/reprogramarrecordatorio`) and project commands (see `project_commands.py`)
- `handle_message`: saves the message to history, builds **time-aware** context (30 messages by default via `get_history_with_timestamps` → `_format_history_for_model` prefixes each turn with `[YYYY-MM-DD HH:MM]` in user `TIMEZONE`), filters `TOOL_DEFINITIONS` by `group_id`, calls the model with tools, and if there is a tool call executes the **tool loop** (max `MAX_TOOL_ROUNDS` rounds, default 12) until a text response is obtained. `handle_voice`/`handle_photo` use the same flow (transcription/description → timestamped history → tool loop).
- `handle_message_group`: handler for group messages. V1 parity: projects + reminders/journal/memory/web_search/dollar shared by `group_id` (only `generate_image`/`set_checkin_time` and `share/unshare/delete/list_members` remain private); injects `_group_id` and `_author_username` and also uses timestamped history.
- `handle_chat_member`: detects when the bot is removed from a group and deletes all group data (`cleanup_group_data`)
- `handle_voice`: downloads the voice note, converts it with ffmpeg and transcribes it with Gemini
- `handle_photo`: downloads the photo and analyzes it with Gemini vision (in memory, deleted when finished)
- `reminder_callback`: handles inline reminder buttons (cancel / postpone 1h)

### `bot/project_commands.py` — Project commands
Handlers for `/proyectos` (with filters `todos`/`mios`/`dueños`/`compartidos`/`invitado`), `/nuevoproyecto`, `/proyecto` (shows owner and shared projects), `/estadoproyecto`, `/prioridadproyecto`, `/agregarnota`, `/buscarproyecto`, `/compartir`, `/quitaracceso` and `/eliminarproyecto` (with confirmation via inline buttons `delproj:<id>` / `delproj_cancel:<id>`). Built on `bot/projects.py`. Sharing uses ACL: `_find_project` delegates to `get_project_for_user`, and `/compartir`/`/quitaracceso` require `is_owner` and proactively notify the invited user (via `get_chat_for_user`). Deletion requires being the owner and the project being `archived`; the actual deletion occurs in the callback after re-validating those same gates. Status/priority/note change commands send notifications to groups when the project is a group project (via `notify_group_project_change`).

### `bot/projects.py` — Project persistence
CRUD in SQLite (`projects.db`) + readable mirror in Markdown (`projects/{owner_id}/{slug}/overview.md`, `notes/{id}.md`, `context.json`). The `context.json` is the compact summary that the `get_project` tool returns to the model.

In addition to `projects` and `project_notes`, the schema includes:
- `project_members` — ACL per object: `(project_id → projects.id CASCADE, user_id, invited_by, created_at, UNIQUE(project_id, user_id))`. The owner lives in `projects.user_id` (single source of truth); this table only stores invited members with a single full collaborator role.
- `users` — Telegram profiles `(user_id PK, username, first_name, private_chat_id, updated_at)` to resolve `@username → user_id` (populated on every update). `private_chat_id` is stored to deliver reminders from group context.
- `group_members` — group membership: `(group_id, user_id, first_seen_at, UNIQUE(group_id, user_id))`. Records which users have interacted in each group.

Authorization core: `get_project_for_user` (owner or member — no filtering by ID), `is_owner`, `add_project_member`/`remove_project_member` (require being owner at the data layer, reject self-invitation), `list_project_members`, `list_owned_projects`, `list_shared_with_me`, `list_shared_by_me`, `resolve_username` (deterministic), `upsert_user`, `get_user_profile`. `delete_project(project_id, actor_id)` deletes **only archived projects and only by the owner** (defense in depth at the data layer, same as `remove_project_member`): deletes the row (FK cascade to `project_notes`/`project_members`) and removes the Markdown folder via `_remove_project_dir` (guarded against path traversal with `is_relative_to(PROJECTS_DIR)`). `search_projects` and `list_projects` include shared projects. The 3 urgent rule always counts against the project owner.

**Group support**: projects can be associated with Telegram groups via the `group_id` field. Key functions:
- `add_group_member(group_id, user_id)` — registers a user as a group member
- `get_group_members(group_id)` — returns all members of a group
- `get_shared_projects_in_group(group_id)` — returns projects associated with the group
- `get_private_chat_id(user_id)` — returns the user's private chat (for reminders)
- `create_group_project(owner_id, group_id, name, ...)` — creates a group project and automatically adds all members as collaborators
- `migrate_group_projects_to_private(group_id)` — migrates projects to private when the bot is removed from a group
- `get_group_project_notify_targets(project_id, exclude_user_id)` — returns user_ids to notify when a group project changes
- `get_project_owner_name(user_id)` — returns the owner's display name (for notifications)

### `bot/reminders.py` — Reminders
SQLite (`projects.db` table `reminders` with `group_id`/`created_by`). Supports one-time and recurring (`daily`/`weekly`/`monthly`) and **group-shared mode**: `group_id IS NOT NULL` delivers in the group and `list_pending_reminders(..., group_id)` isolates by tenant. The PTB scheduler triggers reminders and reschedules the next occurrence. If a delivery fails, it retries with exponential backoff (10s → 8h) using the `attempts`/`next_retry_at` columns; editing or postponing a reminder resets that state.

### `bot/history.py` — Chat history
SQLite (`chat_history.db`), messages per user with order and context window. Each row stores `created_at` (epoch). `get_history()` returns recent window `ORDER BY created_at DESC LIMIT max*2` reversed to chronological order; `get_history_with_timestamps()` also exposes `created_at` so that `handlers._format_history_for_model` prefixes each turn as `[YYYY-MM-DD HH:MM] content` in the user's `TIMEZONE` (fallback UTC). This way the model sees the **gap** (e.g. 10 days of silence) and doesn't assume the last message was yesterday.

### `bot/memory.py` — Journal persistence (long-term memory)
SQLite (`memory.db`) + Markdown per user **and per group** (`group_id`). `memory.db` stores **only metadata** (paths, word counts, date ranges, `current`/`finalized` state); readable content lives in `memory/{user_id}/raw|days|summaries/` or `memory/group_-xxx/...` for groups (signed `@username`). **Isolation by `user_id`/`group_id`**: each tenant has its folder (`_owner_dir` / `_group_memory_dir`) — same philosophy as `projects.py`. **Path traversal protection**: `_validate_day` / `_validate_summary_period` only accept the exact period format (`YYYY-MM-DD`, `YYYY-Www`, `YYYY-Q#`, `YYYY`) before building paths, and `_safe_under_memory` (+ `_safe_under_memory_group`) resolves each path requiring it to stay within `MEMORY_DIR` **and** within the tenant's folder.

Tables:
- `days` — PK `(user_id, day)`; `raw_path`, `raw_word_count`, `entry_count`, `compact_path`, `compact_word_count`, `range_start`, `range_end`, timestamps. A day starts with only `raw` and gains `compact` when compacted; nightly compaction rewrites only `compact_*`, leaving raw stats intact.
- `summaries` — PK `(user_id, level, period)`; `content_path`, `word_count`, `range_start`, `range_end`, `status`. Consistency rule: when saving a summary of a level, any other `current` of the same level becomes `finalized` — there is always exactly **one `current` per level** (the period still being written; closed ones remain `finalized` and navigable).

Main API: `append_raw_entry` (append `- [HH:MM] text`), `get_raw_entries` (range by days), `save_daily_compact`, `save_summary`, `get_summary`, `get_summary_content`, `list_summaries`, `finalize_summary`, `find_summary_covering` (for top-down recall). One connection per operation, WAL and idempotent schema (pattern from `projects.py`/`history.py`).

### `bot/summarizer.py` — Hierarchical summaries
Generates summaries with the **same chat model** (`ai_client.chat_completion`). Each summary is a **navigable index**: metadata line (range, number of sources), "Events with dates" (chronological, with `YYYY-MM-DD` dates), the **11 domain sections** in fixed order (`SUMMARY_SECTIONS`: health and exercise; finances and payments; travel and outings; family and friends; work and projects; mood; pending items and follow-ups; spirituality and extraordinary events; leisure, rest or enjoyment; learnings and studies; historical events or environmental occurrences), "Patterns and streaks" and "References to child level" (pointers to child periods — the basis of top-down recall).

- `build_summary_prompt` — pure deterministic template (no IO) that embeds the structure and labeled sources.
- `generate_daily_compact` — compacts a raw day to `days/YYYY-MM-DD.md` only if it exceeds `MEMORY_DAILY_COMPACT_WORDS`; idempotent (if the compact already exists it doesn't make another model call).
- `generate_weekly/quarterly/yearly_summary` — each level first ensures its children (`ensure_summary`) and reads the already stored child summaries (week ← days, quarter ← weeks, year ← quarters).
- `ensure_summary` — **lazy generation**: returns the summary if it exists; if not, generates it on demand (used by recall and quarterly/yearly jobs).
- Model failures → `None` (logged and lazily retried); `clean_model_output` removes markdown fences.

### `bot/proactive.py` — Proactive engine and memory jobs
Best-effort outgoing messages: morning check-in (greeting + `Today is YYYY-MM-DD (weekday) — TZ zone` + yesterday + week + `Last entry: YYYY-MM-DD (N days ago)` when the last 7 days are empty + upcoming reminders), evening closing (`Today is ...` + `Today:` or `Last entry before today` when today is empty + week) and compaction/summary jobs. This way check-ins are **time-aware**: if the user has been silent for 10 days, the prompt already carries `Today is 2026-09-03` and `Last entry: 2026-08-23 (10 days ago)` so the model doesn't greet as if it were yesterday. Helpers `_hoy_line`, `_fallback_last_entry` (scans 7..30 days) and `_last_entry_before_today` (1..30 days) are best-effort, truncate with `_CONTENT_TRUNCATE_CHARS` and never raise. `proactive_log` (in `projects.db`) tracks the **daily cap per user** (`PROACTIVE_MAX_DAILY`) and the **window markers** `morning_sent_at`/`evening_sent_at` (each window is sent at most once per day); `can_send` with `max_daily <= 0` is fail-closed. `user_proactive_settings` stores **check-in times per user** (NULL → global defaults); `get_checkin_times` resolves by user and `set_checkin_time` validates `HH:MM` (the model's `set_checkin_time` tool writes here). `window_of(settings, morning_time, evening_time)` defines the 3-hour window from the corresponding time as a guard. `send_proactive` retries up to 3 times with exponential backoff (10s, 20s) and falls back to plain text if the model's Markdown is invalid; no persistent queue (unlike `reminders`). Drivers: `run_checkins` (periodic job every 15 min that resolves each user's window with their own times, respects markers and cap), `run_nightly_compaction`, `run_weekly_summaries`, `run_quarterly_summaries`, `run_yearly_summaries` — each user runs in its own `try/except` so one user's failure doesn't stop others. The module doesn't import `bot.tools`/`bot.handlers` (avoids import cycle).

### `bot/tools.py` — Tool definition and dispatch
`TOOL_DEFINITIONS` is the list sent to the model. `_filter_tool_definitions(group_id)` hides `PRIVATE_ONLY_TOOLS` in groups; `_dispatch()` routes each tool call to its logic. **All tools return a JSON string** (`{"ok": True/False, ...}`) — never raw exceptions.

Current tools: `create_project`, `update_project_status`, `update_project_priority`, `update_project_description`, `add_project_note`, `list_projects` (with `scope` parameter: all, owned, shared_with_me, shared_by_me), `search_projects`, `get_project`, `share_project`, `unshare_project`, `list_project_members`, `delete_project`, `create_reminder`, `list_reminders`, `cancel_reminder`, `edit_reminder`, `reschedule_reminder`, `postpone_reminder`, `web_search`, `dolar_rate`, `generate_image`, `journal_append`, `memory_read`, `memory_recall`, `memory_insights`, `set_checkin_time`.

`GROUP_TOOLS` includes v1 parity (web_search, dolar_rate, reminders and memory shared); only `PRIVATE_ONLY_TOOLS={generate_image, set_checkin_time, share/unshare/delete/list_members}` remain blocked in groups and `group_id`/`author` are injected from the handler.

> Tools that list content to the user (`list_projects`, `search_projects`, `list_reminders`, `list_project_members`) also return a `formatted` field with the text already formatted as bullets; the model should repeat it as-is to avoid Markdown tables, which Telegram renders poorly.

> **Project tools scope (ACL)**: `_resolve_slug` delegates to `get_project_for_user`, so a tool operates on a project only if the user is owner or member (a non-member receives "Project not found"). `share_project`/`unshare_project` require being owner; `delete_project` requires being owner **and** the project being `archived` (the tool is documented as irreversible and for use only upon explicit user request). The `user_id` is always injected from the Telegram update in `execute_tool(name, args, user_id)` — never comes from model arguments.

> **Memory tools scope (journal)**: operate only on the speaking user's `DATA_DIR/memory/{user_id}/` — never read project files (AGENTS.md, README, docs/...) or another user's data. `journal_append` saves to the day's journal; `memory_recall` searches top-down (summaries → raw days); `memory_read` reads an exact period; `memory_insights` returns recent context (last days + current week) for patterns and proactivity; `set_checkin_time` adjusts the user's check-in times (defaults to global `PROACTIVE_*`). The `user_id` is always injected from the Telegram update.

> **Group support (group ACL)**: in group context (`group_id` not None), `_filter_tool_definitions` hides `PRIVATE_ONLY_TOOLS` and `execute_tool` validates scope. Only `generate_image`/`set_checkin_time`/sharing remain blocked; `create/list/cancel/edit/reschedule/postpone_reminder`, `journal_append`/`memory_*`, `web_search`/`dolar_rate` are **shared by `group_id`** with injection of `_group_id`/`_author_username` from the handler (strict isolation by `chat_id`).

### `bot/dolar.py` — Dollar rate
Queries official BCV + Binance P2P USDT.

### `bot/websearch.py` — Web search
DuckDuckGo via `ddgs` library (no API key), in `asyncio.to_thread`.

### `bot/media.py` — Media processing
Voice conversion OGG→WAV (ffmpeg), image resizing.

### `bot/media_usage.py` — Media limits
Daily counter per user (images and audio) in `media_usage.db`.

### `bot/bot.py` — Wiring
Creates the `Application`, registers handlers, commands, the reminder scheduler and memory/proactivity jobs (`_register_memory_jobs` — see "Scheduled jobs").

## AI flow with tools

```
User ──> update.message ──> access.py (authorized?) ──> handlers.py
   │
   ├─ commands ──> specific handlers (without AI or with AI depending on command)
   │
   └─ chat ──> save to history ──> build context (last N messages)
                  │
                  ▼
        ai_client.chat_completion_with_tools(model, system_prompt, history, TOOL_DEFINITIONS)
                  │
        ┌─────────┴──────────┐
        │ kind=text          │ kind=tool_call
        │ respond            │ → tools.execute_tool(name, args, user_id)
        │ (send to Telegram) │ → JSON result to model
        │                    │ → repeat (max MAX_TOOL_ROUNDS) until kind=text
        └────────────────────────────────────┘
```

Memory tools (`journal_append`, `memory_read`, `memory_recall`, `memory_insights`) are wired exactly like the rest: definition in `TOOL_DEFINITIONS` + dispatch in `_dispatch()` + logic in `bot/memory.py`/`bot/summarizer.py`, and return JSON string. During conversation the model calls `journal_append` when the user shares something notable about their day; when they ask about their past ("when was my last trip to Panama?") it calls `memory_recall` (and `memory_read` for details of a specific period). Data lives under `DATA_DIR/memory/{user_id}/` — user data, not project documentation.

## Scheduled jobs (job_queue)

| Job | When | What it does | Requires |
|-----|------|--------------|----------|
| `memory_nightly_compaction` | 00:15 daily | Compacts the **last 3 days** over `MEMORY_DAILY_COMPACT_WORDS` to `days/YYYY-MM-DD.md` (users + groups) | `MEMORY_ENABLED` |
| `memory_weekly_summaries` | 00:30 daily | Ensures summaries for the **last 8 ISO weeks** (`summaries/weekly/YYYY-Www.md`, users + groups) | `MEMORY_ENABLED` |
| `memory_quarterly_summaries` | 00:45 daily | Ensures summaries for the **last 4 quarters** (ensures weeks first via `ensure_summary`, users + groups) | `MEMORY_ENABLED` |
| `memory_yearly_summaries` | 01:00 daily | Ensures summaries for the **current and previous year** (ensures quarters first, users + groups) | `MEMORY_ENABLED` |
| `memory_checkins` | Every 15 min | Resolves each user's window (own times or `PROACTIVE_*`) and sends morning/evening check-in if due; `morning_sent_at`/`evening_sent_at` markers prevent duplicates | `MEMORY_ENABLED` + `PROACTIVE_ENABLED` |
| `auto_journal` | Every `AUTO_JOURNAL_INTERVAL` (60min default, 75% savings vs 30min) | Extracts events+entities in 1 call (`_COMBINED_EXTRACT_PROMPT`) with threshold `<6 msgs` skip and fallback `interval×3`, over `LOOKBACK_HOURS=3` | `MEMORY_ENABLED` |

**Catch-up after downtime**: compaction and summary jobs are **recovery scanners**, not single-day events — each time they run they look back a bounded window (3 days, 8 weeks, 4 quarters, 2 years) and generate **everything missing**. If the server was down when a period closed (e.g. Monday for weekly, April 1st for quarterly or January 1st for yearly), the next startup catches up on its own, without intervention. `ensure_summary` is idempotent (skips already generated) and returns `None` without calling the model when a period has no content, so steady-state cost is ~0 calls and a catch-up costs at most 1 call per missing period with data. For periods older than the window there's **lazy generation**: `memory_recall`/`memory_read` (or the parent level) generate them on demand, and the raw is never deleted, so there's no possible loss. Missed check-ins are not resent (a "good morning" from a past day doesn't make sense).

Each job processes users in its own `try/except`: one user's failure (API, filesystem) never stops the rest. Jobs are registered in `bot/bot.py` → `_register_memory_jobs`, governed by `MEMORY_ENABLED` / `PROACTIVE_ENABLED`.

## Persistence

| Data | File | Path (Docker) |
|------|------|---------------|
| Chat history | `chat_history.db` | `/app/data/chat_history.db` |
| Projects (metadata + membership + profiles + groups), reminders, `proactive_log` and `user_proactive_settings` | `projects.db` | `/app/data/projects.db` |
| Projects (content) | `projects/{owner_id}/{slug}/` | `/app/data/projects/` |
| Journal (metadata of days and summaries) | `memory.db` | `/app/data/memory.db` |
| Journal (Markdown content) | `memory/{user_id}/…` + `memory/group_-xxx/…` | `/app/data/memory/` |
| Media limits | `media_usage.db` | `/app/data/media_usage.db` |

Tables in `projects.db` related to groups:
- `projects.group_id` — nullable; projects with `group_id` are visible in that group
- `users.private_chat_id` — user's private chat (legacy, no longer used for delivery; group reminders are delivered in the group)
- `group_members` — `(group_id, user_id)` — users who have interacted in each group

Tables in `memory.db` / `reminders` with group support:
- `memory.db:days(user_id nullable, group_id nullable, CHECK)` + partial indexes `ux_days_private/group`
- `memory.db:summaries` same
- `reminders.group_id` / `created_by` — group-shared reminders

Journal layout under `DATA_DIR/memory/{user_id}/`:

```
memory/{user_id}/
├── raw/YYYY-MM-DD.md        ← raw capture of the day (live append, never deleted)
├── days/YYYY-MM-DD.md       ← daily compact (only if exceeded MEMORY_DAILY_COMPACT_WORDS)
└── summaries/
    ├── weekly/YYYY-Www.md
    ├── quarterly/YYYY-Q#.md
    └── yearly/YYYY.md
```

`DATA_DIR` is injected in Docker (`/app/data`, volume `bot_data`). The container does **not** contain development code or documentation.

## Deployment

1. `git push origin main` → GitHub Actions (`.github/workflows/deploy.yml`)
2. rsync to VPS (excludes `.env`, DBs, `.venv`, `.opencode/`, `AGENTS.md`, `docs/`, `projects/`)
3. `docker compose up -d --build`
4. `docker image prune -f`

The bot uses outgoing long-polling → no exposed ports or domain needed.

## Key decisions

| Decision | Reason |
|----------|--------|
| SQLite + Markdown mirror | Simple, no server, content readable/editable by hand |
| Function calling as the only bridge | The model never touches the system directly; only through approved tools |
| Share projects with ACL + single collaborator role | Standard pattern like Google Drive/GitHub: owner in `projects.user_id`, invitees in `project_members`; single gate `get_project_for_user`; only owner invites/removes; immediate invitation with notification |
| Delete only archived projects and only by owner | Irreversible operation: requires `archived` state to avoid accidentally destroying an active project; `is_owner` re-validated at the data layer (defense in depth) and UI asks for confirmation with inline buttons |
| Configurable provider (opencode/gemini) | Cost/quota fallback: gemini free for media and images |
| Fail-secure access | Without `AUTHORIZED_USERS` nobody gets in (protects budget and privacy) |
| SQLite + MD journal per user (same philosophy as projects) | Memory is user data: metadata in SQLite for fast recall, readable content in Markdown isolated by `user_id`, with path traversal guards on every path |
| Summaries as navigable indexes (week → quarter → year) | A summary is an index (sections + events with dates + pointers to child level): answering about the past is following pointers, not re-reading all history |
| Non-root container with no ports | Smaller attack surface |
