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

import copy
from collections import defaultdict
from datetime import datetime

from bson import ObjectId
from pymongo import InsertOne, UpdateOne
from pymongo.collection import Collection
from pymongo.errors import BulkWriteError, DuplicateKeyError

from . import contacts, dedupe

OSM = "osm"
OVERTURE = "overture"
# Safety cap on duplicate candidates per lookup. Candidates are ranked by distance in Python, and every
# rule needs the places to be close, so a big chain (600+ "Sports Direct") can't hide the right one.
CANDIDATE_LIMIT = 2000

# Fields with PARTIAL unique indexes (web/src/models/Lead.ts). MongoDB only uses a partial index when the
# query itself implies the index filter, and {field: "x"} does NOT imply {$type: "string"}: without it a
# lookup for a lead that isn't saved yet scanned every lead of the user (measured: 21k docs, ~200 ms).
PARTIAL_FIELDS = ("externalIds.osm", "externalIds.overture", "externalIds.googlePlaceId", "dedupeKey")

# Fields the duplicate/merge logic never reads: left out when loading a batch (they are the bulk of a lead).
NOT_NEEDED = frozenset({"overture", "fieldSources", "mergeLog", "jobIds", "notes"})


def str_eq(value) -> dict:
    """Equality that can use a partial {$type: "string"} index."""
    return {"$eq": value, "$type": "string"}


def str_in(values) -> dict:
    return {"$in": list(values), "$type": "string"}


def lane_of(lead: dict) -> str:
    """Licensing mode M2: every lead belongs to exactly one data lane. Leads saved before Phase 2c
    have no dataLane field and are OSM."""
    return lead.get("dataLane") or OSM


def lane_filter(lane: str) -> dict:
    return {"dataLane": OVERTURE} if lane == OVERTURE else {"dataLane": {"$ne": OVERTURE}}


def _keep_non(items: list | None, source_type: str) -> list:
    return [i for i in items or [] if i.get("sourceType") != source_type]


def _keep_non_osm(items: list | None) -> list:
    return _keep_non(items, OSM)


def merge_update(existing: dict, lead: dict, now: datetime) -> dict:
    """The $set for an existing lead whose PRIMARY source id this is: refresh that source's data
    (OSM or Overture, by the lead's lane), keep everything else."""
    OSM = lead["sources"][0]["type"]  # noqa: N806  (the source type being refreshed: "osm" or "overture")
    s: dict = {"name": lead["name"], "nameNormalized": lead["nameNormalized"], "updatedAt": now}
    s["categories"] = list(dict.fromkeys([*existing.get("categories", []), *lead["categories"]]))
    for field in ("address", "geo"):
        if field in lead:
            s[field] = lead[field]
    for field in ("emails", "phones", "whatsapp"):
        if field in lead or any(i.get("sourceType") == OSM for i in existing.get(field) or []):
            s[field] = _keep_non(existing.get(field), OSM) + lead.get(field, [])
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
    key = lane_dedupe_key(lead)
    if key and not existing.get("dedupeKey"):
        s["dedupeKey"] = key
    return s


def lane_dedupe_key(lead: dict) -> str | None:
    """Same name in the same ~110 m cell, per lane: an Overture place must never collide with an OSM
    place on the unique (ownerId, dedupeKey) index (that would be a cross-lane merge)."""
    key = dedupe.dedupe_key(lead["nameNormalized"], dedupe.lead_latlon(lead))
    if key and lane_of(lead) == OVERTURE:
        return f"{OVERTURE}|{key}"
    return key


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
    # Only the same lane: OSM and Overture records are never matched with each other (M2).
    found = leads.find({"ownerId": owner_id, **lane_filter(lane_of(lead)), "$or": ors}, {"_id": 1, **DEDUPE_FIELDS})  # copy
    best = best_match(lead, found.limit(CANDIDATE_LIMIT))
    if best is None:
        return None
    full = leads.find_one({"_id": best[0]["_id"]})
    return (full, best[1]) if full is not None else None


# What dedupe.same_business reads (candidates are loaded with this projection, the winner in full).
DEDUPE_FIELDS = {f: 1 for f in ("name", "nameNormalized", "geo", "phones", "website", "address", "dataLane")}


