"""Build search links for Google Maps and Booking.com.

There is no free hotel-price API, so we hand the user a link and they check real prices on the
site themselves. Prices never pass through this server.

- Google Maps uses the official URL format (Maps URLs): https://developers.google.com/maps/documentation/urls
- Booking.com has no official documentation; we use the commonly used format
  (ss, checkin, checkout, group_adults). If the site changes it, the link may open a search
  without the dates filled in.
- Agoda is not included: its search links need an internal city ID that is not publicly documented.
"""

import datetime as dt
from urllib.parse import urlencode

from .errors import InvalidRequestError

MAX_NIGHTS = 30  # Booking.com does not allow longer stays in one booking


def google_maps_search(query: str) -> str:
    return "https://www.google.com/maps/search/?" + urlencode({"api": 1, "query": query})


def booking_search(
    destination: str,
    *,
    check_in: dt.date | None = None,
    check_out: dt.date | None = None,
    adults: int = 2,
    rooms: int = 1,
) -> str:
    params: dict[str, str | int] = {"ss": destination, "group_adults": adults, "no_rooms": rooms}
    if check_in and check_out:
        params["checkin"] = check_in.isoformat()
        params["checkout"] = check_out.isoformat()
    return "https://www.booking.com/searchresults.html?" + urlencode(params)


def resolve_stay_dates(
    check_in: dt.date | None,
    check_out: dt.date | None,
    today: dt.date | None = None,
) -> tuple[dt.date | None, dt.date | None]:
    """Validate stay dates before building links. If only check-in is given, assume one night."""
    if check_in is None:
        if check_out is not None:
            raise InvalidRequestError("A check-out date was given without a check-in date. Please give both.")
        return None, None

    today = today or dt.date.today()
    if check_in < today:
        raise InvalidRequestError(
            f"Check-in date {check_in.isoformat()} is in the past (today is {today.isoformat()})."
        )
    if check_out is None:
        check_out = check_in + dt.timedelta(days=1)
    if check_out <= check_in:
        raise InvalidRequestError("Check-out date must be after check-in date.")
    if (check_out - check_in).days > MAX_NIGHTS:
        raise InvalidRequestError(f"A search can cover at most {MAX_NIGHTS} nights.")
    return check_in, check_out
