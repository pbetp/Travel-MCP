"""Daily weather forecasts from Open-Meteo (free, no API key)."""

import datetime as dt

import httpx

from .errors import InvalidRequestError, UpstreamServiceError
from .models import DailyForecast, Location, WeatherReport
from .net import TTLCache

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MAX_FORECAST_DAYS = 16  # Open-Meteo forecasts at most 16 days ahead
MAX_PAST_DAYS = 30  # past data is allowed (Open-Meteo supports ~3 months, but this is plenty)

# WMO weather interpretation codes used by Open-Meteo
WMO_CONDITIONS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

_cache = TTLCache(ttl_seconds=30 * 60)


def clear_cache() -> None:
    _cache.clear()


def describe(code: int | None) -> str:
    if code is None:
        return "No data"
    return WMO_CONDITIONS.get(code, f"Unknown (code {code})")


async def get_forecast(
    client: httpx.AsyncClient,
    location: Location,
    days: int = 3,
    start_date: dt.date | None = None,
    today: dt.date | None = None,
) -> WeatherReport:
    """Daily forecast for `days` days starting at `start_date` (default: today)."""
    if not 1 <= days <= MAX_FORECAST_DAYS:
        raise InvalidRequestError(f"Number of days must be between 1 and {MAX_FORECAST_DAYS}.")

    params = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
        "precipitation_probability_max,precipitation_sum",
        "timezone": "auto",  # dates are local dates of the place
    }
    if start_date is None:
        params["forecast_days"] = days
    else:
        today = today or dt.date.today()
        end_date = start_date + dt.timedelta(days=days - 1)
        last_allowed = today + dt.timedelta(days=MAX_FORECAST_DAYS - 1)
        if end_date > last_allowed:
            raise InvalidRequestError(
                f"Forecasts are only available up to {last_allowed.isoformat()} "
                f"({MAX_FORECAST_DAYS} days from today). For a later trip, describe the typical "
                "seasonal weather instead and say clearly that it is not a forecast."
            )
        if start_date < today - dt.timedelta(days=MAX_PAST_DAYS):
            raise InvalidRequestError(f"Past data is limited to {MAX_PAST_DAYS} days back.")
        params["start_date"] = start_date.isoformat()
        params["end_date"] = end_date.isoformat()

    cache_key = (round(location.latitude, 3), round(location.longitude, 3), days, start_date)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached.model_copy(update={"location": location})

    try:
        resp = await client.get(FORECAST_URL, params=params)
        if resp.status_code == 400:
            # Open-Meteo explains why in the `reason` field, e.g. a date outside the allowed range
            raise InvalidRequestError(f"Open-Meteo rejected the request: {resp.json().get('reason', resp.text)}")
        resp.raise_for_status()
        data = resp.json()
        daily = data["daily"]
        forecasts = [
            DailyForecast(
                date=day,
                condition=describe(daily["weather_code"][i]),
                temp_min_c=daily["temperature_2m_min"][i],
                temp_max_c=daily["temperature_2m_max"][i],
                rain_chance_percent=daily["precipitation_probability_max"][i],
                rain_mm=daily["precipitation_sum"][i],
            )
            for i, day in enumerate(daily["time"])
        ]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        raise UpstreamServiceError("Could not fetch weather data from Open-Meteo. Please try again.") from e

    report = WeatherReport(location=location, timezone=data.get("timezone"), days=forecasts)
    _cache.set(cache_key, report)
    return report
