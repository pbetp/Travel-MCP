"""Travel MCP Server: weather, accommodation and places for Claude.

This file is the "shop window" that Claude sees:
- functions decorated with @mcp.tool() are the tools Claude can call
- the docstring and the Field(...) descriptions are what Claude reads to decide when to call a tool
  and which values to pass
- the returned types (WeatherReport etc.) become the outputSchema, so Claude knows the result shape

The real data fetching lives in the thai_travel/ package; this file only wires the pieces together.
"""

import datetime as dt
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from thai_travel import geocoding, links, net, osm, weather
from thai_travel.models import BookingLinks, HotelSearchResult, PlaceSearchResult, WeatherReport

INSTRUCTIONS = """\
Travel-planning data for any city or place in the world. Place names can be given in any language \
(Thai, English, ...).
- get_weather: daily forecast from Open-Meteo, up to 16 days ahead
- search_hotels: real accommodation listings from OpenStreetMap
- find_places: attractions, temples, restaurants, cafes, markets and more from OpenStreetMap

Rules:
- Reply in the same language the user wrote in. Tool results are in English: translate them for the \
user, and keep local place names (see `local_name`) when they help the user find the place.
- There is no price, availability or review data. Never guess prices; give the user booking_links or \
map_url to check them.
- Always check location.full_name in the result to confirm it is the place the user meant. If not, \
make the name more specific and search again.
- OpenStreetMap data is volunteer-edited and may be incomplete or out of date.
"""

mcp = FastMCP("thai-travel", instructions=INSTRUCTIONS)

# Every tool only reads data from external websites and changes nothing, so calling one repeatedly is safe
READ_ONLY_WEB = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True)

LocationArg = Annotated[
    str,
    Field(
        description="City or place name in any language, e.g. 'Chiang Mai', 'เชียงใหม่', 'Patong', 'Tokyo'. "
        "If the name is ambiguous, add the region or country, e.g. 'Nimmanhaemin, Chiang Mai'. "
        "A whole country is too broad and is rejected."
    ),
]


@mcp.tool(title="Weather forecast", annotations=READ_ONLY_WEB)
async def get_weather(
    location: LocationArg,
    days: Annotated[int, Field(ge=1, le=16, description="Number of days to show")] = 3,
    start_date: Annotated[
        dt.date | None,
        Field(description="First day to show (YYYY-MM-DD). Default: today. At most 16 days ahead."),
    ] = None,
) -> WeatherReport:
    """Daily weather forecast for any city or place in the world.

    Gives conditions, min/max temperature, chance of rain and rainfall amount for each day.
    """
    async with net.make_client() as client:
        place = await geocoding.geocode(client, location)
        return await weather.get_forecast(client, place, days=days, start_date=start_date)


@mcp.tool(title="Find accommodation", annotations=READ_ONLY_WEB)
async def search_hotels(
    location: LocationArg,
    lodging_type: Annotated[
        osm.LodgingType,
        Field(description="Type of accommodation: hotel, hostel, guest_house, motel, apartment, or any (all types)"),
    ] = "any",
    min_stars: Annotated[
        int, Field(ge=0, le=5, description="Minimum stars. 0 = no filter (if set, only places with star data remain)")
    ] = 0,
    sort_by: Annotated[
        Literal["distance", "stars"],
        Field(description="distance = nearest to the centre first, stars = most stars first"),
    ] = "distance",
    radius_km: Annotated[float, Field(ge=0.5, le=20, description="Search radius around the centre (km)")] = 5,
    limit: Annotated[int, Field(ge=1, le=30, description="Maximum number of places to return")] = 15,
    check_in: Annotated[
        dt.date | None, Field(description="Check-in date (YYYY-MM-DD), used to fill in the booking links")
    ] = None,
    check_out: Annotated[
        dt.date | None, Field(description="Check-out date (YYYY-MM-DD). Default: one night after check-in")
    ] = None,
    adults: Annotated[int, Field(ge=1, le=16, description="Number of adults, used in the booking links")] = 2,
) -> HotelSearchResult:
    """Find real accommodation (hotels, hostels, guesthouses...) around any city or place, from OpenStreetMap.

    There is NO price, availability or review data. If the user mentions a budget, do not guess prices:
    pick a lodging_type that fits the budget (hostel/guest_house are usually cheaper than hotel),
    then give the user the booking_links to see real prices. If the stay dates are known, pass
    check_in/check_out so the links are filled in.
    """
    # Validate dates before touching the network so mistakes are reported instantly
    check_in, check_out = links.resolve_stay_dates(check_in, check_out)

    async with net.make_client() as client:
        place = await geocoding.geocode(client, location)
        hotels, total, truncated = await osm.search_lodging(
            client,
            place,
            radius_km=radius_km,
            lodging_type=lodging_type,
            min_stars=min_stars,
            sort_by=sort_by,
            limit=limit,
        )

    return HotelSearchResult(
        location=place,
        radius_km=radius_km,
        total_found=total,
        truncated=truncated,
        hotels=hotels,
        # Use the user's search text: place.name may be a single business that was used as the centre
        booking_links=BookingLinks(
            google_maps=links.google_maps_search(f"hotels near {place.search_text}"),
            booking_com=links.booking_search(
                place.search_text, check_in=check_in, check_out=check_out, adults=adults
            ),
        ),
        price_note="No price or availability data. Ask the user to open the links in booking_links, "
        "or the map_url of a place, to see real prices.",
    )


