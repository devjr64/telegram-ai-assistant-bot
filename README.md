# Telegram AI Assistant Bot

![CI](https://github.com/devjr64/telegram-ai-assistant-bot/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Bot de Telegram con IA para uso personal y familiar (Venezuela 🇻🇪). Conversa en español, lleva registro de **proyectos** con memoria a largo plazo, y solo permite acceso a usuarios autorizados.

> 🤖 **Proyecto personal en producción 24/7** en un VPS (instancia privada).

## Características

- 🤖 **Chat con IA** — responde en español vía OpenCode Go (`mimo-v2.5` por defecto, económico)
- 🎤 **Notas de voz** — las transcribe con Gemini (gratis) y responde; el transcript entra a la conversación normal
- 🖼️ **Fotos** — las analiza con visión de Gemini y conversa sobre su contenido
- 🎨 **`/imagina`** — genera imágenes con IA (Nano Banana de Gemini, gratis)
- 🌐 **Búsqueda web** — consulta internet automáticamente cuando la respuesta requiere información actual (noticias, precios, datos recientes) y cita las fuentes
- 💵 **Tasa del dólar** — con `/dolar` (o preguntando) obtienes la tasa oficial BCV y la paralela USDT (Binance P2P)
- 📁 **Proyectos** — crea, busca, actualiza y da seguimiento a temas recurrentes (ej: "arreglar el carro")
- 👥 **Grupos** — usa el bot en grupos de Telegram para proyectos compartidos (privacidad estricta)
- 🔔 **Recordatorios** — pídele que te recuerde algo en el futuro, incluso **recurrentes** ("todos los lunes", "cada mes")
- 🧠 **Memoria** — recuerda proyectos entre conversaciones usando SQLite + archivos Markdown
- 📓 **Bitácora** — guarda tu día a día automáticamente y responde sobre tu pasado con resúmenes jerárquicos
- 🔄 **Auto-journal** — revisa el historial cada 60 min (threshold 6, 1 call) y guarda eventos importantes sin que tengas que pedirlo
- 🏷️ **Tracking de entidades** — registra personas, lugares, casos y documentos para dar seguimiento
- ☀️ **Proactividad** — check-ins matutinos y vespertinos que te recuerdan pendientes y patrones de tu bitácora
- 🔒 **Acceso restringido** — solo usuarios autorizados (fail-secure)
- 🐳 **Docker** — mismo contenedor funciona en tu máquina local y en el VPS
- 💾 **Persistencia** — datos guardados en volumen Docker, sobreviven reinicios y rebuilds

## Arquitectura

```
┌────────────────────────────────────────────┐
│  telegram-bot/                            │
│                                            │
│  main.py          → entry point            │
│  bot/                                     │
│  ├── config.py    → settings (.env)        │
│  ├── access.py    → control de acceso      │
│  ├── ai_client.py → cliente OpenCode Go    │
│  ├── handlers.py  → comandos + chat + tools│
│  ├── project_commands.py → comandos proy.  │
│  ├── projects.py  → CRUD + compartir + .md │
│  ├── reminders.py → recordatorios SQLite   │
│  ├── history.py   → historial de chat      │
│  ├── auto_journal.py → auto-guardado       │
│  ├── entities.py  → tracking entidades     │
│  ├── memory.py    → bitácora SQLite + .md  │
│  ├── summarizer.py → resúmenes jerárquicos │
│  ├── proactive.py → check-ins + jobs       │
│  ├── tools.py     → definiciones de tools  │
│  ├── dolar.py     → tasas dólar (BCV+USDT) │
│  ├── websearch.py → búsqueda web (ddgs)    │
│  ├── media.py     → descarga/convierte     │
│  │                 voz y fotos (ffmpeg+PIL)│
│  ├── media_usage.py → caps diarios media   │
│  └── bot.py       → wiring de PTB          │
│                                            │
│  Dockerfile / docker-compose.yml           │
│  .env.example / .gitignore                 │
│  telegram-bot.service (systemd alternativo)│
└────────────────────────────────────────────┘
```

### Cómo funciona el flujo de IA con herramientas

1. El usuario envía un mensaje
2. El bot envía el historial al modelo (con definiciones de herramientas)
3. El modelo decide: responder directamente **o** llamar una herramienta
4. Si llama una herramienta, el bot la ejecuta y devuelve el resultado al modelo
5. El modelo produce la respuesta final en español

Ejemplo real: *"Tengo que arreglar el carro"* → el modelo llama `create_project` → el bot crea el proyecto y responde confirmándolo.

**Búsqueda web**: si el usuario pregunta algo que requiere información actual (ej: *"¿cuánto está el dólar hoy?"*), el modelo llama la tool `web_search` → el bot busca en DuckDuckGo (librería `ddgs`, sin API key) → devuelve los resultados al modelo → este resume y cita los enlaces.

## Requisitos

- Python 3.12 (para desarrollo local)
- Docker + Docker Compose (para despliegue)
- `ffmpeg` (convierte notas de voz OGG→WAV; ya incluido en la imagen Docker)
- Una cuenta en OpenCode Go (https://opencode.ai/auth)
- Un bot de Telegram creado con @BotFather
- Opcional: API key de Gemini (https://aistudio.google.com/) para notas de voz, fotos y `/imagina`

## Configuración

### 1. Obtener tokens

| Token | Dónde | Para qué |
|-------|-------|----------|
| `BOT_TOKEN` | @BotFather en Telegram (`/newbot`) | Conectar Telegram |
| `OPENCODE_GO_API_KEY` | https://opencode.ai/auth | Llamar a la IA |

### 2. Crear `.env`

```bash
cd telegram-bot
cp .env.example .env
nano .env   # completar tokens y AUTHORIZED_USERS
```

### 3. Configurar usuarios autorizados

```bash
# Obtén tu ID con @userinfobot en Telegram
AUTHORIZED_USERS=111111111,222222222,333333333,444444444
```

> ⚠️ **Importante**: si `AUTHORIZED_USERS` está vacío o ausente, el bot **niega el acceso a todos** (fail-secure). Lista ahí los IDs de las personas que podrán usar el bot.

## Ejecución

> 🐳 **El bot siempre se ejecuta con Docker** (tanto en local como en el VPS).
> La imagen Docker incluye `ffmpeg` (necesario para las notas de voz) y las
> dependencias Python, por lo que no necesitas instalar nada en tu máquina
> más allá de Docker + Docker Compose.

### Desarrollo local (Docker)

```bash
cd telegram-bot
cp .env.example .env   # completar BOT_TOKEN, OPENCODE_GO_API_KEY, etc.
docker compose up -d --build

# Logs (para ver si arrancó bien)
docker compose logs -f

# Reiniciar tras cambios de código
docker compose up -d --build

# Detener
docker compose down
```

> ⚠️ **Importante:** no levantes el bot local con el mismo `BOT_TOKEN` que el
> del VPS mientras producción esté corriendo — Telegram solo permite un
> long-polling por token y el bot en el VPS "gana" (el local recibirá errores
> 409 Conflict). **Práctica recomendada (ya en uso):** usar **dos bots de
> Telegram** — uno de desarrollo (creado con @BotFather, p. ej. `..._dev_bot`)
> para el `.env` local y el de producción para el `.env` del VPS. Así puedes
> probar en local sin tocar el bot de producción.

> *Alternativa (solo desarrollo de módulos, sin Telegram):* el código Python
> también corre directo con `python main.py` desde `.venv`, pero **no** es la
> vía soportada — el contenedor Docker es el entorno de ejecución oficial.

### Deploy automático (CI/CD con GitHub Actions)

El repo incluye un workflow (`.github/workflows/deploy.example.yml`) que despliega al VPS en cada `git push` a `main`:

1. GitHub Actions conecta al VPS por SSH con una clave de deploy dedicada
2. Sincroniza el código con `rsync` (excluye `.env`, bases de datos, `.venv`, `.opencode/`)
3. Ejecuta `docker compose up -d --build` en el VPS
4. Limpia imágenes Docker antiguas

```bash
git add .
git commit -m "mi cambio"
git push          # → deploy automático en ~1 min
```

**Requisitos en el VPS (una sola vez):**
- Docker + Compose instalados
- `.env` con los tokens reales en `~/telegram-bot/.env` (jamás se toca en deploys)
- Clave pública de deploy en `~/.ssh/authorized_keys`
- Secrets en GitHub: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PORT`, `DEPLOY_PATH`, `DEPLOY_KEY`

> El bot usa long-polling hacia Telegram y no expone puertos, por lo que **no necesita dominio** ni Cloudflare. El deploy va directo a la IP del VPS.

### Alternativa systemd (sin Docker)

```bash
# Editar telegram-bot.service con tu usuario real
sudo cp telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot
```

## Tests (pytest)

La suite de tests vive en `tests/` (unitarios + integración con objetos falsos de Telegram, sin red). El CI los ejecuta en cada push antes del deploy.

```bash
# Dependencias de test (solo una vez por .venv)
.venv/bin/pip install -r requirements-dev.txt

# Ejecutar todos los tests con cobertura
.venv/bin/python -m pytest --cov=bot
```

- `--cov=bot` muestra qué % del código de `bot/` ejecutan los tests (reporte por módulo).
- En CI corre con `--cov-fail-under=80`: si la cobertura baja del 80%, el workflow falla y **no despliega**.
- Los tests no requieren red ni tokens reales (todo mockeado).

## Comandos del bot

| Comando | Descripción |
|---------|-------------|
| `/iniciar` (o `/start`) | Mensaje de bienvenida |
| `/ayuda` | Lista de comandos |
| `/info` | Estado del bot, modelo, tiempo activo |
| `/reiniciar` | Limpiar la conversación actual |
| `/dolar` | Ver la tasa del dólar hoy (oficial BCV y paralela USDT) |
| `/proyectos` | Ver todos tus proyectos |
| `/proyectos <estado>` | Ver proyectos por estado (`sin_iniciar`, `en_proceso`, `parado`, `completado`, `archivado`) |
| `/proyectos dueños` | Ver solo los proyectos que eres dueño |
| `/proyectos compartidos` | Ver los proyectos tuyos que compartiste con otros |
| `/proyectos invitado` | Ver los proyectos que otros te compartieron |
| `/nuevoproyecto <nombre>` | Crear un proyecto |
| `/proyecto <nombre>` | Ver detalles, notas, dueño y compartidos de un proyecto |
| `/estadoproyecto <nombre> <estado> [razón]` | Cambiar estado del proyecto |
| `/prioridadproyecto <nombre> <prioridad>` | Cambiar prioridad (`baja`, `media`, `alta`, `urgente`) |
| `/agregarnota <proyecto> <texto>` | Agregar una nota al proyecto |
| `/buscarproyecto <término>` | Buscar proyectos (propios y compartidos) |
| `/compartir <proyecto> @usuario` | Compartir un proyecto con otro usuario (solo el dueño; el invitado es avisado) |
| `/quitaracceso <proyecto> @usuario` | Dejar de compartir un proyecto (solo el dueño) |
| `/eliminarproyecto <nombre>` | Eliminar **definitivamente** un proyecto archivado (solo el dueño; libera espacio) |
| `/recordar <texto>` | Programar un recordatorio (fecha en lenguaje natural) |
| `/recordatorios` | Ver recordatorios pendientes (con botones para cancelar/posponer) |
| `/cancelarrecordatorio <id>` | Cancelar un recordatorio |
| `/editarrecordatorio <id> <texto>` | Editar el mensaje de un recordatorio |
| `/posponerrecordatorio <id> <duración>` | Posponer un recordatorio (ej. `30m`, `2h`, `1d`) |
| `/reprogramarrecordatorio <id> <fecha>` | Cambiar la fecha y hora de un recordatorio |
| `/imagina <descripción>` | Generar una imagen con IA (Nano Banana de Gemini) |

También puedes conversar normalmente y el bot detectará automáticamente temas que merecen un proyecto, peticiones de recordatorio, o preguntas que requieren buscar en internet.

### Proyectos: prioridades, estados y lenguaje natural

Los proyectos tienen **prioridad** y **estado** para organizar el seguimiento:

| Campo | Valores válidos |
|-------|-----------------|
| Prioridad | `baja` → `media` → `alta` → `urgente` |
| Estado | `sin_iniciar` → `en_proceso` → `parado` → `completado` → `archivado` |

Reglas importantes:

- Máximo **3 proyectos urgentes activos** por usuario. Para crear o subir a `urgente` uno nuevo, primero completa, archiva o baja la prioridad de los existentes. (Si un proyecto está compartido, el límite cuenta contra su **dueño**, no contra quien edita.)
- Solo los proyectos **archivados** pueden eliminarse definitivamente (con `/eliminarproyecto <nombre>`, o por texto natural: *"elimina el proyecto X"*). La eliminación es **irreversible** (borra notas y archivos) y libera espacio; solo el dueño puede hacerla. En ambos casos el bot pide **confirmación con botones** antes de borrar.
- También puedes usar **texto natural**: *"crea un proyecto para arreglar el carro"*, *"muéstrame mis proyectos urgentes"*, *"marca Reparar bici como completado"* o *"cambia la prioridad de Viaje a alta"*.

El bot interpreta la intención y ejecuta la acción sin que memorices comandos.

### Proyectos compartidos

Un proyecto tiene un **dueño** (quien lo creó) y puede compartirse con otros usuarios autorizados:

- **`/compartir <proyecto> @usuario`** — solo el dueño puede compartir. El invitado obtiene acceso **inmediato** (ve y edita todo) y recibe un mensaje del bot avisándole.
- **`/quitaracceso <proyecto> @usuario`** — solo el dueño puede quitar el acceso.
- **`/proyecto <nombre>`** muestra quién es el dueño y con quién está compartido.
- **`/proyectos`** acepta filtros: `dueños` (lo que eres dueño), `compartidos` (lo que compartiste) e `invitado` (lo que te compartieron).
- También por **texto natural**: *"comparte el proyecto Casa con @mama"*, *"con quién está compartido el proyecto Casa"*, *"deja de compartir el proyecto Casa con @mama"*.

> 🔒 **Privacidad**: solo ves tus proyectos y los que te compartieron explícitamente. Un invitado **no** puede ver otros proyectos, ni compartir/quitar acceso (solo el dueño), ni auto-invitarse. Para que alguien pueda ser invitado debe haber hablado con el bot al menos una vez (el bot resuelve el `@username` de los usuarios conocidos).

### Grupos de Telegram

El bot funciona en grupos con paridad casi total (ver `specs/group-parity.md`):

- **Proyectos compartidos**: en un grupo, el bot muestra proyectos asociados a ese grupo. Al crear un proyecto en grupo, automáticamente se añaden a todos los miembros como colaboradores.
- **Paridad**: `web_search`, `dolar_rate`, recordatorios y bitácora/memoria funcionan **en el grupo** (compartidos y aislados por `group_id`). Solo `generate_image` y `set_checkin_time` siguen privados.
- **Recordatorios en grupo**: se entregan **en el grupo** y cualquier miembro puede ver/editar/borrar. Privados quedan aislados.
- **Bitácora en grupo**: `memory/group_-xxx/` compartida, firmada `@usuario`, consultable con lenguaje natural.
- **Comandos en grupo**: `/proyectos`, `/proyecto`, `/nuevoproyecto`, `/recordar` y `/recordatorios` funcionan en grupo con scope de grupo.
- **Lenguaje natural**: *"lista los proyectos"*, *"crea un proyecto para X"*, *"recuérdame mañana a las 10"* funciona igual en grupo.
- **Disolución**: si el bot es removido del grupo, se **borra todo** lo del grupo (proyectos, recordatorios, bitácora, historial).

> 🔒 **Privacidad en grupos**: aislamiento estricto por `chat_id`. No ves proyectos/Bitácora/recordatorios de otro grupo ni privados en el grupo.

### Notas de voz y fotos

- **🎤 Notas de voz** — envíale una nota de voz y el bot la transcribe (Gemini) y responde. La transcripción entra a la conversación normal, así que puedes pedirle cosas por voz ("crea un proyecto para arreglar el carro").
- **🖼️ Fotos** — envíale una foto (con o sin pie) y el bot la analiza con visión de Gemini y responde sobre su contenido. Las fotos **se procesan en memoria y se eliminan al terminar** — no se guardan en el servidor.
- **🎨 `/imagina`** — genera una imagen desde una descripción: `/imagina un gato astronauta en la luna, estilo pixel art`.

> Los medios siempre se procesan con Gemini (modelos `GEMINI_MEDIA_MODEL` / `GEMINI_IMAGE_MODEL`), incluso cuando el proveedor de chat es `opencode`, porque OpenCode Go no soporta audio ni visión. Requieren `GEMINI_API_KEY` configurada.

### Límites diarios de medios

Para proteger la cuota gratuita de Gemini, hay un **tope diario por usuario** (se reinicia cada día en la zona horaria del bot):

| Tipo | Variable | Default |
|------|----------|---------|
| Imágenes (fotos analizadas + generadas) | `MEDIA_IMAGE_DAILY_LIMIT` | `30` |
| Notas de voz | `MEDIA_AUDIO_DAILY_LIMIT` | `30` |

Cuando el usuario se acerca al límite (≤3 restantes), el bot lo avisa en la respuesta. Al alcanzarlo, rechaza con un mensaje claro hasta el día siguiente. Poner `0` desactiva el tope.

### Recordatorios

Funcionan de dos formas:

1. **Mensaje normal**: escribe *"recuérdame la semana que viene llamar al mecánico"* — la IA lo detecta automáticamente y agenda el recordatorio. También puedes **gestionarlos hablando**: *"¿qué recordatorios tengo?"*, *"cancela el de mañana"*, *"cambia el recordatorio de llamar al mecánico"*, *"mueve el recordatorio 3 al viernes a las 5pm"*, *"pospón el recordatorio 5 dos horas"*.
2. **Comando**: `/recordar llamar al mecánico el próximo lunes a las 10am`

Cuando llega el momento, el bot te envía un mensaje: `🔔 Recordatorio: llamar al mecánico`. Los recordatorios persisten en SQLite (sobreviven reinicios).

**Gestionar tus recordatorios**: `/recordatorios` lista los pendientes con botones **🔕 Cancelar** y **⏰ Posponer 1h**; también hay comandos `/editarrecordatorio`, `/posponerrecordatorio`, `/reprogramarrecordatorio` y `/cancelarrecordatorio <id>`. Si un envío falla (p. ej. sin conexión), el bot reintenta con esperas crecientes (10s, 20s, 40s...) hasta un máximo de 8 horas, sin perder el recordatorio.

**Recordatorios recurrentes**: también puedes pedir que se repitan — *"recuérdame todos los lunes a las 8am tomar la medicina"*, *"cada día lavar el carro"*, *"el día 15 de cada mes pagar el internet"*. Frecuencias: `daily`, `weekly`, `monthly`. Al dispararse, el bot reprograma la próxima ocurrencia automáticamente; se cancelan con `/cancelarrecordatorio <id>`.

> ⚠️ **Nota para producción**: cuando quieras activar Gemini en el servidor (para el chat, notas de voz, fotos o `/imagina`), **actualiza a mano el `.env` del VPS** (`~/telegram-bot/.env`) añadiendo `GEMINI_API_KEY=...`. Los deploys con `git push` sincronizan el código pero **jamás tocan el `.env` del server** (está excluido en el workflow), por lo que la key de Gemini nunca llega sola — hay que ponerla manualmente. Mientras no la pongas, el bot en producción seguirá usando `opencode` para el chat y los medios no estarán disponibles.

> ⚠️ **`SYSTEM_PROMPT` en producción**: si el `.env` del VPS define un `SYSTEM_PROMPT` viejo, ese texto anulará el prompt por defecto nuevo (el que entiende estados/prioridades en español, el límite de 3 urgentes y las instrucciones de memoria/proactividad de la bitácora). Para activar las reglas nuevas, edita a mano `~/telegram-bot/.env` en el servidor y **borra o actualiza** la línea `SYSTEM_PROMPT`. Los deploys no la tocan.

### Memoria a largo plazo (bitácora)

El bot lleva una **bitácora personal** de tu día a día: cuando le cuentas algo notable (comidas, sueño, ejercicio, viajes, salidas, pagos de servicios, conversaciones, estado de ánimo, anécdotas…), lo guarda automáticamente en la bitácora del día. **No hay comandos nuevos** — todo se maneja por **lenguaje natural**:

- **Captura** — durante la conversación el bot guarda lo que compartes en la bitácora del día.
- **Resúmenes** — de noche compacta los días largos; cada lunes genera un resumen semanal; trimestral y anual, los resúmenes de periodo (jerárquicos y navegables). Si el bot está apagado cuando toca un resumen, **se pone al día solo** en el siguiente arranque: recupera los últimos días, semanas, trimestres y años que hayan quedado pendientes, y tu bitácora original nunca se pierde.
- **Recuerdo** — cuando preguntas por tu pasado (*"¿cuándo fue mi último viaje a Panamá?"*, *"¿qué comí la semana pasada?"*, *"¿cuándo pagué la luz?"*), el bot busca en los resúmenes y en la bitácora y responde con precisión.
- **Proactividad** — check-in matutino y cierre vespertino: te saluda, menciona pendientes y patrones de la bitácora (rachas de ejercicio, pagos pendientes, ausencias) y te recuerda lo importante. Las horas por defecto son las de `PROACTIVE_MORNING_TIME`/`PROACTIVE_EVENING_TIME`, pero **cada usuario puede cambiar las suyas** por lenguaje natural: *"cambia mi check-in de la mañana a las 9"* o *"adelanta mi check-in de la tarde"*.

Para desactivarla (parcial o totalmente):

| Variable | Efecto al poner `false` | Default |
|----------|--------------------------|---------|
| `MEMORY_ENABLED` | Desactiva la captura, los resúmenes y los jobs de memoria | `true` |
| `PROACTIVE_ENABLED` | Desactiva los check-ins y recordatorios proactivos | `true` |

> 🔒 **Privacidad**: tu bitácora es **por usuario** — cada persona autorizada tiene la suya aislada. Vive en `DATA_DIR/memory/{user_id}/` dentro del volumen Docker (dato de usuario, nunca en git ni en el código).

## Variables de entorno

| Variable | Descripción | Default |
|----------|-------------|---------|
| `BOT_TOKEN` | Token del bot de Telegram | *(requerido)* |
| `OPENCODE_GO_API_KEY` | API key de OpenCode Go | *(requerido si AI_PROVIDER=opencode)* |
| `OPENCODE_BASE_URL` | Endpoint de la API | `https://opencode.ai/zen/go/v1` |
| `MODEL` | Modelo de IA | `mimo-v2.5` |
| `AI_PROVIDER` | Proveedor: `opencode` o `gemini` | `opencode` |
| `GEMINI_API_KEY` | API key de Gemini (https://aistudio.google.com/) | vacío |
| `GEMINI_BASE_URL` | Endpoint OpenAI-compatible de Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| `GEMINI_MODEL` | Modelo de Gemini | `gemini-3.1-flash-lite` (el más económico) |
| `GEMINI_MEDIA_MODEL` | Modelo para voz/fotos | `gemini-3.6-flash` (el más barato con audio+visión) |
| `GEMINI_IMAGE_MODEL` | Modelo para `/imagina` | `gemini-2.5-flash-image` (Nano Banana, gratis) |
| `MEDIA_IMAGE_DAILY_LIMIT` | Tope diario de imágenes por usuario | `30` |
| `MEDIA_AUDIO_DAILY_LIMIT` | Tope diario de notas de voz por usuario | `30` |
| `AUTHORIZED_USERS` | IDs Telegram permitidos (coma-separados) | vacío (niega todo) |
| `MAX_CONTEXT_MESSAGES` | Máx. mensajes de contexto por usuario | `30` |
| `RATE_LIMIT_SECONDS` | Segundos entre mensajes por usuario | `5` |
| `MAX_TOOL_ROUNDS` | Máx. rondas de tools por mensaje (válvula anti-loop; cada ronda = 1 llamada al API) | `12` |
| `SYSTEM_PROMPT` | Personalidad del AI (si está vacío usa el default que entiende estados/prioridades, la memoria y la proactividad) | *(default en español)* |
| `MEMORY_ENABLED` | Activa la bitácora (captura + resúmenes + jobs de memoria) | `true` |
| `MEMORY_DAILY_COMPACT_WORDS` | Umbral de palabras para compactar un día de la bitácora | `1200` |
| `PROACTIVE_ENABLED` | Activa check-ins y recordatorios proactivos | `true` |
| `PROACTIVE_MORNING_TIME` | Hora del check-in matutino (HH:MM, 24h, en `TIMEZONE`) | `08:00` |
| `PROACTIVE_EVENING_TIME` | Hora del check-in vespertino (HH:MM, 24h, en `TIMEZONE`) | `20:00` |
| `PROACTIVE_MAX_DAILY` | Tope de mensajes proactivos por usuario y día (anti-molestia) | `3` |
| `TIMEZONE` | Zona horaria para recordatorios | `America/Caracas` |
| `LOG_LEVEL` | Nivel de logging | `INFO` |
| `DATA_DIR` | Directorio de datos (Docker lo pone en `/app/data`) | `.` |

## Modelos disponibles (económicos)

| Modelo | Uso |
|--------|-----|
| `deepseek-v4-flash` | Económico — Flash |
| `mimo-v2.5` | Económico — **Default** |

> Solo se usan modelos económicos. Cambia con `MODEL=...` en `.env` y reinicia el contenedor.

El modelo y el proveedor de IA **no se cambian desde el chat** (no hay comando ni opción por texto). Se fijan en el `.env` (`MODEL=...`, `AI_PROVIDER=...`, `GEMINI_MODEL=...`) y se aplican reiniciando el contenedor.

## Persistencia de datos

Los datos se guardan en un **volumen Docker nombrado** (`telegram-bot_bot_data` → `/app/data`):

```
/app/data/
├── chat_history.db          ← historial de conversaciones
├── projects.db              ← metadatos de proyectos (estado, prioridad, fechas,
│                              compartición, perfiles), recordatorios y proactive_log
├── memory.db                ← metadatos de la bitácora (días y resúmenes)
├── media_usage.db           ← límites diarios de medios (imágenes y voz)
├── projects/                ← contenido legible de proyectos
│   └── {owner_id}/{slug}/
│       ├── overview.md      ← descripción y estado
│       ├── notes/{id}.md    ← notas
│       └── context.json     ← resumen compacto para el AI
└── memory/                  ← bitácora por tenant (usuario o grupo)
    ├── {user_id}/
    │   ├── raw/YYYY-MM-DD.md     ← captura cruda del día (append en vivo)
    │   ├── days/YYYY-MM-DD.md    ← compacto diario (si superó el umbral)
    │   └── summaries/
    │       ├── weekly/YYYY-Www.md
    │       ├── quarterly/YYYY-Q#.md
    │       └── yearly/YYYY.md
    └── group_-xxx/
        ├── raw/YYYY-MM-DD.md     ← bitácora compartida, firmada @usuario
        ├── days/YYYY-MM-DD.md
        └── summaries/...
```

### Backup

```bash
docker run --rm -v telegram-bot_bot_data:/data -v $(pwd):/backup alpine \
  cp -r /data /backup/backup_$(date +%F)
```

## Seguridad

- **Secrets nunca en git** — `.env` está en `.gitignore`
- **Contenedor no-root** — corre como `botuser` (UID 999)
- **Fail-secure** — sin `AUTHORIZED_USERS` nadie entra
- **Rate limiting** — 3s entre mensajes por usuario (protege el presupuesto de Go)
- **Límites de uso de Go** — $12/5h, $30/semana, $60/mes (suficiente para uso personal)

## Límites de OpenCode Go

- 💰 $12 por 5 horas | $30 por semana | $60 por mes
- El bot con rate limiting (3s) y contexto limitado (30 mensajes) es eficiente para 4 usuarios
- Un bot público quemaría el presupuesto rápido — por eso el acceso está restringido

## Troubleshooting

| Problema | Solución |
|----------|----------|
| El bot no responde | Ver logs: `docker compose logs -f` |
| `unable to open database file` | Permisos del volumen: `docker compose down -v && docker compose up -d --build` |
| Usuario no autorizado | Revisar `AUTHORIZED_USERS` en `.env` |
| `Reasoning_content must be passed back` | Reiniciar: `docker compose up -d --build` (ya corregido en código) |
| Contenedor unhealthy | `docker compose ps` → suele ser el healthcheck; corregido con `procps` |

## Futuro (roadmap)

- [ ] **MCP** — conectar Notion, Gmail, Zoom (la arquitectura de tools ya está lista).
      La opción de Google Workspace (Gmail/Calendar/Drive) está investigada y
      documentada en `docs/mcp-google-workspace.md` (implementación pospuesta).
