# Development Guide — Telegram AI Bot

Guide for developers maintaining this bot. **For development only** — the Telegram bot never reads this file (see `AGENTS.md`).

## Requirements

- Python 3.12
- Docker + Docker Compose (for the container)
- `.env` created from `.env.example` (with `BOT_TOKEN` and `OPENCODE_GO_API_KEY`)

## Local setup

```bash
cd telegram-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

> To test with the real bot you need a token from @BotFather and your ID in `AUTHORIZED_USERS`.

## Dependencies (lockfile)

Dependencies are **pinned** so that local, CI, and VPS install exactly the same set, and a deploy doesn't silently break the bot when pip resolves a newer version.

- `requirements.in` — **declared direct dependencies** (the input, what you edit by hand).
- `requirements.txt` — **generated lockfile**: direct + transitive deps with exact versions. This is what the Dockerfile and CI install. **Do not edit by hand.**
- `requirements-dev.txt` — test dependencies (local/CI only), also pinned.

### Why

With loose ranges (`>=21.0`) the container rebuilds from scratch on every deploy and pip picks the latest version available at that moment, which may differ from what was tested locally. Real example: when generating this lock, a fresh resolution installed `openai==3.1.0` while the development environment had `openai==2.54.0` — without the lock, the next push would have deployed a 3.x SDK that nobody had tested. With the lock, the deploy installs exactly what was validated with the test suite.

### How to regenerate the lock

When you want to update dependencies:

```bash
# 1) Edit requirements.in (add/update direct deps)
# 2) Generate the lock in the same production environment (python:3.12-slim):
docker run --rm -v "$PWD":/w -w /w python:3.12-slim \
  sh -c "pip install -r requirements.in && pip freeze" > requirements.txt
