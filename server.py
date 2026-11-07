"""Travel MCP server: weather, accommodation and places for Claude.

How it works
- Every function decorated with @mcp.tool() is a tool that Claude can call.
- Claude reads the function name, the docstring and the type hints to decide when to call it
  and what values to pass. That text is the only thing the model sees of this file.
- Every tool does the same two steps: (1) turn a place name into coordinates,
  (2) ask a free web API for data around those coordinates and return a dict for Claude to read.

Data sources (all free, no API key): Open-Meteo (weather, geocoding), OpenStreetMap (places),
Nominatim (backup geocoder).
"""

import datetime as dt
import math
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

INSTRUCTIONS = """\
Travel-planning data for any city or place in the world. Place names can be given in any language \
(Thai, English, ...).
- get_weather: daily forecast, up to 16 days ahead
- search_hotels: real accommodation listings from OpenStreetMap
- find_places: attractions, temples, restaurants, cafes, markets and more from OpenStreetMap

Rules:
- Reply in the same language the user wrote in. Tool results are in English: translate them for the \
user, and keep local place names (`local_name`) when they help the user find the place.
- There is no price, availability or review data. Never guess prices; give the user booking_links or \
map_url to check them.
- Check location.full_name in the result to confirm it is the place the user meant. If not, make the \
name more specific and search again.
- OpenStreetMap data is volunteer-edited and may be incomplete or out of date.
"""

mcp = FastMCP("thai-travel", instructions=INSTRUCTIONS)

# The tools only read data from websites and never change anything, so they are safe to call repeatedly
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Free services ask clients to say who they are
HEADERS = {"User-Agent": "thai-travel-mcp/1.0 (open-source learning project)"}

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
GEOCODE_BY_ID_URL = "https://geocoding-api.open-meteo.com/v1/get"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Public Overpass servers are free but often slow or full, so we try a second one if the first fails
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
MAX_RESULTS = 1000  # most places we ask Overpass for in one query

# Nicknames that the geocoders do not understand (found by testing them)
ALIASES = {
    "กทม": "กรุงเทพมหานคร",
    "กทม.": "กรุงเทพมหานคร",
    "กรุงเทพฯ": "กรุงเทพมหานคร",
    "บางกอก": "กรุงเทพมหานคร",
    "bkk": "Bangkok",
    "อยุธยา": "พระนครศรีอยุธยา",
    "โคราช": "นครราชสีมา",
    "สิงคโปร์": "Singapore",  # the Thai spelling finds the middle of the island, not the city
}

# Weather codes (WMO standard) used by Open-Meteo -> description
WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    85: "Slight snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}  # fmt: skip

# What each accommodation type and place category means in OpenStreetMap tags
LODGING_TAGS = ["hotel", "hostel", "guest_house", "motel", "apartment"]
PLACE_FILTERS = {
    "attraction": '["tourism"~"^(attraction|viewpoint|museum|gallery|zoo|theme_park|aquarium)$"]',
    "temple": '["amenity"="place_of_worship"]["religion"="buddhist"]',
    "restaurant": '["amenity"="restaurant"]',
    "cafe": '["amenity"="cafe"]',
    "market": '["amenity"="marketplace"]',
    "shopping": '["shop"~"^(mall|department_store)$"]',
    "park": '["leisure"="park"]',
    "nightlife": '["amenity"~"^(bar|pub|nightclub)$"]',
}

# ---------------------------------------------------------------------------
# Step 1: place name -> coordinates
# ---------------------------------------------------------------------------


def geocode(query: str) -> dict[str, Any]:
    """Find the coordinates of a place. Open-Meteo first (good with cities), then Nominatim (finds
    neighbourhoods and landmarks such as "Patong")."""
    text = " ".join(query.split())
    if not text:
        raise ValueError("Please provide a city or place name.")
    text = ALIASES.get(text.casefold(), text)

    place = None
    service_down = False
    for finder in (_find_with_open_meteo, _find_with_nominatim):
        try:
            place = finder(text)
        except httpx.HTTPError:
            service_down = True  # this service failed, try the next one
            continue
        if place:
            return place

    if service_down:
        raise RuntimeError("The location lookup service is not responding. Please try again shortly.")
    raise ValueError(
        f"Could not find a place called '{text}'. Try the full name, add the region or country "
        "(e.g. 'Nimmanhaemin, Chiang Mai'), or use the English name."
    )


