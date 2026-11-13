"""Adaptive area search, so a whole country works on the free, shared Overpass server.

Idea (a "quadtree"): ask for the whole area in one query. If the server says
it's too heavy, split that piece into 4 and try each quarter, and keep going.
Empty areas (sea, mountains) stay big and cost one quick query; only the
crowded areas get split. A city usually needs exactly one query.

Results are yielded per piece, so the caller can save leads as they arrive.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

import httpx

from . import overpass
from .nominatim import bbox_area_km2

MIN_TILE_DEG = 0.05  # ~5 km; a piece this small is never "too heavy" in practice
# Below ~25 km a "took too long" answer means the server is overloaded, not that the
# piece has too many businesses. Splitting further only multiplies the queries, so
# we wait and retry the same piece instead (seen live: 520 tiny UK pieces skipped).
SPLIT_MIN_DEG = 0.25
MAX_BUSY_RETRIES = 6  # per piece
WAIT_STEP = 5.0  # seconds between cancel checks while pausing
MAX_STATUS_FAILURES = 2  # status page unreadable this often -> stop checking it for this job


@dataclass(frozen=True)
class Tile:
    south: float
    west: float
    north: float
    east: float

    @property
    def bbox(self) -> list[float]:
        return [self.south, self.west, self.north, self.east]

    @property
    def area(self) -> float:
        return bbox_area_km2(self.bbox)

    def can_split(self) -> bool:
        return (self.north - self.south) > MIN_TILE_DEG or (self.east - self.west) > MIN_TILE_DEG

    def worth_splitting(self) -> bool:
        return (self.north - self.south) > SPLIT_MIN_DEG or (self.east - self.west) > SPLIT_MIN_DEG

    def split(self) -> list["Tile"]:
        lat = (self.south + self.north) / 2
        lon = (self.west + self.east) / 2
        return [
            Tile(self.south, self.west, lat, lon),
            Tile(self.south, lon, lat, self.east),
            Tile(lat, self.west, self.north, lon),
            Tile(lat, lon, self.north, self.east),
        ]

    def label(self) -> str:
        return f"{self.south:.2f},{self.west:.2f},{self.north:.2f},{self.east:.2f}"


def initial_tiles(bbox: list[float]) -> list[Tile]:
    south, west, north, east = bbox
    if west > east:  # crosses the 180° line (e.g. Fiji): two boxes, one on each side
        return [Tile(south, west, north, 180.0), Tile(south, -180.0, north, east)]
    return [Tile(south, west, north, east)]


@dataclass
class PieceResult:
    elements: list[dict]
    fraction_done: float  # 0..1, by area searched
    queries: int  # queries sent so far
    pending: int  # pieces still waiting
    failed_tile: Tile | None = None
    error: str = ""
    waited: list[float] = field(default_factory=list)
    # Everything needed to continue after this piece (saved as the job's checkpoint).
    state: dict = field(default_factory=dict)


def discover(
    categories: Iterable[str],
    *,
    area_id: int | None,
    bbox: list[float],
    client: httpx.Client,
    url: str,
    status_url: str,
    sleep: Callable[[float], None],
    timeout: int,
    max_queries: int,
    on_wait: Callable[[], None] = lambda: None,
    resume: dict | None = None,
) -> Iterator[PieceResult]:
    """on_wait() is called before every query and every pause: the caller uses it as a
    heartbeat (so a long wait isn't mistaken for a crashed worker) and to notice Cancel.

    resume: a previous PieceResult.state; the search continues with the pieces that were still
    waiting, instead of starting the whole area again."""
    categories = list(categories)
    real_sleep = sleep

    def sleep(seconds: float) -> None:
        # In steps of max 5 s, so Cancel is noticed within seconds even during a 60 s pause.
        on_wait()
        while seconds > 0:
            step = min(seconds, WAIT_STEP)
            real_sleep(step)
            seconds -= step
            on_wait()

    # Each piece carries its share of the whole area (shares always sum to exactly 1), so
    # progress reaches 100% even though km² of 4 quarters only approximately equals their parent.
    if resume:
        pending: deque[tuple[Tile, float]] = deque((Tile(*p[:4]), p[4]) for p in resume["pending"])
        done = float(resume.get("done", 0.0))
        queries = int(resume.get("queries", 0))
    else:
        pending = deque(_with_shares(initial_tiles(bbox), 1.0))
        done = 0.0
        queries = 0
    status_failures = 0

    def finished(share: float) -> float:
        nonlocal done
        done += share
        return 1.0 if not pending else min(done, 1.0)

    def result(elements, fraction, failed_tile=None, error="") -> PieceResult:
        state = {"pending": [[t.south, t.west, t.north, t.east, sh] for t, sh in pending], "done": done,
                 "queries": queries}
        return PieceResult(elements, fraction, queries, len(pending), failed_tile, error, state=state)

    while pending:
        tile, share = pending.popleft()

        if queries >= max_queries:
            yield result([], finished(share), tile, f"Stopped after {max_queries} searches; this part was not searched.")
            continue

        query = overpass.build_query(categories, area_id=area_id, bbox=tile.bbox, timeout=timeout)
        busy = 0
        while True:
            on_wait()
            if status_failures < MAX_STATUS_FAILURES:
                if overpass.wait_for_slot(client, status_url, sleep) is None:
                    status_failures += 1  # after 2 failures, stop asking for this job
            queries += 1
            try:
                elements = overpass.run_query(query, client=client, url=url)
            except overpass.OverpassTooHeavy as exc:
                if tile.worth_splitting():
                    # Search the 4 quarters next (before other pieces, so progress stays local).
                    pending.extendleft(reversed(_with_shares(tile.split(), share)))
                    break
                error = exc  # small piece: the server is slow, treat it like "busy"
            except overpass.OverpassBusy as exc:
                error = exc
            else:
                yield result(elements, finished(share))
                break
            busy += 1
            if busy > MAX_BUSY_RETRIES or queries >= max_queries:
                yield result([], finished(share), tile, str(error))
                break
            sleep(min(15 * busy, 60))  # back off a bit more each time


def _with_shares(tiles: list[Tile], share: float) -> list[tuple[Tile, float]]:
    """Split `share` between tiles in proportion to their area."""
    areas = [t.area for t in tiles]
    total = sum(areas) or 1.0
    return [(t, share * a / total) for t, a in zip(tiles, areas)]
