"""Duplicate detection: is this business one we already have?

Signals, strongest first:
  provider ids (OSM id / Google place id)    -> exact, handled by unique indexes
  phone (E.164) + nearby or similar name
  normalized name + nearby (~150 m)
  website domain + very close / similar name  (domain ALONE never merges: chain branches share a domain)
  name + postcode/house number                (for records without coordinates)

Different physical branches stay separate because every rule also needs the place to be in
(about) the same spot.
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

import phonenumbers

from .osm.mapping import host_of, normalize_name, registrable_domain

# Words that say WHAT a business is, not WHICH one. "Iron Fist" vs "Iron Fist Boxing Gym" is the same club.
GENERIC_TOKENS = {
    "the", "and", "of", "gym", "gyms", "club", "fitness", "boxing", "mma", "martial", "arts", "academy", "school",
    "studio", "centre", "center", "sports", "sport", "ltd", "limited", "llc", "inc", "co", "gmbh", "health",
    "training", "dojo", "wrestling", "kickboxing", "bjj", "jiu", "jitsu", "store", "shop",
}

NEAR_M = 150  # same name this close = same place
CLOSE_M = 60  # very close (same building)
GRID_DECIMALS = 3  # ~110 m cells for the dedupeKey


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def lead_latlon(lead: dict) -> tuple[float, float] | None:
    coords = ((lead.get("geo") or {}).get("coordinates")) or None
    if coords and len(coords) == 2:
        return float(coords[1]), float(coords[0])
    return None


def distance_m(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float | None:
    if a is None or b is None:
        return None
    return haversine_m(a[0], a[1], b[0], b[1])


def _tokens(name: str) -> set[str]:
    return set(normalize_name(name).split())


def name_similarity(a: str, b: str) -> float:
    """0..1. Distinctive words count most; 'Iron Fist' ~ 'Iron Fist Boxing Gym' = 1.0."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na.replace(" ", "") == nb.replace(" ", ""):  # "Ironfist" vs "Iron Fist"
        return 1.0
    ta, tb = _tokens(a), _tokens(b)
    da, db = ta - GENERIC_TOKENS, tb - GENERIC_TOKENS
    if da and db:
        containment = len(da & db) / min(len(da), len(db))
    else:  # only generic words ("Boxing Club"): compare everything, strictly
        containment = len(ta & tb) / max(len(ta), len(tb))
    ratio = SequenceMatcher(None, na, nb).ratio()
    if (da - db) and (db - da):
        # Both names have a distinctive word the other lacks ("Chain Gym Leeds" / "Chain Gym York"):
        # likely two branches. Only an almost identical spelling may still count as the same.
        return ratio if ratio >= 0.9 else min(containment, 0.5)
    return max(containment, ratio)


def normalize_postcode(value: str | None) -> str:
    return re.sub(r"[\s-]", "", value or "").upper()


def normalize_domain(url_or_domain: str | None) -> str:
    if not url_or_domain:
        return ""
    host = host_of(url_or_domain) if "://" in url_or_domain else url_or_domain.lower()
    return registrable_domain(host.removeprefix("www.")) if host else ""


def phone_key(raw: str | None, region: str | None) -> str:
    """E.164 when it parses as a valid number, else '' (an unparseable number can't prove anything)."""
    if not raw:
        return ""
    try:
        number = phonenumbers.parse(raw, (region or "").upper() or None)
    except phonenumbers.NumberParseException:
        return ""
    if not phonenumbers.is_valid_number(number):
        return ""
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def lead_phone_keys(lead: dict) -> set[str]:
    region = (lead.get("address") or {}).get("countryCode")
    keys = set()
    for p in lead.get("phones") or []:
        k = p.get("e164") or phone_key(p.get("raw"), region)
        if k:
            keys.add(k)
    return keys


def house_number(street: str | None) -> str:
    m = re.match(r"^\s*(\d+[a-zA-Z]?)\b", street or "")
    return m.group(1).lower() if m else ""


def dedupe_key(name_normalized: str, latlon: tuple[float, float] | None) -> str | None:
    """Same name inside the same ~110 m grid cell. Backs a UNIQUE index, so two workers can't
    insert the same business at the same moment."""
    if not name_normalized or latlon is None:
        return None
    lat, lon = latlon
    return f"{name_normalized}|{round(lat, GRID_DECIMALS):.{GRID_DECIMALS}f}|{round(lon, GRID_DECIMALS):.{GRID_DECIMALS}f}"


def same_business(new: dict, old: dict) -> str | None:
    """Two lead dicts (OSM/website data). Returns the reason they are the same place, or None."""
    d = distance_m(lead_latlon(new), lead_latlon(old))
    sim = name_similarity(new.get("name", ""), old.get("name", ""))
    same_name = new.get("nameNormalized") and new.get("nameNormalized") == old.get("nameNormalized")

    if lead_phone_keys(new) & lead_phone_keys(old) and ((d is not None and d <= NEAR_M) or sim >= 0.6):
        return "phone"
    if same_name and d is not None and d <= NEAR_M:
        return "name+distance"
    nd, od = (new.get("website") or {}).get("domain"), (old.get("website") or {}).get("domain")
    if nd and nd == od and d is not None and ((d <= CLOSE_M and sim >= 0.6) or (sim >= 0.85 and d <= NEAR_M)):
        return "domain+distance"
    if d is None and same_name:
        na, oa = new.get("address") or {}, old.get("address") or {}
        pc = normalize_postcode(na.get("postcode"))
        hn = house_number(na.get("street"))
        if pc and pc == normalize_postcode(oa.get("postcode")) and (not hn or hn == house_number(oa.get("street"))):
            return "name+postcode"
    return None
