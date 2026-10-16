import asyncio
import base64
import json
import logging
from openai import AsyncOpenAI

from bot.config import Settings

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
BASE_DELAY_SECONDS = 2.0

# Custom User-Agent to identify as a coding agent (required by OpenCode Go)
_USER_AGENT = "telegram-ai-bot/1.0"


def _build_extra_headers(
    session_id: str | None, provider: str = "opencode"
) -> dict[str, str]:
    """Build extra headers for API requests.

    OpenCode Go requires x-opencode-session for routing/caching and a
    custom User-Agent. Gemini doesn't need these headers.

    Args:
        session_id: Stable session identifier for prompt caching.
        provider: "opencode" or "gemini".

    Returns:
        Dict of headers to include, empty for non-opencode providers.
    """
    if provider != "opencode" or session_id is None:
        return {}
    return {
        "x-opencode-session": session_id,
        "User-Agent": _USER_AGENT,
    }


class OpenCodeError(Exception):
    """Raised when the OpenCode Go API returns an error."""


class RateLimitError(OpenCodeError):
    """Raised when rate limited by the API."""


class QuotaExhaustedError(OpenCodeError):
    """Raised when the provider's daily/free-tier quota is exhausted.

    Distinct from RateLimitError: retrying will not help because the
    quota is gone until the reset window (typically the next day).
    """


def _build_client(settings: Settings) -> AsyncOpenAI:
    """Build an AsyncOpenAI client for the configured provider."""
    if settings.ai_provider == "gemini":
        return AsyncOpenAI(
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
        )
    return AsyncOpenAI(
        api_key=settings.opencode_api_key,
        base_url=settings.opencode_base_url,
    )


def build_client_for_provider(
    settings: Settings, provider: str
) -> AsyncOpenAI | None:
    """Build a client for an explicit provider, or None when its key is missing."""
    provider = (provider or "").strip().lower()
    if provider == "gemini":
        if not settings.gemini_api_key:
            return None
        return AsyncOpenAI(
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
        )
    if provider == "opencode":
        return AsyncOpenAI(
            api_key=settings.opencode_api_key,
            base_url=settings.opencode_base_url,
        )
    return None


async def chat_completion(
    client: AsyncOpenAI,
    model: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    session_id: str | None = None,
    provider: str = "opencode",
) -> str:
    """Send a chat completion request with retry logic.

    Args:
        client: Configured AsyncOpenAI client.
        model: Model ID to use.
        system_prompt: System-level instruction for the AI.
        messages: Conversation history as [{"role": "...", "content": "..."}].
        session_id: Stable session ID for OpenCode Go routing/caching.
        provider: "opencode" or "gemini" (controls header injection).

    Returns:
        The AI's response text.

    Raises:
        OpenCodeError: On non-retryable errors after all retries.
        RateLimitError: On 429 responses after all retries.
    """
    full_messages = [
        {"role": "system", "content": system_prompt},
        *messages,
    ]
    extra_headers = _build_extra_headers(session_id, provider)

    async def _request() -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=full_messages,
            timeout=60.0,
            extra_headers=extra_headers or None,
        )
        content = response.choices[0].message.content
        if content is None:
            raise OpenCodeError("API returned empty response")
        return content

    return await _with_retry(_request)


