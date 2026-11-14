"""Name normalization for place search. MUST match web/src/lib/placeText.ts exactly
(both test suites check the same examples)."""

from __future__ import annotations

import math
import re
import unicodedata

_NON_ALNUM = re.compile(r"[^\w]|_")
_SPACES = re.compile(r"\s+")


def norm(text: str) -> str:
    """'São Paulo' -> 'sao paulo', 'U.K.' -> 'u k', 'Köln' -> 'koln'."""
    s = unicodedata.normalize("NFKD", (text or "").lower())
    # Drop ALL marks (Mn, Mc, Me), exactly like JavaScript's /\p{M}/gu (unicodedata.combining would
    # keep spacing marks such as Devanagari vowel signs and break parity with the web app).
    s = "".join(ch for ch in s if not unicodedata.category(ch).startswith("M"))
    s = _NON_ALNUM.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def area_key(query: str) -> str:
    """Stable key for a search text (used to group Google place references by search)."""
    return _SPACES.sub(" ", (query or "").strip().casefold())


def bbox_area_km2(bbox: list[float]) -> float:
    """Approximate area of [south, west, north, east] in km²."""
    south, west, north, east = bbox
    km_per_deg = 111.32
    mid_lat = math.radians((south + north) / 2)
    return abs(north - south) * km_per_deg * abs(east - west) * km_per_deg * max(math.cos(mid_lat), 0.01)