@mcp.tool(title="Find places", annotations=READ_ONLY_WEB)
async def find_places(
    location: LocationArg,
    category: Annotated[
        osm.PlaceCategory,
        Field(
            description="Category: attraction (sights, museums, viewpoints), temple (Buddhist temples), "
            "restaurant, cafe, market, shopping (malls), park, nightlife (bars, pubs, clubs)"
        ),
    ],
    sort_by: Annotated[
        Literal["popular", "distance"],
        Field(description="popular = best-known first (good for sights), distance = nearest to the centre first"),
    ] = "popular",
    radius_km: Annotated[float, Field(ge=0.5, le=20, description="Search radius around the centre (km)")] = 3,
    limit: Annotated[int, Field(ge=1, le=30, description="Maximum number of places to return")] = 15,
) -> PlaceSearchResult:
    """Find real places (sights, temples, restaurants, cafes...) around any city or point, from OpenStreetMap.

    There are no user reviews or ratings (popularity_score is only a rough estimate from map data).
    If the user wants reviews, give them the map_url.
    """
    async with net.make_client() as client:
        place = await geocoding.geocode(client, location)
        places, total, truncated = await osm.search_places(
            client, place, category=category, radius_km=radius_km, sort_by=sort_by, limit=limit
        )

    return PlaceSearchResult(
        location=place,
        category=osm.PLACE_CATEGORIES[category][0],
        radius_km=radius_km,
        total_found=total,
        truncated=truncated,
        places=places,
    )


@mcp.prompt(title="Plan a trip")
def plan_trip(
    destination: str,
    days: int = 3,
    start_date: str | None = None,
    budget: str | None = None,
) -> str:
    """Prompt template for a full trip plan: weather + accommodation + sights + food, arranged day by day."""
    when = f" starting {start_date}" if start_date else ""
    budget_text = f" Accommodation budget: {budget}." if budget else ""
    steps = [
        "Call get_weather for the travel dates. If the trip is more than 16 days away, describe the "
        "typical seasonal weather from general knowledge instead and say clearly that it is not a forecast.",
        "Call search_hotels. If there is a budget, choose a lodging_type that fits it (low budget: hostel or "
        "guest_house). Never guess prices; include booking_links so the user can check real prices.",
        "Call find_places with the categories attraction and temple for things to see, and restaurant or "
        "cafe for meals.",
        "Arrange everything into a day-by-day plan. Group places that are close together (see distance_km) "
        "on the same day, and prefer indoor activities on days with a high chance of rain.",
        "Finish with the accommodation booking links and a short note on the data limits "
        "(no prices; map data may be incomplete).",
    ]
    numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(steps, start=1))
    return f"Help me plan a {days}-day trip to {destination}{when}.{budget_text}\n\nSteps:\n{numbered}\n"


if __name__ == "__main__":
    mcp.run()
