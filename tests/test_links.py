import datetime as dt
from urllib.parse import parse_qs, urlsplit

import pytest

from thai_travel import links
from thai_travel.errors import InvalidRequestError

TODAY = dt.date(2026, 11, 7)


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_google_maps_link_uses_official_format_and_encodes_non_ascii():
    url = links.google_maps_search("hotels near เชียงใหม่")

    assert url.startswith("https://www.google.com/maps/search/?")
    assert query_of(url) == {"api": "1", "query": "hotels near เชียงใหม่"}


def test_booking_link_without_dates():
    url = links.booking_search("Chiang Mai", adults=3)

    assert url.startswith("https://www.booking.com/searchresults.html?")
    assert query_of(url) == {"ss": "Chiang Mai", "group_adults": "3", "no_rooms": "1"}


def test_booking_link_with_dates():
    url = links.booking_search("Phuket", check_in=dt.date(2026, 11, 20), check_out=dt.date(2026, 11, 22))

    params = query_of(url)
    assert params["checkin"] == "2026-11-20"
    assert params["checkout"] == "2026-11-22"


def test_no_dates_is_fine():
    assert links.resolve_stay_dates(None, None, today=TODAY) == (None, None)


def test_check_in_only_means_one_night():
    check_in = dt.date(2026, 11, 20)

    assert links.resolve_stay_dates(check_in, None, today=TODAY) == (check_in, dt.date(2026, 11, 21))


def test_check_in_today_is_allowed():
    assert links.resolve_stay_dates(TODAY, None, today=TODAY)[0] == TODAY


@pytest.mark.parametrize(
    ("check_in", "check_out", "message"),
    [
        (dt.date(2026, 11, 6), None, "in the past"),
        (dt.date(2026, 11, 20), dt.date(2026, 11, 20), "must be after check-in"),
        (dt.date(2026, 11, 20), dt.date(2026, 11, 19), "must be after check-in"),
        (None, dt.date(2026, 11, 20), "without a check-in"),
        (dt.date(2026, 11, 20), dt.date(2026, 12, 21), "30 nights"),
    ],
)
def test_invalid_stays_are_rejected(check_in, check_out, message):
    with pytest.raises(InvalidRequestError, match=message):
        links.resolve_stay_dates(check_in, check_out, today=TODAY)