# 3) Add the comment header to the file (see requirements.txt)
# 4) Mandatory validation with the full test suite:
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest --cov=bot
```

> If a regeneration bumps a major version that breaks the suite, narrow the range in `requirements.in` (e.g. `openai>=2.54,<3`) and regenerate. The lock only pins the result; validation with tests is mandatory before committing.

## Docker ALWAYS (recommended for testing)

> ⛔ **This project ALWAYS runs in Docker** in production, and Docker is also recommended for development: `docker compose up -d --build`. It's the same container that runs on the VPS, so what you test locally is exactly what gets deployed.

```bash
docker compose up -d --build   # build and start (reads your local .env)
docker compose logs -f         # view logs
docker compose up -d --build   # restart after changes
docker compose down -v         # delete data volume (careful!)
```

> ⚠️ **The development Telegram token is different from the production one.** Your local `.env` must use a **test** `BOT_TOKEN` (bot created with @BotFather for development), never the production bot token, which lives only in the VPS's `.env`. Using the production token locally **would break the live bot**. `AUTHORIZED_USERS` may also differ between environments.

## Working structure

- **Bot code**: `main.py` + `bot/` — this is the only thing that goes into the container
- **Development docs**: `AGENTS.md`, `README.md`, `docs/` — never goes into the container
- **User data**: `DATA_DIR` (`/app/data` in Docker) — never goes to git

## How to add a tool to the model

Tools are the only bridge between the model and the system. Steps:

1. **Define the tool** in `TOOL_DEFINITIONS` (`bot/tools.py`):
   ```python
   {
       "type": "function",
       "function": {
           "name": "mi_tool",
           "description": "What it does and WHEN to use it (instructions for the model).",
           "parameters": {"type": "object", "properties": {...}, "required": [...]},
       },
   },
   ```
2. **Add the dispatch** in `_dispatch()`:
   ```python
   if name == "mi_tool":
       resultado = await mi_funcion(args)
       return json.dumps(resultado)
   ```
3. **Implement the logic** in its module (new or existing).
4. **Always return JSON string** to the model: `{"ok": True, ...}` or `{"ok": False, "error": "..."}`. Never raw exceptions.
5. **Validate domain values** before persisting (e.g. project statuses and priorities against `PROJECT_STATUSES` / `PROJECT_PRIORITIES`); return the error in JSON so the model handles it in Spanish.
6. **If the tool lists content for the user** (projects, reminders, etc.), it also returns a `formatted` field with text already in bullet points (•), following the pattern of `_format_projects_list` / `_format_reminders_list` (`bot/tools.py`). The model must repeat that text as-is, **without converting it to a Markdown table** (Telegram doesn't render them well) — include this instruction in the tool description.

> ⛔ **NEVER** add a tool that reads project files (AGENTS.md, README, docs/…). See golden rule in `AGENTS.md`.

> The memory tools (`journal_append`, `memory_read`, `memory_recall`, `memory_insights`) were added following **exactly** this procedure: definition in `TOOL_DEFINITIONS`, dispatch in `_dispatch()`, logic in `bot/memory.py` + `bot/summarizer.py`, and **always JSON string** back to the model. If you add another memory tool, follow the same pattern and the scope restricted to `DATA_DIR/memory/{user_id}/`.

## How to add a command

1. Create/edit the handler in `bot/handlers.py` (or `bot/project_commands.py` if it's project-related).
2. Register the command in `bot/bot.py` (`app.add_handler(CommandHandler("micomando", mi_handler))`).
3. If the command needs AI, use the client from `bot.ai_client`.
4. Validate sensitive inputs (e.g. project statuses and priorities against `PROJECT_STATUSES` / `PROJECT_PRIORITIES`) and return clear errors to the user.
5. Document in `README.md` (commands table).

> **Naming convention**: commands exposed to users are in Spanish, lowercase, and without underscores (e.g. `/nuevoproyecto`, `/estadoproyecto`, `/prioridadproyecto`). Maintain this pattern for new commands.

## Note: python-telegram-bot v22 API (inline buttons)

The project requires `python-telegram-bot>=21` (v22 installed). In v22, the
`Message.edit_message_text()` method was **removed** — using it raises at
runtime `AttributeError: 'Message' object has no attribute 'edit_message_text'`.
Tests **don't** catch this because `tests/fakes.py` defines its own fakes
(`FakeMessage`) that do have that method.

To edit an inline button's message, always use the method on `CallbackQuery`
itself:

```python
await query.edit_message_text("Texto nuevo")   # ✅ correct (PTB v22)
# await query.message.edit_message_text(...)   # ❌ removed in v22
```

Tests simulate this in `FakeCallbackQuery.edit_message_text`
(`tests/fakes.py`), which delegates to the message and records in
`query.message.edited_messages`.

## Project sharing (ACL)

Projects can be shared: the **owner** lives in `projects.user_id` (single source of truth) and invitees in `project_members` (a single full collaborator role). The `users` table stores Telegram profiles (`username`, `first_name`) to resolve `@username → user_id`.

**Golden rules when touching this code:**

1. **Every read/write of a project goes through `get_project_for_user(user_id, slug)`** (returns the project only if you're the owner or a member). Don't use `get_project_by_slug` for user operations — it's the gate that prevents filtering other people's projects. Already applied in `project_commands._find_project` and `tools._resolve_slug`.
2. **Only the owner invites/removes**: `is_owner` is checked in the command/tool **and** again inside `add_project_member`/`remove_project_member` (defense in depth). A new caller must never bypass the data layer.
3. **Self-invitation rejected**: `add_project_member` returns an error if `user_id == invited_by`; don't allow it in the UI layer either.
4. **File paths ALWAYS use the owner**: `read_project_files(project["user_id"], ...)` — the invitee reads/writes in the owner's folder (`projects/{owner_id}/{slug}/`), never in their own.
5. **`user_id` is always injected from the Telegram update** in `execute_tool(name, args, user_id)` — never from model arguments.
6. **Rule of 3 urgents against the owner**: if an invitee edits a shared urgent project, pre-checks use `project["user_id"]` (not the acting `user_id`).
7. **Resolving `@username`**: the user to invite must be in the `users` table, populated with `upsert_user` on every update (message, voice, photo, and `/start`). `resolve_username` is deterministic (`ORDER BY updated_at DESC LIMIT 1`) to avoid routing to a reused username.

New sharing commands: `/compartir <project> @user` and `/quitaracceso <project> @user`. New tools: `share_project`, `unshare_project`, `list_project_members`.

## Project deletion (archived only)

To free up space, an archived project can be **permanently deleted**. It's the only destructive operation in the bot, so there are strict rules (same defensive philosophy as the ACL):

1. **Owner only**: `is_owner` is checked in the command/tool **and** inside `delete_project()` (`bot/projects.py`) — the data layer rejects any `actor_id != projects.user_id`, just like `remove_project_member`.
2. **Archived only**: if the status isn't `archivado`, `delete_project()` returns an error. An active project can never be deleted directly; you must archive it first with `/estadoproyecto <name> archivado`.
3. **Confirmation**: `/eliminarproyecto <name>` doesn't delete anything by itself — it renders inline buttons **"Yes, delete"** (`delproj:<id>`) / **"Cancel"** (`delproj_cancel:<id>`). The `CallbackQueryHandler` `delete_project_callback` (pattern `^delproj(_cancel)?:\d+$`) re-validates auth/owner/archived before executing the deletion. Deletion requested via **natural language** (tool `delete_project`) goes through the same confirmation: `handlers._forward_tool_confirmation` detects the `confirmation_required` returned by the tool and sends the same buttons to the chat.
4. **Full cascade**: when deleting the `projects` row, `project_notes` and `project_members` are deleted via FK `ON DELETE CASCADE`, and the folder `projects/{owner_id}/{slug}/` (overview, notes, context.json) is deleted from disk with `_remove_project_dir` — protected against path traversal (`is_relative_to(PROJECTS_DIR)`).
5. **Model tool**: `delete_project` (AI) applies the same rules and **never deletes directly** — it returns `confirmation_required` (with the target project) and `handlers._forward_tool_confirmation` handles showing the buttons; the tool description insists it's **irreversible** and should only be used upon explicit user request.
6. **`user_id` never comes from model args** or `callback_data` — always from the Telegram update.

## How to add a reminder

Reminders are created:
- **Automatically** by the AI: the model calls the `create_reminder` tool
- **By command**: `/recordar <text>` → `parse_reminder_datetime()` extracts date/message/recurrence

Persistence lives in `bot/reminders.py` (SQLite). The scheduler is in `bot/bot.py`.

**Management**: reminders can be edited/postponed/cancelled in three ways:
- **By conversation**: AI tools `list_reminders`, `cancel_reminder`, `edit_reminder`, `reschedule_reminder`, `postpone_reminder` (`bot/tools.py`)
- **By commands**: `/recordatorios` (list with inline buttons), `/editarrecordatorio`, `/posponerrecordatorio`, `/reprogramarrecordatorio`, `/cancelarrecordatorio` (`bot/handlers.py`, registered in `bot/bot.py`)
- **Inline buttons**: the `CallbackQueryHandler` `reminder_callback` (pattern `^remind_(cancel|postpone):\d+$`) handles the taps

**Delivery with retries**: `check_due_reminders` (`bot/handlers.py`) retries failed sends with exponential backoff defined in `bot/reminders.py` (`retry_delay`, `schedule_retry`, `reset_retry`): 10s, 20s, 40s... up to a maximum of 8h. Editing a reminder (text, date, or postpone) resets the retry state.

## How memory works (journal)

Long-term memory is a **per-tenant journal** (`user_id` or `group_id`): SQLite (`memory.db`) stores the **metadata** (days and summaries) and the readable content lives in Markdown under `DATA_DIR/memory/{user_id}/` or `memory/group_-xxx/` (`raw/` = raw daily capture, `days/` = daily compact, `summaries/{weekly,quarterly,yearly}/` = hierarchical summaries). Same SQLite+MD pattern as `projects.py`, with the same defensive philosophy:

1. **Isolation by `user_id`/`group_id`** — each tenant has its folder `memory/{user_id}/` or `memory/group_-xxx/` and rows with `CHECK((user_id IS NOT NULL AND group_id IS NULL) OR ...)` + partial indexes; `user_id`/`group_id` is always injected from the Telegram update (`execute_tool(name, args, user_id, _group_id)`), never from model args. The group journal is **shared and signed `@user`**.
2. **Path traversal** — `_validate_day` / `_validate_summary_period` only accept the exact period format (`YYYY-MM-DD`, `YYYY-Www`, `YYYY-Q#`, `YYYY`) and `_safe_under_memory` / `_safe_under_memory_group` resolve each path requiring it to stay within `MEMORY_DIR` **and** within the tenant's folder (double guard, same as `_remove_project_dir` with `is_relative_to`). If you add a new function that builds memory paths, run it through those guards.
3. **One connection per operation** with WAL and idempotent schema (`CREATE TABLE IF NOT EXISTS`), like the rest of the modules.
4. **Summary consistency** — saving a summary at a given level finalizes any other `current` at the same level: there's always exactly one `current` per level (the current period); closed ones become `finalized` and navigable.

