"""Find accommodation and places from OpenStreetMap through the Overpass API (free, no key).

How it works: we send a query like "give me places of type X that have a name, within R metres of
this point". Overpass answers with a JSON list, and we filter, sort and trim it in our own code.
"""

import asyncio
import logging
import math
from collections.abc import Iterator
from typing import Any, Literal

import httpx

from . import links
from .errors import UpstreamServiceError
from .models import Location, Lodging, Place
from .net import TTLCache

# Logs go to stderr (stdout is reserved for the MCP protocol); see your MCP client's server log.
logger = logging.getLogger(__name__)

# Public Overpass servers are free but, when tested for real, slow and often answer 502/504 ("queue full").
# So we use several servers plus spaced-out retries, as the Overpass operators recommend.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
# Declare the time/memory a query really needs: Overpass schedules queries by the declared values.
# The defaults are 180 s / 512 MB, far more than our small queries use.
OVERPASS_TIMEOUT_SECONDS = 20
OVERPASS_MAXSIZE_BYTES = 64 * 1024 * 1024
HEDGE_DELAY_SECONDS = 6.0  # if a server has not answered by then, also ask the next one in parallel
RETRY_DELAYS_SECONDS = (3.0, 6.0)  # on "queue full", wait and retry (exponential backoff)
# Total budget for all attempts. Kept under 60 s, where some MCP clients cut a tool call off.
TOTAL_DEADLINE_SECONDS = 50.0
RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_ELEMENTS = 1000  # cap on results per query, to keep responses a sensible size

# OSM tag tourism=... -> label
LODGING_TYPES = {
    "hotel": "Hotel",
    "hostel": "Hostel",
    "guest_house": "Guesthouse",
    "motel": "Motel",
    "apartment": "Apartment / holiday rental",
}
LodgingType = Literal["any", "hotel", "hostel", "guest_house", "motel", "apartment"]

# category -> (label, Overpass filters)
PLACE_CATEGORIES: dict[str, tuple[str, list[str]]] = {
    "attraction": (
        "Attraction",
        ['["tourism"~"^(attraction|viewpoint|museum|gallery|zoo|theme_park|aquarium)$"]'],
    ),
    "temple": ("Buddhist temple", ['["amenity"="place_of_worship"]["religion"="buddhist"]']),
    "restaurant": ("Restaurant", ['["amenity"="restaurant"]']),
    "cafe": ("Cafe", ['["amenity"="cafe"]']),
    "market": ("Market", ['["amenity"="marketplace"]']),
    "shopping": ("Shopping mall", ['["shop"~"^(mall|department_store)$"]']),
    "park": ("Park", ['["leisure"="park"]']),
    "nightlife": ("Bar / nightlife", ['["amenity"~"^(bar|pub|nightclub)$"]']),
}
PlaceCategory = Literal[
    "attraction", "temple", "restaurant", "cafe", "market", "shopping", "park", "nightlife"
]

# Labels for the specific sub-tag, to say exactly what a place is
KIND_LABELS = {
    "attraction": "Attraction",
    "viewpoint": "Viewpoint",
    "museum": "Museum",
    "gallery": "Gallery",
    "zoo": "Zoo",
    "theme_park": "Theme park",
    "aquarium": "Aquarium",
    "place_of_worship": "Buddhist temple",
    "restaurant": "Restaurant",
    "cafe": "Cafe",
    "marketplace": "Market",
    "mall": "Shopping mall",
    "department_store": "Department store",
    "park": "Park",
    "bar": "Bar",
    "pub": "Pub",
    "nightclub": "Nightclub",
}

_cache = TTLCache(ttl_seconds=6 * 3600, max_items=64)


def clear_cache() -> None:
    _cache.clear()


def build_query(filters: list[str], lat: float, lon: float, radius_m: int) -> str:
    """Build an Overpass QL query: everything matching the filters, with a name, within the radius."""
    parts = "".join(f'nwr{f}["name"](around:{radius_m},{lat:.5f},{lon:.5f});' for f in filters)
    return (
        f"[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}][maxsize:{OVERPASS_MAXSIZE_BYTES}];"
        f"({parts});"
        f"out center tags {MAX_ELEMENTS};"  # center = middle point of buildings/areas
    )


