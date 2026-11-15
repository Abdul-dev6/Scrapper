"""Overture place row -> lead (the "overture" data lane). Only values present in the record are used;
a missing field stays empty. Every value records sourceType "overture" and a provenance id
"overture:<release>:<GERS id>" (Overture has no official per-place web page)."""

from __future__ import annotations

import re
from datetime import datetime

from .. import contacts
from ..osm.mapping import EMAIL_RE, GENERIC_EMAIL_LOCALS, normalize_name, normalize_url, registrable_domain, host_of, \
    social_kind
from .places import categories_for

SOURCE_TYPE = "overture"
LANE = "overture"


def provenance_id(release: str, gers_id: str) -> str:
    return f"overture:{release}:{gers_id}"


def source_keys(sources: list[dict] | None) -> list[str]:
    """Upstream record ids ("meta:798829223526334"): survive a change of the Overture (GERS) id."""
    out = []
    for s in sources or []:
        if s.get("dataset") and s.get("record_id") and s["dataset"].lower() != "overture":
            k = f"{s['dataset'].lower()}:{s['record_id']}"
            if k not in out:
                out.append(k)
    return out


def row_to_lead(row: dict, *, release: str, now: datetime) -> dict | None:
    name = (row.get("name") or "").strip()
    gers = row.get("id")
    cats = categories_for(row.get("cat"), row.get("alts"))
    if not name or not gers or not cats or row.get("lat") is None:
        return None
    prov = provenance_id(release, gers)
    src = {"sourceUrl": prov, "sourceType": SOURCE_TYPE}
    sources = [s for s in row.get("sources") or [] if isinstance(s, dict)]
    lead: dict = {
        "dataLane": LANE,
        "name": name[:300],
        "nameNormalized": normalize_name(name),
        "categories": cats,
        "externalIds": {"overture": gers, "overtureSources": source_keys(sources)},
        "geo": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
        "overture": {
            "release": release, "gersId": gers, "taxonomy": row.get("cat"),
            "confidence": row.get("confidence"),
            "sources": [{"dataset": s.get("dataset"), "recordId": s.get("record_id"), "license": s.get("license"),
                         "updateTime": s.get("update_time")} for s in sources],
        },
        "sources": [{"url": prov, "type": SOURCE_TYPE, "fetchedAt": now}],
    }

    addr = next((a for a in row.get("addresses") or [] if isinstance(a, dict)), None)
    if addr:
        a = {"street": (addr.get("freeform") or "").strip(), "city": (addr.get("locality") or "").strip(),
             "region": (addr.get("region") or "").strip(), "postcode": (addr.get("postcode") or "").strip()}
        cc = (addr.get("country") or "").strip()
        if re.fullmatch(r"[A-Za-z]{2}", cc):
            a["countryCode"] = cc.upper()
        a = {k: v for k, v in a.items() if v}
        if a:
            lead["address"] = a

    for raw in row.get("websites") or []:
        url = normalize_url(raw or "")
        if url and not social_kind(url):  # a Facebook page is not the official website
            lead["website"] = {"url": url, "domain": registrable_domain(host_of(url))}
            break

    socials = {}
    for raw in row.get("socials") or []:
        url = normalize_url(raw or "")
        kind = social_kind(url) if url else None
        if kind and kind not in socials:
            socials[kind] = {"url": url, **src}
    if socials:
        lead["socials"] = socials

    phones = [{"raw": p.strip(), **src} for p in dict.fromkeys(row.get("phones") or []) if p and p.strip()]
    if phones:
        lead["phones"] = phones  # E.164 + format check happen on save (leads._normalize_phones)

    emails, seen = [], set()
    for raw in row.get("emails") or []:
        v = (raw or "").removeprefix("mailto:").strip()
        if EMAIL_RE.match(v) and v.lower() not in seen:
            seen.add(v.lower())
            emails.append({"value": v, "kind": "generic" if v.split("@")[0].lower() in GENERIC_EMAIL_LOCALS else "named",
                           **src, "checks": contacts.email_checks(None, mx=None, now=now)})
    if emails:
        lead["emails"] = emails

    lead["fieldSources"] = {f: dict(src) for f in ("name", "address", "geo", "website") if f in lead}
    lead["contactStatus"] = contacts.contact_status(emails, phones, [])
    return lead