def best_match(lead: dict, candidates) -> tuple[dict, str] | None:
    """The NEAREST candidate that dedupe.same_business accepts (ties: the first one)."""
    best = None
    here = dedupe.lead_latlon(lead)
    for old in candidates:
        if lane_of(old) != lane_of(lead):  # defence in depth: the query already filters by lane
            continue
        reason = dedupe.same_business(lead, old)
        if reason:
            d = dedupe.distance_m(here, dedupe.lead_latlon(old))
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
    # Top-level $or with ownerId in every branch + $type: each branch uses its own index.
    return {"$or": [{"ownerId": owner_id, "externalIds.osm": str_eq(osm_id)},
                    {"ownerId": owner_id, "externalIds.osmAliases": osm_id}]}


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

    key = lane_dedupe_key(lead)
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
        existing = leads.find_one({"ownerId": owner_id, "dedupeKey": str_eq(key)}) if key else None
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



# ---------------------------------------------------------------- Overture lane


def _overture_filter(owner_id, lead: dict) -> dict:
    ids = lead["externalIds"]
    base = {"ownerId": owner_id, "dataLane": OVERTURE}
    # Top-level $or, every branch complete (+ $type for the partial index): each branch uses its own index.
    ors: list[dict] = [{**base, "externalIds.overture": str_eq(ids["overture"])},
                       {**base, "externalIds.overtureAliases": ids["overture"]}]
    if ids.get("overtureSources"):
        ors.append({**base, "externalIds.overtureSources": {"$in": ids["overtureSources"]}})
    return {"$or": ors}


def upsert_overture_lead(leads: Collection, *, owner_id, job_id, lead: dict, now: datetime) -> str:
    """Overture lane only, one lead at a time (the reference path; batches fall back to it).
    Returns 'new', 'updated' or 'merged'.

    Identity: the Overture (GERS) id, earlier ids (aliases) and the upstream source record ids
    ("meta:798829223526334"). GERS ids can change between releases (rename, move, re-match); the
    source record id usually survives, so the same place updates its lead instead of duplicating it.
    Overture itself contains duplicates: same_business() (same lane only) merges them like OSM does.
    """
    _normalize_phones(lead)
    existing = leads.find_one(_overture_filter(owner_id, lead))
    if existing is not None:
        return _write_overture_update(leads, existing, lead, job_id, now)
    match = find_same_business(leads, owner_id, lead)
    if match:
        leads.update_one({"_id": match[0]["_id"]}, overture_merge_update(match[0], lead, job_id, now, match[1]))
        return "merged"
    doc = overture_new_doc(lead, owner_id, job_id, now)
    key = doc.get("dedupeKey")
    try:
        leads.insert_one(doc)
        return "new"
    except DuplicateKeyError:  # another job/worker inserted the same place a moment ago
        doc.pop("_id", None)
        existing = leads.find_one(_overture_filter(owner_id, lead)) or (
            leads.find_one({"ownerId": owner_id, "dedupeKey": str_eq(key)}) if key else None)
        if existing is None:
            raise
        return _write_overture_update(leads, existing, lead, job_id, now)


def _write_overture_update(leads, existing, lead, job_id, now) -> str:
    update = overture_identity_update(existing, lead, job_id, now)
    try:
        leads.update_one({"_id": existing["_id"]}, update)
    except DuplicateKeyError:  # dedupeKey already used by another lead: keep the rest of the update
        _drop_dedupe_key(update)
        leads.update_one({"_id": existing["_id"]}, update)
    return "updated"


def _drop_dedupe_key(update: dict) -> None:
    update["$set"].pop("dedupeKey", None)
    update["$addToSet"].pop("externalIds.overtureSources", None)


# The three writes, shared by the single and the batch path (so both always do exactly the same).
def overture_identity_update(existing: dict, lead: dict, job_id, now) -> dict:
    """Same place (same GERS id, an alias or the same upstream source record)."""
    gers = lead["externalIds"]["overture"]
    if existing.get("externalIds", {}).get("overture") == gers:
        update = merge_update(existing, lead, now)
        extra: dict = {}
    else:  # same place, new GERS id (or a duplicate found by source id): keep the old id as an alias
        update = alias_update(existing, lead, now)
        extra = {"externalIds.overtureAliases": gers}
    update["overture"] = lead["overture"]  # newest release's provenance
    return {"$set": update, "$addToSet": {
        "jobIds": job_id, **extra,
        "externalIds.overtureSources": {"$each": lead["externalIds"].get("overtureSources", [])}}}


def overture_merge_update(match: dict, lead: dict, job_id, now, reason: str) -> dict:
    """Same business under another Overture id (Overture's own duplicates): fill gaps, keep an alias."""
    gers = lead["externalIds"]["overture"]
    return {
        "$set": alias_update(match, lead, now),
        "$addToSet": {"jobIds": job_id, "externalIds.overtureAliases": gers,
                      "externalIds.overtureSources": {"$each": lead["externalIds"].get("overtureSources", [])}},
        "$push": {"mergeLog": {"$each": [{"at": now, "overture": gers, "reason": reason}], "$slice": -20}}}


