"""Unit tests for bot.ai_client — retries, error mapping, tool-call parsing."""

import json

import pytest

from bot import ai_client
from bot.config import Settings


@pytest.fixture
def settings():
    return Settings(
        bot_token="tok",
        opencode_api_key="key",
        ai_provider="opencode",
    )


@pytest.fixture
def no_sleep(monkeypatch):
    """Keep retry backoff instant so tests stay fast."""
    async def fake_sleep(delay):
        pass
    monkeypatch.setattr(ai_client.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ai_client, "MAX_RETRIES", 2)


class _FakeMessage:
    def __init__(self, content, tool_calls=None, reasoning_content=None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class _FakeToolCall:
    def __init__(self, name, arguments, call_id="call_1", model_extra=None):
        self.function = _FakeFunction(name, arguments)
        self.id = call_id
        self.model_extra = model_extra


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeClient:
    """Minimal AsyncOpenAI-compatible client with a pluggable create()."""

    def __init__(self, handler):
        self.chat = _FakeChat(handler)

    async def images(self, **kwargs):
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeChat:
    def __init__(self, handler):
        self.completions = _FakeCompletions(handler)


class _FakeCompletions:
    def __init__(self, handler):
        self._handler = handler

    async def create(self, **kwargs):
        """Awaitable create(): runs the sync handler and propagates errors."""
        return self._handler(**kwargs)


class TestChatCompletion:
    async def test_returns_content(self, settings, no_sleep):
        client = _FakeClient(lambda **kw: _FakeResponse(_FakeMessage("hola")))
        result = await ai_client.chat_completion(client, "model", "sys", [])
        assert result == "hola"

    async def test_raises_on_empty_content(self, settings, no_sleep):
        client = _FakeClient(lambda **kw: _FakeResponse(_FakeMessage(None)))
        with pytest.raises(ai_client.OpenCodeError, match="empty"):
            await ai_client.chat_completion(client, "model", "sys", [])

    async def test_retries_on_429_then_succeeds(self, settings, no_sleep):
        calls = {"n": 0}

        def handler(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _StatusError(429)
            return _FakeResponse(_FakeMessage("ok"))

        client = _FakeClient(handler)
        result = await ai_client.chat_completion(client, "model", "sys", [])
        assert result == "ok"
        assert calls["n"] == 2

    async def test_raises_rate_limit_after_retries(self, settings, no_sleep):
        def handler(**kw):
            raise _StatusError(429)

        client = _FakeClient(handler)
        with pytest.raises(ai_client.RateLimitError):
            await ai_client.chat_completion(client, "model", "sys", [])

    async def test_retries_on_500_then_succeeds(self, settings, no_sleep):
        calls = {"n": 0}

        def handler(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _StatusError(500)
            return _FakeResponse(_FakeMessage("ok"))

        client = _FakeClient(handler)
        result = await ai_client.chat_completion(client, "model", "sys", [])
        assert result == "ok"

    async def test_non_retryable_error_raises(self, settings, no_sleep):
        def handler(**kw):
            raise _StatusError(400)

        client = _FakeClient(handler)
        with pytest.raises(ai_client.OpenCodeError):
            await ai_client.chat_completion(client, "model", "sys", [])


class TestChatCompletionWithTools:
    async def test_text_response(self, settings, no_sleep):
        client = _FakeClient(lambda **kw: _FakeResponse(_FakeMessage("respuesta")))
        result = await ai_client.chat_completion_with_tools(client, "m", "sys", [], [])
        assert result == {"kind": "text", "content": "respuesta"}

    async def test_tool_call_parsed(self, settings, no_sleep):
        tool_call = _FakeToolCall("create_project", '{"name": "x"}')
        message = _FakeMessage(None, tool_calls=[tool_call])
        client = _FakeClient(lambda **kw: _FakeResponse(message))

        result = await ai_client.chat_completion_with_tools(client, "m", "sys", [], [])
        assert result["kind"] == "tool_call"
        assert result["name"] == "create_project"
        assert result["arguments"] == {"name": "x"}
        assert result["call_id"] == "call_1"

    async def test_invalid_json_arguments_become_empty(self, settings, no_sleep):
        tool_call = _FakeToolCall("create_project", "not json {")
        message = _FakeMessage(None, tool_calls=[tool_call])
        client = _FakeClient(lambda **kw: _FakeResponse(message))

        result = await ai_client.chat_completion_with_tools(client, "m", "sys", [], [])
        assert result["arguments"] == {}

    async def test_reasoning_content_captured(self, settings, no_sleep):
        tool_call = _FakeToolCall("create_project", "{}")
        message = _FakeMessage("thinking...", tool_calls=[tool_call],
                               reasoning_content="hidden reasoning")
        client = _FakeClient(lambda **kw: _FakeResponse(message))

        result = await ai_client.chat_completion_with_tools(client, "m", "sys", [], [])
        assert result["reasoning_content"] == "hidden reasoning"

    async def test_thought_signature_captured(self, settings, no_sleep):
        tool_call = _FakeToolCall(
            "create_project",
            "{}",
            model_extra={"extra_content": {"google": {"thought_signature": "sig-123"}}},
        )
        message = _FakeMessage(None, tool_calls=[tool_call])
        client = _FakeClient(lambda **kw: _FakeResponse(message))

        result = await ai_client.chat_completion_with_tools(client, "m", "sys", [], [])
        assert result["thought_signature"] == "sig-123"


class TestExtractStatus:
    def test_from_status_code(self):
        exc = _StatusError(429)
        assert ai_client._extract_status(exc) == 429

    def test_from_response(self):
        exc = Exception()
        exc.response = type("R", (), {"status_code": 503})()
        assert ai_client._extract_status(exc) == 503

    def test_unknown_returns_none(self):
        assert ai_client._extract_status(Exception("x")) is None


class TestQuotaDetection:
    def test_quota_exhausted_body(self):
        exc = Exception("quota")
        exc.body = {"status": "RESOURCE_EXHAUSTED", "message": "quota"}
        assert ai_client._is_quota_exhausted(exc) is True

    def test_quota_message(self):
        exc = Exception("quota limit: 0 exceeded")
        assert ai_client._is_quota_exhausted(exc) is True

    def test_not_quota(self):
        exc = Exception("some other error")
        assert ai_client._is_quota_exhausted(exc) is False


class TestParseReminderDatetime:
    async def test_parses_valid_json(self, settings, no_sleep):
        client = _FakeClient(
            lambda **kw: _FakeResponse(
                _FakeMessage('{"datetime": "2026-08-13T10:00", "message": "Llamar", "recurrence": "none"}')
            )
        )
        result = await ai_client.parse_reminder_datetime(
            client, "model", "recuérdame llamar", "2026-08-12T09:00", "America/Caracas"
        )
        assert result == {
            "datetime": "2026-08-13T10:00",
            "message": "Llamar",
            "recurrence": "none",
        }

    async def test_parses_json_in_code_fence(self, settings, no_sleep):
        client = _FakeClient(
            lambda **kw: _FakeResponse(
                _FakeMessage('```json\n{"datetime": null, "message": "", "recurrence": "none"}\n```')
            )
        )
        result = await ai_client.parse_reminder_datetime(
            client, "model", "hola", "2026-08-12T09:00", "America/Caracas"
        )
        assert result == {"datetime": None, "message": "", "recurrence": "none"}

    async def test_invalid_json_returns_none(self, settings, no_sleep):
        client = _FakeClient(lambda **kw: _FakeResponse(_FakeMessage("no json")))
        result = await ai_client.parse_reminder_datetime(
            client, "model", "texto", "2026-08-12T09:00", "America/Caracas"
        )
        assert result is None

    async def test_api_error_returns_none(self, settings, no_sleep):
        def handler(**kw):
            raise _StatusError(429)

        client = _FakeClient(handler)
        result = await ai_client.parse_reminder_datetime(
            client, "model", "texto", "2026-08-12T09:00", "America/Caracas"
        )
        assert result is None


class _StatusError(Exception):
    def __init__(self, status_code):
        super().__init__(f"error {status_code}")
        self.status_code = status_code


class TestBuildExtraHeaders:
    """Tests for _build_extra_headers helper."""

    def test_returns_empty_for_gemini(self):
        """Gemini provider should not get session headers."""
        headers = ai_client._build_extra_headers("tg-user-123", "gemini")
        assert headers == {}

    def test_returns_empty_when_session_id_none(self):
        """No session_id means no headers."""
        headers = ai_client._build_extra_headers(None, "opencode")
        assert headers == {}

    def test_returns_headers_for_opencode(self):
        """OpenCode provider with session_id gets both headers."""
        headers = ai_client._build_extra_headers("tg-user-123", "opencode")
        assert headers == {
            "x-opencode-session": "tg-user-123",
            "User-Agent": "telegram-ai-bot/1.0",
        }

    def test_user_agent_constant(self):
        """Verify the User-Agent constant is set correctly."""
        assert ai_client._USER_AGENT == "telegram-ai-bot/1.0"


class TestSessionHeaderInRequests:
    """Tests that session headers are passed to API requests."""

    async def test_chat_completion_passes_session_header(self, settings, no_sleep):
        """chat_completion should pass extra_headers when session_id is provided."""
        captured_headers = {}

        class _CapturingClient:
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kwargs):
                        captured_headers.update(kwargs.get("extra_headers") or {})
                        return _FakeResponse(_FakeMessage("ok"))

        client = _CapturingClient()
        await ai_client.chat_completion(
            client, "model", "sys", [],
            session_id="tg-user-456", provider="opencode",
        )
        assert captured_headers.get("x-opencode-session") == "tg-user-456"
        assert captured_headers.get("User-Agent") == "telegram-ai-bot/1.0"

    async def test_chat_completion_no_header_for_gemini(self, settings, no_sleep):
        """chat_completion should not pass session headers for Gemini."""
        captured_headers = {}

        class _CapturingClient:
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kwargs):
                        captured_headers.update(kwargs.get("extra_headers") or {})
                        return _FakeResponse(_FakeMessage("ok"))

        client = _CapturingClient()
        await ai_client.chat_completion(
            client, "model", "sys", [],
            session_id="tg-user-456", provider="gemini",
        )
        # For gemini, extra_headers should be None or empty
        assert captured_headers == {}

    async def test_chat_completion_with_tools_passes_session_header(self, settings, no_sleep):
        """chat_completion_with_tools should pass extra_headers."""
        captured_headers = {}

        class _CapturingClient:
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kwargs):
                        captured_headers.update(kwargs.get("extra_headers") or {})
                        return _FakeResponse(_FakeMessage("ok"))

        client = _CapturingClient()
        await ai_client.chat_completion_with_tools(
            client, "model", "sys", [], [],
            session_id="tg-user-789", provider="opencode",
        )
        assert captured_headers.get("x-opencode-session") == "tg-user-789"

    async def test_parse_reminder_datetime_passes_session_header(self, settings, no_sleep):
        """parse_reminder_datetime should pass session_id through."""
        captured_headers = {}

        class _CapturingClient:
            class chat:
                class completions:
                    @staticmethod
                    async def create(**kwargs):
                        captured_headers.update(kwargs.get("extra_headers") or {})
                        return _FakeResponse(
                            _FakeMessage('{"datetime": null, "message": "", "recurrence": "none"}')
                        )

        client = _CapturingClient()
        await ai_client.parse_reminder_datetime(
            client, "model", "test", "2026-09-07T10:00", "UTC",
            session_id="tg-user-999", provider="opencode",
        )
        assert captured_headers.get("x-opencode-session") == "tg-user-999"