def _country_too_broad(name: str) -> ValueError:
    return ValueError(f"'{name}' is a country, which is too broad. Please give a city or place instead.")


def _find_with_open_meteo(text: str) -> dict[str, Any] | None:
    # Open-Meteo only matches names written in the language we ask for (tested: Thai text finds
    # nothing with language=en), so Thai input is searched in Thai and then translated by id.
    is_thai = any(0x0E00 <= ord(ch) <= 0x0E7F for ch in text)
    data = _get_json(GEOCODE_URL, {"name": text, "count": 5, "language": "th" if is_thai else "en"})
    results = [r for r in data.get("results", []) if r.get("feature_code") != "AIRP"]  # skip airports
    if not results:
        return None

    best = results[0]
    if is_thai:
        try:  # noqa: SIM105
            best = _get_json(GEOCODE_BY_ID_URL, {"id": best["id"], "language": "en"})
        except (httpx.HTTPError, KeyError):
            pass  # keep the Thai record: still usable, just not translated
    if best.get("feature_code", "").startswith("PCL"):  # PCL = a country
        raise _country_too_broad(best["name"])

    region = [part for part in (best.get("admin1"), best.get("country")) if part and part != best["name"]]
    return {
        "search_text": text,
        "name": best["name"],
        "full_name": ", ".join([best["name"], *region]),
        "latitude": best["latitude"],
        "longitude": best["longitude"],
    }


def _find_with_nominatim(text: str) -> dict[str, Any] | None:
    results = _get_json(NOMINATIM_URL, {"q": text, "format": "jsonv2", "limit": 1, "accept-language": "en"})
    if not results:
        return None
    best = results[0]
    name = best.get("name") or best["display_name"].split(",")[0]
    if best.get("addresstype") == "country":
        raise _country_too_broad(name)
    return {
        "search_text": text,
        "name": name,
        "full_name": best["display_name"],
        "latitude": float(best["lat"]),
        "longitude": float(best["lon"]),
    }


