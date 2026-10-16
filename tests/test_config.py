"""Unit tests for bot.config — Settings dataclass and fail-secure parsing."""

import pytest

from bot.config import _parse_authorized_users, load_settings, Settings


class TestParseAuthorizedUsers:
    def test_parses_valid_ids(self):
        assert _parse_authorized_users("111,222,333") == frozenset({111, 222, 333})

    def test_ignores_whitespace(self):
        assert _parse_authorized_users(" 111 , 222 ") == frozenset({111, 222})

    def test_ignores_malformed_entries(self):
        assert _parse_authorized_users("111,abc,0,-5,222") == frozenset({111, 222})

    def test_empty_string_denies_everyone(self):
        assert _parse_authorized_users("") == frozenset()

    def test_none_denies_everyone(self):
        assert _parse_authorized_users(None) == frozenset()

    def test_only_invalid_denies_everyone(self):
        assert _parse_authorized_users("abc,xyz") == frozenset()


class TestLoadSettings:
    def test_valid_config(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tok")
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "key")
        monkeypatch.setenv("AUTHORIZED_USERS", "111")
        monkeypatch.delenv("AI_PROVIDER", raising=False)

        settings = load_settings()
        assert settings.bot_token == "tok"
        assert settings.ai_provider == "opencode"
        assert 111 in settings.authorized_users

    def test_missing_bot_token_raises(self, monkeypatch):
        monkeypatch.delenv("BOT_TOKEN", raising=False)
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "key")
        with pytest.raises(ValueError, match="BOT_TOKEN"):
            load_settings()

    def test_missing_opencode_key_raises(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tok")
        monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
        with pytest.raises(ValueError, match="OPENCODE_GO_API_KEY"):
            load_settings()

    def test_gemini_provider_requires_gemini_key(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tok")
        monkeypatch.setenv("AI_PROVIDER", "gemini")
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "key")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(ValueError, match="GEMINI_API_KEY"):
            load_settings()

    def test_gemini_provider_ok_with_key(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tok")
        monkeypatch.setenv("AI_PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "gkey")
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "key")
        settings = load_settings()
        assert settings.ai_provider == "gemini"

    def test_invalid_provider_raises(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tok")
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "key")
        monkeypatch.setenv("AI_PROVIDER", "anthropic")
        with pytest.raises(ValueError, match="AI_PROVIDER"):
            load_settings()

    def test_defaults_applied(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tok")
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "key")
        # The local .env pins MAX_CONTEXT_MESSAGES; drop it so this test
        # checks the code default, not the developer's override.
        monkeypatch.delenv("MAX_CONTEXT_MESSAGES", raising=False)
        monkeypatch.delenv("MODEL", raising=False)
        settings = load_settings()
        assert settings.model == "mimo-v2.5"
        assert settings.timezone == "America/Caracas"
        assert settings.max_context_messages == 30
        assert settings.rate_limit_seconds == 5.0


class TestDefaultSystemPrompt:
    def test_includes_project_statuses_and_priorities(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tok")
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "key")
        monkeypatch.delenv("SYSTEM_PROMPT", raising=False)

        settings = load_settings()
        prompt = settings.system_prompt.lower()
        for status in ("sin iniciar", "en proceso", "parado", "completado", "archivado"):
            assert status in prompt
        for priority in ("baja", "media", "alta", "urgente"):
            assert priority in prompt

    def test_includes_urgent_limit_and_natural_language_examples(self, monkeypatch):
        monkeypatch.setenv("BOT_TOKEN", "tok")
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "key")
        monkeypatch.delenv("SYSTEM_PROMPT", raising=False)

        settings = load_settings()
        prompt = settings.system_prompt.lower()
        assert "3 proyectos urgentes" in prompt
        assert "texto natural" in prompt
        assert "crea un proyecto" in prompt
        assert "recuérdame" in prompt
