"""Shared web helpers: the HTTP client and an expiring in-memory cache."""

import time
from typing import Any

import httpx

# Free services such as Nominatim and Overpass ask clients to identify themselves in the User-Agent
USER_AGENT = "thai-travel-mcp/1.0 (open-source learning project)"


def make_client() -> httpx.AsyncClient:
    """Create one HTTP client per tool call.

    Tests replace this function with a fake client so no real network traffic happens.
    """
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
    )


class TTLCache:
    """In-memory cache whose entries expire after a fixed time.

    Makes repeated questions fast and reduces load on the free services we use
    (Nominatim's usage policy requires caching results).
    """

    def __init__(self, ttl_seconds: float, max_items: int = 256) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._items: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.monotonic() >= expires_at:
            del self._items[key]
            return None
        return value

    def set(self, key: Any, value: Any) -> None:
        self._items.pop(key, None)
        if len(self._items) >= self.max_items:
            # dicts keep insertion order, so the first key is the oldest entry
            del self._items[next(iter(self._items))]
        self._items[key] = (time.monotonic() + self.ttl_seconds, value)

    def clear(self) -> None:
        self._items.clear()
