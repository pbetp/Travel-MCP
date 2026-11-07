"""Tests against the real APIs over the internet. Not run by default.

Run with: pytest -m live
Use it to check that the external services still answer in the format we expect
(Overpass may be slow or down temporarily, which can make these tests fail).
"""

import pytest

from thai_travel import geocoding, net, osm, weather

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_geocode_thai_and_english_names():
    async with net.make_client() as client:
        thai = await geocoding.geocode(client, "เชียงใหม่")
        english = await geocoding.geocode(client, "Chiang Mai")
        spot = await geocoding.geocode(client, "ป่าตอง")  # must fall through to Nominatim

    assert thai.country_code == "TH"
    assert thai.name == "Chiang Mai"  # Thai input, English output
    assert (thai.latitude, thai.longitude) == (english.latitude, english.longitude)
    assert "Phuket" in spot.full_name


async def test_live_weather_uses_local_timezone():
    async with net.make_client() as client:
        place = await geocoding.geocode(client, "Tokyo")
        report = await weather.get_forecast(client, place, days=2)

    assert len(report.days) == 2
    assert report.timezone == "Asia/Tokyo"


async def test_live_temples_rank_famous_first():
    async with net.make_client() as client:
        place = await geocoding.geocode(client, "Chiang Mai")
        temples, total, _ = await osm.search_places(client, place, category="temple", limit=5)

    assert total > 20
    names = " ".join(t.name for t in temples)
    assert "Phra Singh" in names or "Chedi Luang" in names