def overture_new_doc(lead: dict, owner_id, job_id, now) -> dict:
    doc = {"contactStatus": "none", **lead, "ownerId": owner_id, "jobIds": [job_id], "confidence": 0, "saved": False,
           "status": "new", "notes": "", "discoveredAt": now, "createdAt": now, "updatedAt": now,
           "contactsVersion": contacts.CONTACTS_VERSION}
    key = lane_dedupe_key(lead)
    if key:
        doc["dedupeKey"] = key
    return doc


# ---------------------------------------------------------------- Overture lane, batched

class _Pool:
    """The user's Overture leads a batch can touch (identity or duplicate candidates), loaded with ONE
    query and kept up to date in memory while the batch is planned, so row 300 sees what rows 1-299 did.
    Lookup tables only grow; every hit is re-checked against the document itself."""

    def __init__(self, docs):
        self.docs: dict = {}
        self.by_gers: dict = {}
        self.by_key: dict = {}
        self.by_alias, self.by_source = defaultdict(set), defaultdict(set)
        self.by_name, self.by_domain, self.by_phone = defaultdict(set), defaultdict(set), defaultdict(set)
        for d in docs:
            self.add(d)

    def add(self, doc: dict) -> None:
        _id = doc["_id"]
        self.docs[_id] = doc
        ids = doc.get("externalIds") or {}
        if isinstance(ids.get("overture"), str):
            self.by_gers[ids["overture"]] = _id
        for a in ids.get("overtureAliases") or []:
            self.by_alias[a].add(_id)
        for k in ids.get("overtureSources") or []:
            self.by_source[k].add(_id)
        if isinstance(doc.get("dedupeKey"), str):
            self.by_key[doc["dedupeKey"]] = _id
        if doc.get("nameNormalized"):
            self.by_name[doc["nameNormalized"]].add(_id)
        if (doc.get("website") or {}).get("domain"):
            self.by_domain[doc["website"]["domain"]].add(_id)
        for p in dedupe.lead_phone_keys(doc):
            self.by_phone[p].add(_id)

    def identity(self, lead: dict) -> dict | None:
        """Same as _overture_filter: GERS id, then an alias, then an upstream source record id."""
        ids = lead["externalIds"]
        if ids["overture"] in self.by_gers:
            return self.docs[self.by_gers[ids["overture"]]]
        hits = set(self.by_alias.get(ids["overture"], ()))
        for k in ids.get("overtureSources") or []:
            hits |= self.by_source.get(k, set())
        return self.docs[min(hits)] if hits else None  # several: the oldest lead (deterministic)

    def candidates(self, lead: dict) -> list[dict]:
        """Same as find_same_business's query: same name, domain or phone (same lane: the pool is Overture only)."""
        hits = set(self.by_name.get(lead["nameNormalized"], ()))
        if (lead.get("website") or {}).get("domain"):
            hits |= self.by_domain.get(lead["website"]["domain"], set())
        for p in dedupe.lead_phone_keys(lead):
            hits |= self.by_phone.get(p, set())
        return [self.docs[h] for h in sorted(hits)]


def _batch_filter(owner_id, items: list[dict]) -> dict:
    base = {"ownerId": owner_id, "dataLane": OVERTURE}
    gers = sorted({lead["externalIds"]["overture"] for lead in items})
    srcs = sorted({k for lead in items for k in lead["externalIds"].get("overtureSources") or []})
    keys = sorted({k for lead in items if (k := lane_dedupe_key(lead))})
    names = sorted({lead["nameNormalized"] for lead in items if lead.get("nameNormalized")})
    domains = sorted({d for lead in items if (d := (lead.get("website") or {}).get("domain"))})
    phones = sorted(set().union(*(dedupe.lead_phone_keys(lead) for lead in items)))
    ors = [{**base, "externalIds.overture": str_in(gers)}, {**base, "externalIds.overtureAliases": {"$in": gers}}]
    for field, values in (("externalIds.overtureSources", srcs), ("nameNormalized", names),
                          ("website.domain", domains), ("phones.e164", phones)):
        if values:
            ors.append({**base, field: {"$in": values}})
    if keys:
        ors.append({**base, "dedupeKey": str_in(keys)})
    return {"$or": ors}


def _set_path(doc: dict, path: str, value) -> None:
    *parents, last = path.split(".")
    for p in parents:
        doc = doc.setdefault(p, {})
    doc[last] = value


