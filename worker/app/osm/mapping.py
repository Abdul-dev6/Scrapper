"""OSM element (tags) -> lead fields.

Rules: only values that are actually in the tags. A missing tag means the field
is left out, never guessed. Every contact value carries sourceUrl + sourceType.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit

import tldextract

from .. import contacts
from .overpass import match_categories

# Offline: uses the public-suffix snapshot bundled with tldextract, never downloads.
_tld = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

SOURCE_TYPE = "osm"
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
GENERIC_EMAIL_LOCALS = {
    "info", "contact", "hello", "hi", "admin", "office", "enquiries", "enquiry", "inquiries",
    "bookings", "booking", "sales", "support", "team", "mail", "reception", "membership", "gym",
}
SOCIAL_DOMAINS = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com", "fb.me"),
    "linkedin": ("linkedin.com",),
}
_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9._-]{1,100})$")


def osm_url(element: dict) -> str:
    return f"https://www.openstreetmap.org/{element.get('type')}/{element.get('id')}"


def normalize_name(name: str) -> str:
    """'Café  Boxing-Club!' -> 'cafe boxing club' (for search and fuzzy dedupe later)."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _values(tags: dict, *keys: str) -> list[str]:
    """All values of these tags; OSM separates multiple values with ';'."""
    out: list[str] = []
    for key in keys:
        for part in (tags.get(key) or "").split(";"):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def normalize_url(raw: str) -> str | None:
    value = raw.strip()
    if " " in value:
        return None
    if not re.match(r"^https?://", value, re.IGNORECASE):
        if "://" in value:
            return None  # ftp://, mailto: etc. are not websites
        value = "https://" + value  # OSM often omits the scheme ("examplegym.com")
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    if "." not in host:
        return None
    return value


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def registrable_domain(host: str) -> str:
    """'www.gym.co.uk' -> 'gym.co.uk'."""
    return _tld(host).top_domain_under_public_suffix or host.removeprefix("www.")


def social_kind(url: str) -> str | None:
    domain = registrable_domain(host_of(url))
    for kind, domains in SOCIAL_DOMAINS.items():
        if domain in domains:
            return kind
    return None


def social_url(kind: str, raw: str) -> str | None:
    """contact:instagram may hold a URL or a bare handle. Only accept the right network."""
    raw = raw.strip()
    if "/" in raw or re.match(r"^https?:", raw, re.IGNORECASE):
        url = normalize_url(raw)
        return url if url and social_kind(url) == kind else None
    match = _HANDLE_RE.match(raw)  # "@my.gym" or "my.gym" -> handle (dots are allowed in handles)
    if match and kind in ("instagram", "facebook"):  # LinkedIn handles are ambiguous (company vs person)
        return f"https://www.{SOCIAL_DOMAINS[kind][0]}/{match.group(1)}"
    return None


def _address(tags: dict, area_country_code: str | None) -> dict:
    street = " ".join(_values(tags, "addr:housenumber") + _values(tags, "addr:street"))
    address = {
        "full": tags.get("addr:full", "").strip(),
        "street": street,
        "city": (tags.get("addr:city") or "").strip(),
        "region": (tags.get("addr:state") or tags.get("addr:province") or "").strip(),
        "postcode": (tags.get("addr:postcode") or "").strip(),
    }
    country = (tags.get("addr:country") or "").strip()
    if re.fullmatch(r"[A-Za-z]{2}", country):
        address["countryCode"] = country.upper()
    elif area_country_code:
        # The query only returned places inside the geocoded area, so its country is a fact, not a guess.
        address["countryCode"] = area_country_code
    if country and "countryCode" not in address:
        address["country"] = country
    return {k: v for k, v in address.items() if v}


def element_to_lead(element: dict, *, now: datetime, area_country_code: str | None = None) -> dict | None:
    """Returns None for elements that can't be a lead (no name)."""
    tags = element.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None

    src_url = osm_url(element)
    src = {"sourceUrl": src_url, "sourceType": SOURCE_TYPE}
    lead: dict = {
        "name": name[:300],
        "nameNormalized": normalize_name(name),
        "categories": match_categories(tags),
        "externalIds": {"osm": f"{element['type']}/{element['id']}"},
        "sources": [{"url": src_url, "type": SOURCE_TYPE, "fetchedAt": now}],
    }

    address = _address(tags, area_country_code)
    if address:
        lead["address"] = address

    point = element if "lat" in element else element.get("center")
    if point and "lat" in point and "lon" in point:
        lead["geo"] = {"type": "Point", "coordinates": [float(point["lon"]), float(point["lat"])]}

    for raw in _values(tags, "website", "contact:website", "url"):
        url = normalize_url(raw)
        # A Facebook page in `website` is not an official website, and social links may only
        # come from contact:* tags, so it is skipped rather than moved.
        if url and not social_kind(url):
            lead["website"] = {"url": url, "domain": registrable_domain(host_of(url))}
            break

    socials = {}
    for kind in SOCIAL_DOMAINS:
        for raw in _values(tags, f"contact:{kind}"):
            url = social_url(kind, raw)
            if url:
                socials[kind] = {"url": url, **src}
                break
    if socials:
        lead["socials"] = socials

    phones = [{"raw": raw, **src} for raw in _values(tags, "phone", "contact:phone", "contact:mobile", "mobile")]
    if phones:
        lead["phones"] = phones  # e164 is filled in by verification (Step 3)

    emails, seen = [], set()
    for raw in _values(tags, "email", "contact:email"):
        value = raw.removeprefix("mailto:").strip()
        if EMAIL_RE.match(value) and value.lower() not in seen:
            seen.add(value.lower())
            local = value.split("@")[0].lower()
            emails.append({"value": value, "kind": "generic" if local in GENERIC_EMAIL_LOCALS else "named", **src,
                           "checks": contacts.email_checks(None, mx=None, now=now)})  # MX is checked later
    if emails:
        lead["emails"] = emails  # mxValid is filled in by verification (Step 3)

    whatsapp = []
    for raw in _values(tags, "contact:whatsapp"):
        if re.match(r"^https?://", raw, re.IGNORECASE):
            whatsapp.append({"url": raw, **src})
        else:
            digits = re.sub(r"[\s().-]", "", raw)
            if re.fullmatch(r"\+\d{7,15}", digits):
                whatsapp.append({"e164": digits, **src})
    if whatsapp:
        lead["whatsapp"] = whatsapp

    # Phone formats are checked when the lead is saved (leads._normalize_phones), MX by the crawler.
    lead["contactStatus"] = contacts.contact_status(emails, phones, whatsapp)
    # Provenance for the non-contact fields (contact values carry their own sourceUrl/sourceType).
    lead["fieldSources"] = {f: dict(src) for f in ("name", "address", "geo", "website") if f in lead}
    return lead
