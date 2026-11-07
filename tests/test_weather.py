import datetime as dt

import httpx
import pytest
from samples import OPEN_METEO_FORECAST, forecast

from thai_travel import weather
from thai_travel.errors import InvalidRequestError, UpstreamServiceError
from thai_travel.models import Location

pytestmark = pytest.mark.anyio

CHIANG_MAI = Location(search_text="Chiang Mai", name="Chiang Mai", full_name="Chiang Mai, Thailand",
                      country_code="TH", latitude=18.79038, longitude=98.98468)
TODAY = dt.date(2026, 11, 7)


async def test_parses_daily_forecast(web, client):
    web.on(OPEN_METEO_FORECAST, forecast(
        ("2026-11-07", 95, 22.6, 27.1, 100, 19.0),
        ("2026-11-08", 1, 21.9, 28.1, 10, 0.0),
    ))

    report = await weather.get_forecast(client, CHIANG_MAI, days=2)

    assert report.timezone == "Asia/Bangkok"
    assert [d.date for d in report.days] == [dt.date(2026, 11, 7), dt.date(2026, 11, 8)]
    assert report.days[0].condition == "Thunderstorm"
    assert report.days[0].rain_chance_percent == 100
    assert report.days[1].condition == "Mainly clear"
    assert report.days[1].temp_max_c == 28.1
    assert "Open-Meteo" in report.attribution

    params = web.requests[0].url.params
    assert params["forecast_days"] == "2"
    assert params["timezone"] == "auto"
    assert "start_date" not in params


async def test_start_date_requests_exact_range(web, client):
    web.on(OPEN_METEO_FORECAST, forecast(("2026-11-20", 3, 20, 30, 5, 0), ("2026-11-21", 3, 20, 30, 5, 0)))

    await weather.get_forecast(client, CHIANG_MAI, days=2, start_date=dt.date(2026, 11, 20), today=TODAY)

    params = web.requests[0].url.params
    assert (params["start_date"], params["end_date"]) == ("2026-11-20", "2026-11-21")
    assert "forecast_days" not in params


async def test_dates_beyond_forecast_horizon_are_rejected_early(web, client):
    last_day = TODAY + dt.timedelta(days=15)

    with pytest.raises(InvalidRequestError, match="16 days"):
        await weather.get_forecast(client, CHIANG_MAI, days=2, start_date=last_day, today=TODAY)
    assert web.requests == []


async def test_last_forecastable_day_is_allowed(web, client):
    last_day = TODAY + dt.timedelta(days=15)
    web.on(OPEN_METEO_FORECAST, forecast((last_day.isoformat(), 0, 20, 30, 0, 0)))

    report = await weather.get_forecast(client, CHIANG_MAI, days=1, start_date=last_day, today=TODAY)

    assert report.days[0].date == last_day


async def test_missing_values_become_none(web, client):
    web.on(OPEN_METEO_FORECAST, forecast(("2026-11-07", None, None, None, None, None)))

    report = await weather.get_forecast(client, CHIANG_MAI, days=1)

    day = report.days[0]
    assert day.condition == "No data"
    assert (day.temp_min_c, day.temp_max_c, day.rain_chance_percent, day.rain_mm) == (None, None, None, None)


def test_unknown_weather_code_is_reported_honestly():
    assert weather.describe(42) == "Unknown (code 42)"


async def test_open_meteo_400_reason_is_passed_on(web, client):
    web.on(OPEN_METEO_FORECAST, httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"}))

    with pytest.raises(InvalidRequestError, match="Latitude must be in range"):
        await weather.get_forecast(client, CHIANG_MAI)


async def test_outage_is_reported_as_service_error(web, client):
    web.on(OPEN_METEO_FORECAST, 503)

    with pytest.raises(UpstreamServiceError):
        await weather.get_forecast(client, CHIANG_MAI)


async def test_forecast_is_cached(web, client):
    web.on(OPEN_METEO_FORECAST, forecast(("2026-11-07", 0, 20, 30, 0, 0)))

    await weather.get_forecast(client, CHIANG_MAI, days=1)
    await weather.get_forecast(client, CHIANG_MAI, days=1)

    assert len(web.requests) == 1
