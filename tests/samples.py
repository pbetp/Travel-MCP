"""Sample answers from each API, shortened from real responses seen during development."""

from typing import Any

OPEN_METEO_GEO = "geocoding-api.open-meteo.com"
OPEN_METEO_FORECAST = "api.open-meteo.com"
NOMINATIM = "nominatim.openstreetmap.org"
OVERPASS_MAIN = "overpass-api.de"
OVERPASS_SECOND = "maps.mail.ru"
OVERPASS_THIRD = "overpass.private.coffee"

CHIANG_MAI = (18.79038, 98.98468)


def om_place(name: str, lat: float, lon: float, feature_code: str = "PPLA", **extra: Any) -> dict:
    return {"name": name, "latitude": lat, "longitude": lon, "feature_code": feature_code, **extra}


def om_results(*places: dict) -> dict:
    return {"results": list(places)} if places else {"generationtime_ms": 0.1}


CHIANG_MAI_OM = om_results(
    om_place("Chiang Mai", *CHIANG_MAI, admin1="Chiang Mai Province", country="Thailand", country_code="TH")
)

# The same place as Open-Meteo returns it for a Thai query (language=th), then its English record (/v1/get)
CHIANG_MAI_TH = om_results(
    om_place("เชียงใหม่", *CHIANG_MAI, id=1153671, admin1="จังหวัดเชียงใหม่", country="ไทย", country_code="TH")
)
CHIANG_MAI_EN_RECORD = om_place(
    "Chiang Mai", *CHIANG_MAI, id=1153671, admin1="Chiang Mai Province", country="Thailand", country_code="TH"
)

PATONG_NOMINATIM = [
    {
        "name": "Patong",
        "display_name": "Patong, Kathu District, Phuket, 83150, Thailand",
        "lat": "7.8949476",
        "lon": "98.2988023",
        "addresstype": "town",
        "address": {"country_code": "th"},
    }
]


def node(osm_id: int, lat: float, lon: float, tags: dict[str, str]) -> dict:
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags}


def way(osm_id: int, lat: float, lon: float, tags: dict[str, str]) -> dict:
    """A building or area: Overpass gives its middle point in `center`."""
    return {"type": "way", "id": osm_id, "center": {"lat": lat, "lon": lon}, "tags": tags}


def overpass(*elements: dict, remark: str | None = None) -> dict:
    data: dict[str, Any] = {"version": 0.6, "elements": list(elements)}
    if remark:
        data["remark"] = remark
    return data


def forecast(*days: tuple, timezone: str = "Asia/Bangkok") -> dict:
    """days: (date, weather code, min, max, rain chance %, rain mm)"""
    columns = list(zip(*days, strict=True)) if days else [[]] * 6
    keys = [
        "time",
        "weather_code",
        "temperature_2m_min",
        "temperature_2m_max",
        "precipitation_probability_max",
        "precipitation_sum",
    ]
    return {"timezone": timezone, "daily": {k: list(v) for k, v in zip(keys, columns, strict=True)}}
