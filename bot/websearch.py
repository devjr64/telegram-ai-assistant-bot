"""Web search tool for the AI.

Runs DuckDuckGo searches (via the `ddgs` library) in a worker thread so
the async event loop is never blocked. Results are returned as plain
dicts so callers can serialize them for the model.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 10
MAX_QUERY_LENGTH = 200
SEARCH_TIMEOUT_SECONDS = 10.0
DDGS_TIMEOUT_SECONDS = 8


def validate_query(query: str) -> str | None:
    """Return a normalized query, or None when it isn't searchable."""
    normalized = query.strip()
    if not normalized or len(normalized) > MAX_QUERY_LENGTH:
        return None
    return normalized


async def web_search(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> dict:
    """Search the web and return a result payload for the AI.

    Returns:
        {"ok": True, "query": ..., "results": [{"title", "href", "body"}]}
        or {"ok": False, "error": "..."} when the query is invalid or the
        search engine is unavailable.
    """
    normalized = validate_query(query)
    if normalized is None:
        return {"ok": False, "error": "búsqueda vacía o demasiado larga"}

    capped = max(1, min(max_results, MAX_RESULTS_CAP))

    try:
        results = await asyncio.wait_for(
            asyncio.to_thread(_search_sync, normalized, capped),
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Web search timed out for query %r", normalized[:50])
        return {"ok": False, "error": "la búsqueda tardó demasiado"}
    except Exception as exc:  # noqa: BLE001 - engine/network errors vary
        logger.warning("Web search failed for query %r: %s", normalized[:50], exc)
        return {"ok": False, "error": f"error de búsqueda: {exc}"}

    return {"ok": True, "query": normalized, "results": results}


def _search_sync(query: str, max_results: int) -> list[dict[str, str]]:
    """Run the blocking DDGS search (executed in a worker thread)."""
    from ddgs import DDGS

    raw = DDGS(timeout=DDGS_TIMEOUT_SECONDS).text(
        query,
        region="ve-es",
        safesearch="moderate",
        max_results=max_results,
    )
    return [
        {
            "title": item.get("title", ""),
            "href": item.get("href", ""),
            "body": item.get("body", ""),
        }
        for item in raw
        if item.get("href")
    ]
