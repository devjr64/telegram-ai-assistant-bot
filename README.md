# Telegram AI Assistant Bot

![CI](https://github.com/devjr64/telegram-ai-assistant-bot/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

AI-powered Telegram bot for personal and family use (Venezuela 🇻🇪). Converses in Spanish, tracks **projects** with long-term memory, and only allows access to authorized users.

> 🤖 **Personal project running 24/7** on a VPS (private instance).

## Features

- 🤖 **AI Chat** — responds in Spanish via OpenCode Go (`mimo-v2.5` by default, cost-effective)
- 🎤 **Voice notes** — transcribes with Gemini (free) and responds; transcript enters normal conversation
- 🖼️ **Photos** — analyzes with Gemini vision and converses about their content
- 🎨 **`/imagina`** — generates images with AI (Nano Banana by Gemini, free)
- 🌐 **Web search** — automatically searches the internet when answers require current info (news, prices, recent data) and cites sources
- 💵 **Exchange rate** — with `/dolar` (or just asking) get the official BCV rate and parallel USDT rate (Binance P2P)
- 📁 **Projects** — creates, searches, updates and tracks recurring topics (e.g., "fix the car")
- 👥 **Groups** — use the bot in Telegram groups for shared projects (strict privacy)
- 🔔 **Reminders** — ask it to remind you of something in the future, even **recurring** ("every Monday", "monthly")
- 🧠 **Memory** — remembers projects across conversations using SQLite + Markdown files
- 📓 **Journal** — automatically logs your day and answers about your past with hierarchical summaries
- 🔄 **Auto-journal** — reviews history every 60 min (threshold 6, 1 call) and saves important events without being asked
- 🏷️ **Entity tracking** — registers people, places, cases and documents for follow-up
- ☀️ **Proactive** — morning and evening check-ins that remind you of pending tasks and patterns from your journal
- 🔒 **Restricted access** — only authorized users (fail-secure)
- 🐳 **Docker** — same container works on your local machine and on the VPS
- 💾 **Persistence** — data stored in Docker volume, survives restarts and rebuilds

## Architecture

```
┌────────────────────────────────────────────┐
│  telegram-bot/                            │
│                                            │
│  main.py          → entry point            │
│  bot/                                     │
│  ├── config.py    → settings (.env)        │
│  ├── access.py    → access control         │
│  ├── ai_client.py → OpenCode Go client     │
│  ├── handlers.py  → commands + chat + tools│
│  ├── project_commands.py → project cmds    │
│  ├── projects.py  → CRUD + sharing + .md   │
│  ├── reminders.py → SQLite reminders       │
│  ├── history.py   → chat history           │
│  ├── auto_journal.py → auto-save           │
│  ├── entities.py  → entity tracking        │
│  ├── memory.py    → journal SQLite + .md   │
│  ├── summarizer.py → hierarchical summaries│
│  ├── proactive.py → check-ins + jobs       │
│  ├── tools.py     → tool definitions       │
│  ├── dolar.py     → exchange rates         │
│  ├── websearch.py → web search (ddgs)      │
│  ├── media.py     → download/convert       │
│  │                 voice & photos (ffmpeg)  │
│  ├── media_usage.py → daily media limits   │
│  └── bot.py       → PTB wiring             │
│                                            │
│  Dockerfile / docker-compose.yml           │
│  .env.example / .gitignore                 │
│  telegram-bot.service (systemd alt)        │
└────────────────────────────────────────────┘
```

### How the AI tool-calling flow works

1. User sends a message
2. Bot sends history to the model (with tool definitions)
3. Model decides: respond directly **or** call a tool
4. If it calls a tool, the bot executes it and returns the result to the model
5. The model produces the final response in Spanish

Real example: *"I need to fix the car"* → the model calls `create_project` → the bot creates the project and responds confirming it.

**Web search**: if the user asks something requiring current info (e.g., *"what's the dollar rate today?"*), the model calls the `web_search` tool → the bot searches DuckDuckGo (`ddgs` library, no API key needed) → returns results to the model → it summarizes and cites links.

## Requirements

- Python 3.12 (for local development)
- Docker + Docker Compose (for deployment)
- `ffmpeg` (converts voice notes OGG→WAV; already included in Docker image)
- An OpenCode Go account (https://opencode.ai/auth)
- A Telegram bot created with @BotFather
- Optional: Gemini API key (https://aistudio.google.com/) for voice notes, photos and `/imagina`

## Configuration

### 1. Get tokens

| Token | Where | Purpose |
|-------|-------|---------|
| `BOT_TOKEN` | @BotFather on Telegram (`/newbot`) | Connect to Telegram |
| `OPENCODE_GO_API_KEY` | https://opencode.ai/auth | Call the AI |

### 2. Create `.env`

```bash
cd telegram-bot
cp .env.example .env
nano .env   # fill in tokens and AUTHORIZED_USERS
```

### 3. Configure authorized users

```bash
# Get your ID with @userinfobot on Telegram
AUTHORIZED_USERS=111111111,222222222,333333333,444444444
```

> ⚠️ **Important**: if `AUTHORIZED_USERS` is empty or missing, the bot **denies access to everyone** (fail-secure). List the IDs of people who can use the bot.

## Running

> 🐳 **The bot always runs with Docker** (both locally and on the VPS).
> The Docker image includes `ffmpeg` (needed for voice notes) and Python
> dependencies, so you don't need to install anything on your machine
> beyond Docker + Docker Compose.

### Local development (Docker)

```bash
cd telegram-bot
cp .env.example .env   # fill in BOT_TOKEN, OPENCODE_GO_API_KEY, etc.
docker compose up -d --build

# Logs (to check it started correctly)
docker compose logs -f

# Restart after code changes
docker compose up -d --build

# Stop
docker compose down
```

> ⚠️ **Important:** don't run the local bot with the same `BOT_TOKEN` as the
> VPS while production is running — Telegram only allows one long-polling
> per token and the VPS bot "wins" (local will get 409 Conflict errors).
> **Recommended practice (already in use):** use **two Telegram bots** —
> one for development (created with @BotFather, e.g., `..._dev_bot`) for
> the local `.env` and the production one for the VPS `.env`. This way you
> can test locally without touching the production bot.

> *Alternative (for module development only, no Telegram):* the Python code
> also runs directly with `python main.py` from `.venv`, but this is **not**
> the supported path — the Docker container is the official runtime.

### Automatic deploy (CI/CD with GitHub Actions)

The repo includes a workflow (`.github/workflows/deploy.example.yml`) that deploys to the VPS on every `git push` to `main`:

1. GitHub Actions connects to the VPS via SSH with a dedicated deploy key
2. Syncs code with `rsync` (excludes `.env`, databases, `.venv`, `.opencode/`)
3. Runs `docker compose up -d --build` on the VPS
4. Cleans up old Docker images

```bash
git add .
git commit -m "my change"
git push          # → auto deploy in ~1 min
```

**VPS requirements (one-time setup):**
- Docker + Compose installed
- `.env` with real tokens in `~/telegram-bot/.env` (never touched by deploys)
- Deploy public key in `~/.ssh/authorized_keys`
- Secrets in GitHub: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PORT`, `DEPLOY_PATH`, `DEPLOY_KEY`

> The bot uses long-polling to Telegram and exposes no ports, so it **doesn't need a domain** or Cloudflare. Deploy goes directly to the VPS IP.

### Alternative: systemd (no Docker)

```bash
# Edit telegram-bot.service with your actual user
sudo cp telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot
```

## Tests (pytest)

The test suite lives in `tests/` (unit + integration with fake Telegram objects, no network). CI runs them on every push before deploy.

```bash
# Test dependencies (only once per .venv)
.venv/bin/pip install -r requirements-dev.txt

# Run all tests with coverage
.venv/bin/python -m pytest --cov=bot
```

- `--cov=bot` shows what % of `bot/` code tests execute (report per module).
- CI runs with `--cov-fail-under=80`: if coverage drops below 80%, the workflow fails and **doesn't deploy**.
- Tests don't require network or real tokens (everything mocked).

## Bot commands

| Command | Description |
|---------|-------------|
| `/iniciar` (or `/start`) | Welcome message |
| `/ayuda` | Command list |
| `/info` | Bot status, model, uptime |
| `/reiniciar` | Clear current conversation |
| `/dolar` | Today's dollar rate (official BCV and parallel USDT) |
| `/proyectos` | View all your projects |
| `/proyectos <status>` | View projects by status (`sin_iniciar`, `en_proceso`, `parado`, `completado`, `archivado`) |
| `/proyectos owners` | View only projects you own |
| `/proyectos shared` | View your projects shared with others |
| `/proyectos guest` | View projects shared with you |
| `/nuevoproyecto <name>` | Create a project |
| `/proyecto <name>` | View project details, notes, owner and shared status |
| `/estadoproyecto <name> <status> [reason]` | Change project status |
| `/prioridadproyecto <name> <priority>` | Change priority (`baja`, `media`, `alta`, `urgente`) |
| `/agregarnota <project> <text>` | Add a note to a project |
| `/buscarproyecto <term>` | Search projects (yours and shared) |
| `/compartir <project> @user` | Share a project with another user (owner only; guest is notified) |
| `/quitaracceso <project> @user` | Stop sharing a project (owner only) |
| `/eliminarproyecto <name>` | **Permanently** delete an archived project (owner only; frees space) |
| `/recordar <text>` | Schedule a reminder (date in natural language) |
| `/recordatorios` | View pending reminders (with cancel/postpone buttons) |
| `/cancelarrecordatorio <id>` | Cancel a reminder |
| `/editarrecordatorio <id> <text>` | Edit reminder message |
| `/posponerrecordatorio <id> <duration>` | Postpone a reminder (e.g., `30m`, `2h`, `1d`) |
| `/reprogramarrecordatorio <id> <date>` | Change reminder date and time |
| `/imagina <description>` | Generate an image with AI (Nano Banana by Gemini) |

You can also chat normally and the bot will automatically detect topics worthy of a project, reminder requests, or questions requiring web search.

### Projects: priorities, statuses and natural language

Projects have **priority** and **status** for tracking organization:

| Field | Valid values |
|-------|-------------|
| Priority | `baja` → `media` → `alta` → `urgente` |
| Status | `sin_iniciar` → `en_proceso` → `parado` → `completado` → `archivado` |

Important rules:

- Maximum **3 active urgent projects** per user. To create or upgrade a new one to `urgente`, first complete, archive or lower priority of existing ones. (If a project is shared, the limit counts against its **owner**, not the editor.)
- Only **archived** projects can be permanently deleted (with `/eliminarproyecto <name>`, or by natural language: *"delete project X"*). Deletion is **irreversible** (erases notes and files) and frees space; only the owner can do it. In both cases the bot asks for **button confirmation** before deleting.
- You can also use **natural language**: *"create a project to fix the car"*, *"show me my urgent projects"*, *"mark Repair bike as completed"* or *"change Trip priority to high"*.

The bot interprets the intent and executes the action without memorizing commands.

### Shared projects

A project has an **owner** (who created it) and can be shared with other authorized users:

- **`/compartir <project> @user`** — only the owner can share. The guest gets **immediate** access (sees and edits everything) and receives a bot notification.
- **`/quitaracceso <project> @user`** — only the owner can revoke access.
- **`/proyecto <name>`** shows who owns it and who it's shared with.
- **`/proyectos`** accepts filters: `owners` (what you own), `shared` (what you shared) and `guest` (what was shared with you).
- Also via **natural language**: *"share project House with @mom"*, *"who is project House shared with"*, *"stop sharing project House with @mom"*.

> 🔒 **Privacy**: you only see your projects and those explicitly shared with you. A guest **cannot** see other projects, share/revoke access (only the owner can), or self-invite. For someone to be invited, they must have talked to the bot at least once (the bot resolves the `@username` of known users).

### Telegram groups

The bot works in groups with near-total parity (see `specs/group-parity.md`):

- **Shared projects**: in a group, the bot shows projects associated with that group. When creating a project in a group, all members are automatically added as collaborators.
- **Parity**: `web_search`, `dolar_rate`, reminders and journal/memory work **in the group** (shared and isolated by `group_id`). Only `generate_image` and `set_checkin_time` remain private.
- **Group reminders**: delivered **in the group** and any member can view/edit/delete. Private ones remain isolated.
- **Group journal**: shared `memory/group_-xxx/`, signed `@user`, queryable with natural language.
- **Commands in groups**: `/proyectos`, `/proyecto`, `/nuevoproyecto`, `/recordar` and `/recordatorios` work in groups with group scope.
- **Natural language**: *"list projects"*, *"create a project for X"*, *"remind me tomorrow at 10"* works the same in groups.
- **Dissolution**: if the bot is removed from the group, **everything** for that group is deleted (projects, reminders, journal, history).

> 🔒 **Group privacy**: strict isolation by `chat_id`. You don't see projects/journal/reminders from another group or private ones in the group.

### Voice notes and photos

- **🎤 Voice notes** — send a voice note and the bot transcribes it (Gemini) and responds. The transcript enters normal conversation, so you can ask things by voice ("create a project to fix the car").
- **🖼️ Photos** — send a photo (with or without caption) and the bot analyzes it with Gemini vision and responds about its content. Photos **are processed in memory and deleted when done** — not stored on the server.
- **🎨 `/imagina`** — generates an image from a description: `/imagina an astronaut cat on the moon, pixel art style`.

> Media is always processed with Gemini (models `GEMINI_MEDIA_MODEL` / `GEMINI_IMAGE_MODEL`), even when the chat provider is `opencode`, because OpenCode Go doesn't support audio or vision. Requires `GEMINI_API_KEY` configured.

### Daily media limits

To protect the free Gemini quota, there's a **daily per-user cap** (resets each day in the bot's timezone):

| Type | Variable | Default |
|------|----------|---------|
| Images (analyzed + generated photos) | `MEDIA_IMAGE_DAILY_LIMIT` | `30` |
| Voice notes | `MEDIA_AUDIO_DAILY_LIMIT` | `30` |

When the user approaches the limit (≤3 remaining), the bot warns in the response. When reached, it rejects with a clear message until the next day. Setting `0` disables the cap.

### Reminders

They work in two ways:

1. **Normal message**: write *"remind me next week to call the mechanic"* — the AI automatically detects it and schedules the reminder. You can also **manage them by talking**: *"what reminders do I have?"*, *"cancel tomorrow's"*, *"change the call mechanic reminder"*, *"move reminder 3 to Friday at 5pm"*, *"postpone reminder 5 by two hours"*.
2. **Command**: `/recordar call the mechanic next Monday at 10am`

When the time comes, the bot sends you a message: `🔔 Reminder: call the mechanic`. Reminders persist in SQLite (survive restarts).

**Managing your reminders**: `/recordatorios` lists pending ones with **🔕 Cancel** and **⏰ Postpone 1h** buttons; there are also commands `/editarrecordatorio`, `/posponerrecordatorio`, `/reprogramarrecordatorio` and `/cancelarrecordatorio <id>`. If a delivery fails (e.g., no connection), the bot retries with increasing waits (10s, 20s, 40s...) up to a maximum of 8 hours, without losing the reminder.

**Recurring reminders**: you can also ask for repetitions — *"remind me every Monday at 8am to take medicine"*, *"every day wash the car"*, *"on the 15th of each month pay the internet"*. Frequencies: `daily`, `weekly`, `monthly`. When triggered, the bot automatically reschedules the next occurrence; cancel with `/cancelarrecordatorio <id>`.

### Long-term memory (journal)

The bot keeps a **personal journal** of your day-to-day: when you tell it something notable (meals, sleep, exercise, travel, outings, bill payments, conversations, mood, anecdotes…), it automatically saves it to the day's journal. **No new commands** — everything is handled by **natural language**:

- **Capture** — during conversation the bot saves what you share to the day's journal.
- **Summaries** — at night it compacts long days; every Monday generates a weekly summary; quarterly and yearly, period summaries (hierarchical and navigable). If the bot is off when a summary is due, **it catches up on its own** at next startup: recovers the last days, weeks, quarters and years that were pending, and your original journal is never lost.
- **Recall** — when you ask about your past (*"when was my last trip to Panama?"*, *"what did I eat last week?"*, *"when did I pay the electric bill?"*), the bot searches summaries and the journal and answers accurately.
- **Proactive** — morning check-in and evening wrap-up: it greets you, mentions pending tasks and journal patterns (exercise streaks, pending payments, absences) and reminds you of what's important. Default times are from `PROACTIVE_MORNING_TIME`/`PROACTIVE_EVENING_TIME`, but **each user can change theirs** via natural language: *"change my morning check-in to 9"* or *"move my evening check-in earlier"*.

To disable (partially or fully):

| Variable | Effect when `false` | Default |
|----------|---------------------|---------|
| `MEMORY_ENABLED` | Disables capture, summaries and memory jobs | `true` |
| `PROACTIVE_ENABLED` | Disables check-ins and proactive reminders | `true` |

> 🔒 **Privacy**: your journal is **per-user** — each authorized person has their own isolated journal. Lives in `DATA_DIR/memory/{user_id}/` inside the Docker volume (user data, never in git or code).

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BOT_TOKEN` | Telegram bot token | *(required)* |
| `OPENCODE_GO_API_KEY` | OpenCode Go API key | *(required if AI_PROVIDER=opencode)* |
| `OPENCODE_BASE_URL` | API endpoint | `https://opencode.ai/zen/go/v1` |
| `MODEL` | AI model | `mimo-v2.5` |
| `AI_PROVIDER` | Provider: `opencode` or `gemini` | `opencode` |
| `GEMINI_API_KEY` | Gemini API key (https://aistudio.google.com/) | empty |
| `GEMINI_BASE_URL` | Gemini OpenAI-compatible endpoint | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `GEMINI_MODEL` | Gemini model | `gemini-3.1-flash-lite` (most economical) |
| `GEMINI_MEDIA_MODEL` | Voice/photos model | `gemini-3.6-flash` (cheapest with audio+vision) |
| `GEMINI_IMAGE_MODEL` | `/imagina` model | `gemini-2.5-flash-image` (Nano Banana, free) |
| `MEDIA_IMAGE_DAILY_LIMIT` | Daily image cap per user | `30` |
| `MEDIA_AUDIO_DAILY_LIMIT` | Daily voice note cap per user | `30` |
| `AUTHORIZED_USERS` | Allowed Telegram IDs (comma-separated) | empty (denies all) |
| `MAX_CONTEXT_MESSAGES` | Max context messages per user | `30` |
| `RATE_LIMIT_SECONDS` | Seconds between messages per user | `5` |
| `MAX_TOOL_ROUNDS` | Max tool rounds per message (anti-loop valve; each round = 1 API call) | `12` |
| `SYSTEM_PROMPT` | AI personality (if empty uses default that understands statuses/priorities, memory and proactivity) | *(default in Spanish)* |
| `MEMORY_ENABLED` | Enables journal (capture + summaries + memory jobs) | `true` |
| `MEMORY_DAILY_COMPACT_WORDS` | Word threshold to compact a journal day | `1200` |
| `PROACTIVE_ENABLED` | Enables check-ins and proactive reminders | `true` |
| `PROACTIVE_MORNING_TIME` | Morning check-in time (HH:MM, 24h, in `TIMEZONE`) | `08:00` |
| `PROACTIVE_EVENING_TIME` | Evening check-in time (HH:MM, 24h, in `TIMEZONE`) | `20:00` |
| `PROACTIVE_MAX_DAILY` | Max proactive messages per user per day (anti-annoyance) | `3` |
| `TIMEZONE` | Timezone for reminders | `America/Caracas` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `DATA_DIR` | Data directory (Docker sets to `/app/data`) | `.` |

## Available models (cost-effective)

| Model | Use |
|-------|-----|
| `deepseek-v4-flash` | Economical — Flash |
| `mimo-v2.5` | Economical — **Default** |

> Only cost-effective models are used. Change with `MODEL=...` in `.env` and restart the container.

The AI model and provider **cannot be changed from chat** (no command or text option). They're set in `.env` (`MODEL=...`, `AI_PROVIDER=...`, `GEMINI_MODEL=...`) and applied by restarting the container.

## Data persistence

Data is stored in a **named Docker volume** (`telegram-bot_bot_data` → `/app/data`):

```
/app/data/
├── chat_history.db          ← conversation history
├── projects.db              ← project metadata (status, priority, dates,
│                              sharing, profiles), reminders and proactive_log
├── memory.db                ← journal metadata (days and summaries)
├── media_usage.db           ← daily media limits (images and voice)
├── projects/                ← readable project content
│   └── {owner_id}/{slug}/
│       ├── overview.md      ← description and status
│       ├── notes/{id}.md    ← notes
│       └── context.json     ← compact summary for AI
└── memory/                  ← journal per tenant (user or group)
    ├── {user_id}/
    │   ├── raw/YYYY-MM-DD.md     ← raw daily capture (live append)
    │   ├── days/YYYY-MM-DD.md    ← daily compact (if over threshold)
    │   └── summaries/
    │       ├── weekly/YYYY-Www.md
    │       ├── quarterly/YYYY-Q#.md
    │       └── yearly/YYYY.md
    └── group_-xxx/
        ├── raw/YYYY-MM-DD.md     ← shared journal, signed @user
        ├── days/YYYY-MM-DD.md
        └── summaries/...
```

### Backup

```bash
docker run --rm -v telegram-bot_bot_data:/data -v $(pwd):/backup alpine \
  cp -r /data /backup/backup_$(date +%F)
```

## Security

- **Secrets never in git** — `.env` is in `.gitignore`
- **Non-root container** — runs as `botuser` (UID 999)
- **Fail-secure** — without `AUTHORIZED_USERS` nobody gets in
- **Rate limiting** — 3s between messages per user (protects Go budget)
- **Go usage limits** — $12/5h, $30/week, $60/month (enough for personal use)

## OpenCode Go limits

- 💰 $12 per 5 hours | $30 per week | $60 per month
- The bot with rate limiting (3s) and limited context (30 messages) is efficient for 4 users
- A public bot would burn through the budget quickly — that's why access is restricted

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Bot doesn't respond | Check logs: `docker compose logs -f` |
| `unable to open database file` | Volume permissions: `docker compose down -v && docker compose up -d --build` |
| Unauthorized user | Check `AUTHORIZED_USERS` in `.env` |
| `Reasoning_content must be passed back` | Restart: `docker compose up -d --build` (already fixed in code) |
| Container unhealthy | `docker compose ps` → usually the healthcheck; fixed with `procps` |

## Future (roadmap)

- [ ] **MCP** — connect Notion, Gmail, Zoom (tool architecture is ready).
      Google Workspace option (Gmail/Calendar/Drive) is researched and
      documented in `docs/mcp-google-workspace.md` (implementation postponed).
