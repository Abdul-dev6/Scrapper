"""Minimal Places API (New) client: Text Search + Place Details.

Field masks are chosen so each request falls into exactly one free-tier SKU:
  Text Search  -> "Text Search Pro"           (displayName/address/location are Pro fields;
                                                NO websiteUri/phone/rating = no Enterprise)
  Place Details -> "Place Details Essentials" (address + location only; NO displayName = no Pro)

The API key travels only in the X-Goog-Api-Key header. It is never put in a URL, a log line or an
error message.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import httpx

from .usage import DETAILS, TEXT_SEARCH, UsageLimiter

TEXT_SEARCH_FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.location",
    "places.addressComponents", "places.types", "places.businessStatus", "nextPageToken",
])
DETAILS_FIELDS = "id,formattedAddress,location,addressComponents,types"
PAGE_SIZE = 20
MAX_PAGES = 3  # Text Search returns at most 60 results (3 pages of 20) per query
RETRY_WAITS = (2, 6)
PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,300}$")


class GoogleError(Exception):
    """Message is safe to show to the user (never contains the key)."""


class QuotaExhausted(GoogleError):
    """Our internal monthly limit is reached: switch to OSM-only."""


class GoogleFatal(GoogleError):
    """Bad key / API not enabled / request rejected: stop using Google for this job."""


class GoogleTransient(GoogleError):
    """Busy or network problem after retries."""


def parse_place(p: dict) -> dict:
    """API place -> in-memory candidate. Never written to the database as-is."""
    comps = p.get("addressComponents") or []

    def comp(kind: str, short: bool = False) -> str:
        for c in comps:
            if kind in (c.get("types") or []):
                return (c.get("shortText") if short else c.get("longText")) or ""
        return ""

    loc = p.get("location") or {}
    return {
        "id": p.get("id", ""),
        "name": ((p.get("displayName") or {}).get("text") or "").strip(),
        "lat": loc.get("latitude"),
        "lon": loc.get("longitude"),
        "formattedAddress": p.get("formattedAddress", ""),
        "postcode": comp("postal_code"),
        "houseNumber": comp("street_number"),
        "route": comp("route"),
        "city": comp("locality") or comp("postal_town"),
        "countryCode": comp("country", short=True).upper(),
        "types": p.get("types") or [],
        "businessStatus": p.get("businessStatus", ""),
    }


class PlacesClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        api_key: str,
        base_url: str,
        usage: UsageLimiter,
        job_id=None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 20.0,
    ):
        if not api_key:
            raise GoogleFatal("GOOGLE_PLACES_API_KEY is not set.")
        self._client = client
        self._key = api_key
        self._base = base_url.rstrip("/")
        self.usage = usage
        self.job_id = job_id
        self.sleep = sleep
        self.timeout = timeout
        self.requests = {TEXT_SEARCH: 0, DETAILS: 0}  # sent by this client (this job)

    def _headers(self, mask: str) -> dict:
        return {"X-Goog-Api-Key": self._key, "X-Goog-FieldMask": mask, "Content-Type": "application/json"}

    def _send(self, sku: str, what: str, build: Callable[[], httpx.Request]) -> dict:
        for attempt in range(len(RETRY_WAITS) + 1):
            res = self.usage.reserve(sku)  # BEFORE the request, every attempt
            if res is None:
                raise QuotaExhausted("Monthly Google limit reached; continuing with OpenStreetMap only.")
            self.requests[sku] += 1
            try:
                resp = self._client.send(build())
            except httpx.HTTPError as exc:
                self.usage.record(res, job_id=self.job_id, ok=False, error=type(exc).__name__, what=what)
                if attempt < len(RETRY_WAITS):
                    self.sleep(RETRY_WAITS[attempt])
                    continue
                raise GoogleTransient("Couldn't reach Google Places.") from None
            ok = resp.status_code == 200
            error = "" if ok else _error_text(resp).replace(self._key, "***")  # belt and braces
            self.usage.record(res, job_id=self.job_id, ok=ok, http_status=resp.status_code, error=error, what=what)
            if ok:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < len(RETRY_WAITS):
                    self.sleep(RETRY_WAITS[attempt])
                    continue
                raise GoogleTransient(f"Google Places is busy (HTTP {resp.status_code}).")
            if resp.status_code in (400, 401, 403):
                raise GoogleFatal(f"Google Places rejected the request (HTTP {resp.status_code}): {error}")
            if resp.status_code == 404:
                return {}
            raise GoogleTransient(f"Google Places error (HTTP {resp.status_code}).")
        raise AssertionError("unreachable")

    def text_search(self, query: str, bbox: list[float], page_token: str | None = None) -> tuple[list[dict], str | None]:
        """One page (max 20) of places inside bbox [south, west, north, east]."""
        south, west, north, east = bbox
        body: dict = {
            "textQuery": query,
            "pageSize": PAGE_SIZE,
            "locationRestriction": {"rectangle": {
                "low": {"latitude": south, "longitude": west},
                "high": {"latitude": north, "longitude": east},
            }},
        }
        if page_token:
            body["pageToken"] = page_token

        def build() -> httpx.Request:
            return self._client.build_request("POST", f"{self._base}/places:searchText", json=body,
                                              headers=self._headers(TEXT_SEARCH_FIELDS), timeout=self.timeout)

        data = self._send(TEXT_SEARCH, f"textSearch '{query}' {bbox}", build)
        places = [parse_place(p) for p in data.get("places") or [] if p.get("id")]
        return places, data.get("nextPageToken") or None

    def place_details(self, place_id: str) -> dict | None:
        if not PLACE_ID_RE.match(place_id):  # it goes into the URL path
            return None

        def build() -> httpx.Request:
            return self._client.build_request("GET", f"{self._base}/places/{place_id}",
                                              headers=self._headers(DETAILS_FIELDS), timeout=self.timeout)

        data = self._send(DETAILS, f"details {place_id}", build)
        return parse_place(data) if data.get("id") else None


def _error_text(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        return f"{err.get('status', '')} {err.get('message', '')}".strip()[:200]
    except ValueError:
        return ""
