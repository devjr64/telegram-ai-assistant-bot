# Google MCP — Research and integration plan

> **Status**: research completed · Decision: **postpone implementation** (2026-08-12)
> **Development only** — the Telegram bot never reads this file (see `AGENTS.md`).

## Summary

Future goal: give the bot access to the user's Google account
(**Gmail**, **Calendar**, **Drive**) through MCP, so the model can
"see" emails, schedule events, or search for files when the user asks
via chat. This fulfills the README roadmap: *"MCP — connect Notion, Gmail,
Zoom (the tools architecture is already ready)"*.

The integration **is not implemented yet**: it requires manual Google OAuth,
a Node.js process, and security measures (see [Decision](#decisión)). This
document saves all the research so that future implementation is fast.

## Bot technical context (why MCP fits)

- The bot runs in **Docker, non-root, with no exposed ports** — it only makes
  **outgoing connections** (long-polling to Telegram).
- MCP in **client** mode does exactly that: outgoing HTTP connections.
  There is no conflict with the architecture.
- The bot already has a *function calling* loop: `TOOL_DEFINITIONS` in
  `bot/tools.py` + `_dispatch()` + `execute_tool()`, all returning JSON
  string to the model. The MCP bridge fits into this loop (see below).
- Current dependencies: `httpx`, `AsyncOpenAI` (openai), no MCP client yet.

## The Google MCP landscape (current state, August 2026)

> ⚠️ **`https://mcp.google.com/mcp/serve` no longer exists / is not in the
> official catalog.** Many old tutorials reference it — do not use it.
> Also do not use the npm package `mcp-google-studio-mcp` (obsolete).

There are **three** current official options:

| Option | What it exposes | Auth | Effort |
|--------|----------------|------|--------|
| **A. Google Cloud remote MCP** | GCP products (BigQuery, Cloud SQL, Storage...) | OAuth/IAM (`roles/mcp.toolUser`) | Medium-High (GCP account) |
| **B. Google Workspace MCP** (open-source) | **Gmail, Calendar, Drive, Docs, Sheets, Slides, Chat, People** | Google account OAuth (manual one-time) | Medium |
| **C. Gemini MCP** (hosted by Google) | `search` (Google grounding), `code_execution`, `fetch`, `google_earth`, `mcp_notes` | API key (`X-Goog-Api-Key`) | Low |

For the bot's personal use, the relevant option is **B (Workspace)** — it's
the one that connects Gmail/Calendar/Drive. Option C is trivial to integrate
but adds little new (the bot already has `web_search` with ddgs); it's the
good one if someday you want more powerful Google grounding or code execution.

## Google Workspace MCP server (details)

- **Official repo**: https://github.com/gemini-cli-extensions/workspace
  (previously inside `google/mcp`; today it's a Gemini CLI extension, v0.0.8).
- **npm package**: `gemini-workspace-extension` (**private**, not installed
  with `npm i` directly; installed as a Gemini CLI extension:
  `gemini extensions install https://github.com/gemini-cli-extensions/workspace`).
- **Binary**: `gemini-workspace-server` (Node.js process, **stdio** transport).
- **Hosting alternative**: the repo can be hosted on **Cloud Run** to
  expose it via **Streamable HTTP** (necessary if the bot connects over the network).

### Tools it exposes (complete list from official docs)

- **Auth**: `auth.clear`, `auth.refreshToken`
- **Gmail**: `gmail.search`, `gmail.get`, `gmail.downloadAttachment`,
  `gmail.modify`, `gmail.batchModify`, `gmail.modifyThread`, `gmail.send`,
  `gmail.createDraft`, `gmail.sendDraft`, `gmail.listLabels`, `gmail.createLabel`
- **Calendar**: `calendar.list`, `calendar.createEvent`, `calendar.listEvents`,
  `calendar.getEvent`, `calendar.findFreeTime`, `calendar.updateEvent`,
  `calendar.respondToEvent`, `calendar.deleteEvent`
- **Drive**: `drive.search`, `drive.findFolder`, `drive.createFolder`,
  `drive.downloadFile`, `drive.trashFile`, `drive.renameFile`
- **Docs**: `docs.create`, `docs.getSuggestions`, `docs.getComments`,
  `docs.writeText`, `docs.getText`, `docs.replaceText`, `docs.formatText`
- **Sheets**: `sheets.getText`, `sheets.getRange`, `sheets.getMetadata`
- **Slides**: `slides.getText`, `slides.getMetadata`, `slides.getImages`,
  `slides.getSlideThumbnail`
- **Chat**: `chat.listSpaces`, `chat.sendMessage`, `chat.getMessages`,
  `chat.sendDm`, `chat.listThreads`, `chat.setUpSpace`, ...
- **Time**: `time.getCurrentDate`, `time.getCurrentTime`, `time.getTimeZone`
- **People**: `people.getUserProfile`, `people.getMe`, `people.getUserRelations`

### Authentication (OAuth)

- Requires Google account session (OAuth 2.0, per-user tokens).
- **Headless flow for Docker** (one-time per account):
  `npm run auth-utils -- login` → prints an OAuth URL → opens in
  any browser (local machine or phone) → paste the credentials JSON
  into the CLI. Credentials **are never exposed to the model**.
- Tokens are persisted (via `keytar`/credential storage).

### ⚠️ Security (Google's warning in the README itself)

- The server can **read, modify and delete** real account data.
- Risk of **indirect prompt injection**: don't put untrusted content
  (emails, docs, files from unverified sources) into the model context
  while writing tools are connected. Review the agent's actions.
- In the bot: all MCP access must be behind the `AUTHORIZED_USERS` gate
  (like the rest of the bot) and **start with a safe subset of tools**
  (read + `gmail.send`/`calendar.createEvent`), leaving out the destructive
  ones (`gmail.modify`, `drive.trashFile`, deletions) in a first phase.

## How it would be integrated into the bot (technical plan)

Integration point: **the bot acts as an MCP client** and "translates" the
server tools to the OpenAI function-calling format the model already uses.

1. **Dependency**: `mcp>=2.0,<3` (official Python SDK, async, over `httpx2`).
   The v2 SDK connects to remote servers (Streamable HTTP) and to stdio
   subprocesses. Alternative without new dependency: implement MCP's
   JSON-RPC 2.0 by hand with the existing `httpx` (more work).
2. **New module `bot/mcp_client.py`**: connects to the server, does
   `list_tools()`, keeps the session alive between messages, and exposes
   `call_tool(name, args)`.
3. **Bridge in `bot/tools.py`**: each MCP tool already comes with its
   JSON Schema (`inputSchema`) — it maps 1:1 to OpenAI function format
   with server prefix to avoid collisions, and is **appended to `TOOL_DEFINITIONS`**:
   ```python
   def to_openai_tool(mcp_tool, server_name: str) -> dict:
       return {
           "type": "function",
           "function": {
               "name": f"{server_name}__{mcp_tool.name}",
               "description": mcp_tool.description or "",
               "parameters": mcp_tool.inputSchema,  # JSON Schema already
           },
       }
   ```
4. **Dispatch in `_dispatch()`**: if the name starts with `server__`, route to
   `client.call_tool(...)` and return JSON string:
   ```python
   result = await mcp_sessions[server].call_tool(name, args)
   if result.is_error:
       return json.dumps({"ok": False, "error": str(result.structured_content)})
   return json.dumps({"ok": True, "result": result.structured_content})
   ```
5. **Config** (`bot/config.py` + `.env.example`): server URL, which services
   to enable, OAuth credentials.
6. **Docker**: see deployment decision below.

### Deployment decisions (evaluated)

The server is a **Node.js (stdio)** process. Three ways to integrate it:

| Option | How | Pros | Cons |
|--------|-----|------|------|
| **A. Node in the bot container** | Install Node in the `Dockerfile`; the bot launches the server as a stdio subprocess | Everything in one container, simple | Adds Node.js; credentials via file/volume |
| **B. Separate service in docker-compose** | Separate Node container exposing the server via Streamable HTTP | Clean separation, bot remains without Node | More pieces; wrap stdio→HTTP (adapter) |
| **C. Host on Cloud Run** | The server on Cloud Run (HTTP), the bot connects remotely | Bot without Node, scalable | Requires GCP account + another deploy |

**Recommendation for a personal bot on VPS: option A.**

### Timeouts and limits to respect

- MCP calls like `code_execution`/searches can take 30–300 s → use
  long read timeouts (`httpx2.Timeout(30.0, read=300.0)`) and keep
  everything async (never block the event loop).
- Reuse the existing rate limiter (3 s/message/user) also for
  heavy MCP operations.

## Decision

**Date**: 2026-08-12 · **Status**: postponed · **Reason**: complexity vs. current value

Implementing option B (Google Workspace) in the branch
`chore/mcp-config` was evaluated. It was decided **not to implement for now** because:

- It requires **manual Google OAuth** (headless flow one-time per account) and
  **Node.js** in the Docker environment (or an extra service/hosting).
- It adds a **new dependency** (`mcp` SDK, `httpx2`) and changes in
  Dockerfile/compose, `config.py`, `tools.py`, `handlers.py`.
- It implies **security risk** (writing tools over real Google data
  + prompt injection) that must be carefully designed.
- The bot's current value (projects, reminders, dollar, web, media) does
  not yet require access to Gmail/Calendar/Drive.

**Lightweight future alternative**: option C (Gemini MCP, API key only) could
be integrated in hours if someday you want Google grounding/code_execution
without touching OAuth or Node.

## Future implementation plan (checklist)

When it's decided to resume:

- [ ] Define which services and tools to expose (start with read +
      `gmail.send` + `calendar.createEvent`).
- [ ] Create `bot/mcp_client.py` (MCP v2 client + live session).
- [ ] Add `mcp>=2.0,<3` to `requirements.txt`.
- [ ] Bridge in `bot/tools.py` (`to_openai_tool` + prefix + dispatch).
- [ ] Config in `bot/config.py` + `.env.example`.
- [ ] Choose deployment (recommended: **option A**) and adjust `Dockerfile`.
- [ ] Headless OAuth flow with the user (one-time) and save credentials
      outside the container/volume.
- [ ] Tests with mocked MCP client (no network, pattern from `tests/`).
- [ ] Security review (`AUTHORIZED_USERS` gate, safe subset,
      prompt injection alerts).

## References

- Google Workspace MCP: https://github.com/gemini-cli-extensions/workspace
- MCP Python SDK (v2): https://py.sdk.modelcontextprotocol.io/
- Google Cloud MCP (overview): https://docs.cloud.google.com/mcp/overview
- Gemini MCP: https://mcp.ai.google.dev
- Raw research documents (external context, temporary):
  `.tmp/external-context/google-mcp/`, `.tmp/external-context/gemini-mcp/`,
  `.tmp/external-context/mcp-python-sdk/`