# Spec: OpenCode Go Session Header (`x-opencode-session`)

**Estado:** Implementado
**Fecha:** 2026-09-07
**Objetivo:** Añadir el header `x-opencode-session` y un `User-Agent` propio a todas las peticiones al API de OpenCode Go, tal como requiere la API desde septiembre 2026.

---

## 1. Contexto y problema

El 2026-09-07 el bot dejó de responder a todos los mensajes. Los logs mostraban:

```
ERROR bot.handlers: AI request failed for user 1234567890: API request failed:
Error code: 400 - {'type': 'error', 'error': {'type': 'MissingSessionID',
'message': 'Error from provider (Console Go): Request is missing x-opencode-session
and cannot be routed efficiently. Please see https://opencode.ai/docs/go/#where-can-i-use-it'}}
```

El error afectaba **todas** las llamadas al API:
- Chats de usuario → el bot recibe el mensaje pero no puede responder
- Auto-journal (background) → falla silenciosamente cada hora
- Proactive check-ins → fallan al generar mensajes
- Sumarios → fallan al compactar

### Requisito de OpenCode Go

Según la [documentación oficial](https://opencode.ai/docs/go/#where-can-i-use-it), los clientes deben:

1. Enviar tráfico típico de agente de código
2. Identificarse con un User-Agent propio (no el genérico del SDK HTTP)
3. Enviar un **session ID estable** en `x-opencode-session` por conversación, para optimizar routing y prompt caching

El bot usaba el SDK `openai` (AsyncOpenAI) sin estos headers, por lo que todas las peticiones eran rechazadas con 400.

---

## 2. Solución

### 2.1 Estrategia: `extra_headers` por petición

El SDK `openai` Python soporta `extra_headers` en cada llamada a `.create()`. Esto permite:

- **Mantener el cliente compartido** en `bot_data["ai_client"]` (un solo pool de conexiones httpx)
- **Pasar `session_id`** a través de la cadena de funciones
- **Inyectar headers** solo en las peticiones al provider OpenCode (no afecta a Gemini)

**Por qué no crear un cliente nuevo por petición:**
Cada `AsyncOpenAI()` crea un `httpx.AsyncClient` con su propio pool de conexiones. Crear uno por mensaje sería lento y desperdiciaría recursos. El cliente compartido es la arquitectura correcta.

### 2.2 Session IDs por contexto

| Contexto | Session ID | Rationale |
|----------|-----------|-----------|
| Chat de usuario | `tg-user-{user_id}` | Estable por usuario → buen prompt caching |
| Media (voz/foto) | `tg-media-{user_id}` | Routing separado para multimodal |
| Auto-journal | `tg-auto-journal` | Background job, sesión única |
| Proactive check-ins | `tg-proactive-{user_id}` | Proactivo por usuario |
| Generación de imagen | `tg-image-{user_id}` | Routing separado para image gen |
| Sumarios | `tg-summary-{user_id}` | Background summaries |

### 2.3 User-Agent

Todas las peticiones incluyen `User-Agent: telegram-ai-bot/1.0` para identificarse como agente, no como SDK genérico.

---

## 3. Cambios en código

### 3.1 `bot/ai_client.py`

Funciones modificadas para aceptar `session_id: str | None = None`:

- `chat_completion()`
- `chat_completion_with_tools()`
- `transcribe_audio()`
- `describe_image()`
- `generate_image()`
- `parse_reminder_datetime()`
- `_chat_request_with_retry()`

Cuando `session_id` no es None y el provider es `opencode`, se añaden:
```python
extra_headers={
    "x-opencode-session": session_id,
    "User-Agent": "telegram-ai-bot/1.0",
}
```

### 3.2 `bot/handlers.py`

- `_run_ai_tool_loop()` recibe `session_id` y lo pasa a `chat_completion_with_tools()`
- `_media_processor()` crea session ID `tg-media-{user_id}` para transcripción/análisis
- Handlers de voz/foto/imagen pasan session IDs apropiados

### 3.3 `bot/auto_journal.py`

Tres call sites usan `session_id="tg-auto-journal"`:
- `_extract_entities_from_messages()`
- `_extract_combined_from_messages()`
- `_extract_events_from_messages()`

### 3.4 `bot/proactive.py`

- `build_morning_message()` y `build_evening_message()` reciben `session_id=f"tg-proactive-{user_id}"`
- `_generate_proactive_message()` pasa el session ID a `chat_completion()`

### 3.5 `bot/tools.py`

- `_dispatch_generate_image()` usa `session_id=f"tg-image-{user_id}"`

### 3.6 `bot/summarizer.py`

- `_call_model()` recibe `session_id` y lo pasa a `chat_completion()`
- Las funciones de generación de sumarios construyen `session_id=f"tg-summary-{user_id}"`

---

## 4. Tests

### 4.1 Nuevos tests en `tests/test_ai_client.py`

- `test_session_header_sent_to_opencode`: verifica que `x-opencode-session` se envía cuando `session_id` no es None
- `test_no_session_header_for_gemini`: verifica que el header NO se envía para provider Gemini
- `test_user_agent_header`: verifica que `User-Agent: telegram-ai-bot/1.0` se envía

### 4.2 Tests existentes

Todos los tests existentes deben seguir pasando sin cambios. El parámetro `session_id` es opcional (default None), por lo que el comportamiento sin session ID se mantiene.

---

## 5. Verificación post-deploy

Después de desplegar, verificar en los logs del VPS:

```bash
docker compose logs -f | grep -E "(HTTP Request|ERROR)"
```

- **OK**: `HTTP/1.1 200 OK` en las peticiones POST
- **FAIL**: `HTTP/1.1 400 Bad Request` con `MissingSessionID`

---

## 6. Referencias

- [OpenCode Go Docs — Where can I use it?](https://opencode.ai/docs/go/#where-can-i-use-it)
- [OpenAI Python SDK — extra_headers](https://github.com/openai/openai-python#configuring-the-http-client)
- Issue detectado: 2026-09-07 16:36 UTC
