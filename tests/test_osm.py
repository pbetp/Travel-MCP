import asyncio
import time
from urllib.parse import parse_qs

import httpx
import pytest
from samples import OVERPASS_MAIN, OVERPASS_SECOND, OVERPASS_THIRD, node, overpass, way

from thai_travel import osm
from thai_travel.errors import UpstreamServiceError
from thai_travel.models import Location

pytestmark = pytest.mark.anyio

CENTER = Location(search_text="Chiang Mai", name="Chiang Mai", full_name="Chiang Mai, Thailand",
                  country_code="TH", latitude=18.79038, longitude=98.98468)


def query_sent(request: httpx.Request) -> str:
    return parse_qs(request.content.decode())["data"][0]


# ---------- helper functions ----------


def test_build_query_asks_for_named_places_around_point():
    q = osm.build_query(['["amenity"="cafe"]'], 18.79038, 98.98468, 3000)

    assert q.startswith("[out:json][timeout:20][maxsize:")
    assert 'nwr["amenity"="cafe"]["name"](around:3000,18.79038,98.98468);' in q
    assert q.endswith(f"out center tags {osm.MAX_ELEMENTS};")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("3", 3), ("4S", 4), ("3.5", 3), ("5", 5), (None, None), ("", None), ("0", None), ("yes", None)],
)
def test_parse_stars(raw, expected):
    assert osm.parse_stars(raw) == expected


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        # English name first, local-script name kept as the second name
        ({"name": "วัดพันเตา", "name:en": "Wat Phan Tao"}, ("Wat Phan Tao", "วัดพันเตา")),
        ({"name": "浅草寺", "name:en": "Senso-ji"}, ("Senso-ji", "浅草寺")),
        # no English name: the local name is all we have, and there is no second name
        ({"name": "วัดเล็ก"}, ("วัดเล็ก", None)),
        # the local name is already English: nothing to add
        ({"name": "Parasol Inn"}, ("Parasol Inn", None)),
        ({"name": "Parasol Inn", "name:en": "Parasol Inn"}, ("Parasol Inn", None)),
    ],
)
def test_pick_names(tags, expected):
    assert osm.pick_names(tags) == expected


def test_popularity_prefers_famous_places():
    famous = {"name": "วัดพระสิงห์", "name:en": "Wat Phra Singh", "name:zh": "帕辛寺", "name:ja": "ワット・プラシン",
              "wikipedia": "th:วัดพระสิงห์", "wikidata": "Q1"}
    unknown = {"name": "วัดเล็ก"}

    assert osm.popularity_score(famous) > osm.popularity_score(unknown) == 0


def test_distance_km():
    # Chiang Mai -> Bangkok is roughly 580 km as the crow flies
    assert osm.distance_km(18.79, 98.98, 13.75, 100.50) == pytest.approx(580, rel=0.02)


# ---------- accommodation search ----------

HOTELS = overpass(
    node(1, 18.80, 98.99, {"name": "Far Hotel", "tourism": "hotel", "stars": "5"}),
    way(2, 18.7905, 98.9848, {"name": "Near Hostel", "tourism": "hostel", "contact:website": "https://near.example"}),
    node(3, 18.795, 98.985,
         {"name": "Mid Guesthouse", "tourism": "guest_house", "stars": "3", "addr:city": "Chiang Mai"}),
    node(4, 18.795, 98.985, {"name": "Mid Guesthouse", "tourism": "guest_house"}),  # duplicate, should be dropped
    node(5, 18.79, 98.98, {"tourism": "hotel"}),  # no name, should be dropped
    {"type": "relation", "id": 6, "tags": {"name": "No coords", "tourism": "hotel"}},  # no coordinates
)


async def test_lodging_is_parsed_deduplicated_and_sorted_by_distance(web, client):
    web.on(OVERPASS_MAIN, HOTELS)

    hotels, total, truncated = await osm.search_lodging(client, CENTER)

    assert [h.name for h in hotels] == ["Near Hostel", "Mid Guesthouse", "Far Hotel"]
    assert (total, truncated) == (3, False)
    near = hotels[0]
    assert near.type == "Hostel"
    assert near.stars is None
    assert near.website == "https://near.example"
    assert near.distance_km < 0.1
    assert "Near+Hostel" in near.map_url
    assert "Chiang+Mai" in hotels[1].map_url  # uses addr:city when present


async def test_lodging_sorted_by_stars(web, client):
    web.on(OVERPASS_MAIN, HOTELS)

    hotels, _, _ = await osm.search_lodging(client, CENTER, sort_by="stars")

    assert [h.stars for h in hotels] == [5, 3, None]


async def test_min_stars_drops_lodging_without_star_data(web, client):
    web.on(OVERPASS_MAIN, HOTELS)

    hotels, total, _ = await osm.search_lodging(client, CENTER, min_stars=4)

    assert [h.name for h in hotels] == ["Far Hotel"]
    assert total == 1


async def test_limit_keeps_total_count(web, client):
    web.on(OVERPASS_MAIN, HOTELS)

    hotels, total, _ = await osm.search_lodging(client, CENTER, limit=1)

    assert len(hotels) == 1
    assert total == 3


async def test_lodging_type_and_radius_go_into_query(web, client):
    web.on(OVERPASS_MAIN, overpass())

    await osm.search_lodging(client, CENTER, lodging_type="hostel", radius_km=2.5)

    q = query_sent(web.requests[0])
    assert '["tourism"~"^(hostel)$"]' in q
    assert "around:2500," in q


