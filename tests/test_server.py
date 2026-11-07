"""Tests for server.py.

No test touches the real internet: the `web` fixture replaces httpx.get / httpx.post with a fake
that returns canned answers and remembers every request. Because @mcp.tool() returns the original
function, the tests can simply call the tools like normal Python functions.
"""

import datetime as dt
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import server

# ---------- fake web ----------

GEOCODE = server.GEOCODE_URL
GEOCODE_BY_ID = server.GEOCODE_BY_ID_URL
NOMINATIM = server.NOMINATIM_URL
FORECAST = server.FORECAST_URL
OVERPASS_1, OVERPASS_2 = server.OVERPASS_URLS


class FakeWeb:
    def __init__(self):
        self.answers = {}  # url -> data, a status code, or a function(params) -> data
        self.calls = []  # (url, params) of every request

    def on(self, url, answer):
        self.answers[url] = answer

    def _respond(self, method, url, params):
        self.calls.append((url, params))
        answer = self.answers.get(url)
        if answer is None:
            raise httpx.ConnectError(f"{url} is not faked")
        if callable(answer):
            answer = answer(params)
        request = httpx.Request(method, url)
        if isinstance(answer, int):
            return httpx.Response(answer, request=request)
        return httpx.Response(200, json=answer, request=request)

    def get(self, url, params=None, **_):
        return self._respond("GET", url, params or {})

    def post(self, url, data=None, **_):
        return self._respond("POST", url, data or {})

    def urls(self):
        return [url for url, _ in self.calls]


@pytest.fixture
def web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "post", fake.post)
    return fake


# ---------- sample answers (shortened from real responses) ----------

CHIANG_MAI = {
    "id": 1153671, "name": "Chiang Mai", "latitude": 18.79038, "longitude": 98.98468,
    "feature_code": "PPLA", "admin1": "Chiang Mai Province", "country": "Thailand",
}  # fmt: skip
CHIANG_MAI_THAI = {**CHIANG_MAI, "name": "เชียงใหม่", "admin1": "จังหวัดเชียงใหม่", "country": "ไทย"}


def node(osm_id, lat, lon, **tags):
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags}


def overpass_answer(*elements):
    return {"elements": list(elements)}


def forecast_answer(*days):
    """days: (date, weather code, min, max, rain chance %, rain mm)"""
    keys = ["time", "weather_code", "temperature_2m_min", "temperature_2m_max",
            "precipitation_probability_max", "precipitation_sum"]  # fmt: skip
    return {"timezone": "Asia/Bangkok", "daily": dict(zip(keys, map(list, zip(*days, strict=True)), strict=True))}


# ---------- geocoding ----------


def test_english_name_is_found_with_one_request(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})

    place = server.geocode("  Chiang Mai ")

    assert place["name"] == "Chiang Mai"
    assert place["full_name"] == "Chiang Mai, Chiang Mai Province, Thailand"
    assert web.urls() == [GEOCODE]
    assert web.calls[0][1]["language"] == "en"


def test_thai_name_is_searched_in_thai_but_returned_in_english(web):
    # Seen for real: Open-Meteo finds nothing for Thai text when language=en
    web.on(GEOCODE, lambda params: {"results": [CHIANG_MAI_THAI]} if params["language"] == "th" else {})
    web.on(GEOCODE_BY_ID, CHIANG_MAI)

    place = server.geocode("เชียงใหม่")

    assert place["name"] == "Chiang Mai"
    assert web.calls[0][1]["language"] == "th"
    assert web.calls[1][1] == {"id": 1153671, "language": "en"}


def test_nickname_is_expanded(web):
    web.on(GEOCODE, {"results": [{**CHIANG_MAI, "name": "Bangkok"}]})
    web.on(GEOCODE_BY_ID, 500)  # translation fails: the Thai record is kept

    place = server.geocode("กทม")

    assert web.calls[0][1]["name"] == "กรุงเทพมหานคร"
    assert place["search_text"] == "กรุงเทพมหานคร"


def test_nominatim_is_the_backup_for_neighbourhoods(web):
    web.on(GEOCODE, {})
    web.on(NOMINATIM, [{"name": "Patong", "display_name": "Patong, Kathu District, Phuket, Thailand",
                        "lat": "7.89", "lon": "98.29", "addresstype": "town"}])  # fmt: skip

    place = server.geocode("Patong")

    assert place["name"] == "Patong"
    assert "Phuket" in place["full_name"]
    assert web.urls() == [GEOCODE, NOMINATIM]


