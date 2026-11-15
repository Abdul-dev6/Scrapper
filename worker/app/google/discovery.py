"""Text Search over an area, split into smaller tiles where Google's 60-result cap is hit.

Text Search returns at most 60 places (3 pages x 20) per query. If a tile comes back "full"
(60 results), there are probably more: the tile is split into 4 and each quarter is searched.

Coverage memory (googleCoverage, per user, 30 days): a tile already searched completely is
skipped next time; a FULL tile is not searched again but its quarters are. So repeating a
search spends the budget on unexplored, deeper tiles and finds NEW gyms instead of paying for
the same 60 results again. Coverage stores only counts, no Google content.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pymongo.database import Database

from ..geo.tiles import Tile, initial_tiles
from .places import MAX_PAGES, PAGE_SIZE, GoogleFatal, GoogleTransient, PlacesClient, QuotaExhausted

# One plain query per category. Several categories can share one query.
CATEGORY_QUERIES: dict[str, str] = {
    "gym": "gym",
    "fitness_center": "gym",
    "boxing_club": "boxing club",
    "mma_academy": "MMA gym",
    "martial_arts_school": "martial arts school",
    "wrestling_club": "wrestling club",
    "sports_store": "sporting goods store",
}
FULL = MAX_PAGES * PAGE_SIZE  # 60
MIN_TILE_DEG = 0.01  # ~1 km: never split below this
COVERAGE_DAYS = 30


def queries_for(categories: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(CATEGORY_QUERIES[c] for c in categories if c in CATEGORY_QUERIES))


def tile_key(owner_id, query: str, tile: Tile) -> str:
    return f"{owner_id}|{query}|{tile.south:.4f},{tile.west:.4f},{tile.north:.4f},{tile.east:.4f}"


def _can_split(tile: Tile) -> bool:
    return (tile.north - tile.south) > MIN_TILE_DEG * 2 or (tile.east - tile.west) > MIN_TILE_DEG * 2


@dataclass
class GoogleDiscovery:
    candidates: dict[str, dict] = field(default_factory=dict)  # place id -> in-memory candidate
    requests: int = 0
    tiles_searched: int = 0
    tiles_skipped: int = 0
    tiles_split: int = 0
    stopped: str = ""  # "", "quota", "budget", "error", "cancelled"
    error: str = ""


def discover(
    places: PlacesClient,
    db: Database,
    *,
    owner_id,
    categories: Iterable[str],
    bbox: list[float],
    max_requests: int,
    now: datetime,
    stop: threading.Event | None = None,
) -> GoogleDiscovery:
    out = GoogleDiscovery()
    stop = stop or threading.Event()
    coverage = db.googleCoverage
    fresh_after = now - timedelta(days=COVERAGE_DAYS)

    # Round-robin over queries so a small budget is shared fairly between categories.
    frontier: deque[tuple[str, Tile]] = deque()
    roots = initial_tiles(bbox)
    for tile in roots:
        for query in queries_for(categories):
            frontier.append((query, tile))

    while frontier:
        if stop.is_set():
            out.stopped = "cancelled"
            break
        query, tile = frontier.popleft()
        key = tile_key(owner_id, query, tile)
        cov = coverage.find_one({"_id": key})
        if cov and cov.get("searchedAt") and _aware(cov["searchedAt"], now) >= fresh_after:
            out.tiles_skipped += 1
            if cov.get("saturated") and _can_split(tile):
                frontier.extend((query, t) for t in tile.split())
            continue
        if out.requests >= max_requests:
            out.stopped = "budget"
            break

        found, complete = 0, False
        token = None
        try:
            for _page in range(MAX_PAGES):
                if out.requests >= max_requests or stop.is_set():
                    break
                out.requests += 1
                results, token = places.text_search(query, tile.bbox, token)
                for c in results:
                    if c["businessStatus"] == "CLOSED_PERMANENTLY":
                        continue
                    out.candidates.setdefault(c["id"], c)
                found += len(results)
                if not token:
                    complete = True
                    break
            else:
                complete = True  # 3 full pages: that's everything Google will give for this tile
        except QuotaExhausted as exc:
            out.stopped, out.error = "quota", str(exc)
            break
        except GoogleFatal as exc:
            out.stopped, out.error = "error", str(exc)
            break
        except GoogleTransient as exc:
            out.error = str(exc)  # skip this tile, try the others
            continue

        out.tiles_searched += 1
        saturated = found >= FULL
        if complete:  # an unfinished tile (budget/cancel) is not remembered, so it's retried next time
            coverage.update_one(
                {"_id": key},
                {"$set": {"ownerId": owner_id, "query": query, "bbox": tile.bbox, "results": found,
                          "saturated": saturated, "searchedAt": now}},
                upsert=True,
            )
        if saturated and _can_split(tile):
            out.tiles_split += 1
            frontier.extend((query, t) for t in tile.split())
    return out


def _aware(dt: datetime, ref: datetime) -> datetime:
    """mongomock/pymongo without tz_aware may return naive datetimes (stored as UTC)."""
    if dt.tzinfo is None and ref.tzinfo is not None:
        return dt.replace(tzinfo=ref.tzinfo)
    return dt