def _get_json(url: str, params: dict[str, Any]) -> Any:
    resp = httpx.get(url, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()  # turns HTTP errors such as 500 into exceptions
    return resp.json()


# ---------------------------------------------------------------------------
# Tool 1: weather
# ---------------------------------------------------------------------------


@mcp.tool(title="Weather forecast", annotations=READ_ONLY)
def get_weather(location: str, days: int = 3, start_date: dt.date | None = None) -> dict[str, Any]:
    """Daily weather forecast for any city or place in the world.

    Gives conditions, min/max temperature, chance of rain and rainfall amount for each day.

    Args:
        location: City or place name in any language, e.g. 'Chiang Mai', 'เชียงใหม่', 'Tokyo'.
            Add the region or country if the name is ambiguous. A whole country is too broad.
        days: Number of days to show, 1-16.
        start_date: First day to show (YYYY-MM-DD). Default: today. Forecasts reach 16 days ahead.
    """
    # Check the request first, so mistakes are reported instantly without using the network
    if not 1 <= days <= 16:
        raise ValueError("days must be between 1 and 16.")
    params: dict[str, Any] = {
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,precipitation_sum",
        "timezone": "auto",  # dates are local dates of the place
    }
    if start_date is None:
        params["forecast_days"] = days
    else:
        today = dt.date.today()
        end_date = start_date + dt.timedelta(days=days - 1)
        if start_date < today:
            raise ValueError(f"start_date {start_date} is in the past (today is {today}).")
        if end_date > today + dt.timedelta(days=15):
            raise ValueError(
                f"Forecasts only reach {today + dt.timedelta(days=15)} (16 days from today). For a later "
                "trip, describe the typical seasonal weather and say clearly that it is not a forecast."
            )
        params["start_date"] = start_date.isoformat()
        params["end_date"] = end_date.isoformat()

    place = geocode(location)
    params["latitude"] = place["latitude"]
    params["longitude"] = place["longitude"]
    try:
        data = _get_json(FORECAST_URL, params)
    except httpx.HTTPError as e:
        raise RuntimeError("Could not fetch weather data. Please try again.") from e

    daily = data["daily"]
    forecast = [
        {
            "date": day,
            "condition": WEATHER_CODES.get(code, f"Unknown (code {code})"),
            "temp_min_c": low,
            "temp_max_c": high,
            "rain_chance_percent": rain_chance,
            "rain_mm": rain_mm,
        }
        for day, code, low, high, rain_chance, rain_mm in zip(
            daily["time"],
            daily["weather_code"],
            daily["temperature_2m_min"],
            daily["temperature_2m_max"],
            daily["precipitation_probability_max"],
            daily["precipitation_sum"],
            strict=True,
        )
    ]
    return {
        "location": place,
        "timezone": data.get("timezone"),
        "days": forecast,
        "source": "Weather data by Open-Meteo.com (CC BY 4.0)",
    }


# ---------------------------------------------------------------------------
# Helpers for the two OpenStreetMap tools
# ---------------------------------------------------------------------------


def overpass(filters: list[str], place: dict[str, Any], radius_km: float) -> list[dict[str, Any]]:
    """Ask OpenStreetMap (through the Overpass API) for every named place that matches `filters`
    within `radius_km` of `place`. Returns Overpass's raw list of results."""
    around = f"around:{int(radius_km * 1000)},{place['latitude']},{place['longitude']}"
    parts = "".join(f'nwr{f}["name"]({around});' for f in filters)
    query = f"[out:json][timeout:20];({parts});out center tags {MAX_RESULTS};"

    for url in OVERPASS_URLS:
        try:
            # Busy public servers can take 30+ seconds even for small queries, so wait patiently
            resp = httpx.post(url, data={"data": query}, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            continue  # this server failed or is too busy, try the next one
        if "error" in data.get("remark", ""):  # the query ran out of time, so the result is incomplete
            continue
        return data["elements"]
    raise RuntimeError(
        "The map server (OpenStreetMap/Overpass) is not responding. This happens often with free public "
        "servers. Please try again shortly, or use a smaller radius."
    )


def read_places(elements: list[dict[str, Any]], place: dict[str, Any]) -> list[tuple[dict[str, str], float]]:
    """From Overpass's raw results, keep the (tags, distance_km) of each place that has coordinates,
    dropping duplicates (some places are drawn both as a point and as a building)."""
    found = []
    seen = set()
    for element in elements:
        tags = element.get("tags", {})
        point = element if "lat" in element else element.get("center")  # points vs buildings/areas
        if "name" not in tags or point is None:
            continue
        key = (tags["name"].casefold(), round(point["lat"], 3), round(point["lon"], 3))
        if key in seen:
            continue
        seen.add(key)
        distance = _distance_km(place["latitude"], place["longitude"], point["lat"], point["lon"])
        found.append((tags, round(distance, 2)))
    return found


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance over the Earth's surface (haversine formula)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def names(tags: dict[str, str]) -> tuple[str, str | None]:
    """(name, local_name): the English name if OpenStreetMap has one, plus the local-script name."""
    local = tags["name"]
    name = tags.get("name:en") or local
    return name, (local if local != name else None)


def popularity(tags: dict[str, str]) -> int:
    """Guess how famous a place is using only OpenStreetMap data. Famous places tend to have names in
    many languages (name:en, name:zh, ...) and a Wikipedia article."""
    languages = sum(1 for key in tags if key.startswith("name:"))
    return languages + 2 * bool(tags.get("wikipedia")) + bool(tags.get("wikidata"))


def map_link(tags: dict[str, str], place: dict[str, Any]) -> str:
    """Google Maps search for one place. We add the text the user searched (not place["name"], which
    can be a single business that happened to be picked as the centre)."""
    where = tags.get("addr:city") or place["search_text"]
    return "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": f"{tags['name']}, {where}"})