**Memory tools and their contracts** (all return JSON string; defined in `bot/tools.py`, logic in `bot/memory.py`/`bot/summarizer.py`):

| Tool | Contract |
|------|----------|
| `journal_append(content, topic)` | Saves to the day's journal (`raw/YYYY-MM-DD.md`) with optional `[topic]` prefix; unknown topic is discarded (soft validation); returns `{ok, day, entry_count, word_count}` |
| `memory_read(period, level=auto)` | Reads a specific day/summary; `auto` deduces the level from the period format; day → daily compact preferred, raw as fallback; returns `{ok, level, period, content}` (or `note` if nothing found) |
| `memory_recall(query, date_from?, date_to?)` | Top-down search: scores summaries year→quarter→week (1 best per level, with `pointer` to children); if none match, falls back to raw days in the range (top 3, default window 60 days); returns `{ok, matches, hint/note}` |
| `memory_insights(days=7)` | Last `days` raw days + current week summary; feeds habit questions and the proactive engine; returns `{ok, days, week, week_summary}` |
| `set_checkin_time(period, time)` | Adjusts the user's check-in times in `user_proactive_settings` (table in `projects.db`): `period` ∈ `morning`/`evening`, `time` in `HH:MM` (24h); validates and returns `{ok, period, time, morning, evening}` or an error in Spanish (**private only**; blocked in groups) |

