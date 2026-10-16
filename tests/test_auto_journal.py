"""Tests for the auto-journal background job."""

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot import auto_journal
from bot import history
from bot import memory as memory_store


class TestGetRecentMessages:
    """Tests for _get_recent_messages function."""

    def test_returns_recent_messages(self, tmp_path):
        """Messages from the last 2 hours are returned."""
        user_id = 12345
        chat_id = 12345
        now = time.time()

        # Add recent messages
        history.add_message(user_id, "user", "Voy a comprar", chat_id=chat_id)
        history.add_message(user_id, "assistant", "¡Genial!", chat_id=chat_id)

        messages = auto_journal._get_recent_messages(user_id, chat_id, hours=2)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Voy a comprar"
        assert messages[1]["role"] == "assistant"

    def test_excludes_old_messages(self, tmp_path):
        """Messages older than the lookback window are excluded."""
        user_id = 12345
        chat_id = 12345

        # Add an old message
        history.add_message(user_id, "user", "Mensaje viejo", chat_id=chat_id)
        with history._get_connection() as conn:
            conn.execute(
                "UPDATE chat_history SET created_at = ? WHERE user_id = ?",
                (time.time() - 7200, user_id),  # 2 hours ago
            )
            conn.commit()

        # Add a recent message
        history.add_message(user_id, "user", "Mensaje reciente", chat_id=chat_id)

        messages = auto_journal._get_recent_messages(user_id, chat_id, hours=2)
        assert len(messages) == 1
        assert messages[0]["content"] == "Mensaje reciente"

    def test_empty_when_no_messages(self, tmp_path):
        """Returns empty list when there are no messages."""
        messages = auto_journal._get_recent_messages(99999, 99999, hours=2)
        assert messages == []


class TestParseEvents:
    """Tests for _parse_events function."""

    def test_parses_valid_json(self):
        """Valid JSON array is parsed correctly."""
        response = '[{"content": "Usuario compró cosas", "topic": "finanzas"}]'
        events = auto_journal._parse_events(response)
        assert len(events) == 1
        assert events[0]["content"] == "Usuario compró cosas"
        assert events[0]["topic"] == "finanzas"

    def test_parses_json_in_code_block(self):
        """JSON wrapped in markdown code blocks is parsed."""
        response = '```json\n[{"content": "Test event", "topic": "ocio"}]\n```'
        events = auto_journal._parse_events(response)
        assert len(events) == 1
        assert events[0]["content"] == "Test event"

    def test_returns_empty_for_no_events(self):
        """Returns empty list when no events found."""
        response = "[]"
        events = auto_journal._parse_events(response)
        assert events == []

    def test_returns_empty_for_invalid_json(self):
        """Returns empty list for invalid JSON."""
        response = "This is not JSON"
        events = auto_journal._parse_events(response)
        assert events == []

    def test_validates_topic(self):
        """Invalid topics are replaced with empty string."""
        response = '[{"content": "Test", "topic": "invalid_topic"}]'
        events = auto_journal._parse_events(response)
        assert events[0]["topic"] == ""

    def test_strips_empty_content(self):
        """Events with empty content are excluded."""
        response = '[{"content": "", "topic": "ocio"}, {"content": "Valid", "topic": "ocio"}]'
        events = auto_journal._parse_events(response)
        assert len(events) == 1
        assert events[0]["content"] == "Valid"


class TestFormatConversation:
    """Tests for _format_conversation_for_extraction function."""

    def test_formats_messages(self):
        """Messages are formatted correctly for the model."""
        messages = [
            {"role": "user", "content": "Hola", "created_at": time.time()},
            {"role": "assistant", "content": "¡Hola!", "created_at": time.time()},
        ]
        formatted = auto_journal._format_conversation_for_extraction(messages)
        assert len(formatted) == 2
        assert formatted[0] == {"role": "user", "content": "Hola"}
        assert formatted[1] == {"role": "assistant", "content": "¡Hola!"}


class TestSaveEventsToJournal:
    """Tests for _save_events_to_journal function."""

    def test_saves_events(self, tmp_path):
        """Events are saved to the journal."""
        user_id = 12345
        events = [
            {"content": "Usuario compró leche", "topic": "finanzas"},
            {"content": "Usuario fue a correr", "topic": "salud"},
        ]

        saved = auto_journal._save_events_to_journal(
            user_id, events, "America/Caracas"
        )
        assert saved == 2

        # Verify events were saved
        tz = ZoneInfo("America/Caracas")
        day = datetime.now(tz).strftime("%Y-%m-%d")
        raw_content = memory_store.get_raw_content(user_id, day)
        assert raw_content is not None
        assert "Usuario compró leche" in raw_content
        assert "Usuario fue a correr" in raw_content

    def test_returns_count_on_failure(self, tmp_path):
        """Returns 0 when saving fails."""
        events = [{"content": "Test", "topic": "ocio"}]

        with patch("bot.memory.append_raw_entry", return_value=None):
            saved = auto_journal._save_events_to_journal(
                12345, events, "America/Caracas"
            )
            assert saved == 0