# ---------------------------------------------------------------------------
# Tool 2: accommodation
# ---------------------------------------------------------------------------


@mcp.tool(title="Find accommodation", annotations=READ_ONLY)
def search_hotels(
    location: str,
    lodging_type: Literal["any", "hotel", "hostel", "guest_house", "motel", "apartment"] = "any",
    min_stars: int = 0,
    sort_by: Literal["distance", "stars"] = "distance",
    radius_km: float = 5,
    limit: int = 15,
    check_in: dt.date | None = None,
    check_out: dt.date | None = None,
    adults: int = 2,
) -> dict[str, Any]:
    """Find real accommodation (hotels, hostels, guesthouses...) around any city or place, from OpenStreetMap.

    There is NO price, availability or review data. If the user mentions a budget, do not guess prices:
    pick a lodging_type that fits (hostel/guest_house are usually cheaper than hotel) and give the user
    the booking_links to see real prices. If the stay dates are known, pass check_in/check_out so the
    links are filled in.

    Args:
        location: City or place name in any language, e.g. 'Nimmanhaemin, Chiang Mai'.
        lodging_type: hotel, hostel, guest_house, motel, apartment, or any (all types).
        min_stars: Minimum star rating, 0 = no filter. If set, only places with star data remain.
        sort_by: 'distance' = nearest to the centre first, 'stars' = most stars first.
        radius_km: Search radius around the centre, in km.
        limit: Maximum number of places to return.
        check_in: Check-in date (YYYY-MM-DD), used to fill in the booking links.
        check_out: Check-out date (YYYY-MM-DD). Default: one night after check-in.
        adults: Number of adults, used in the booking links.
    """
    check_in, check_out = _check_stay_dates(check_in, check_out)  # fail fast, before using the network
    place = geocode(location)

    types = LODGING_TAGS if lodging_type == "any" else [lodging_type]
    elements = overpass([f'["tourism"~"^({"|".join(types)})$"]'], place, radius_km)

    hotels = []
    for tags, distance in read_places(elements, place):
        stars = int(tags["stars"][0]) if tags.get("stars", "")[:1].isdigit() else None  # '4S' -> 4
        if min_stars and (stars is None or stars < min_stars):
            continue
        name, local_name = names(tags)
        hotels.append(
            {
                "name": name,
                "local_name": local_name,
                "type": tags.get("tourism"),
                "stars": stars,  # None = no data (NOT "no stars")
                "distance_km": distance,
                "website": tags.get("website") or tags.get("contact:website"),
                "phone": tags.get("phone") or tags.get("contact:phone"),
                "map_url": map_link(tags, place),
            }
        )

    if sort_by == "stars":
        hotels.sort(key=lambda h: (h["stars"] is None, -(h["stars"] or 0), h["distance_km"]))
    else:
        hotels.sort(key=lambda h: h["distance_km"])

    booking = {"ss": place["search_text"], "group_adults": adults, "no_rooms": 1}
    if check_in and check_out:
        booking.update(checkin=check_in.isoformat(), checkout=check_out.isoformat())
    return {
        "location": place,
        "total_found": len(hotels),
        "hotels": hotels[:limit],
        "booking_links": {
            "google_maps": "https://www.google.com/maps/search/?"
            + urlencode({"api": 1, "query": f"hotels near {place['search_text']}"}),
            "booking_com": "https://www.booking.com/searchresults.html?" + urlencode(booking),
        },
        "price_note": "No price or availability data. Ask the user to open the links in booking_links, "
        "or the map_url of a place, to see real prices.",
        "source": "© OpenStreetMap contributors (ODbL)",
    }


