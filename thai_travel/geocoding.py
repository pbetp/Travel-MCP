"""Turn a place name (in any language) into coordinates.

Two sources are chained because each is good at something different (tested against the real services):
1. Open-Meteo Geocoding: good with city names such as "เชียงใหม่" or "Tokyo", but cannot find
   neighbourhoods or landmarks.
2. Nominatim (OpenStreetMap): finds neighbourhoods and landmarks such as "ป่าตอง" or "Doi Suthep",
   but its usage policy limits clients to 1 request per second, so it is only the fallback.

Results are returned in English so that downstream tools and the LLM get one consistent language.
"""

import asyncio
import time

import httpx

from .errors import LocationNotFoundError, UpstreamServiceError
from .models import Location
from .net import TTLCache

OPEN_METEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_GET_URL = "https://geocoding-api.open-meteo.com/v1/get"  # look a place up by id
THAI_BLOCK = range(0x0E00, 0x0E80)  # Unicode range of Thai script
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Nominatim usage policy: at most one request per second
NOMINATIM_MIN_INTERVAL = 1.0

# Nicknames and abbreviations that both services miss or get wrong (found by testing the real services)
ALIASES = {
    "กทม": "กรุงเทพมหานคร",
    "กทม.": "กรุงเทพมหานคร",
    "กรุงเทพฯ": "กรุงเทพมหานคร",
    "บางกอก": "กรุงเทพมหานคร",
    "bkk": "Bangkok",
    "อยุธยา": "พระนครศรีอยุธยา",
    "โคราช": "นครราชสีมา",
    "สิงคโปร์": "Singapore",  # the Thai spelling resolves to the middle of the island, not the city
}

_cache = TTLCache(ttl_seconds=24 * 3600)
_nominatim_lock = asyncio.Lock()
_nominatim_last_call = 0.0


def clear_cache() -> None:
    _cache.clear()


async def geocode(client: httpx.AsyncClient, query: str) -> Location:
    """Find the coordinates of a place. Tries Open-Meteo first, then Nominatim."""
    cleaned = " ".join(query.split())
    if not cleaned:
        raise LocationNotFoundError("Please provide a city or place name.")

    key = cleaned.casefold()
    cached = _cache.get(key)
    if cached is not None:
        return cached

    search_text = ALIASES.get(key, cleaned)
    service_failed = False
    for provider in (_search_open_meteo, _search_nominatim):
        try:
            location = await provider(client, search_text)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            # network failure or unexpected response -> try the next source
            service_failed = True
            continue
        if location is not None:
            _cache.set(key, location)
            return location

    if service_failed:
        raise UpstreamServiceError("The location lookup service is temporarily unavailable. Please try again shortly.")
    raise LocationNotFoundError(
        f"Could not find a place called '{cleaned}'. Try the full name, add the region or country "
        "(e.g. 'Nimmanhaemin, Chiang Mai'), or use the English name."
    )


def _reject_country(name: str) -> None:
    raise LocationNotFoundError(
        f"'{name}' is a country, which is too broad. Please give a city or place "
        "(e.g. 'Tokyo' instead of 'Japan')."
    )


def _has_thai(text: str) -> bool:
    return any(ord(ch) in THAI_BLOCK for ch in text)


async def _in_english(client: httpx.AsyncClient, record: dict) -> dict:
    """Fetch the English version of an Open-Meteo place record, or keep the original if that fails."""
    try:
        resp = await client.get(
            OPEN_METEO_GET_URL, params={"id": record["id"], "language": "en", "format": "json"}
        )
        resp.raise_for_status()
        english = resp.json()
        return english if "name" in english else record
    except (httpx.HTTPError, ValueError, KeyError):
        return record


async def _search_open_meteo(client: httpx.AsyncClient, text: str) -> Location | None:
    # Open-Meteo only matches names in the language it is told to search. Tested for real:
    # "เชียงใหม่" finds nothing with language=en, and "Chiang Mai" works with either.
    language = "th" if _has_thai(text) else "en"
    resp = await client.get(
        OPEN_METEO_URL,
        params={"name": text, "count": 5, "language": language, "format": "json"},
    )
    resp.raise_for_status()
    results = [r for r in resp.json().get("results") or [] if r.get("feature_code") != "AIRP"]
    if not results:
        return None

    best = results[0]  # Open-Meteo already sorts by relevance and population
    if language != "en":
        best = await _in_english(client, best)  # keep every result in English, whatever the input language
    if str(best.get("feature_code", "")).startswith("PCL"):
        _reject_country(best["name"])

    extra = [p for p in (best.get("admin1"), best.get("country")) if p and p != best["name"]]
    return Location(
        search_text=text,
        name=best["name"],
        full_name=", ".join([best["name"], *extra]),
        country_code=best.get("country_code"),
        latitude=best["latitude"],
        longitude=best["longitude"],
    )


async def _search_nominatim(client: httpx.AsyncClient, text: str) -> Location | None:
    global _nominatim_last_call
    async with _nominatim_lock:
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _nominatim_last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await client.get(
                NOMINATIM_URL,
                params={
                    "q": text,
                    "format": "jsonv2",
                    "limit": 1,
                    "addressdetails": 1,
                    "accept-language": "en",
                },
            )
        finally:
            _nominatim_last_call = time.monotonic()
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None

    best = results[0]
    name = best.get("name") or best["display_name"].split(",")[0]
    if best.get("addresstype") == "country":
        _reject_country(name)

    country_code = (best.get("address") or {}).get("country_code")
    return Location(
        search_text=text,
        name=name,
        full_name=best["display_name"],
        country_code=country_code.upper() if country_code else None,
        latitude=float(best["lat"]),
        longitude=float(best["lon"]),
    )