class TestExtractEventsFromMessages:
    """Tests for _extract_events_from_messages function."""

    @pytest.mark.asyncio
    async def test_extracts_events(self):
        """Events are extracted from messages using the model."""
        messages = [
            {"role": "user", "content": "Voy a comprar unas cosas"},
            {"role": "assistant", "content": "¡Genial! ¿Qué vas a comprar?"},
        ]

        mock_response = '[{"content": "Usuario va a comprar cosas", "topic": "finanzas"}]'

        with patch("bot.auto_journal.chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            events = await auto_journal._extract_events_from_messages(messages)
            assert len(events) == 1
            assert events[0]["content"] == "Usuario va a comprar cosas"

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        """Returns empty list when model call fails."""
        messages = [{"role": "user", "content": "Test"}]

        with patch("bot.auto_journal.chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.side_effect = Exception("API error")
            events = await auto_journal._extract_events_from_messages(messages)
            assert events == []


class TestAutoJournalJob:
    """Tests for the auto_journal_job function."""

    @pytest.mark.asyncio
    async def test_saves_events_for_active_user(self, tmp_path):
        """Events are saved for users with recent activity."""
        user_id = 12345
        settings = MagicMock()
        settings.memory_enabled = True
        settings.timezone = "America/Caracas"
        settings.authorized_users = frozenset([user_id])
        settings.model = "test-model"
        settings.auto_journal_min_messages = 1
        settings.auto_journal_interval = 3600

        context = MagicMock()
        context.bot_data = {"settings": settings}

        # Add recent messages
        history.add_message(user_id, "user", "Voy a comprar", chat_id=user_id)

        mock_events = [{"content": "Usuario va a comprar", "topic": "finanzas"}]

        with (
            patch("bot.auto_journal._extract_combined_from_messages", new_callable=AsyncMock) as mock_extract,
            patch("bot.auto_journal._save_events_to_journal") as mock_save,
            patch("bot.auto_journal._save_entities") as mock_save_entities,
        ):
            mock_extract.return_value = (mock_events, [])
            mock_save.return_value = 1
            mock_save_entities.return_value = 0

            await auto_journal.auto_journal_job(context)

            mock_extract.assert_called_once()
            mock_save.assert_called_once_with(user_id, mock_events, "America/Caracas")

    @pytest.mark.asyncio
    async def test_skips_users_without_messages(self, tmp_path):
        """No processing for users without recent messages."""
        settings = MagicMock()
        settings.memory_enabled = True
        settings.timezone = "America/Caracas"
        settings.authorized_users = frozenset([99999])

        context = MagicMock()
        context.bot_data = {"settings": settings}

        with patch("bot.auto_journal._extract_combined_from_messages") as mock_extract:
            await auto_journal.auto_journal_job(context)
            mock_extract.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_memory_disabled(self, tmp_path):
        """No processing when memory is disabled."""
        settings = MagicMock()
        settings.memory_enabled = False

        context = MagicMock()
        context.bot_data = {"settings": settings}

        with patch("bot.auto_journal._get_recent_messages") as mock_get:
            await auto_journal.auto_journal_job(context)
            mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_below_threshold(self, tmp_path):
        """Skip model call when fewer than threshold messages and fallback not due."""
        user_id = 12345
        settings = MagicMock()
        settings.memory_enabled = True
        settings.timezone = "America/Caracas"
        settings.authorized_users = frozenset([user_id])
        settings.auto_journal_min_messages = 6
        context = MagicMock()
        context.bot_data = {"settings": settings}
        history.add_message(user_id, "user", "hola", chat_id=user_id)
        # Pretend we just ran
        auto_journal._last_run_ts[f"user:{user_id}"] = time.time()
        with patch("bot.auto_journal._extract_combined_from_messages", new_callable=AsyncMock) as mock_extract:
            await auto_journal.auto_journal_job(context)
            mock_extract.assert_not_called()
        auto_journal._last_run_ts.pop(f"user:{user_id}", None)


class TestCombinedExtraction:
    """Tests for new combined extraction (60min + 50% saving)."""

    def test_parse_combined_valid(self):
        resp = '{"events": [{"content": "Compró pan", "topic": "finanzas"}], "entities": [{"type": "person", "name": "María"}]}'
        events, entities = auto_journal._parse_combined(resp)
        assert len(events) == 1
        assert len(entities) == 1
        assert events[0]["content"] == "Compró pan"
        assert entities[0]["name"] == "María"

    def test_parse_combined_code_block(self):
        resp = '```json\n{"events": [{"content": "Test", "topic": "ocio"}], "entities": []}\n```'
        events, entities = auto_journal._parse_combined(resp)
        assert len(events) == 1
        assert entities == []

    def test_parse_combined_invalid(self):
        events, entities = auto_journal._parse_combined("not json")
        assert events == []
        assert entities == []

    def test_should_skip_logic(self):
        key = "test:skip"
        auto_journal._last_run_ts[key] = time.time()
        assert auto_journal._should_skip(key, 1, 6) is True
        assert auto_journal._should_skip(key, 6, 6) is False
        # Fallback due
        auto_journal._last_run_ts[key] = time.time() - 20000
        assert auto_journal._should_skip(key, 1, 6) is False
        auto_journal._last_run_ts.pop(key, None)

    @pytest.mark.asyncio
    async def test_extract_combined(self):
        messages = [{"role": "user", "content": "hola"}]
        mock_resp = '{"events": [{"content": "Saludo", "topic": "eventos"}], "entities": []}'
        with patch("bot.auto_journal.chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_resp
            events, entities = await auto_journal._extract_combined_from_messages(messages)
            assert len(events) == 1
            assert entities == []

    @pytest.mark.asyncio
    async def test_extract_combined_error(self):
        messages = [{"role": "user", "content": "hola"}]
        with patch("bot.auto_journal.chat_completion", new_callable=AsyncMock) as mock_chat:
            mock_chat.side_effect = Exception("fail")
            events, entities = await auto_journal._extract_combined_from_messages(messages)
            assert events == []
            assert entities == []
