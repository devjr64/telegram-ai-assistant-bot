"""Unit tests for bot.websearch — query validation and search errors.

The search engine is patched at ``websearch._search_sync`` (the function
wrapped in asyncio.to_thread) so no network is ever touched.
"""

import asyncio

from bot import websearch


class TestValidateQuery:
    def test_trims_whitespace(self):
        assert websearch.validate_query("  hola  ") == "hola"

    def test_empty_is_invalid(self):
        assert websearch.validate_query("   ") is None

    def test_too_long_is_invalid(self):
        assert websearch.validate_query("x" * (websearch.MAX_QUERY_LENGTH + 1)) is None

    def test_valid_query_passes(self):
        assert websearch.validate_query("precio del dólar hoy") == "precio del dólar hoy"


class TestWebSearch:
    async def test_invalid_query_returns_error(self):
        result = await websearch.web_search("   ")
        assert result["ok"] is False

    async def test_timeout_returns_error(self, monkeypatch):
        def fake_wait_for(coro, timeout):
            coro.close()  # avoid "coroutine never awaited" warnings
            raise asyncio.TimeoutError()

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        result = await websearch.web_search("hola")
        assert result["ok"] is False
        assert "tardó" in result["error"]

    async def test_success_returns_results(self, monkeypatch):
        def fake_search_sync(query, max_results):
            return [{"title": "T", "href": "https://x", "body": "B"}]

        monkeypatch.setattr(websearch, "_search_sync", fake_search_sync)
        result = await websearch.web_search("hola")
        assert result["ok"] is True
        assert result["query"] == "hola"
        assert result["results"][0]["href"] == "https://x"

    async def test_exception_returns_error(self, monkeypatch):
        def fake_search_sync(query, max_results):
            raise RuntimeError("engine down")

        monkeypatch.setattr(websearch, "_search_sync", fake_search_sync)
        result = await websearch.web_search("hola")
        assert result["ok"] is False
        assert "engine down" in result["error"]
