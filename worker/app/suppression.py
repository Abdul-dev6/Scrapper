"""Opt-out list. Values are stored as sha256(normalized value), never in plain text.

The web app (Step 5) must normalize the same way when adding entries:
  email  -> trimmed, lowercased
  phone  -> digits only, keeping a leading '+'
  domain -> lowercased, no 'www.' prefix
"""

from __future__ import annotations

import hashlib
import re

from pymongo.collection import Collection


def normalize(kind: str, value: str) -> str:
    value = value.strip()
    if kind == "email":
        return value.lower()
    if kind == "phone":
        plus = "+" if value.startswith("+") else ""
        return plus + re.sub(r"\D", "", value)
    if kind == "domain":
        return value.lower().removeprefix("www.")
    raise ValueError(f"Unknown suppression type: {kind}")


def value_hash(kind: str, value: str) -> str:
    return hashlib.sha256(normalize(kind, value).encode("utf-8")).hexdigest()


def load_hashes(coll: Collection, owner_id) -> set[tuple[str, str]]:
    """Load once per job; the list is small and this avoids one query per lead."""
    return {(d["type"], d["valueHash"]) for d in coll.find({"ownerId": owner_id}, {"type": 1, "valueHash": 1})}


def is_suppressed(lead: dict, hashes: set[tuple[str, str]]) -> bool:
    if not hashes:
        return False
    candidates = [("email", e["value"]) for e in lead.get("emails", [])]
    for p in lead.get("phones", []) + lead.get("whatsapp", []):
        # Check both the number as written and its international form ("020 7946 0000" = "+442079460000").
        candidates += [("phone", v) for v in (p.get("raw"), p.get("e164")) if v]
    if lead.get("website", {}).get("domain"):
        candidates.append(("domain", lead["website"]["domain"]))
    return any((kind, value_hash(kind, value)) in hashes for kind, value in candidates)
