"""Unit tests for bot.dolar — rate parsing, formatting, caching."""

import time

import httpx
import pytest

from bot import dolar


class TestFormatBs:
    def test_formats_with_comma_decimal(self):
        assert dolar.format_bs(761.22) == "Bs. 761,22"

    def test_formats_thousands(self):
        assert dolar.format_bs(1234.56) == "Bs. 1.234,56"

    def test_formats_integer(self):
        assert dolar.format_bs(800) == "Bs. 800,00"


class TestFetchDolarapi:
    async def test_parses_official_and_parallel(self, monkeypatch):
        fake_data = [
            {"fuente": "oficial", "promedio": 76.5},
            {"fuente": "paralelo", "promedio": 78.2},
        ]

        async def fake_get(url, **kwargs):
            return _FakeResponse(fake_data)

        monkeypatch.setattr(dolar.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_get))

        result = await dolar._fetch_dolarapi()
        assert result == {"oficial": 76.5, "paralelo": 78.2}

    async def test_missing_oficial_returns_none(self, monkeypatch):
        async def fake_get(url, **kwargs):
            return _FakeResponse([{"fuente": "paralelo", "promedio": 78.2}])

        monkeypatch.setattr(dolar.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_get))
        assert await dolar._fetch_dolarapi() is None

    async def test_network_error_returns_none(self, monkeypatch):
        async def fake_get(url, **kwargs):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(dolar.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_get))
        assert await dolar._fetch_dolarapi() is None


class TestFetchUsdtVe:
    async def test_parses_price_from_html(self, monkeypatch):
        html = '<span>"price":"75.40","priceCurrency":"VES"</span>'

        async def fake_get(url, **kwargs):
            return _FakeResponse(html)

        monkeypatch.setattr(dolar.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_get))
        assert await dolar._fetch_usdt_ve() == 75.4

    async def test_missing_price_returns_none(self, monkeypatch):
        async def fake_get(url, **kwargs):
            return _FakeResponse("<html>no price</html>")

        monkeypatch.setattr(dolar.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_get))
        assert await dolar._fetch_usdt_ve() is None

    async def test_network_error_returns_none(self, monkeypatch):
        async def fake_get(url, **kwargs):
            raise httpx.ConnectError("boom")

        monkeypatch.setattr(dolar.httpx, "AsyncClient", lambda **kw: _FakeClient(fake_get))
        assert await dolar._fetch_usdt_ve() is None


class TestGetDolarInfo:
    async def test_combines_both_sources(self, monkeypatch):
        async def fake_fetch():
            return {
                "ok": True,
                "oficial": 76.5,
                "paralela": 78.2,
                "brecha_porcentaje": 2.2,
                "actualizado": time.time(),
            }

        monkeypatch.setattr(dolar, "_fetch_dolar_info", fake_fetch)
        info = await dolar.get_dolar_info()
        assert info["ok"] is True
        assert info["oficial"] == 76.5
        assert info["paralela"] == 78.2

    async def test_cache_used(self, monkeypatch):
        calls = {"n": 0}

        async def fake_fetch():
            calls["n"] += 1
            return {"ok": True, "oficial": 1.0, "paralela": 1.1,
                    "brecha_porcentaje": 10.0, "actualizado": time.time()}

        monkeypatch.setattr(dolar, "_fetch_dolar_info", fake_fetch)
        await dolar.get_dolar_info()
        await dolar.get_dolar_info()
        assert calls["n"] == 1  # second call served from cache

    async def test_error_when_sources_unavailable(self, monkeypatch):
        async def fake_fetch():
            return {"ok": False, "error": "fuentes no disponibles: paralela (USDT)"}

        monkeypatch.setattr(dolar, "_fetch_dolar_info", fake_fetch)
        info = await dolar.get_dolar_info()
        assert info["ok"] is False


class _FakeClient:
    """Minimal async context manager client returning a fixed response."""

    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        return await self._handler(url, **kwargs)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = payload if isinstance(payload, str) else ""

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload
