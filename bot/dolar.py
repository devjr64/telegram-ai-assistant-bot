"""Dollar exchange-rate tool for the AI.

Fetches the official BCV rate from ve.dolarapi.com and the parallel USDT
(Binance P2P) rate from www.usdt.com.ve. Results are cached briefly so the
external sources aren't hammered on every chat message.
"""

import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 300  # 5 minutes
TIMEOUT_SECONDS = 12.0
DOLARAPI_URL = "https://ve.dolarapi.com/v1/dolares"
USDT_VE_URL = "https://www.usdt.com.ve/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,*/*",
}

# usdt.com.ve embeds the Binance P2P price in its JSON-LD markup.
_USDT_VE_PRICE_RE = re.compile(
    r'"price":"([\d.]+)"\s*,\s*"priceCurrency":"VES"'
)

_cache: dict = {"ts": 0.0, "data": None}


def format_bs(value: float) -> str:
    """Format a float as a Venezuelan bolivar amount: 'Bs. 761,22'."""
    raw = f"{value:,.2f}"          # e.g. "1,234.56"
    raw = raw.replace(",", "X")    # mark thousands separators
    raw = raw.replace(".", ",")    # decimal point -> comma
    raw = raw.replace("X", ".")    # thousands -> dot
    return f"Bs. {raw}"


async def get_dolar_info() -> dict:
    """Return official + parallel dollar rates, cached for 5 minutes.

    Returns:
        {"ok": True, "oficial": float, "paralela": float,
         "brecha_porcentaje": float, "actualizado": epoch_seconds}
        or {"ok": False, "error": "..."} when no source is reachable.
    """
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL_SECONDS:
        return _cache["data"]

    info = await _fetch_dolar_info()
    if info.get("ok"):
        _cache["data"] = info
        _cache["ts"] = now
    return info


async def _fetch_dolar_info() -> dict:
    """Fetch both rates, cross-using available sources."""
    oficial: float | None = None
    paralela: float | None = None

    dolarapi = await _fetch_dolarapi()
    if dolarapi:
        oficial = dolarapi.get("oficial")
        paralela = dolarapi.get("paralelo")

    usdt_ve = await _fetch_usdt_ve()
    if usdt_ve is not None:
        paralela = usdt_ve  # Binance P2P is the preferred parallel source

    if oficial is None or paralela is None:
        missing = [name for name, value in
                   (("oficial (BCV)", oficial), ("paralela (USDT)", paralela))
                   if value is None]
        return {
            "ok": False,
            "error": f"fuentes no disponibles: {', '.join(missing)}",
        }

    return {
        "ok": True,
        "oficial": oficial,
        "paralela": paralela,
        "brecha_porcentaje": round((paralela - oficial) / oficial * 100, 1),
        "actualizado": time.time(),
    }


async def _fetch_dolarapi() -> dict | None:
    """Fetch rates from ve.dolarapi.com (official BCV + parallel)."""
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, headers=_HEADERS
        ) as client:
            resp = await client.get(DOLARAPI_URL)
            resp.raise_for_status()
        data = resp.json()
        oficial = next(
            (item.get("promedio") for item in data
             if item.get("fuente") == "oficial" and item.get("promedio")),
            None,
        )
        paralelo = next(
            (item.get("promedio") for item in data
             if item.get("fuente") == "paralelo" and item.get("promedio")),
            None,
        )
        if oficial is None:
            return None
        return {
            "oficial": float(oficial),
            "paralelo": float(paralelo) if paralelo is not None else None,
        }
    except Exception as exc:  # noqa: BLE001 - network errors vary
        logger.warning("dolarapi fetch failed: %s", exc)
        return None


async def _fetch_usdt_ve() -> float | None:
    """Fetch the Binance P2P USDT price from www.usdt.com.ve."""
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, headers=_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.get(USDT_VE_URL)
            resp.raise_for_status()
        match = _USDT_VE_PRICE_RE.search(resp.text)
        if match is None:
            logger.warning("usdt.com.ve: price not found in HTML")
            return None
        return float(match.group(1))
    except Exception as exc:  # noqa: BLE001 - network errors vary
        logger.warning("usdt.com.ve fetch failed: %s", exc)
        return None
