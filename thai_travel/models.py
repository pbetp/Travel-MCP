"""Data structures returned by the tools.

FastMCP turns these classes into the MCP `outputSchema` automatically, so Claude knows the shape
of every result in advance, and the descriptions in Field(...) are sent to Claude as well.
"""

import datetime as dt

from pydantic import BaseModel, Field

OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
OPEN_METEO_ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"


class Location(BaseModel):
    search_text: str = Field(
        description="The text actually searched (after expanding abbreviations, e.g. 'กทม' -> 'กรุงเทพมหานคร')"
    )
    name: str = Field(description="Name of the place that was found")
    full_name: str = Field(
        description="Full name with region/country. Check this matches the place the user meant"
    )
    country_code: str | None = Field(default=None, description="Two-letter country code, e.g. TH")
    latitude: float
    longitude: float


# ---------- Weather ----------


class DailyForecast(BaseModel):
    date: dt.date
    condition: str = Field(description="Overall weather for the day")
    temp_min_c: float | None = Field(description="Lowest temperature (°C)")
    temp_max_c: float | None = Field(description="Highest temperature (°C)")
    rain_chance_percent: int | None = Field(description="Highest chance of precipitation during the day (%)")
    rain_mm: float | None = Field(description="Total precipitation for the day (mm)")


class WeatherReport(BaseModel):
    location: Location
    timezone: str | None = Field(description="Time zone of the place; all dates are local dates in this zone")
    days: list[DailyForecast]
    attribution: str = OPEN_METEO_ATTRIBUTION


# ---------- Accommodation ----------


class Lodging(BaseModel):
    name: str = Field(description="English name if OpenStreetMap has one, otherwise the local name")
    local_name: str | None = Field(description="Name in the local language/script, if different from `name`")
    type: str = Field(description="Type of accommodation")
    stars: int | None = Field(description="Star rating. None = no data (it does NOT mean the place has no stars)")
    distance_km: float = Field(description="Straight-line distance from the centre of the searched place (km)")
    website: str | None
    phone: str | None
    map_url: str = Field(description="Google Maps link for this place, where the user can see prices, reviews, photos")


class BookingLinks(BaseModel):
    google_maps: str = Field(description="Search for accommodation around this place on Google Maps")
    booking_com: str = Field(
        description="Search on Booking.com, with dates and number of guests filled in when they were given"
    )


class HotelSearchResult(BaseModel):
    location: Location
    radius_km: float
    total_found: int = Field(description="Number of matching places before applying `limit`")
    truncated: bool = Field(description="True = too much data, results may be incomplete; try a smaller radius")
    hotels: list[Lodging]
    booking_links: BookingLinks
    price_note: str
    attribution: str = OSM_ATTRIBUTION


# ---------- General places ----------


class Place(BaseModel):
    name: str = Field(description="English name if OpenStreetMap has one, otherwise the local name")
    local_name: str | None = Field(description="Name in the local language/script, if different from `name`")
    kind: str = Field(description="Type of place")
    distance_km: float = Field(description="Straight-line distance from the centre of the searched place (km)")
    popularity_score: int = Field(
        description="Rough fame estimate (languages the name exists in + Wikipedia article). Higher = better known"
    )
    cuisine: str | None = Field(default=None, description="Cuisine (restaurants only)")
    opening_hours: str | None = Field(default=None, description="Opening hours in OpenStreetMap format, if known")
    website: str | None = None
    map_url: str


class PlaceSearchResult(BaseModel):
    location: Location
    category: str
    radius_km: float
    total_found: int
    truncated: bool
    places: list[Place]
    attribution: str = OSM_ATTRIBUTION
