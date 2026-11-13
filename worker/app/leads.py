"""Saving discovered businesses. One lead per physical business (branches of a chain stay separate).

How a duplicate is caught, in order:
  1. Same OSM id (or an OSM id we already merged into a lead: `externalIds.osmAliases`).
  2. Same business under another OSM id (a gym mapped as a node AND a building, or re-mapped):
     phone / name / domain + distance rules in dedupe.same_business. The new id becomes an alias.
  3. At insert time, MongoDB's unique indexes (OSM id, dedupeKey = name + ~110 m cell) stop two
     workers inserting the same business at the same moment; the loser updates instead.

Re-running a search updates the OSM-sourced fields and never touches what the
user (saved, status, notes) or the website crawler added.
"""

from __future__ import annotations

from datetime import datetime

from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from . import contacts, dedupe

OSM = "osm"
CANDIDATE_LIMIT = 25


def _keep_non_osm(items: list | None) -> list:
    return [i for i in items or [] if i.get("sourceType") != OSM]


def merge_update(existing: dict, lead: dict, now: datetime) -> dict:
    """The $set for an existing lead whose PRIMARY OSM id this is: refresh OSM data, keep everything else."""
    s: dict = {"name": lead["name"], "nameNormalized": lead["nameNormalized"], "updatedAt": now}
    s["categories"] = list(dict.fromkeys([*existing.get("categories", []), *lead["categories"]]))
    for field in ("address", "geo"):
        if field in lead:
            s[field] = lead[field]
    for field in ("emails", "phones", "whatsapp"):
        if field in lead or any(i.get("sourceType") == OSM for i in existing.get(field) or []):
            s[field] = _keep_non_osm(existing.get(field)) + lead.get(field, [])
    # Refreshed OSM values keep a person's manual verification (matched by value).
    if "emails" in s:
        contacts.carry_manual(s["emails"], contacts.manual_by_key(existing.get("emails"), contacts.email_key),
                              contacts.email_key)
    if "phones" in s:
        contacts.carry_manual(s["phones"], contacts.manual_by_key(existing.get("phones"), contacts.phone_key),
                              contacts.phone_key)
    for kind, value in lead.get("socials", {}).items():
        current = (existing.get("socials") or {}).get(kind)
        if not current or current.get("sourceType") == OSM:
            s[f"socials.{kind}"] = value
    if "website" in lead and (existing.get("website") or {}).get("url") != lead["website"]["url"]:
        s["website"] = lead["website"]  # new URL -> reachability must be checked again
    for field, src in (lead.get("fieldSources") or {}).items():
        if field != "website" or "website" in s:
            s[f"fieldSources.{field}"] = src
    osm_source = lead["sources"][0]["url"]
    s["sources"] = [x for x in existing.get("sources", []) if x.get("url") != osm_source] + lead["sources"]
    s["contactStatus"] = contacts.contact_status(
        s.get("emails", existing.get("emails")), s.get("phones", existing.get("phones")),
        s.get("whatsapp", existing.get("whatsapp")))
    key = dedupe.dedupe_key(lead["nameNormalized"], dedupe.lead_latlon(lead))
    if key and not existing.get("dedupeKey"):
        s["dedupeKey"] = key
    return s


def alias_update(existing: dict, lead: dict, now: datetime) -> dict:
    """The update for a lead that a SECOND OSM element describes: only fill gaps, never overwrite
    (otherwise the two elements would overwrite each other on every search)."""
    s: dict = {"updatedAt": now}
    s["categories"] = list(dict.fromkeys([*existing.get("categories", []), *lead["categories"]]))
    for field in ("address", "geo", "website"):
        if field in lead and not existing.get(field):
            s[field] = lead[field]
            if field in (lead.get("fieldSources") or {}):
                s[f"fieldSources.{field}"] = lead["fieldSources"][field]
    region = (lead.get("address") or existing.get("address") or {}).get("countryCode")
    for field, key in (("emails", "value"), ("phones", "raw"), ("whatsapp", "e164")):
        have = {str(i.get(key, "")).lower() for i in existing.get(field) or []}
        if field == "phones":
            have |= dedupe.lead_phone_keys(existing)
        new = []
        for item in lead.get(field, []):
            ident = dedupe.phone_key(item.get("raw"), region) if field == "phones" else ""
            if str(item.get(key, "")).lower() not in have and (not ident or ident not in have):
                new.append(item)
        if new:
            s[field] = list(existing.get(field) or []) + new
    for kind, value in lead.get("socials", {}).items():
        if not (existing.get("socials") or {}).get(kind):
            s[f"socials.{kind}"] = value
    s["contactStatus"] = contacts.contact_status(
        s.get("emails", existing.get("emails")), s.get("phones", existing.get("phones")),
        s.get("whatsapp", existing.get("whatsapp")))
    known = {x.get("url") for x in existing.get("sources", [])}
    s["sources"] = list(existing.get("sources", [])) + [x for x in lead["sources"] if x["url"] not in known]
    return s


