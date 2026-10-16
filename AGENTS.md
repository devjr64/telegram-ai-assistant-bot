# AGENTS.md — Telegram AI Bot (OpenCode Go)

Instructions for the agent (opencode / OpenAgents) that **develops and maintains** this project.
This file is for the human developer and the development agent. **It is NOT for the Telegram bot.**

---

## ⛔ Golden rule: separation of worlds (READ FIRST)

This project uses the same "OpenCode Go" service for **two completely different purposes**:

| World | Who reads it | What it contains |
|-------|-------------|--------------|
| **Development** | You + opencode (coding agent) | `AGENTS.md`, `README.md`, `docs/`, `.opencode/context/` |
| **Bot runtime** | Only the bot in Docker | `SYSTEM_PROMPT` (in `.env`), tools, and user data (`DATA_DIR/projects/*.md`, `chat_history.db`, `projects.db`, `memory.db`, `memory/`) |

### Absolute rules

1. **The Telegram bot must NEVER read or interpret project documentation** (`AGENTS.md`, `README.md`, `docs/`, `.opencode/`).
2. **NEVER add file reading tools to the bot** (e.g., `read_file`, `list_directory`). Bot tools are closed and limited to: user projects, reminders, web search, dollar rate, image generation, user journal/memory (journal_append, memory_read, memory_recall, memory_insights, set_checkin_time) and nothing else.
3. **NEVER include content from `AGENTS.md`, `README.md` or `docs/` in the `SYSTEM_PROMPT` or in the context sent to the bot's model.**
4. Development documentation lives in root/`docs/` and **is never copied to the Docker container** (see `.dockerignore`) nor to the VPS (see `deploy.yml`).
5. The bot only persists user data in `DATA_DIR` (default `/app/data` in Docker): `projects.db`, `chat_history.db`, `projects/`, `media_usage.db`, `memory.db`, `memory/` (per-user journal) and `entities.db` (entity tracking). Nothing else.

> If a development task requires the bot to access something from the project, **stop and consult the user first**.

---

## Project

AI-powered Telegram bot for personal/family use (Venezuela). Converses in Spanish via OpenCode Go API, manages projects with long-term memory (SQLite + Markdown), reminders (including recurring), web search, dollar rate and media (voice notes, photo analysis and image generation with Gemini).

