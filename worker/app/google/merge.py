"""Record what Google Text Search found, WITHOUT linking it to any lead.

Decision (2026-11-14): Google places are never matched with OSM, Overture or any other lead
record. Each place Google returns is stored only as a reference:

    googlePlaceRefs { ownerId, placeId, status: "seen", area, jobIds, firstSeenAt, lastSeenAt }

The place ID is the one Google value its policy allows us to keep indefinitely. No name, address,
coordinates or other Google content is stored, and refs never point at a lead. The UI shows them
as "open in Google Maps" links; exports never include them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pymongo.database import Database

from ..geo.text import area_key as cache_key


def maps_url(place_id: str) -> str:
    """Google's documented link format for opening a place by its ID."""
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"


@dataclass
class RefStats:
    seen: int = 0  # places Google returned in this job
    new: int = 0  # place IDs this user had never seen before


def record_refs(db: Database, *, owner_id, job_id, candidates: dict[str, dict], area_query: str,
                now: datetime) -> RefStats:
    """Upsert one reference per place ID (unique per user: the same place is never stored twice)."""
    stats = RefStats()
    area = cache_key(area_query)
    for place_id in candidates:
        stats.seen += 1
        res = db.googlePlaceRefs.update_one(
            {"ownerId": owner_id, "placeId": place_id},
            {"$set": {"status": "seen", "area": area, "lastSeenAt": now, "lastJobId": job_id},
             "$setOnInsert": {"firstSeenAt": now},
             "$addToSet": {"jobIds": job_id}},
            upsert=True,
        )
        stats.new += res.upserted_id is not None
    return stats


def purge_expired_locations(db: Database, now: datetime) -> int:
    """Clean-up for references written by the earlier version, which cached coordinates for max
    30 days (Google policy). New references never contain coordinates. Place IDs are kept."""
    return db.googlePlaceRefs.update_many(
        {"locationExpiresAt": {"$lt": now}},
        {"$unset": {"location": "", "locationExpiresAt": ""}},
    ).modified_count