async def chat_completion_with_tools(
    client: AsyncOpenAI,
    model: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    tools: list[dict],
    session_id: str | None = None,
    provider: str = "opencode",
) -> dict:
    """Send a chat completion request that may return tool calls.

    Args:
        client: Configured AsyncOpenAI client.
        model: Model ID to use.
        system_prompt: System-level instruction for the AI.
        messages: Conversation history.
        tools: Function-calling tool definitions.
        session_id: Stable session ID for OpenCode Go routing/caching.
        provider: "opencode" or "gemini" (controls header injection).

    Returns:
        A dict with either:
          - {"kind": "text", "content": str} for a plain reply, or
          - {"kind": "tool_call", "name": str, "arguments": dict,
             "call_id": str, "content": str|None} when the model
             requested a tool.

    Raises:
        OpenCodeError: On non-retryable errors after all retries.
        RateLimitError: On 429 responses after all retries.
    """
    full_messages = [
        {"role": "system", "content": system_prompt},
        *messages,
    ]
    extra_headers = _build_extra_headers(session_id, provider)

    async def _request() -> dict:
        response = await client.chat.completions.create(
            model=model,
            messages=full_messages,
            tools=tools,
            timeout=60.0,
            extra_headers=extra_headers or None,
        )
        message = response.choices[0].message

        if message.tool_calls:
            tool_call = message.tool_calls[0]
            try:
                arguments = json.loads(
                    tool_call.function.arguments or "{}"
                )
            except json.JSONDecodeError:
                arguments = {}
            return {
                "kind": "tool_call",
                "name": tool_call.function.name,
                "arguments": arguments,
                "call_id": tool_call.id,
                "content": message.content,
                # DeepSeek thinking mode: must echo this back on the
                # assistant tool-call message or the API rejects it.
                "reasoning_content": getattr(
                    message, "reasoning_content", None
                ),
                # Gemini thought signature: must echo this back on the
                # assistant tool-call message or the API rejects it.
                "thought_signature": _extract_thought_signature(tool_call),
            }

        content = message.content
        if content is None:
            raise OpenCodeError("API returned empty response")
        return {"kind": "text", "content": content}

    return await _with_retry(_request)


async def transcribe_audio(
    client: AsyncOpenAI,
    model: str,
    audio_b64: str,
    audio_format: str = "wav",
    session_id: str | None = None,
    provider: str = "gemini",
) -> str:
    """Transcribe an audio clip (base64 WAV) with a Gemini multimodal model.

    Args:
        client: Configured AsyncOpenAI client (Gemini).
        model: Multimodal model that accepts input_audio (e.g. gemini-3.6-flash).
        audio_b64: Base64-encoded audio data.
        audio_format: Container format ("wav", "mp3").
        session_id: Stable session ID for routing/caching.
        provider: "opencode" or "gemini" (controls header injection).

    Returns:
        The transcription text.
    """
    content = [
        {
            "type": "input_audio",
            "input_audio": {"data": audio_b64, "format": audio_format},
        },
        {
            "type": "text",
            "text": (
                "Transcribe la nota de voz al español, tal como se habla. "
                "Devuelve SOLO la transcripción literal, sin comentarios ni "
                "edición. Si es inaudible, responde: [audio inaudible]."
            ),
        },
    ]
    response = await _chat_request_with_retry(
        client=client,
        model=model,
        messages=[{"role": "user", "content": content}],
        session_id=session_id,
        provider=provider,
    )
    text = response.choices[0].message.content
    if not text:
        raise OpenCodeError("API returned empty transcription")
    return text