- **Production:** [@<bot-produccion>](https://t.me/bot-produccion-ejemplo)
- **Testing:** [@<bot-pruebas>](https://t.me/bot-pruebas-ejemplo) — only used for local testing, NEVER for production
- **GitHub repo:** `github.com/<usuario>/<repo>` (private)
- **Deploy:** automatic via GitHub Actions to VPS (Docker)

> ⚠️ **Token rule**: the production bot (`@<bot-produccion>`) and testing bot (`@<bot-pruebas>`) use different tokens. **NEVER** use the production token for local testing. The testing bot does NOT run 24/7 — it's only started when manual testing is needed.

## Architecture

```
main.py                      → entry point (Python 3.12, python-telegram-bot)
bot/
├── config.py                → Settings dataclass (.env variables, fail-secure)
├── access.py                → access control (only AUTHORIZED_USERS)
├── ai_client.py             → OpenAI-compatible client (opencode / gemini)
├── handlers.py              → commands + chat + tool loop + group handlers
├── project_commands.py      → project commands (/proyectos, /nuevoproyecto,
│                              /compartir, /quitaracceso…)
│                              Scope filters: all, mine, owners, shared, guest
│                              Notifications for group project changes
├── projects.py              → SQLite project CRUD + Markdown + sharing
│                              + group support (ACL: project_members, users,
│                              group_members, get_project_for_user, get_shared_projects_in_group)
├── reminders.py             → SQLite reminders (one-time + recurring)
├── history.py               → SQLite chat history (PERMANENT, no pruning)
├── tools.py                 → tool definitions + model tool execution
│                              (GROUP_TOOLS, PRIVATE_ONLY_TOOLS for group scope)
│                              list_projects supports scope: all, owned, shared_with_me, shared_by_me
│                              Group tools generate _notify metadata for notifications
├── auto_journal.py          → auto-save events and entities (60min + 1 call, threshold 6)
├── entities.py              → entity tracking (people, places, cases, documents)
├── memory.py                → journal (SQLite + Markdown, hierarchical summaries)
├── summarizer.py            → summary generation (daily, weekly, quarterly, yearly)
├── proactive.py             → morning/evening check-ins + summary jobs
├── dolar.py                 → dollar rates (official BCV + Binance P2P USDT)
├── websearch.py             → DuckDuckGo web search (ddgs, no API key)
├── media.py                 → voice processing (ffmpeg) and images
├── media_usage.py           → daily media generation limits
└── bot.py                   → python-telegram-bot wiring (private + group handlers)

Dockerfile / docker-compose.yml
.github/workflows/deploy.yml → CI/CD: rsync + docker compose on VPS
.env.example / .gitignore
```

### AI tool-calling flow (key)

1. User sends message → bot sends history + tool definitions to model
2. Model decides: respond directly or call a tool
3. If it calls a tool, `tools.py` executes it and returns JSON result to model
4. Model produces final response in Spanish

**Adding a new tool**: definition in `TOOL_DEFINITIONS` (tools.py) + handler in `_dispatch()` + logic in its module. Tools are the ONLY bridge between model and system — keep them limited and secure.

### UX: natural language over commands

The bot's ultimate goal is for the user to interact via **natural chat** (natural language), not commands. The end user is personal/family, not technical.

- **Always prefer chat interaction** over commands (`/proyectos`, `/recordar`, etc.). When adding a new feature, the goal is to make it work through conversation, not requiring a command.
- Commands exist for: **testing**, **implementation**, **technical decisions** and **security** (e.g., confirmation buttons for destructive actions). They're a development tool, not the UX goal.
- Security is not relaxed: the bot MUST always call a tool to perform actions. The difference is that the model decides when to call the tool based on the user's natural language, not because the user typed a command.
- When designing a new feature, think first: *"how would the user ask for this in a conversation?"* and make the model resolve it via tools. The equivalent command is secondary or optional.

### Project sharing (ACL) — security rules

Projects can be shared with authorized users. **Non-negotiable rules** (see `docs/development.md` → "Project sharing (ACL)"):

1. **All project read/write goes through `get_project_for_user(user_id, slug)`** — owner or member; never query by another's `user_id` or allow filtering projects by ID.
2. **Only owner invites/removes** — `is_owner` is verified in the UI layer **and** inside `add_project_member`/`remove_project_member`.
3. **Self-invitation rejected** — a user cannot add themselves to another's project.
4. **File paths ALWAYS with owner** — guest reads/writes in `projects/{owner_id}/{slug}/`.
5. **`user_id` always injected from Telegram update** in `execute_tool(name, args, user_id)` — never from model args.
6. **3 urgent rule against owner** — even when edited by a guest.

### Group support — security rules (parity v1 — see `specs/group-parity.md`)

Telegram groups allow full collaboration with strict isolation by `chat_id`:

1. **Group projects**: a project with `group_id` is visible in that group. When created in a group, all members are automatically added as collaborators.
2. **Group parity**: `web_search`, `dolar_rate`, reminders and journal/memory are **shared by group** (`group_id` tenant). Only `generate_image` and `set_checkin_time` (and `share/unshare/delete/list_members`) remain private.
3. **Group reminders**: created with `group_id` and **delivered in the group**; any member can view/edit/delete (`group_id` isolated, `created_by` records author). The `/recordar` command and `list_reminders` etc. work in groups.
4. **Group journal**: shared `memory/group_-xxx/`, entries signed `@user` (`append_raw_entry(..., group_id, author_username)`), queryable via `journal_append`/`memory_*` in group.
5. **Group notifications**: when status, priority changes or a note is added to a group project, a brief notification is sent to the group (best-effort, never breaks operation).
6. **Group dissolution**: if bot is removed (`kicked/left`), **everything** for the group is deleted (`cleanup_group_data`: reminders, `days/summaries`, `memory/group_-xxx/`, `chat_history`, disassociated projects).
7. **`group_members`**: table recording which users have interacted in each group. Used when creating group projects to auto-add members and to discover groups in jobs.

## Code standards

- **Python 3.12** with type hints in all signatures (`def foo(x: int) -> str:`)
- **Native async** (`async def`, `await`) — never block the event loop; blocking CPU/IO tasks in `asyncio.to_thread`
- **Docstrings** in modules and public functions (explain *why*, not *what*)
- **Errors**: explicit handling at boundaries; tools ALWAYS return string JSON to model (`{"ok": ...}`), never raw exceptions
- **Logging** with `logging.getLogger(__name__)` (never `print`)
- **SQLite**: one connection per operation with `with closing(get_connection())`; idempotent schema (`CREATE TABLE IF NOT EXISTS`)
- **Configuration** only via `Settings` (config.py) — never hardcode sensitive values
- **Small functions** (< ~50 lines), composition over inheritance, no mutable global state
- **Always run in Docker** (`docker compose up -d --build`) — never `python main.py` directly

## Tests (pytest)

The test suite lives in `tests/` (unit + integration with fake Telegram objects, no network). **Before committing code changes, run the suite and confirm it passes:**

```bash
# Test dependencies (only once per .venv)
.venv/bin/pip install -r requirements-dev.txt

# Run all tests with coverage
.venv/bin/python -m pytest --cov=bot
```

- `--cov=bot` measures % of `bot/` code executed by tests (report per module).
- CI runs `pytest --cov=bot --cov-fail-under=80` — coverage <80% blocks deploy.
- Tests do NOT require network or real tokens (all mocked) and use SQLite DBs in `tmp_path`.
- If you add a new feature, add tests for it (happy path + edge + error).

## Security

- **Never** commit `.env`, keys, tokens or `.private/` (see `.gitignore`)
- Bot is **fail-secure**: without `AUTHORIZED_USERS` nobody gets in
- Container is **non-root** (`botuser`, UID 999), no exposed ports (outbound long-polling)
- Rate limiting (3s/message/user) and limited context (30 messages) protect Go budget
- VPS `.env` is never touched in deploys (excluded from rsync)
- Repo is private on GitHub

## Commits

Conventional Commits, in English, descriptive:

```
feat: add new command
fix: correct rate limit handling
docs: update architecture
ci: adjust deploy workflow
chore: bump dependency
refactor: extract dolar module
```

## Docker ALWAYS (development and production)

> ⛔ **This project ALWAYS runs in Docker.** Never run the bot directly with `python main.py` on your machine.

- **Local development/testing**: `docker compose up -d --build` (reads your local `.env`, data in `bot_data` volume). It's the same container that runs on the VPS: what you test locally is exactly what gets deployed.
- **Production**: same `docker compose up -d --build` on VPS via CI/CD.
- To view logs: `docker compose logs -f`. To restart: `docker compose up -d --build`. To clean volume: `docker compose down -v && docker compose up -d --build`.

### ⚠️ Telegram tokens: development ≠ production

- Your **local** `.env` uses a **testing** `BOT_TOKEN` (bot created with @BotFather for development only).
- The **VPS** has its own `.env` with the **production** `BOT_TOKEN` (`@<bot-produccion>`). That server `.env` is **never touched** from here: deploys don't sync it (excluded from rsync).
- **NEVER** copy the production `.env` to local or vice versa, and **never** use the production token for local testing — you'd break the live bot.
- IDs in `AUTHORIZED_USERS` may differ between environments: the testing bot only responds to IDs listed in your local `.env`.

## Deploy (no push without explicit user approval)

- `git push origin main` → GitHub Actions → rsync to VPS → `docker compose up -d --build`
- **Before pushing, always ask for explicit user approval.**
- Deploy does NOT include: `.env`, databases, `.venv`, `.opencode/`, `AGENTS.md`, `docs/`, `projects/` (see `.dockerignore` and `deploy.yml`)

## Persistent data (Docker volume `bot_data` → `/app/data`)

```
/app/data/
├── chat_history.db          ← conversation history (PERMANENT, no pruning, isolated by chat_id)
├── projects.db              ← project metadata (+ reminders with group_id + proactive_log)
├── projects/{user_id}/{slug}/  ← project content (overview.md, notes/, context.json)
├── media_usage.db           ← media limits
├── memory.db                ← journal metadata (days and summaries, with group_id)
├── memory/{user_id}/        ← per-user journal (raw/, days/, summaries/)
├── memory/group_-xxx/       ← group shared journal (isolated, signed @user)
└── entities.db              ← entity tracking (people, places, cases, documents)
```

These files are **bot user data** — don't confuse with project documentation.

## Automated testing with Telegram MCP

Automated tests with Telegram MCP **always** run against the testing bot `@<bot-pruebas>`, **NEVER** against production.

### Testing flow

1. **Start testing bot**: `docker compose up -d --build` (uses testing `BOT_TOKEN` from your local `.env`)
2. **Run MCP tests**: use Telegram MCP to send messages to `@<bot-pruebas>` bot
3. **Verify responses**: check that bot responds correctly in private and groups
4. **Stop bot**: `docker compose down` (testing bot should NOT run 24/7)

### Testing rules

- **Always** use the testing bot's chat_id (`@<bot-pruebas>`), never production
- Testing bot only responds to `AUTHORIZED_USERS` configured in your local `.env`
- After testing, **stop the container** — testing bot should not run permanently
- Test data is stored in Docker volume `bot_data` — can be cleaned with `docker compose down -v`

### Reference IDs

| Bot | Username | Use |
|-----|----------|-----|
| Production | `@<bot-produccion>` | Live bot, 24/7 |
| Testing | `@<bot-pruebas>` | Local testing only, on demand |