def test_a_whole_country_is_rejected(web):
    web.on(GEOCODE, {"results": [{**CHIANG_MAI, "name": "Japan", "feature_code": "PCLI"}]})

    with pytest.raises(ValueError, match="is a country"):
        server.geocode("Japan")


def test_unknown_place_gives_a_helpful_error(web):
    web.on(GEOCODE, {})
    web.on(NOMINATIM, [])

    with pytest.raises(ValueError, match="Could not find a place"):
        server.geocode("Nowhereville")


def test_services_down_is_reported_differently_from_not_found(web):
    web.on(GEOCODE, 500)
    web.on(NOMINATIM, 503)

    with pytest.raises(RuntimeError, match="not responding"):
        server.geocode("Chiang Mai")


# ---------- weather ----------


def test_weather_is_parsed(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(FORECAST, forecast_answer(("2026-11-07", 95, 22.6, 27.1, 100, 19.0), ("2026-11-08", 1, 21.9, 28.1, 10, 0.0)))

    result = server.get_weather("Chiang Mai", days=2)

    assert [d["condition"] for d in result["days"]] == ["Thunderstorm", "Mainly clear"]
    assert result["days"][0]["rain_chance_percent"] == 100
    assert result["location"]["name"] == "Chiang Mai"
    assert web.calls[-1][1]["forecast_days"] == 2


def test_weather_too_far_ahead_is_rejected_before_using_the_network(web):
    too_far = dt.date.today() + dt.timedelta(days=20)

    with pytest.raises(ValueError, match="16 days"):
        server.get_weather("Chiang Mai", start_date=too_far)
    assert web.calls == []


def test_weather_in_the_past_is_rejected(web):
    with pytest.raises(ValueError, match="in the past"):
        server.get_weather("Chiang Mai", start_date=dt.date.today() - dt.timedelta(days=1))


# ---------- accommodation ----------

HOTELS = overpass_answer(
    node(1, 18.80, 98.99, name="Far Hotel", tourism="hotel", stars="5"),
    node(2, 18.7905, 98.9848, name="Near Hostel", tourism="hostel"),
    node(3, 18.795, 98.985, **{"name": "Mid Guesthouse", "tourism": "guest_house", "stars": "3S"}),
    node(4, 18.795, 98.985, name="Mid Guesthouse", tourism="guest_house"),  # duplicate: dropped
    node(5, 18.79, 98.98, tourism="hotel"),  # no name: dropped
)


def test_hotels_are_sorted_by_distance_and_deduplicated(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(OVERPASS_1, HOTELS)

    result = server.search_hotels("Chiang Mai")

    assert [h["name"] for h in result["hotels"]] == ["Near Hostel", "Mid Guesthouse", "Far Hotel"]
    assert result["total_found"] == 3
    assert result["hotels"][1]["stars"] == 3  # "3S" -> 3
    assert result["hotels"][0]["stars"] is None
    assert "No price" in result["price_note"]


def test_hotels_filter_by_stars_and_sort_by_stars(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(OVERPASS_1, HOTELS)

    by_stars = server.search_hotels("Chiang Mai", sort_by="stars")
    four_plus = server.search_hotels("Chiang Mai", min_stars=4)

    assert [h["stars"] for h in by_stars["hotels"]] == [5, 3, None]
    assert [h["name"] for h in four_plus["hotels"]] == ["Far Hotel"]  # places with no star data are dropped


def test_hotel_type_goes_into_the_query(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(OVERPASS_1, overpass_answer())

    server.search_hotels("Chiang Mai", lodging_type="hostel", radius_km=2.5)

    query = web.calls[-1][1]["data"]
    assert '["tourism"~"^(hostel)$"]' in query
    assert "around:2500," in query


def test_booking_link_carries_dates_and_uses_the_text_the_user_searched(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(OVERPASS_1, overpass_answer())
    check_in = dt.date.today() + dt.timedelta(days=10)

    result = server.search_hotels("Chiang Mai", check_in=check_in, adults=3)

    params = parse_qs(urlsplit(result["booking_links"]["booking_com"]).query)
    assert params["ss"] == ["Chiang Mai"]
    assert params["checkin"] == [check_in.isoformat()]
    assert params["checkout"] == [(check_in + dt.timedelta(days=1)).isoformat()]
    assert params["group_adults"] == ["3"]


@pytest.mark.parametrize(
    ("check_in", "check_out", "message"),
    [
        (dt.date.today() - dt.timedelta(days=1), None, "in the past"),
        (dt.date.today() + dt.timedelta(days=5), dt.date.today() + dt.timedelta(days=5), "must be after"),
        (None, dt.date.today() + dt.timedelta(days=5), "without a check-in"),
    ],
)
def test_bad_dates_are_rejected_before_using_the_network(web, check_in, check_out, message):
    with pytest.raises(ValueError, match=message):
        server.search_hotels("Chiang Mai", check_in=check_in, check_out=check_out)
    assert web.calls == []


# ---------- places ----------

TEMPLES = overpass_answer(
    node(10, 18.7905, 98.9848, name="วัดเล็กใกล้ๆ", amenity="place_of_worship"),
    node(11, 18.7883, 98.9820, **{"name": "วัดพระสิงห์", "name:en": "Wat Phra Singh", "name:zh": "帕辛寺",
                                  "wikipedia": "th:วัดพระสิงห์", "amenity": "place_of_worship"}),
)  # fmt: skip


def test_famous_places_come_first_by_default(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(OVERPASS_1, TEMPLES)

    result = server.find_places("Chiang Mai", "temple")

    assert [p["name"] for p in result["places"]] == ["Wat Phra Singh", "วัดเล็กใกล้ๆ"]
    assert result["places"][0]["local_name"] == "วัดพระสิงห์"  # English name first, local name kept
    assert result["places"][1]["local_name"] is None
    assert '["religion"="buddhist"]' in web.calls[-1][1]["data"]


def test_places_can_be_sorted_by_distance(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(OVERPASS_1, TEMPLES)

    result = server.find_places("Chiang Mai", "temple", sort_by="distance")

    assert [p["name"] for p in result["places"]] == ["วัดเล็กใกล้ๆ", "Wat Phra Singh"]


# ---------- Overpass servers ----------


def test_second_overpass_server_is_used_when_the_first_fails(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(OVERPASS_1, 504)
    web.on(OVERPASS_2, TEMPLES)

    result = server.find_places("Chiang Mai", "temple")

    assert result["total_found"] == 2
    assert web.urls()[-2:] == [OVERPASS_1, OVERPASS_2]


def test_incomplete_overpass_answer_is_not_trusted(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(OVERPASS_1, {"remark": "runtime error: Query timed out", "elements": []})
    web.on(OVERPASS_2, TEMPLES)

    assert server.find_places("Chiang Mai", "temple")["total_found"] == 2


def test_all_overpass_servers_down_gives_a_clear_error(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(OVERPASS_1, 502)
    web.on(OVERPASS_2, 502)

    with pytest.raises(RuntimeError, match="not responding"):
        server.find_places("Chiang Mai", "temple")


# ---------- what Claude sees through the MCP protocol ----------


@pytest.mark.anyio
async def test_claude_sees_three_read_only_tools_with_english_descriptions():
    async with create_connected_server_and_client_session(server.mcp) as session:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        prompts = (await session.list_prompts()).prompts

    assert set(tools) == {"get_weather", "search_hotels", "find_places"}
    assert all(t.annotations.readOnlyHint for t in tools.values())
    assert tools["find_places"].inputSchema["required"] == ["location", "category"]
    assert "any" in tools["search_hotels"].inputSchema["properties"]["lodging_type"]["enum"]
    assert [p.name for p in prompts] == ["plan_trip"]
    assert "same language the user wrote in" in server.mcp.instructions


@pytest.mark.anyio
async def test_a_tool_call_returns_structured_data_and_errors_become_messages(web):
    web.on(GEOCODE, {"results": [CHIANG_MAI]})
    web.on(FORECAST, forecast_answer(("2026-11-07", 61, 22.0, 29.5, 70, 4.2)))

    async with create_connected_server_and_client_session(server.mcp) as session:
        ok = await session.call_tool("get_weather", {"location": "Chiang Mai", "days": 1})
        bad = await session.call_tool("get_weather", {"location": "Chiang Mai", "days": 30})

    assert ok.isError is False
    assert ok.structuredContent["days"][0]["condition"] == "Slight rain"
    assert bad.isError is True
