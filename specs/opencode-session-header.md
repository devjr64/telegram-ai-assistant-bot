# Spec: OpenCode Go Session Header (`x-opencode-session`)

**Status:** Implemented
**Date:** 2026-09-07
**Objective:** Add the `x-opencode-session` header and a custom `User-Agent` to all requests to the OpenCode Go API, as required by the API since September 2026.

---

## 1. Context and problem

On 2026-09-07 the bot stopped responding to all messages. The logs showed:

```
ERROR bot.handlers: AI request failed for user 1234567890: API request failed:
Error code: 400 - {'type': 'error', 'error': {'type': 'MissingSessionID',
'message': 'Error from provider (Console Go): Request is missing x-opencode-session
and cannot be routed efficiently. Please see https://opencode.ai/docs/go/#where-can-i-use-it'}}
```

The error affected **all** API calls:
- User chats → the bot receives the message but cannot respond
- Auto-journal (background) → fails silently every hour
- Proactive check-ins → fail to generate messages
- Summaries → fail to compact

### OpenCode Go requirement

According to the [official documentation](https://opencode.ai/docs/go/#where-can-i-use-it), clients must:

1. Send typical code agent traffic
2. Identify themselves with a custom User-Agent (not the generic HTTP SDK one)
3. Send a **stable session ID** in `x-opencode-session` per conversation, to optimize routing and prompt caching

The bot was using the `openai` SDK (AsyncOpenAI) without these headers, so all requests were rejected with 400.

---

## 2. Solution

### 2.1 Strategy: `extra_headers` per request

The Python `openai` SDK supports `extra_headers` on each `.create()` call. This allows:

- **Keeping the shared client** in `bot_data["ai_client"]` (a single httpx connection pool)
- **Passing `session_id`** through the function chain
- **Injecting headers** only on requests to the OpenCode provider (does not affect Gemini)

**Why not create a new client per request:**
Each `AsyncOpenAI()` creates an `httpx.AsyncClient` with its own connection pool. Creating one per message would be slow and waste resources. The shared client is the correct architecture.

### 2.2 Session IDs by context

| Context | Session ID | Rationale |
|----------|-----------|-----------|
| User chat | `tg-user-{user_id}` | Stable per user → good prompt caching |
| Media (voice/photo) | `tg-media-{user_id}` | Separate routing for multimodal |
| Auto-journal | `tg-auto-journal` | Background job, single session |
| Proactive check-ins | `tg-proactive-{user_id}` | Proactive per user |
| Image generation | `tg-image-{user_id}` | Separate routing for image gen |
| Summaries | `tg-summary-{user_id}` | Background summaries |

### 2.3 User-Agent

All requests include `User-Agent: telegram-ai-bot/1.0` to identify as an agent, not as a generic SDK.

---

## 3. Code changes

### 3.1 `bot/ai_client.py`

Functions modified to accept `session_id: str | None = None`:

- `chat_completion()`
- `chat_completion_with_tools()`
- `transcribe_audio()`
- `describe_image()`
- `generate_image()`
- `parse_reminder_datetime()`
- `_chat_request_with_retry()`

When `session_id` is not None and the provider is `opencode`, the following are added:
```python
extra_headers={
    "x-opencode-session": session_id,
    "User-Agent": "telegram-ai-bot/1.0",
}
```

### 3.2 `bot/handlers.py`

- `_run_ai_tool_loop()` receives `session_id` and passes it to `chat_completion_with_tools()`
- `_media_processor()` creates session ID `tg-media-{user_id}` for transcription/analysis
- Voice/photo/image handlers pass appropriate session IDs

### 3.3 `bot/auto_journal.py`

Three call sites use `session_id="tg-auto-journal"`:
- `_extract_entities_from_messages()`
- `_extract_combined_from_messages()`
- `_extract_events_from_messages()`

### 3.4 `bot/proactive.py`

- `build_morning_message()` and `build_evening_message()` receive `session_id=f"tg-proactive-{user_id}"`
- `_generate_proactive_message()` passes the session ID to `chat_completion()`

### 3.5 `bot/tools.py`

- `_dispatch_generate_image()` uses `session_id=f"tg-image-{user_id}"`

### 3.6 `bot/summarizer.py`

- `_call_model()` receives `session_id` and passes it to `chat_completion()`
- Summary generation functions build `session_id=f"tg-summary-{user_id}"`

---

## 4. Tests

### 4.1 New tests in `tests/test_ai_client.py`

- `test_session_header_sent_to_opencode`: verifies that `x-opencode-session` is sent when `session_id` is not None
- `test_no_session_header_for_gemini`: verifies that the header is NOT sent for the Gemini provider
- `test_user_agent_header`: verifies that `User-Agent: telegram-ai-bot/1.0` is sent

### 4.2 Existing tests

All existing tests must continue to pass without changes. The `session_id` parameter is optional (default None), so behavior without a session ID is preserved.

---

## 5. Post-deploy verification

After deploying, verify in the VPS logs:

```bash
docker compose logs -f | grep -E "(HTTP Request|ERROR)"
```

- **OK**: `HTTP/1.1 200 OK` on POST requests
- **FAIL**: `HTTP/1.1 400 Bad Request` with `MissingSessionID`

---

## 6. References

- [OpenCode Go Docs — Where can I use it?](https://opencode.ai/docs/go/#where-can-i-use-it)
- [OpenAI Python SDK — extra_headers](https://github.com/openai/openai-python#configuring-the-http-client)
- Issue detected: 2026-09-07 16:36 UTC