def _apply(doc: dict, update: dict) -> None:
    """Apply a $set/$addToSet update to the in-memory copy (what MongoDB will do to the stored lead)."""
    for path, value in update.get("$set", {}).items():
        _set_path(doc, path, copy.deepcopy(value))
    for path, value in update.get("$addToSet", {}).items():
        if path == "jobIds":
            continue
        *parents, last = path.split(".")
        node = doc
        for p in parents:
            node = node.setdefault(p, {})
        current = node.setdefault(last, [])
        for v in value["$each"] if isinstance(value, dict) and "$each" in value else [value]:
            if v not in current:
                current.append(copy.deepcopy(v))


def upsert_overture_batch(leads: Collection, *, owner_id, job_id, items: list[dict], now: datetime) -> list:
    """Save many Overture leads with 2 database round trips (one read, one ordered bulk write) instead of
    3+ per lead. Same decisions as upsert_overture_lead, row by row in order. Returns one result per
    item: 'new' / 'updated' / 'merged', or the Exception that one lead raised.

    Safety:
      - every update only applies if the lead is unchanged since it was read (updatedAt, as the crawler
        and the web app do), so a person's verification, notes or the crawler's data are never overwritten;
      - unique indexes stay the final guard against another worker inserting the same place;
      - anything that didn't apply cleanly (conflict, duplicate key, error) is redone one lead at a time
        through the single path, which re-reads the database. Every write is idempotent.
    """
    if not items:
        return []
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)  # what MongoDB stores (ms): updatedAt check
    for lead in items:
        _normalize_phones(lead)
    pool = _Pool(leads.find(_batch_filter(owner_id, items), {f: 0 for f in NOT_NEEDED}))  # fresh dict per call
    ops: list = []
    op_item: list[int] = []  # op index -> item index
    results: list = []
    touched: dict = defaultdict(list)  # lead _id -> items that updated it
    for idx, lead in enumerate(items):
        target, kind = pool.identity(lead), "updated"
        if target is None:
            match = best_match(lead, pool.candidates(lead))
            if match:
                target, kind = match[0], "merged"
                update = overture_merge_update(target, lead, job_id, now, match[1])
            else:
                key = lane_dedupe_key(lead)
                if key and key in pool.by_key:  # what the single path does on DuplicateKeyError
                    target = pool.docs[pool.by_key[key]]
                else:
                    doc = overture_new_doc(lead, owner_id, job_id, now)
                    doc["_id"] = ObjectId()
                    ops.append(InsertOne(copy.deepcopy(doc)))
                    op_item.append(idx)
                    pool.add({k: v for k, v in copy.deepcopy(doc).items() if k not in NOT_NEEDED})
                    results.append("new")
                    continue
        if kind == "updated":
            update = overture_identity_update(target, lead, job_id, now)
            key = update["$set"].get("dedupeKey")
            if key and pool.by_key.get(key, target["_id"]) != target["_id"]:
                _drop_dedupe_key(update)  # another lead has that key
        ops.append(UpdateOne({"_id": target["_id"], "updatedAt": target.get("updatedAt")}, copy.deepcopy(update)))
        op_item.append(idx)
        touched[target["_id"]].append(idx)
        _apply(target, update)
        pool.add(target)
        results.append(kind)

    redo: set[int] = set()
    try:
        res = leads.bulk_write(ops, ordered=True)
        applied, matched = len(ops), res.matched_count
    except BulkWriteError as exc:  # ordered: everything before the failing op was written, nothing after
        applied = exc.details["writeErrors"][0]["index"]
        matched = exc.details.get("nMatched", 0)
        redo |= {op_item[i] for i in range(applied, len(ops))}
    updates = sum(1 for op in ops[:applied] if isinstance(op, UpdateOne))
    if matched < updates:  # someone changed a lead after we read it: find which, redo those
        done = {op_item[i] for i in range(applied)}
        ids = [_id for _id, idxs in touched.items() if set(idxs) & done]
        stored = {d["_id"]: d.get("updatedAt") for d in leads.find({"_id": {"$in": ids}}, {"updatedAt": 1})}
        for _id in ids:
            if not _same_time(stored.get(_id), now):
                redo |= set(touched[_id])
    for idx in sorted(redo):
        try:
            results[idx] = upsert_overture_lead(leads, owner_id=owner_id, job_id=job_id, lead=items[idx], now=now)
        except Exception as exc:  # noqa: BLE001  (reported per lead by the caller)
            results[idx] = exc
    return results


def _same_time(stored, now: datetime) -> bool:
    if stored is None:
        return False
    if stored.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif stored.tzinfo is not None and now.tzinfo is None:
        stored = stored.replace(tzinfo=None)
    return stored == now