**Summary generation**: nightly compaction, weekly, quarterly, and yearly run as jobs in `bot/bot.py` → `bot/proactive.py` and are **catch-up scanners**: each time they run they ensure *everything* missing in their recovery window (last 3 days, last 8 weeks, last 4 quarters, current + previous year), so if the server was down when a period closed, the next startup catches up automatically (no calendar guards: quarterly/yearly run daily and are no-op when nothing is missing). **Lazy generation** (`summarizer.ensure_summary`) complements catch-up: regenerates on demand if recall doesn't find anything, is idempotent and doesn't call the model for empty periods, so the steady-state cost is ~0. The proactive engine (`bot/proactive.py`) generates check-ins with the chat model: `run_checkins` (periodic job every 15 min) resolves each user's window — their own times in `user_proactive_settings` or the defaults `PROACTIVE_MORNING_TIME`/`PROACTIVE_EVENING_TIME` —, respects the `morning_sent_at`/`evening_sent_at` markers (one window per day) and `PROACTIVE_MAX_DAILY` (daily cap in `proactive_log`), and delivers best-effort with backoff.

> ⛔ **Golden rule (reminder)**: memory is **tenant data** (`DATA_DIR/memory/{user_id}/` or `group_-xxx/`), not project documentation. Memory tools **never** read project files (AGENTS.md, README, docs/…) — their only scope is the talking tenant's memory folder. Never add a tool that reads project files to "enrich" memory.