async def describe_image(
    client: AsyncOpenAI,
    model: str,
    image_b64: str,
    mime_type: str = "image/jpeg",
    question: str = "Describe la imagen en detalle.",
    session_id: str | None = None,
    provider: str = "gemini",
) -> str:
    """Analyze an image (base64) with a Gemini vision model.

    Args:
        client: Configured AsyncOpenAI client (Gemini).
        model: Multimodal model that accepts image_url (e.g. gemini-3.6-flash).
        image_b64: Base64-encoded image data.
        mime_type: MIME type of the image.
        question: User's question or default description request.
        session_id: Stable session ID for routing/caching.
        provider: "opencode" or "gemini" (controls header injection).

    Returns:
        The model's description/answer text.
    """
    content = [
        {
            "type": "text",
            "text": (
                f"{question}\n\n"
                "Responde en español, de forma concisa y útil para un "
                "asistente personal."
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
        },
    ]
    response = await _chat_request_with_retry(
        client=client,
        model=model,
        messages=[{"role": "user", "content": content}],
        session_id=session_id,
        provider=provider,
    )
    text = response.choices[0].message.content
    if not text:
        raise OpenCodeError("API returned empty image description")
    return text


async def generate_image(
    client: AsyncOpenAI,
    model: str,
    prompt: str,
    session_id: str | None = None,
    provider: str = "gemini",
) -> bytes:
    """Generate an image from a text prompt (Gemini image model).

    Args:
        client: Configured AsyncOpenAI client (Gemini).
        model: Image generation model (e.g. gemini-2.5-flash-image).
        prompt: Text prompt describing the desired image.
        session_id: Stable session ID for routing/caching.
        provider: "opencode" or "gemini" (controls header injection).

    Returns:
        The generated image bytes (PNG/JPEG depending on the model).
    """
    extra_headers = _build_extra_headers(session_id, provider)

    async def _generate() -> bytes:
        try:
            # Disable the SDK's own retries so we fully control the backoff
            # and never hammer a quota that is already exhausted.
            response = await client.images.generate(
                model=model,
                prompt=prompt,
                n=1,
                response_format="b64_json",
                timeout=120.0,
                max_retries=0,
                extra_headers=extra_headers or None,
            )
        except Exception as exc:
            # Quota exhaustion is not retryable: translate it up front so
            # _with_retry never retries a quota that is already gone.
            if _is_quota_exhausted(exc):
                raise QuotaExhaustedError(
                    "La cuota gratuita de generación de imágenes está agotada."
                ) from exc
            raise
        image_data = response.data[0].b64_json
        if not image_data:
            raise OpenCodeError("API returned empty image")
        return base64.b64decode(image_data)

    return await _with_retry(
        _generate,
        error_prefix="Image generation failed",
        raise_rate_limit_error=False,
    )


def _is_quota_exhausted(exc: Exception) -> bool:
    """Return True when the exception is a quota-exhaustion (not retryable).

    Looks for Gemini's RESOURCE_EXHAUSTED body with a QuotaFailure detail,
    or the classic "quota" phrasing from the OpenAI layer.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        status = body.get("status", "")
        if "RESOURCE_EXHAUSTED" in str(status):
            return True
        message = str(body.get("message", ""))
        return "quota" in message.lower() and "limit: 0" in message
    message = str(getattr(exc, "message", exc))
    return "quota" in message.lower() and (
        "limit: 0" in message or "RESOURCE_EXHAUSTED" in message
    )


async def _chat_request_with_retry(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    session_id: str | None = None,
    provider: str = "opencode",
):
    """Call chat.completions with shared retry logic; return the response.

    Used by the multimodal helpers. Keeps the same 429/5xx backoff policy
    as chat_completion.
    """
    extra_headers = _build_extra_headers(session_id, provider)

    async def _request():
        return await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            timeout=60.0,
            extra_headers=extra_headers or None,
        )

    return await _with_retry(_request)


def _extract_thought_signature(tool_call) -> str | None:
    """Extract the Gemini thought_signature from a tool call, if present."""
    extra = getattr(tool_call, "model_extra", None) or {}
    google = (extra.get("extra_content") or {}).get("google") or {}
    return google.get("thought_signature")


def _extract_status(exc: Exception) -> int | None:
    """Extract HTTP status code from an exception if available."""
    if hasattr(exc, "status_code"):
        return exc.status_code
    if hasattr(exc, "status"):
        return exc.status
    if hasattr(exc, "response") and exc.response is not None:
        return getattr(exc.response, "status_code", None)
    return None


async def _with_retry(
    operation,
    *,
    error_prefix: str = "API request failed",
    raise_rate_limit_error: bool = True,
):
    """Run an async ``operation`` with shared 429/5xx exponential backoff.

    Retries up to ``MAX_RETRIES`` times with ``BASE_DELAY_SECONDS``
    exponential backoff on HTTP 429 (rate limit) and 5xx (server) errors,
    then gives up. When 429 retries are exhausted, ``RateLimitError`` is
    raised if ``raise_rate_limit_error`` is True (the chat paths); with
    False (image generation) the loop falls through to the generic
    ``OpenCodeError`` instead. A ``QuotaExhaustedError`` is never retried
    and propagates unchanged. The final exception message is
    ``f"{error_prefix}: {last_error}"``.
    """
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await operation()
        except QuotaExhaustedError:
            raise
        except Exception as exc:
            last_error = exc
            status = _extract_status(exc)

            if status == 429:
                if attempt < MAX_RETRIES:
                    delay = BASE_DELAY_SECONDS * (2**attempt)
                    logger.warning(
                        "Rate limited (429). Retrying in %.1fs (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                if raise_rate_limit_error:
                    raise RateLimitError(
                        "OpenCode Go rate limit exceeded after retries"
                    ) from exc

            if attempt < MAX_RETRIES and status is not None and status >= 500:
                delay = BASE_DELAY_SECONDS * (2**attempt)
                logger.warning(
                    "Server error (%d). Retrying in %.1fs (attempt %d/%d)",
                    status,
                    delay,
                    attempt + 1,
                    MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                continue

            break

    raise OpenCodeError(f"{error_prefix}: {last_error}") from last_error


async def parse_reminder_datetime(
    client: AsyncOpenAI,
    model: str,
    text: str,
    now_iso: str,
    timezone_name: str,
    session_id: str | None = None,
    provider: str = "opencode",
) -> dict | None:
    """Ask the model to extract a datetime + message from reminder text.

    Args:
        client: Configured AsyncOpenAI client.
        model: Model ID to use.
        text: The user's reminder request in natural language.
        now_iso: Current local time as "YYYY-MM-DDTHH:MM" in user's tz.
        timezone_name: IANA timezone name.
        session_id: Stable session ID for OpenCode Go routing/caching.
        provider: "opencode" or "gemini" (controls header injection).

    Returns:
        {"datetime": "YYYY-MM-DDTHH:MM" | None, "message": str}
        or None when parsing fails.
    """
    prompt = (
        "Eres un parser de fechas. El usuario quiere crear un recordatorio.\n"
        f"Fecha y hora actuales del usuario: {now_iso} (zona {timezone_name}).\n"
        f"Texto del usuario: \"{text}\"\n\n"
        "Responde SOLO con JSON válido, sin markdown, con este formato exacto:\n"
        "{\"datetime\": \"YYYY-MM-DDTHH:MM\", \"message\": \"texto del recordatorio\", "
        "\"recurrence\": \"none\"}\n\n"
        "Reglas:\n"
        "- datetime: la fecha/hora (en la zona horaria del usuario) de la PRÓXIMA "
        "ocurrencia del recordatorio.\n"
        "- Si el texto no menciona una fecha/hora, pon \"datetime\": null.\n"
        "- recurrence: 'daily' si pide que se repita todos los días; 'weekly' si pide "
        "cada semana o el mismo día de la semana (ej. 'todos los lunes', 'cada "
        "sábado'); 'monthly' si pide cada mes (ej. 'el día 15 de cada mes', 'todos "
        "los meses'); 'none' si es una sola vez. Si no se menciona recurrencia, "
        "pon 'none'.\n"
        "- message: el recordatorio en sí, sin frases como 'recuérdame', "
        "'quiero que me recuerde', etc.\n"
        "- Si el texto NO es una petición de recordatorio, pon message vacío y datetime null."
    )

    try:
        raw = await chat_completion(
            client=client,
            model=model,
            system_prompt="",
            messages=[{"role": "user", "content": prompt}],
            session_id=session_id,
            provider=provider,
        )
    except OpenCodeError:
        return None

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Reminder date parsing produced invalid JSON: %s", raw)
        return None
