"""Shared test helpers.

All tests except test_live.py avoid the real network. FakeWeb imitates each API's answers instead,
so tests are fast, deterministic, and put no load on the free services we use.
"""

import inspect
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from thai_travel import geocoding, net, osm, weather


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Clear caches before every test, and shorten retry/hedge waits so tests run fast."""
    for module in (geocoding, weather, osm):
        module.clear_cache()
    if request.node.get_closest_marker("live") is None:
        monkeypatch.setattr(geocoding, "NOMINATIM_MIN_INTERVAL", 0)
        monkeypatch.setattr(osm, "HEDGE_DELAY_SECONDS", 0.05)
        monkeypatch.setattr(osm, "RETRY_DELAYS_SECONDS", (0, 0))
        monkeypatch.setattr(osm, "TOTAL_DEADLINE_SECONDS", 2)
    yield
    for module in (geocoding, weather, osm):
        module.clear_cache()


Handler = Callable[[httpx.Request], Any]


class FakeWeb:
    """Fake web APIs: set an answer per host, and every request is recorded for later checks.

    A handler can return a dict/list (answered as 200 JSON), an int (a bare status code),
    or an httpx.Response. It can also be an async function (used to imitate a slow server).
    """

    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}
        self.requests: list[httpx.Request] = []

    def on(self, host: str, handler: Handler | dict | list | int) -> None:
        self.handlers[host] = handler if callable(handler) else (lambda _req, value=handler: value)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._dispatch))

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]

    async def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.handlers.get(request.url.host)
        if handler is None:
            raise httpx.ConnectError(f"{request.url.host} is not faked", request=request)
        result = handler(request)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, httpx.Response):
            return result
        if isinstance(result, int):
            return httpx.Response(result)
        return httpx.Response(200, json=result)


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
def client(web: FakeWeb):
    return web.client()


@pytest.fixture
def use_fake_web(web: FakeWeb, monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    """Make server.py use FakeWeb instead of the real network."""
    monkeypatch.setattr(net, "make_client", web.client)
    return web
