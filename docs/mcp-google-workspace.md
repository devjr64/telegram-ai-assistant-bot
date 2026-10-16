# Google MCP — Investigación y plan de integración

> **Estado**: investigación completada · Decisión: **posponer implementación** (2026-08-12)
> **Solo desarrollo** — el bot de Telegram jamás lee este archivo (ver `AGENTS.md`).

## Resumen

Objetivo a futuro: darle al bot acceso a la cuenta de Google del usuario
(**Gmail**, **Calendar**, **Drive**) a través de MCP, para que el modelo pueda
"ver" correos, agendar eventos o buscar archivos cuando el usuario se lo pida
por chat. Esto cumple el roadmap del README: *"MCP — conectar Notion, Gmail,
Zoom (la arquitectura de tools ya está lista)"*.

La integración **no se implementa todavía**: requiere OAuth manual de Google,
un proceso Node.js y medidas de seguridad (ver [Decisión](#decisión)). Este
documento guarda todo lo investigado para que la implementación futura sea
rápida.

## Contexto técnico del bot (por qué encaja MCP)

- El bot corre en **Docker, no-root, sin puertos expuestos** — solo hace
  **conexiones salientes** (long-polling a Telegram).
- MCP en modo **cliente** hace exactamente eso: conexiones HTTP salientes.
  No hay conflicto con la arquitectura.
- El bot ya tiene un loop de *function calling*: `TOOL_DEFINITIONS` en
  `bot/tools.py` + `_dispatch()` + `execute_tool()`, todo devolviendo JSON
  string al modelo. El bridge MCP encaja en este loop (ver más abajo).
- Dependencias actuales: `httpx`, `AsyncOpenAI` (openai), sin cliente MCP aún.

## El panorama de Google MCP (estado actual, agosto 2026)

> ⚠️ **`https://mcp.google.com/mcp/serve` ya NO existe / no está en el
> catálogo oficial.** Muchos tutoriales antiguos lo referencian — no usarlo.
> Tampoco usar el paquete npm `mcp-google-studio-mcp` (obsoleto).

Hay **tres** opciones oficiales actuales:

| Opción | Qué expone | Auth | Esfuerzo |
|--------|-----------|------|----------|
| **A. Google Cloud remote MCP** | Productos GCP (BigQuery, Cloud SQL, Storage...) | OAuth/IAM (`roles/mcp.toolUser`) | Medio-Alto (cuenta GCP) |
| **B. Google Workspace MCP** (open-source) | **Gmail, Calendar, Drive, Docs, Sheets, Slides, Chat, People** | OAuth de cuenta Google (manual una vez) | Medio |
| **C. Gemini MCP** (hosted por Google) | `search` (grounding Google), `code_execution`, `fetch`, `google_earth`, `mcp_notes` | API key (`X-Goog-Api-Key`) | Bajo |

Para el uso personal del bot, la opción relevante es la **B (Workspace)** — es
la que conecta Gmail/Calendar/Drive. La opción C es trivial de integrar pero
aporta poco nuevo (el bot ya tiene `web_search` con ddgs); es la buena si algún
día se quiere grounding de Google más potente o ejecución de código.

## Google Workspace MCP server (detalle)

- **Repo oficial**: https://github.com/gemini-cli-extensions/workspace
  (antes dentro de `google/mcp`; hoy es una extensión de Gemini CLI, v0.0.8).
- **Paquete npm**: `gemini-workspace-extension` (**privado**, no se instala con
  `npm i` directo; se instala como extensión de Gemini CLI:
  `gemini extensions install https://github.com/gemini-cli-extensions/workspace`).
- **Binario**: `gemini-workspace-server` (proceso Node.js, transporte **stdio**).
- **Alternativa de hosting**: el repo puede hostearse en **Cloud Run** para
  exponerlo vía **Streamable HTTP** (necesario si el bot se conecta por red).

### Tools que expone (lista completa de la doc oficial)

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

### Autenticación (OAuth)

- Requiere sesión de cuenta Google (OAuth 2.0, tokens por usuario).
- **Flujo headless para Docker** (una sola vez por cuenta):
  `npm run auth-utils -- login` → imprime una URL de OAuth → se abre en
  cualquier navegador (máquina local o teléfono) → se pega el JSON de
  credenciales en la CLI. Las credenciales **nunca se exponen al modelo**.
- Los tokens se persisten (vía `keytar`/credential storage).

### ⚠️ Seguridad (advertencia de Google en el propio README)

- El server puede **leer, modificar y borrar** datos reales de la cuenta.
- Riesgo de **prompt injection indirecto**: no meter al contexto del modelo
  contenido no confiable (correos, docs, archivos de fuentes no verificadas)
  mientras haya tools de escritura conectadas. Revisar las acciones del agente.
- En el bot: todo acceso MCP debe quedar detrás del gate de `AUTHORIZED_USERS`
  (como el resto del bot) y **empezar con un subconjunto seguro de tools**
  (lectura + `gmail.send`/`calendar.createEvent`), dejando fuera las destructivas
  (`gmail.modify`, `drive.trashFile`, borrados) en una primera fase.

## Cómo se integraría en el bot (plan técnico)

Punto de integración: **el bot actúa como cliente MCP** y "traduce" las tools
del server al formato function-calling de OpenAI que ya usa el modelo.

1. **Dependencia**: `mcp>=2.0,<3` (SDK Python oficial, async, sobre `httpx2`).
   El SDK v2 conecta a servidores remotos (Streamable HTTP) y a subprocesos
   stdio. Alternativa sin dependencia nueva: implementar el JSON-RPC 2.0 de
   MCP a mano con el `httpx` existente (más trabajo).
2. **Módulo nuevo `bot/mcp_client.py`**: conecta al server, hace
   `list_tools()`, mantiene la sesión viva entre mensajes, y expone
   `call_tool(name, args)`.
3. **Bridge en `bot/tools.py`**: cada tool MCP ya trae su JSON Schema
   (`inputSchema`) — se mapea 1:1 a OpenAI function format con prefijo de
   servidor para evitar colisiones, y se **anexa a `TOOL_DEFINITIONS`**:
   ```python
   def to_openai_tool(mcp_tool, server_name: str) -> dict:
       return {
           "type": "function",
           "function": {
               "name": f"{server_name}__{mcp_tool.name}",
               "description": mcp_tool.description or "",
               "parameters": mcp_tool.inputSchema,  # JSON Schema ya
           },
       }
   ```
4. **Dispatch en `_dispatch()`**: si el nombre empieza con `server__`, rutear a
   `client.call_tool(...)` y devolver JSON string:
   ```python
   result = await mcp_sessions[server].call_tool(name, args)
   if result.is_error:
       return json.dumps({"ok": False, "error": str(result.structured_content)})
   return json.dumps({"ok": True, "result": result.structured_content})
   ```
5. **Config** (`bot/config.py` + `.env.example`): URL del server, qué services
   habilitar, credenciales OAuth.
6. **Docker**: ver decisión de despliegue abajo.

### Decisiones de despliegue (evaluadas)

El server es un proceso **Node.js (stdio)**. Tres formas de integrarlo:

| Opción | Cómo | Pros | Contras |
|--------|------|------|---------|
| **A. Node en el contenedor del bot** | Instalar Node en el `Dockerfile`; el bot lanza el server como subproceso stdio | Todo en un contenedor, simple | Añade Node.js; credenciales vía archivo/volumen |
| **B. Servicio separado en docker-compose** | Contenedor Node aparte exponiendo el server vía Streamable HTTP | Separación limpia, el bot sigue sin Node | Más piezas; envolver stdio→HTTP (adaptador) |
| **C. Hostear en Cloud Run** | El server en Cloud Run (HTTP), el bot se conecta remoto | Bot sin Node, escalable | Requiere cuenta GCP + otro deploy |

**Recomendación para un bot personal en VPS: opción A.**

### Timeouts y límites a respetar

- Llamadas MCP tipo `code_execution`/búsquedas pueden tardar 30–300 s → usar
  timeouts de lectura largos (`httpx2.Timeout(30.0, read=300.0)`) y mantener
  todo async (nunca bloquear el event loop).
- Reutilizar el rate limiter existente (3 s/mensaje/usuario) también para
  operaciones MCP pesadas.

## Decisión

**Fecha**: 2026-08-12 · **Estado**: pospuesto · **Razón**: complejidad vs. valor actual

Se evaluó implementar la opción B (Google Workspace) en la rama
`chore/mcp-config`. Se decidió **no implementar por ahora** porque:

- Requiere **OAuth manual de Google** (flujo headless una vez por cuenta) y
  **Node.js** en el entorno Docker (o un servicio/hosting extra).
- Añade una **dependencia nueva** (`mcp` SDK, `httpx2`) y cambios en
  Dockerfile/compose, `config.py`, `tools.py`, `handlers.py`.
- Implica **riesgo de seguridad** (tools de escritura sobre datos reales de
  Google + prompt injection) que hay que diseñar con cuidado.
- El valor actual del bot (proyectos, recordatorios, dólar, web, medios) no
  exige aún acceso a Gmail/Calendar/Drive.

**Alternativa ligera futura**: la opción C (Gemini MCP, solo API key) podría
integrarse en horas si algún día se quiere grounding de Google/code_execution
sin tocar OAuth ni Node.

## Plan de implementación futura (checklist)

Cuando se decida retomar:

- [ ] Definir qué services y qué tools se exponen (empezar por lectura +
      `gmail.send` + `calendar.createEvent`).
- [ ] Crear `bot/mcp_client.py` (cliente MCP v2 + sesión viva).
- [ ] Añadir `mcp>=2.0,<3` a `requirements.txt`.
- [ ] Bridge en `bot/tools.py` (`to_openai_tool` + prefijo + dispatch).
- [ ] Config en `bot/config.py` + `.env.example`.
- [ ] Elegir despliegue (recomendado: **opción A**) y ajustar `Dockerfile`.
- [ ] Flujo OAuth headless con el usuario (una vez) y guardar credenciales
      fuera del contenedor/volumen.
- [ ] Tests con el cliente MCP mockeado (sin red, patrón de `tests/`).
- [ ] Revisión de seguridad (gate `AUTHORIZED_USERS`, subconjunto seguro,
      alertas de prompt injection).

## Referencias

- Google Workspace MCP: https://github.com/gemini-cli-extensions/workspace
- MCP Python SDK (v2): https://py.sdk.modelcontextprotocol.io/
- Google Cloud MCP (overview): https://docs.cloud.google.com/mcp/overview
- Gemini MCP: https://mcp.ai.google.dev
- Documentos de investigación crudos (contexto externo, temporales):
  `.tmp/external-context/google-mcp/`, `.tmp/external-context/gemini-mcp/`,
  `.tmp/external-context/mcp-python-sdk/`
