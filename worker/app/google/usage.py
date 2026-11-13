"""Global monthly Google usage counters, shared by every job and every worker.

One document per calendar month (Pacific time, like Google's billing):
    apiUsage { _id: "google:2026-11", textSearchPro: 37, placeDetailsEssentials: 120, ... }

reserve() is ONE atomic find_one_and_update whose filter says "both counters are still under
their internal limits". Two jobs racing for the last unit can't both win: MongoDB applies the
updates one at a time and the second one no longer matches the filter.

When EITHER counter reaches its limit, every Google call is refused (the whole provider is
switched off) and jobs continue OSM-only. A new month is a new document, so Google comes back
by itself after the reset. Units are never given back after a failed request: being slightly
over-counted is safe, being under-counted could cost money.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument
from pymongo.database import Database

log = logging.getLogger("worker.google.usage")

TEXT_SEARCH = "textSearchPro"
DETAILS = "placeDetailsEssentials"
SKUS = (TEXT_SEARCH, DETAILS)
# First-tier list prices per request (USD), Places API (New), for the cost log only.
LIST_PRICE_USD = {TEXT_SEARCH: 0.032, DETAILS: 0.005}
LOG_TTL_DAYS = 90


@dataclass(frozen=True)
class Reservation:
    sku: str
    month: str
    count: int  # the counter value including this request

    def billable(self, free_cap: int) -> bool:
        """Would Google bill this request? Only past the free cap (and only if no other project
        on the same billing account used the free allowance)."""
        return self.count > free_cap


def month_key(now: datetime, tz: str) -> str:
    return now.astimezone(ZoneInfo(tz)).strftime("%Y-%m")


def next_reset(now: datetime, tz: str) -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return nxt.astimezone(timezone.utc)


class UsageLimiter:
    def __init__(
        self,
        db: Database,
        *,
        limits: dict[str, int],
        free_caps: dict[str, int],
        tz: str = "America/Los_Angeles",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.coll = db.apiUsage
        self.log_coll = db.apiUsageLog
        self.limits = limits
        self.free_caps = free_caps
        self.tz = tz
        self.clock = clock

    def _doc_id(self, now: datetime) -> tuple[str, str]:
        month = month_key(now, self.tz)
        return f"google:{month}", month

    def _ensure(self, now: datetime) -> tuple[str, str]:
        doc_id, month = self._doc_id(now)
        self.coll.update_one(
            {"_id": doc_id},
            {
                "$setOnInsert": {"provider": "google", "month": month, "timezone": self.tz, "createdAt": now,
                                 **{sku: 0 for sku in SKUS}, **{f"failed.{sku}": 0 for sku in SKUS}},
                "$set": {"limits": self.limits, "freeCaps": self.free_caps,
                         "resetsAt": next_reset(now, self.tz)},
            },
            upsert=True,
        )
        return doc_id, month

    def _under_limits(self) -> dict:
        return {sku: {"$lt": self.limits[sku]} for sku in SKUS}

    def reserve(self, sku: str) -> Reservation | None:
        """Take one unit BEFORE sending the request. None = limit reached, don't call Google."""
        assert sku in SKUS
        now = self.clock()
        doc_id, month = self._ensure(now)
        doc = self.coll.find_one_and_update(
            {"_id": doc_id, **self._under_limits()},
            {"$inc": {sku: 1}, "$set": {"updatedAt": now}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            self.coll.update_one({"_id": doc_id, "exhaustedAt": {"$exists": False}}, {"$set": {"exhaustedAt": now}})
            return None
        if any(doc.get(s, 0) >= self.limits[s] for s in SKUS):
            self.coll.update_one({"_id": doc_id, "exhaustedAt": {"$exists": False}}, {"$set": {"exhaustedAt": now}})
            log.warning("Google internal monthly limit reached for %s: switching to OSM-only until %s",
                        month, next_reset(now, self.tz).isoformat())
        return Reservation(sku, month, doc[sku])

    def available(self) -> bool:
        """Read-only check (the real gate is reserve())."""
        now = self.clock()
        doc_id, _ = self._doc_id(now)
        doc = self.coll.find_one({"_id": doc_id})
        return doc is None or all(doc.get(s, 0) < self.limits[s] for s in SKUS)

    def record(self, res: Reservation, *, job_id=None, ok: bool, http_status: int | None = None,
               error: str = "", what: str = "") -> None:
        """Log one request (success or failure) with its estimated cost."""
        now = self.clock()
        billable = res.billable(self.free_caps[res.sku])
        cost = LIST_PRICE_USD[res.sku] if billable else 0.0
        self.log_coll.insert_one({
            "at": now, "provider": "google", "sku": res.sku, "month": res.month, "jobId": job_id, "ok": ok,
            "httpStatus": http_status, "error": error[:300], "what": what[:200],
            "listPriceUsd": LIST_PRICE_USD[res.sku], "estimatedCostUsd": cost, "counter": res.count,
        })
        inc: dict = {}
        if not ok:
            inc[f"failed.{res.sku}"] = 1
        if cost:
            inc["estimatedCostUsd"] = cost
        if inc:
            self.coll.update_one({"_id": f"google:{res.month}"}, {"$inc": inc})
        log.info("google %s #%s %s%s%s", res.sku, res.count, "ok" if ok else "FAILED",
                 f" http={http_status}" if http_status else "", f" est_cost=${cost:.3f}" if cost else "")

    def status(self) -> dict:
        now = self.clock()
        doc_id, month = self._doc_id(now)
        doc = self.coll.find_one({"_id": doc_id}) or {}
        used = {s: doc.get(s, 0) for s in SKUS}
        return {
            "month": month,
            "used": used,
            "limits": dict(self.limits),
            "available": all(used[s] < self.limits[s] for s in SKUS),
            "resetsAt": next_reset(now, self.tz),
        }