async def _fetch_from(client: httpx.AsyncClient, url: str, query: str) -> list[dict[str, Any]]:
    """Ask a single server. If its queue is full, wait and retry per RETRY_DELAYS_SECONDS."""
    for retry_delay in (*RETRY_DELAYS_SECONDS, None):
        resp = await client.post(url, data={"data": query}, timeout=OVERPASS_TIMEOUT_SECONDS + 15)
        if resp.status_code in RETRYABLE_STATUS and retry_delay is not None:
            logger.info("Overpass %s answered %s (queue full), retrying in %ss", url, resp.status_code, retry_delay)
            await asyncio.sleep(retry_delay)
            continue
        resp.raise_for_status()
        data = resp.json()
        # If a query runs out of time the server answers 200 with partial data and an error in `remark`
        if "error" in str(data.get("remark", "")).lower():
            raise ValueError(data["remark"])
        return data["elements"]
    raise AssertionError("unreachable")  # pragma: no cover


async def fetch_elements(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """Send a query to Overpass using hedged requests.

    Start with the first server. If it fails or takes longer than HEDGE_DELAY_SECONDS, also ask the
    next one. Whoever answers successfully first wins and the rest are cancelled. This is almost as
    fast as asking every server at once, without adding load on free services when the first one is quick.
    """
    cached = _cache.get(query)
    if cached is not None:
        return cached

    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_DEADLINE_SECONDS
    remaining_urls = iter(OVERPASS_URLS)
    running: set[asyncio.Task[list[dict[str, Any]]]] = set()

    def launch_next() -> None:
        url = next(remaining_urls, None)
        if url is not None:
            logger.info("Overpass: sending request to %s", url)
            running.add(asyncio.create_task(_fetch_from(client, url, query)))

    launch_next()
    try:
        while running and (time_left := deadline - loop.time()) > 0:
            done, _ = await asyncio.wait(
                running, timeout=min(HEDGE_DELAY_SECONDS, time_left), return_when=asyncio.FIRST_COMPLETED
            )
            if not done:  # nobody has answered yet -> add a backup server in parallel
                launch_next()
                continue
            # at least one server has finished, successfully or not
            for task in done:
                running.discard(task)
                error = task.exception()
                if error is None:
                    elements = task.result()
                    _cache.set(query, elements)
                    return elements
                logger.info("Overpass: a server failed (%s: %s)", type(error).__name__, error)
                launch_next()  # this one failed -> try the next one right away
    finally:
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    raise UpstreamServiceError(
        "The map server (OpenStreetMap/Overpass) is not responding. This happens often with free "
        "public servers. Please try again shortly, or use a smaller radius."
    )


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance over the Earth's surface (haversine formula)."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_stars(raw: str | None) -> int | None:
    """OSM stores stars as text such as '3', '4S' or '3.5' -> take the first digit."""
    if raw and raw[0].isdigit() and 1 <= int(raw[0]) <= 7:
        return int(raw[0])
    return None


def pick_names(tags: dict[str, str]) -> tuple[str, str | None]:
    """Return (name, local_name): the English name if there is one, plus the local-script name.

    `name` is what OpenStreetMap calls the place locally (Thai in Thailand, Japanese in Japan...).
    We show the English name first so every user gets a readable result, and keep the local name
    because that is what is written on signs and in navigation apps.
    """
    local = tags["name"]
    name = tags.get("name:en") or local
    return name, (local if local != name else None)


def popularity_score(tags: dict[str, str]) -> int:
    """Estimate how well known a place is from OSM data alone (no extra API calls).

    Famous places tend to have names in many languages (name:en, name:zh, name:ja ...) and a
    Wikipedia article. Checked against 92 temples in Chiang Mai: the top of the list was
    Wat Phra Singh, Wat Chedi Luang, Wat Phan Tao, Wat Chiang Man.
    """
    languages = sum(1 for key in tags if key.startswith("name:"))
    return (
        languages
        + 2 * bool(tags.get("wikipedia"))
        + bool(tags.get("wikidata"))
        + bool(tags.get("heritage") or tags.get("tourism") == "attraction")
    )


def _coords(element: dict[str, Any]) -> tuple[float, float] | None:
    if "lat" in element:  # node = a point
        return element["lat"], element["lon"]
    center = element.get("center")  # way/relation = a building or an area
    if center:
        return center["lat"], center["lon"]
    return None


def _iter_unique(
    elements: list[dict[str, Any]], origin: Location
) -> Iterator[tuple[dict[str, str], float]]:
    """Yield only places that have coordinates, dropping duplicates (some are mapped as both a point and a building)."""
    seen: set[tuple[str, float, float]] = set()
    for el in elements:
        tags = el.get("tags") or {}
        coords = _coords(el)
        if not tags.get("name") or coords is None:
            continue
        key = (tags["name"].casefold(), round(coords[0], 3), round(coords[1], 3))
        if key in seen:
            continue
        seen.add(key)
        dist = distance_km(origin.latitude, origin.longitude, *coords)
        yield tags, round(dist, 2)


def _map_url(tags: dict[str, str], origin: Location) -> str:
    # Use the text the user searched, not origin.name, because Nominatim may pick a single business
    # as the centre (seen for real: searching "นิมมานเหมินท์ เชียงใหม่" returned one guesthouse).
    locality = tags.get("addr:city") or origin.search_text
    return links.google_maps_search(f"{tags['name']}, {locality}")


async def search_lodging(
    client: httpx.AsyncClient,
    origin: Location,
    *,
    radius_km: float = 5,
    lodging_type: LodgingType = "any",
    min_stars: int = 0,
    sort_by: Literal["distance", "stars"] = "distance",
    limit: int = 15,
) -> tuple[list[Lodging], int, bool]:
    """Return (lodging after applying `limit`, total matches, whether results hit the element cap)."""
    types = list(LODGING_TYPES) if lodging_type == "any" else [lodging_type]
    tag_filter = f'["tourism"~"^({"|".join(types)})$"]'
    query = build_query([tag_filter], origin.latitude, origin.longitude, int(radius_km * 1000))
    elements = await fetch_elements(client, query)

    hotels = []
    for tags, dist in _iter_unique(elements, origin):
        stars = parse_stars(tags.get("stars"))
        if min_stars > 0 and (stars is None or stars < min_stars):
            continue
        name, local_name = pick_names(tags)
        hotels.append(
            Lodging(
                name=name,
                local_name=local_name,
                type=LODGING_TYPES.get(tags.get("tourism", ""), "Accommodation"),
                stars=stars,
                distance_km=dist,
                website=tags.get("website") or tags.get("contact:website"),
                phone=tags.get("phone") or tags.get("contact:phone"),
                map_url=_map_url(tags, origin),
            )
        )

    if sort_by == "stars":
        # rated places first, most stars first, then nearest first
        hotels.sort(key=lambda h: (h.stars is None, -(h.stars or 0), h.distance_km))
    else:
        hotels.sort(key=lambda h: h.distance_km)
    return hotels[:limit], len(hotels), len(elements) >= MAX_ELEMENTS


async def search_places(
    client: httpx.AsyncClient,
    origin: Location,
    *,
    category: PlaceCategory,
    radius_km: float = 3,
    sort_by: Literal["popular", "distance"] = "popular",
    limit: int = 15,
) -> tuple[list[Place], int, bool]:
    """Return (places after applying `limit`, total matches, whether results hit the element cap)."""
    label, filters = PLACE_CATEGORIES[category]
    query = build_query(filters, origin.latitude, origin.longitude, int(radius_km * 1000))
    elements = await fetch_elements(client, query)

    places = []
    for tags, dist in _iter_unique(elements, origin):
        name, local_name = pick_names(tags)
        subtype = tags.get("tourism") or tags.get("amenity") or tags.get("shop") or tags.get("leisure")
        places.append(
            Place(
                name=name,
                local_name=local_name,
                kind=KIND_LABELS.get(subtype or "", label),
                distance_km=dist,
                popularity_score=popularity_score(tags),
                cuisine=tags.get("cuisine"),
                opening_hours=tags.get("opening_hours"),
                website=tags.get("website") or tags.get("contact:website"),
                map_url=_map_url(tags, origin),
            )
        )

    if sort_by == "popular":
        # Sorting by distance alone would fill the list with minor places that happen to be near the centre
        places.sort(key=lambda p: (-p.popularity_score, p.distance_km))
    else:
        places.sort(key=lambda p: p.distance_km)
    return places[:limit], len(places), len(elements) >= MAX_ELEMENTS
