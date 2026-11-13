"""Geocoding: turn "Leeds, UK" into a bounding box and an OSM area id.

Results are cached in the `geocodeCache` collection (TTL index built by
`npm run db:indexes`), so repeating a search doesn't hit Nominatim again.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from datetime import datetime

import httpx
from pymongo.collection import Collection

from .ratelimit import RateLimiter


class GeocodeError(Exception):
    """The message is safe to show to the user."""


class GeocodeUnavailable(GeocodeError):
    """The location service is down or busy: worth retrying later (the place itself may be fine)."""


# Whole countries are fine (searched piece by piece); a continent would take hours.
TOO_BIG_TYPES = {"continent"}


def cache_key(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip().casefold())


def bbox_area_km2(bbox: list[float]) -> float:
    """Approximate area of [south, west, north, east] in km²."""
    south, west, north, east = bbox
    km_per_deg = 111.32
    mid_lat = math.radians((south + north) / 2)
    return abs(north - south) * km_per_deg * abs(east - west) * km_per_deg * max(math.cos(mid_lat), 0.01)


def area_id(osm_type: str | None, osm_id: int) -> int | None:
    """Overpass area ids are derived from the OSM relation/way id. Nodes have no area."""
    if osm_type == "relation":
        return 3_600_000_000 + osm_id
    if osm_type == "way":
        return 2_400_000_000 + osm_id
    return None


def parse_result(item: dict) -> dict:
    south, north, west, east = (float(x) for x in item["boundingbox"])  # Nominatim order: S, N, W, E
    address = item.get("address") or {}
    osm_type, osm_id = item.get("osm_type"), int(item["osm_id"])
    return {
        "displayName": item.get("display_name", ""),
        "country": address.get("country", ""),
        "countryCode": (address.get("country_code") or "").upper(),
        "bbox": [south, west, north, east],
        "areaId": area_id(osm_type, osm_id),
        "placeType": item.get("addresstype") or item.get("type") or "",
        "osmUrl": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
    }


def check_size(place: dict, query: str) -> None:
    if place["placeType"] in TOO_BIG_TYPES:
        raise GeocodeError(f'"{query}" is a whole continent. Search a country, region or city instead.')


def geocode(
    query: str,
    *,
    client: httpx.Client,
    cache: Collection,
    limiter: RateLimiter,
    url: str,
    now: datetime,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    key = cache_key(query)
    hit = cache.find_one({"_id": key})
    if hit:
        place = hit["result"]
    else:
        resp = _fetch(query, client=client, limiter=limiter, url=url, sleep=sleep)
        results = resp.json()
        if not results:
            raise GeocodeError(f'Couldn\'t find "{query}". Try adding the country, for example "Leeds, UK".')
        place = parse_result(results[0])
        cache.update_one({"_id": key}, {"$set": {"result": place, "createdAt": now}}, upsert=True)

    check_size(place, query)
    return place


RETRY_WAITS = (5, 15)  # a busy answer (429 / 5xx) is usually gone within seconds


def _fetch(query: str, *, client: httpx.Client, limiter: RateLimiter, url: str, sleep) -> httpx.Response:
    """GET with a couple of retries, so one busy moment doesn't fail the whole job."""
    for attempt in range(len(RETRY_WAITS) + 1):
        limiter.wait()
        try:
            resp = client.get(
                url,
                params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1, "accept-language": "en"},
            )
        except httpx.HTTPError as exc:
            if attempt < len(RETRY_WAITS):
                sleep(RETRY_WAITS[attempt])
                continue
            raise GeocodeUnavailable("Couldn't reach the location service. Try again in a few minutes.") from exc
        if resp.status_code == 200:
            return resp
        if (resp.status_code == 429 or resp.status_code >= 500) and attempt < len(RETRY_WAITS):
            sleep(RETRY_WAITS[attempt])
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            raise GeocodeUnavailable(f"The location service is busy (HTTP {resp.status_code}). Try again later.")
        raise GeocodeError(f"The location service returned an error (HTTP {resp.status_code}). Try again later.")
    raise AssertionError("unreachable")