def _check_stay_dates(check_in: dt.date | None, check_out: dt.date | None) -> tuple[dt.date | None, dt.date | None]:
    """Reject impossible dates. If only check-in is given, assume one night."""
    if check_in is None:
        if check_out is not None:
            raise ValueError("A check-out date was given without a check-in date. Please give both.")
        return None, None
    if check_in < dt.date.today():
        raise ValueError(f"Check-in date {check_in} is in the past (today is {dt.date.today()}).")
    check_out = check_out or check_in + dt.timedelta(days=1)
    if check_out <= check_in:
        raise ValueError("Check-out date must be after check-in date.")
    return check_in, check_out


# ---------------------------------------------------------------------------
# Tool 3: places to visit
# ---------------------------------------------------------------------------


@mcp.tool(title="Find places", annotations=READ_ONLY)
def find_places(
    location: str,
    category: Literal["attraction", "temple", "restaurant", "cafe", "market", "shopping", "park", "nightlife"],
    sort_by: Literal["popular", "distance"] = "popular",
    radius_km: float = 3,
    limit: int = 15,
) -> dict[str, Any]:
    """Find real places (sights, temples, restaurants, cafes...) around any city or point, from OpenStreetMap.

    There are no user reviews or ratings (popularity_score is only a rough guess from map data).
    If the user wants reviews, give them the map_url.

    Args:
        location: City or place name in any language, e.g. 'Chiang Mai'.
        category: attraction (sights, museums, viewpoints), temple (Buddhist temples), restaurant, cafe,
            market, shopping (malls), park, nightlife (bars, pubs, clubs).
        sort_by: 'popular' = best-known first (good for sights), 'distance' = nearest to the centre first.
        radius_km: Search radius around the centre, in km.
        limit: Maximum number of places to return.
    """
    place = geocode(location)
    elements = overpass([PLACE_FILTERS[category]], place, radius_km)

    places = []
    for tags, distance in read_places(elements, place):
        name, local_name = names(tags)
        places.append(
            {
                "name": name,
                "local_name": local_name,
                "kind": tags.get("tourism") or tags.get("amenity") or tags.get("shop") or tags.get("leisure"),
                "distance_km": distance,
                "popularity_score": popularity(tags),
                "cuisine": tags.get("cuisine"),
                "opening_hours": tags.get("opening_hours"),
                "website": tags.get("website") or tags.get("contact:website"),
                "map_url": map_link(tags, place),
            }
        )

    if sort_by == "popular":
        # Sorting by distance alone would fill the list with minor places that happen to be near the centre
        places.sort(key=lambda p: (-p["popularity_score"], p["distance_km"]))
    else:
        places.sort(key=lambda p: p["distance_km"])
    return {
        "location": place,
        "category": category,
        "total_found": len(places),
        "places": places[:limit],
        "source": "© OpenStreetMap contributors (ODbL)",
    }


# ---------------------------------------------------------------------------
# Prompt: a ready-made request users can pick in their MCP client
# ---------------------------------------------------------------------------


@mcp.prompt(title="Plan a trip")
def plan_trip(destination: str, days: int = 3, start_date: str | None = None, budget: str | None = None) -> str:
    """Ask Claude for a full trip plan: weather + accommodation + sights + food, day by day."""
    when = f" starting {start_date}" if start_date else ""
    money = f" Accommodation budget: {budget}." if budget else ""
    return f"""Help me plan a {days}-day trip to {destination}{when}.{money}

Steps:
1. Call get_weather for the travel dates. If the trip is more than 16 days away, describe the typical \
seasonal weather instead and say clearly that it is not a forecast.
2. Call search_hotels. With a budget, choose a lodging_type that fits it. Never guess prices; include \
booking_links so I can check real prices.
3. Call find_places for attraction and temple (things to see) and for restaurant or cafe (meals).
4. Arrange a day-by-day plan: group places that are close together (see distance_km) on the same day, \
and prefer indoor activities on days with a high chance of rain.
5. Finish with the booking links and a short note on the data limits (no prices, map data may be incomplete).
"""


if __name__ == "__main__":
    mcp.run()
