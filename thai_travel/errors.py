"""Errors that are meant to be shown to the user.

Messages are in English and say how to fix the problem, because Claude relays them to the user
(FastMCP turns an exception raised inside a tool into an `isError` result automatically).
"""


class TravelError(Exception):
    """Base class for all errors in this package."""


class LocationNotFoundError(TravelError):
    """The place the user typed was not found, or is too broad (e.g. a whole country)."""


class InvalidRequestError(TravelError):
    """The request values are unusable, e.g. check-out before check-in."""


class UpstreamServiceError(TravelError):
    """An external service (Open-Meteo, OpenStreetMap) is down or answered in an unexpected format."""
