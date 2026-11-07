import httpx
import pytest
from samples import (
    CHIANG_MAI_EN_RECORD,
    CHIANG_MAI_OM,
    CHIANG_MAI_TH,
    NOMINATIM,
    OPEN_METEO_GEO,
    PATONG_NOMINATIM,
    om_place,
    om_results,
)

from thai_travel import geocoding
from thai_travel.errors import LocationNotFoundError, UpstreamServiceError

pytestmark = pytest.mark.anyio


async def test_english_city_is_found_by_open_meteo(web, client):
    web.on(OPEN_METEO_GEO, CHIANG_MAI_OM)

    loc = await geocoding.geocode(client, "  Chiang Mai ")

    assert loc.name == "Chiang Mai"
    assert loc.full_name == "Chiang Mai, Chiang Mai Province, Thailand"
    assert loc.country_code == "TH"
    assert (loc.latitude, loc.longitude) == (18.79038, 98.98468)
    assert web.hosts() == [OPEN_METEO_GEO]  # one request: found, no translation needed, no Nominatim
    assert web.requests[0].url.params["name"] == "Chiang Mai"
    assert web.requests[0].url.params["language"] == "en"


async def test_thai_input_is_searched_in_thai_but_returned_in_english(web, client):
    # Seen for real: with language=en Open-Meteo finds nothing for Thai text, so Thai input must be
    # searched with language=th, then the English record is fetched by id.
    def open_meteo(request):
        if request.url.path.endswith("/search"):
            return CHIANG_MAI_TH if request.url.params["language"] == "th" else om_results()
        return CHIANG_MAI_EN_RECORD

    web.on(OPEN_METEO_GEO, open_meteo)

    loc = await geocoding.geocode(client, "เชียงใหม่")

    assert loc.name == "Chiang Mai"
    assert loc.full_name == "Chiang Mai, Chiang Mai Province, Thailand"
    assert loc.search_text == "เชียงใหม่"
    search, lookup = web.requests
    assert search.url.params["language"] == "th"
    assert (lookup.url.path, lookup.url.params["id"], lookup.url.params["language"]) == ("/v1/get", "1153671", "en")
    assert web.hosts() == [OPEN_METEO_GEO, OPEN_METEO_GEO]  # Nominatim was not needed


async def test_thai_input_keeps_thai_record_when_english_lookup_fails(web, client):
    def open_meteo(request):
        return CHIANG_MAI_TH if request.url.path.endswith("/search") else httpx.Response(500)

    web.on(OPEN_METEO_GEO, open_meteo)

    loc = await geocoding.geocode(client, "เชียงใหม่")

    assert loc.name == "เชียงใหม่"  # still usable, just not translated
    assert (loc.latitude, loc.longitude) == (18.79038, 98.98468)


async def test_falls_back_to_nominatim_for_neighbourhoods(web, client):
    web.on(OPEN_METEO_GEO, om_results())  # Open-Meteo cannot find neighbourhoods
    web.on(NOMINATIM, PATONG_NOMINATIM)

    loc = await geocoding.geocode(client, "Patong")

    assert loc.name == "Patong"
    assert "Phuket" in loc.full_name
    assert loc.country_code == "TH"
    assert loc.latitude == pytest.approx(7.8949476)
    assert web.hosts() == [OPEN_METEO_GEO, NOMINATIM]
    assert web.requests[1].url.params["accept-language"] == "en"


async def test_alias_is_expanded_before_searching(web, client):
    web.on(OPEN_METEO_GEO, om_results(om_place("Bangkok", 13.75, 100.50, "PPLC")))

    loc = await geocoding.geocode(client, "กทม")

    assert loc.name == "Bangkok"
    assert loc.search_text == "กรุงเทพมหานคร"  # reused in booking links, which do not know the abbreviation
    assert web.requests[0].url.params["name"] == "กรุงเทพมหานคร"


async def test_airports_are_skipped(web, client):
    web.on(
        OPEN_METEO_GEO,
        om_results(
            om_place("Chiang Mai International Airport", 18.77, 98.96, "AIRP"),
            om_place("Chiang Mai", 18.79, 98.98, "PPLA"),
        ),
    )

    loc = await geocoding.geocode(client, "Chiang Mai")

    assert loc.name == "Chiang Mai"


async def test_country_is_rejected_as_too_broad(web, client):
    web.on(OPEN_METEO_GEO, om_results(om_place("Japan", 35.68, 139.75, "PCLI")))

    with pytest.raises(LocationNotFoundError, match="is a country"):
        await geocoding.geocode(client, "Japan")


async def test_country_from_nominatim_is_rejected(web, client):
    web.on(OPEN_METEO_GEO, om_results())
    web.on(NOMINATIM, [{"name": "Singapore", "display_name": "Singapore", "lat": "1.35",
                        "lon": "103.8", "addresstype": "country"}])

    with pytest.raises(LocationNotFoundError, match="is a country"):
        await geocoding.geocode(client, "Republic of Singapore")


async def test_unknown_place_raises_not_found_with_hint(web, client):
    web.on(OPEN_METEO_GEO, om_results())
    web.on(NOMINATIM, [])

    with pytest.raises(LocationNotFoundError, match="Could not find a place"):
        await geocoding.geocode(client, "Nowhereville")


async def test_open_meteo_outage_still_tries_nominatim(web, client):
    web.on(OPEN_METEO_GEO, 500)
    web.on(NOMINATIM, PATONG_NOMINATIM)

    loc = await geocoding.geocode(client, "Patong")

    assert loc.name == "Patong"


async def test_both_services_down_is_reported_as_service_error(web, client):
    web.on(OPEN_METEO_GEO, 500)
    web.on(NOMINATIM, 503)

    with pytest.raises(UpstreamServiceError):
        await geocoding.geocode(client, "Chiang Mai")


async def test_results_are_cached(web, client):
    web.on(OPEN_METEO_GEO, CHIANG_MAI_OM)

    first = await geocoding.geocode(client, "Chiang Mai")
    second = await geocoding.geocode(client, "Chiang Mai")

    assert first == second
    assert len(web.requests) == 1


async def test_blank_query_is_rejected_without_network(web, client):
    with pytest.raises(LocationNotFoundError):
        await geocoding.geocode(client, "   ")
    assert web.requests == []