async def test_truncated_when_hitting_element_cap(web, client, monkeypatch):
    monkeypatch.setattr(osm, "MAX_ELEMENTS", 2)
    web.on(OVERPASS_MAIN, overpass(
        node(1, 18.79, 98.98, {"name": "A", "tourism": "hotel"}),
        node(2, 18.79, 98.98, {"name": "B", "tourism": "hotel"}),
    ))

    _, _, truncated = await osm.search_lodging(client, CENTER)

    assert truncated is True


# ---------- place search ----------

TEMPLES = overpass(
    node(10, 18.7905, 98.9848, {"name": "วัดเล็กใกล้ๆ", "amenity": "place_of_worship"}),
    node(11, 18.7883, 98.9820, {"name": "วัดพระสิงห์", "name:en": "Wat Phra Singh", "name:zh": "帕辛寺",
                                "wikipedia": "th:วัดพระสิงห์", "amenity": "place_of_worship",
                                "opening_hours": "Mo-Su 06:00-18:00"}),
)


async def test_places_rank_famous_first_by_default(web, client):
    web.on(OVERPASS_MAIN, TEMPLES)

    places, total, _ = await osm.search_places(client, CENTER, category="temple")

    assert [p.name for p in places] == ["Wat Phra Singh", "วัดเล็กใกล้ๆ"]
    assert places[0].local_name == "วัดพระสิงห์"
    assert places[1].local_name is None
    assert places[0].kind == "Buddhist temple"
    assert places[0].opening_hours == "Mo-Su 06:00-18:00"
    assert total == 2
    assert '["religion"="buddhist"]' in query_sent(web.requests[0])


async def test_places_can_be_sorted_by_distance(web, client):
    web.on(OVERPASS_MAIN, TEMPLES)

    places, _, _ = await osm.search_places(client, CENTER, category="temple", sort_by="distance")

    assert [p.name for p in places] == ["วัดเล็กใกล้ๆ", "Wat Phra Singh"]


async def test_restaurant_cuisine_and_specific_kind(web, client):
    web.on(OVERPASS_MAIN, overpass(
        node(20, 18.79, 98.98, {"name": "Khao Soi Lamduan", "amenity": "restaurant", "cuisine": "thai"}),
    ))

    places, _, _ = await osm.search_places(client, CENTER, category="restaurant")

    assert (places[0].kind, places[0].cuisine) == ("Restaurant", "thai")


# ---------- resilience against flaky Overpass servers ----------


async def test_retries_same_server_when_queue_is_full(web, client, monkeypatch):
    monkeypatch.setattr(osm, "HEDGE_DELAY_SECONDS", 5)  # this test is only about retrying, so no backup request
    responses = iter([httpx.Response(504), httpx.Response(200, json=TEMPLES)])
    web.on(OVERPASS_MAIN, lambda _req: next(responses))

    elements = await osm.fetch_elements(client, "q")

    assert len(elements) == 2
    assert web.hosts() == [OVERPASS_MAIN, OVERPASS_MAIN]


async def test_failed_server_hands_over_to_next(web, client, monkeypatch):
    monkeypatch.setattr(osm, "HEDGE_DELAY_SECONDS", 5)  # must switch because of failure, not slowness
    web.on(OVERPASS_MAIN, 400)  # a failure that retrying cannot fix -> move on to the next server at once
    web.on(OVERPASS_SECOND, TEMPLES)

    elements = await osm.fetch_elements(client, "q")

    assert len(elements) == 2
    assert web.hosts() == [OVERPASS_MAIN, OVERPASS_SECOND]


async def test_slow_server_is_hedged_with_backup(web, client):
    async def slow(_req):
        await asyncio.sleep(5)
        return overpass()

    web.on(OVERPASS_MAIN, slow)
    web.on(OVERPASS_SECOND, TEMPLES)

    started = time.monotonic()
    elements = await osm.fetch_elements(client, "q")

    assert len(elements) == 2  # came from the backup server
    assert time.monotonic() - started < 1  # did not wait for the slow server


async def test_partial_result_with_error_remark_is_not_trusted(web, client):
    web.on(OVERPASS_MAIN, overpass(remark="runtime error: Query timed out in \"query\" at line 1"))
    web.on(OVERPASS_SECOND, TEMPLES)

    elements = await osm.fetch_elements(client, "q")

    assert len(elements) == 2


async def test_all_servers_down_raises_service_error(web, client):
    for host in (OVERPASS_MAIN, OVERPASS_SECOND, OVERPASS_THIRD):
        web.on(host, 502)

    with pytest.raises(UpstreamServiceError, match="Overpass"):
        await osm.fetch_elements(client, "q")
    # every server is tried 3 times (first attempt + 2 retries)
    assert len(web.requests) == 9


async def test_gives_up_at_deadline(web, client, monkeypatch):
    monkeypatch.setattr(osm, "TOTAL_DEADLINE_SECONDS", 0.3)

    async def hang(_req):
        await asyncio.sleep(10)

    for host in (OVERPASS_MAIN, OVERPASS_SECOND, OVERPASS_THIRD):
        web.on(host, hang)

    started = time.monotonic()
    with pytest.raises(UpstreamServiceError):
        await osm.fetch_elements(client, "q")
    assert time.monotonic() - started < 1


async def test_results_are_cached(web, client):
    web.on(OVERPASS_MAIN, TEMPLES)

    await osm.fetch_elements(client, "q")
    await osm.fetch_elements(client, "q")

    assert len(web.requests) == 1
