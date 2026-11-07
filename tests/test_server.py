"""Tests through the real MCP protocol (client <-> server in memory).

They see exactly what Claude sees: the tool list, schemas, results and errors.
"""

import datetime as dt
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from samples import (
    CHIANG_MAI_OM,
    NOMINATIM,
    OPEN_METEO_FORECAST,
    OPEN_METEO_GEO,
    OVERPASS_MAIN,
    forecast,
    node,
    om_results,
    overpass,
)

import server

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def mcp_client():
    async with create_connected_server_and_client_session(server.mcp) as session:
        yield session


async def call(session: ClientSession, tool: str, **arguments):
    return await session.call_tool(tool, arguments)


async def test_exposes_three_read_only_tools():
    async with mcp_client() as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}

    assert set(tools) == {"get_weather", "search_hotels", "find_places"}
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.openWorldHint is True
        assert tool.outputSchema is not None  # structured output
        assert tool.title  # human-readable display name
    assert tools["get_weather"].inputSchema["required"] == ["location"]
    assert tools["find_places"].inputSchema["required"] == ["location", "category"]
    assert tools["search_hotels"].inputSchema["properties"]["lodging_type"]["enum"] == [
        "any", "hotel", "hostel", "guest_house", "motel", "apartment"
    ]


async def test_tool_descriptions_are_in_english():
    async with mcp_client() as session:
        tools = (await session.list_tools()).tools

    for tool in tools:
        texts = [tool.description or "", *(p.get("description", "") for p in tool.inputSchema["properties"].values())]
        for text in texts:
            # allow Thai only inside quoted examples such as 'เชียงใหม่'
            outside_examples = "".join(part for i, part in enumerate(text.split("'")) if i % 2 == 0)
            assert not any("฀" <= ch <= "๿" for ch in outside_examples), (tool.name, text)


def test_server_instructions_tell_claude_to_match_language_and_not_guess_prices():
    assert "same language the user wrote in" in server.mcp.instructions
    assert "Never guess prices" in server.mcp.instructions


async def test_get_weather_end_to_end(use_fake_web):
    use_fake_web.on(OPEN_METEO_GEO, CHIANG_MAI_OM)
    use_fake_web.on(OPEN_METEO_FORECAST, forecast(("2026-11-07", 61, 22.0, 29.5, 70, 4.2)))

    async with mcp_client() as session:
        result = await call(session, "get_weather", location="Chiang Mai", days=1)

    assert result.isError is False
    data = result.structuredContent
    assert data["location"]["name"] == "Chiang Mai"
    assert data["days"][0]["condition"] == "Slight rain"
    assert data["days"][0]["rain_chance_percent"] == 70


async def test_search_hotels_end_to_end(use_fake_web):
    use_fake_web.on(OPEN_METEO_GEO, CHIANG_MAI_OM)
    use_fake_web.on(OVERPASS_MAIN, overpass(
        node(1, 18.80, 98.99, {"name": "Far Hotel", "tourism": "hotel", "stars": "4"}),
        node(2, 18.791, 98.985, {"name": "Near Hostel", "tourism": "hostel"}),
    ))
    check_in = dt.date.today() + dt.timedelta(days=10)

    async with mcp_client() as session:
        result = await call(session, "search_hotels", location="Chiang Mai", check_in=check_in.isoformat(), adults=3)

    assert result.isError is False
    data = result.structuredContent
    assert [h["name"] for h in data["hotels"]] == ["Near Hostel", "Far Hotel"]
    assert data["total_found"] == 2
    assert "No price" in data["price_note"]
    assert "OpenStreetMap" in data["attribution"]

    booking = parse_qs(urlsplit(data["booking_links"]["booking_com"]).query)
    assert booking["ss"] == ["Chiang Mai"]
    assert booking["checkin"] == [check_in.isoformat()]
    assert booking["checkout"] == [(check_in + dt.timedelta(days=1)).isoformat()]
    assert booking["group_adults"] == ["3"]


async def test_links_use_search_text_when_center_is_a_specific_business(use_fake_web):
    # Seen for real: Nominatim returned one guesthouse as the centre of "นิมมานเหมินท์ เชียงใหม่".
    # The links must search the area the user typed, not that guesthouse's name.
    use_fake_web.on(OPEN_METEO_GEO, om_results())
    use_fake_web.on(NOMINATIM, [{
        "name": "Baan Puen CNX", "display_name": "Baan Puen CNX, Nimmanhaemin Road, Chiang Mai, Thailand",
        "lat": "18.798", "lon": "98.970", "addresstype": "amenity", "address": {"country_code": "th"},
    }])
    use_fake_web.on(OVERPASS_MAIN, overpass(node(1, 18.799, 98.971, {"name": "Timely", "tourism": "guest_house"})))

    async with mcp_client() as session:
        result = await call(session, "search_hotels", location="Nimmanhaemin, Chiang Mai")

    data = result.structuredContent
    booking = parse_qs(urlsplit(data["booking_links"]["booking_com"]).query)
    assert booking["ss"] == ["Nimmanhaemin, Chiang Mai"]
    hotel_map = parse_qs(urlsplit(data["hotels"][0]["map_url"]).query)
    assert hotel_map["query"] == ["Timely, Nimmanhaemin, Chiang Mai"]


async def test_find_places_end_to_end(use_fake_web):
    use_fake_web.on(OPEN_METEO_GEO, CHIANG_MAI_OM)
    use_fake_web.on(OVERPASS_MAIN, overpass(
        node(1, 18.79, 98.98, {"name": "วัดพระสิงห์", "name:en": "Wat Phra Singh",
                               "amenity": "place_of_worship", "wikipedia": "th:x"}),
    ))

    async with mcp_client() as session:
        result = await call(session, "find_places", location="Chiang Mai", category="temple")

    data = result.structuredContent
    assert data["category"] == "Buddhist temple"
    assert data["places"][0]["name"] == "Wat Phra Singh"
    assert data["places"][0]["local_name"] == "วัดพระสิงห์"


async def test_past_check_in_fails_fast_without_network(use_fake_web):
    yesterday = dt.date.today() - dt.timedelta(days=1)

    async with mcp_client() as session:
        result = await call(session, "search_hotels", location="Chiang Mai", check_in=yesterday.isoformat())

    assert result.isError is True
    assert "in the past" in result.content[0].text
    assert use_fake_web.requests == []


async def test_unknown_place_is_a_tool_error_with_a_hint(use_fake_web):
    use_fake_web.on(OPEN_METEO_GEO, om_results())
    use_fake_web.on(NOMINATIM, [])

    async with mcp_client() as session:
        result = await call(session, "get_weather", location="Nowhereville")

    assert result.isError is True
    assert "Could not find a place" in result.content[0].text


async def test_out_of_range_argument_is_rejected_by_schema(use_fake_web):
    async with mcp_client() as session:
        result = await call(session, "get_weather", location="Chiang Mai", days=30)

    assert result.isError is True
    assert use_fake_web.requests == []


async def test_plan_trip_prompt_guides_tool_use():
    async with mcp_client() as session:
        prompts = (await session.list_prompts()).prompts
        result = await session.get_prompt(
            "plan_trip", {"destination": "Chiang Mai", "days": "2", "budget": "1500 THB per night"}
        )

    assert [p.name for p in prompts] == ["plan_trip"]
    text = result.messages[0].content.text
    assert "2-day trip to Chiang Mai" in text
    assert "Accommodation budget: 1500 THB per night" in text
    for tool in ("get_weather", "search_hotels", "find_places"):
        assert tool in text