def find_same_business(leads: Collection, owner_id, lead: dict) -> tuple[dict, str] | None:
    """Look for an existing lead that is this business under another id (indexed lookups only)."""
    ors: list[dict] = [{"nameNormalized": lead["nameNormalized"]}]
    domain = (lead.get("website") or {}).get("domain")
    if domain:
        ors.append({"website.domain": domain})
    phones = sorted(dedupe.lead_phone_keys(lead))
    if phones:
        ors.append({"phones.e164": {"$in": phones}})
    best = None
    for old in leads.find({"ownerId": owner_id, "$or": ors}).limit(CANDIDATE_LIMIT):
        reason = dedupe.same_business(lead, old)
        if reason:
            d = dedupe.distance_m(dedupe.lead_latlon(lead), dedupe.lead_latlon(old))
            rank = d if d is not None else 1e9
            if best is None or rank < best[2]:
                best = (old, reason, rank)
    return (best[0], best[1]) if best else None


def _normalize_phones(lead: dict) -> None:
    """International format (E.164) at save time, so `phones.e164` can be used to find duplicates.
    Numbers that aren't valid keep only their raw text (verification may still fix them later)."""
    region = (lead.get("address") or {}).get("countryCode")
    for p in lead.get("phones") or []:
        if not p.get("e164"):
            e164 = dedupe.phone_key(p.get("raw"), region)
            if e164:
                p["e164"] = e164
        p["checks"] = contacts.phone_checks(p.get("checks"), e164=p.get("e164"), now=lead["sources"][0]["fetchedAt"])
    lead["contactStatus"] = contacts.contact_status(lead.get("emails"), lead.get("phones"), lead.get("whatsapp"))


def _id_filter(owner_id, osm_id: str) -> dict:
    return {"ownerId": owner_id, "$or": [{"externalIds.osm": osm_id}, {"externalIds.osmAliases": osm_id}]}


def upsert_lead(leads: Collection, *, owner_id, job_id, lead: dict, now: datetime) -> str:
    """Returns 'new', 'updated' (same OSM id) or 'merged' (same business, other OSM id)."""
    osm_id = lead["externalIds"]["osm"]
    _normalize_phones(lead)
    existing = leads.find_one(_id_filter(owner_id, osm_id))
    if existing is not None:
        return _update_existing(leads, existing, lead, osm_id, job_id, now)

    match = find_same_business(leads, owner_id, lead)
    if match:
        return _merge_alias(leads, match[0], lead, osm_id, job_id, now, match[1])

    key = dedupe.dedupe_key(lead["nameNormalized"], dedupe.lead_latlon(lead))
    doc = {
        "contactStatus": "none",
        **lead,
        "ownerId": owner_id,
        "jobIds": [job_id],
        "confidence": 0,
        "saved": False,
        "status": "new",
        "notes": "",
        "discoveredAt": now,
        "createdAt": now,
        "updatedAt": now,
    }
    if key:
        doc["dedupeKey"] = key
    doc["contactsVersion"] = contacts.CONTACTS_VERSION
    try:
        leads.insert_one(doc)
        return "new"
    except DuplicateKeyError:
        # Another job/worker inserted this business a moment ago (same OSM id or same name+spot).
        doc.pop("_id", None)
        existing = leads.find_one(_id_filter(owner_id, osm_id))
        if existing is not None:
            return _update_existing(leads, existing, lead, osm_id, job_id, now)
        existing = leads.find_one({"ownerId": owner_id, "dedupeKey": key}) if key else None
        if existing is None:
            raise
        return _merge_alias(leads, existing, lead, osm_id, job_id, now, "dedupeKey")


def _update_existing(leads, existing, lead, osm_id, job_id, now) -> str:
    if existing.get("externalIds", {}).get("osm") == osm_id:
        update = merge_update(existing, lead, now)
    else:  # matched through an alias
        update = alias_update(existing, lead, now)
    try:
        leads.update_one({"_id": existing["_id"]}, {"$set": update, "$addToSet": {"jobIds": job_id}})
    except DuplicateKeyError:  # dedupeKey already used by another lead: keep the rest of the update
        update.pop("dedupeKey", None)
        leads.update_one({"_id": existing["_id"]}, {"$set": update, "$addToSet": {"jobIds": job_id}})
    return "updated"


def _merge_alias(leads, existing, lead, osm_id, job_id, now, reason: str) -> str:
    update = alias_update(existing, lead, now)
    leads.update_one(
        {"_id": existing["_id"]},
        {"$set": update, "$addToSet": {"jobIds": job_id, "externalIds.osmAliases": osm_id},
         "$push": {"mergeLog": {"$each": [{"at": now, "osm": osm_id, "reason": reason}], "$slice": -20}}},
    )
    return "merged"