> The default `SYSTEM_PROMPT` (`bot/config.py`) already includes memory and proactivity instructions (when to use `journal_append`, `memory_recall`, `memory_read`, `memory_insights`, `set_checkin_time` and how to respond to check-ins). If the VPS's `.env` defines an old `SYSTEM_PROMPT`, that text **overrides** the default and disables those rules: manually edit `~/telegram-bot/.env` on the server and delete/update the `SYSTEM_PROMPT` line (deploys don't touch that file).

## How to add a new AI provider

1. Add the environment variables in `bot/config.py` (`Settings`).
2. Create the client builder in `bot/ai_client.py` (`build_client_for_provider` pattern).
3. Add the option in the `load_settings()` validation.

> The provider and model are **fixed by configuration** (`.env`) and are not
> changed from chat: there's no command or tool for that (see `bot/handlers.py`).

## Testing

### Automated suite (pytest)

There's a test suite in `tests/` (unit + integration with fake Telegram objects, no network):

```bash
# 1) Install test dependencies (only once per .venv)
.venv/bin/pip install -r requirements-dev.txt

# 2) Run tests with coverage
.venv/bin/python -m pytest --cov=bot
```

If you already have the virtual environment activated (`source .venv/bin/activate`), just `python -m pytest --cov=bot` is enough.

- `--cov=bot` measures what percentage of the `bot/` package code was executed by the tests and shows a per-module report (e.g. `access 100%`, `handlers 46%`). Without the flag, pytest only runs the tests.
- CI uses `pytest --cov=bot --cov-fail-under=80`: if coverage drops below 80%, the workflow fails and blocks the deploy.
- Runs in CI on every push (`test` job in the workflow).
- All externals (httpx, ddgs, AI, Telegram) are mocked — no network or tokens required.
- Tests are deterministic and isolated (SQLite DB in `tmp_path` per test).

### Bot wiring smoke test (`bot/bot.py`)

> **Implemented** in `tests/test_bot.py` (7 tests).

`bot/bot.py` (`create_application`) registers ~25 handlers and 6 scheduled jobs and is the single critical startup point: a typo in a handler name or a misregistered job breaks the bot at startup. The smoke test calls `create_application()` with test settings (no network or tokens) and verifies:

- That the expected `CommandHandler` / `MessageHandler` / `CallbackQueryHandler` exist (correct commands and callback patterns).
- That `bot_data["settings"]`, `bot_data["provider"]`, and `bot_data["ai_client"]` are populated.
- That jobs are in the job queue with correct names/intervals (`prune_stale_chats` 600s, `check_due_reminders` 20s, `memory_*` at their times, `run_checkins` 900s).
- That with `MEMORY_ENABLED=false` / `PROACTIVE_ENABLED=false` the corresponding jobs are NOT registered (configuration gates).

Thanks to this test, `bot/bot.py` went from ~34% to 100% coverage.

### Manual end-to-end tests

To validate the full real flow (Telegram + AI + tools), test directly against the development bot:

1. Start the bot: `docker compose up -d --build`
2. Send real messages from your authorized account: normal chat, `/dolar`, `/proyectos`, `/nuevoproyecto`, `/recordar`, `/recordatorios`, voice notes, `/imagina`
3. Verify responses and review logs: `docker compose logs -f`

> There are no automated E2E tests with a simulated client bot — direct manual tests are the supported way to validate the real flow.

## Deploy to production

```bash
git add .
git commit -m "feat: my change"
git push origin main   # ← ask for user approval before
```

CI/CD automatically deploys to the VPS. **Never push without explicit user approval.**

## Checklist before a commit

- [ ] No secrets or user data (check `git status` / `git diff`)
- [ ] Conventional commit in English
- [ ] `README.md` or `docs/` not accidentally modified with bot content
- [ ] If you added a tool: it returns JSON, doesn't read project files
- [ ] If you touched memory: tools only operate in `DATA_DIR/memory/{user_id}/`; don't document new commands (the journal is 100% natural language)
- [ ] Development docs were not copied to the container (`.dockerignore` intact)
