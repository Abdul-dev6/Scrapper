"""Overture Maps Places: release discovery, DuckDB GeoParquet queries and a per-release tile cache.

Data access (official): GeoParquet on the public S3 bucket (anonymous), read with DuckDB.
Only the parts of the files whose bbox statistics overlap the query are fetched; for Great Britain
a full scan of the gym categories took ~139 s with memory_limit=1GB (measured 2026-11-15).

Cache: the world is cut into 1°x1° tiles. For each (release, tile) the gym places are stored ONCE as
an immutable, checksummed gzip JSONL file on the persistent volume (OVERTURE_CACHE_DIR), with
metadata in MongoDB `overtureTiles`. Releases are immutable, so a cached tile never goes stale:
a NEW release simply means new tiles (repeat searches then see the new data). Leases, reader pins,
validation, crash recovery and LRU eviction work like the OSM extract cache (osm/extracts.py).
"""

from __future__ import annotations

import math
import os
import re
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from pymongo import ReturnDocument
from pymongo.database import Database
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from ..osm.extracts import (
    MB, DiskSpaceError, ExtractCorrupt, ExtractError, ExtractTooLarge, ExtractUnavailable, _aware, _fsync_dir,
    disk_free, read_elements, utcnow, validate_filtered, write_filtered,
)

# ---------------------------------------------------------------- categories (verified IDs only)

# Overture taxonomy ids -> our category ids. Every id below was seen in the 2026-10-21.0 release
# (Great Britain scan, 2026-11-15). Deliberately NOT mapped: fitness_trainer (personal trainers),
# sport_or_fitness_facility (generic: pools, courts...), sportswear_store (clothing).
# No MMA- or wrestling-specific id exists in that data, so those categories get nothing from Overture.
TAXONOMY_TO_CATEGORIES: dict[str, tuple[str, ...]] = {
    "gym": ("gym", "fitness_center"),
    "aerial_fitness_center": ("fitness_center",),
    "boxing_gym": ("boxing_club",),
    "boxing_class": ("boxing_club",),
    "martial_arts_club": ("martial_arts_school",),
    "karate_club": ("martial_arts_school",),
    "taekwondo_club": ("martial_arts_school",),
    "kickboxing_club": ("martial_arts_school",),
    "chinese_martial_arts_club": ("martial_arts_school",),
    "muay_thai_club": ("martial_arts_school",),
    "brazilian_jiu_jitsu_club": ("martial_arts_school",),
    "self_defense_class": ("martial_arts_school",),
    "sporting_goods_store": ("sports_store",),
    "fitness_exercise_store": ("sports_store",),
}
TAXONOMY_IDS = tuple(sorted(TAXONOMY_TO_CATEGORIES))


def categories_for(primary: str | None, alternates: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for t in [primary, *(alternates or [])]:
        for c in TAXONOMY_TO_CATEGORIES.get(t or "", ()):
            if c not in out:
                out.append(c)
    return out


# ---------------------------------------------------------------- errors / config


class OvertureUnavailable(ExtractUnavailable):
    """Temporary: STAC catalogue or the GeoParquet files can't be read right now (retried with backoff)."""


@dataclass(frozen=True)
class OvertureConfig:
    cache_dir: Path  # persistent volume, e.g. /data/overture
    tmp_dir: Path  # DuckDB spill files
    source: str = "s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    stac_url: str = "https://stac.overturemaps.org/catalog.json"
    pinned_release: str = ""  # use this release instead of "latest" (empty = latest from STAC)
    release_check_hours: float = 6
    cache_max_bytes: int = 512 * MB
    volume_margin_bytes: int = 256 * MB
    min_confidence: float = 0.5  # Overture: "a relative filtering tool"; 0 disables
    duckdb_memory: str = "1GB"
    duckdb_threads: int = 2
    max_tiles_per_job: int = 400  # Great Britain needs ~70; the whole USA would need thousands
    tile_lease_seconds: int = 30 * 60
    reader_lease_seconds: int = 15 * 60
    orphan_grace_seconds: int = 60 * 60
    filtered_estimate_bytes: int = 16 * MB

    @classmethod
    def from_settings(cls, s) -> "OvertureConfig":
        import tempfile

        tmp = Path(s.osm_tmp_dir) if s.osm_tmp_dir else Path(tempfile.gettempdir()) / "leadfinder-osm"
        return cls(
            cache_dir=Path(s.overture_cache_dir), tmp_dir=tmp / "duckdb", source=s.overture_source,
            stac_url=s.overture_stac_url, pinned_release=s.overture_release, cache_max_bytes=s.overture_cache_max_bytes,
            volume_margin_bytes=s.osm_volume_margin_bytes, min_confidence=s.overture_min_confidence,
            duckdb_memory=s.overture_duckdb_memory, max_tiles_per_job=s.overture_max_tiles_per_job,
        )


# ---------------------------------------------------------------- releases


_RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")


def current_release(db: Database, client: httpx.Client, cfg: OvertureConfig, now: datetime | None = None) -> str:
    """The release to use: pinned, else the STAC catalogue's `latest` (checked every few hours and
    remembered in MongoDB, so an unreachable catalogue doesn't stop searches)."""
    if cfg.pinned_release:
        return cfg.pinned_release
    now = now or utcnow()
    doc = db.overtureReleases.find_one({"_id": "latest"})
    if doc and _aware(doc.get("checkedAt")) and now - _aware(doc["checkedAt"]) < timedelta(hours=cfg.release_check_hours):
        return doc["release"]
    try:
        resp = client.get(cfg.stac_url, follow_redirects=True)
        if resp.status_code != 200:
            raise OvertureUnavailable(f"Overture release catalogue HTTP {resp.status_code}")
        latest = resp.json().get("latest")
        if not isinstance(latest, str) or not _RELEASE_RE.match(latest):
            raise OvertureUnavailable("Overture release catalogue has no valid 'latest' release")
        db.overtureReleases.update_one({"_id": "latest"}, {"$set": {"release": latest, "checkedAt": now}}, upsert=True)
        return latest
    except (httpx.HTTPError, ValueError, OvertureUnavailable) as exc:
        if doc:
            return doc["release"]  # last known release keeps working while the catalogue is down
        raise OvertureUnavailable("Couldn't read the Overture Maps release catalogue. The search will be retried "
                                  "automatically.") from exc


# ---------------------------------------------------------------- tiles


def tile_id(lat: float, lon: float) -> str:
    return f"{math.floor(lat)}_{math.floor(lon)}"


def tile_box(tid: str) -> tuple[float, float, float, float]:
    lat, lon = (int(x) for x in tid.split("_"))
    return float(lat), float(lon), float(lat + 1), float(lon + 1)  # south, west, north, east


def tiles_for(area: BaseGeometry, max_tiles: int) -> list[str]:
    w, s, e, n = area.bounds
    out = []
    for lat in range(math.floor(s), math.floor(n) + 1):
        for lon in range(math.floor(w), math.floor(e) + 1):
            if box(lon, lat, lon + 1, lat + 1).intersects(area):
                out.append(f"{lat}_{lon}")
                if len(out) > max_tiles:
                    raise ExtractTooLarge(f"This area is too large for Overture Maps (more than {max_tiles} map tiles). "
                                          "Search a smaller area (for example a state, region or city).")
    return out


# ---------------------------------------------------------------- DuckDB query


QueryFn = Callable[[str, tuple[float, float, float, float], Callable[[], None]], Iterable[dict]]


def query_duckdb(cfg: OvertureConfig) -> QueryFn:
    """A function (release, (s, w, n, e), heartbeat) -> rows of gym places, streamed in batches."""

    def run(release: str, bbox: tuple[float, float, float, float], heartbeat: Callable[[], None]) -> Iterator[dict]:
        import duckdb

        s, w, n, e = bbox
        cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        try:
            con.execute(f"SET memory_limit='{cfg.duckdb_memory}'")
            con.execute(f"SET threads={int(cfg.duckdb_threads)}")
            con.execute(f"SET temp_directory='{cfg.tmp_dir.as_posix()}'")
            con.execute("SET enable_progress_bar=false")
            ext_dir = os.getenv("DUCKDB_EXTENSION_DIRECTORY", "")
            if ext_dir:  # the Docker image pre-installs httpfs here (independent of the OS user)
                con.execute(f"SET extension_directory='{Path(ext_dir).as_posix()}'")
            src = cfg.source.format(release=release)
            if src.startswith("s3://"):
                con.execute("LOAD httpfs")
                con.execute("SET s3_region='us-west-2'")
            ids = list(TAXONOMY_IDS)
            sql = f"""
                SELECT id, names."primary" AS name, taxonomy."primary" AS cat, taxonomy.alternates AS alts,
                       confidence, operating_status, websites, socials, emails, phones, addresses, sources,
                       bbox.xmin AS lon, bbox.ymin AS lat
                FROM read_parquet(?, hive_partitioning=1)
                WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
                  AND (taxonomy."primary" IN ({",".join("?" * len(ids))}) OR list_has_any(taxonomy.alternates, ?::VARCHAR[]))
                  AND coalesce(confidence, 1.0) >= ?
                  AND coalesce(operating_status, '') <> 'permanently_closed'
            """
            cur = con.execute(sql, [src, w, e, s, n, *ids, ids, cfg.min_confidence])
            cols = [d[0] for d in cur.description]
            while True:
                heartbeat()
                batch = cur.fetchmany(5000)
                if not batch:
                    break
                for row in batch:
                    yield dict(zip(cols, row))
        except duckdb.IOException as exc:
            raise OvertureUnavailable(f"Couldn't read Overture Maps data for release {release} ({exc}).") from exc
        except duckdb.Error as exc:
            raise ExtractCorrupt(f"The Overture Maps data couldn't be queried ({type(exc).__name__}).") from exc
        finally:
            con.close()

    return run


def _row_element(row: dict) -> dict:
    """JSON-safe copy of a query row (what the tile file stores; mapping happens per job)."""
    def clean(v):
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [clean(x) for x in v]
        return v

    return {k: clean(v) for k, v in row.items()}


# ---------------------------------------------------------------- tile cache


@dataclass(frozen=True)
class TilePin:
    tile: str
    release: str
    file: str
    sha256: str
    features: int
    lease: str

    def as_checkpoint(self) -> dict:
        return {"tile": self.tile, "release": self.release, "file": self.file, "sha256": self.sha256,
                "features": self.features}


@dataclass
class TileCache:
    db: Database
    cfg: OvertureConfig
    owner: str
    query_fn: QueryFn | None = None
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], None] = time.sleep
    coll: object = field(init=False)

    def __post_init__(self):
        self.coll = self.db.overtureTiles
        self.query_fn = self.query_fn or query_duckdb(self.cfg)
        (self.cfg.cache_dir / "files").mkdir(parents=True, exist_ok=True)

    def abs(self, rel: str) -> Path:
        return self.cfg.cache_dir / rel

    @staticmethod
    def doc_id(release: str, tile: str) -> str:
        return f"{release}|{tile}"

    def _file_ok(self, doc: dict | None) -> bool:
        if not doc or not doc.get("file"):
            return False
        p = self.abs(doc["file"])
        return p.exists() and p.stat().st_size == doc.get("bytes")

    # -------- leases + pins

    def _acquire(self, release: str, tile: str) -> bool:
        now = self.clock()
        _id = self.doc_id(release, tile)
        self.coll.update_one({"_id": _id}, {"$setOnInsert": {"release": release, "tile": tile, "status": "missing",
                                                             "readers": [], "lease": None, "createdAt": now}},
                             upsert=True)
        return self.coll.find_one_and_update(
            {"_id": _id, "status": {"$ne": "ready"}, "$or": [{"lease": None}, {"lease.until": {"$lt": now}}]},
            {"$set": {"lease": {"owner": self.owner, "until": now + timedelta(seconds=self.cfg.tile_lease_seconds)}}},
            return_document=ReturnDocument.AFTER) is not None

    def _renew(self, ids: Iterable[str]) -> None:
        until = self.clock() + timedelta(seconds=self.cfg.tile_lease_seconds)
        for _id in ids:
            self.coll.update_one({"_id": _id, "lease.owner": self.owner}, {"$set": {"lease.until": until}})

    def _release(self, ids: Iterable[str]) -> None:
        for _id in ids:
            self.coll.update_one({"_id": _id, "lease.owner": self.owner}, {"$set": {"lease": None}})

    def pin(self, release: str, tile: str, *, job_id=None) -> TilePin | None:
        _id = self.doc_id(release, tile)
        doc = self.coll.find_one({"_id": _id})
        if not (doc and doc.get("status") == "ready" and self._file_ok(doc)):
            return None
        lease = uuid.uuid4().hex
        res = self.coll.update_one(
            {"_id": _id, "file": doc["file"]},
            {"$push": {"readers": {"lease": lease, "owner": self.owner, "jobId": job_id, "file": doc["file"],
                                   "until": self.clock() + timedelta(seconds=self.cfg.reader_lease_seconds)}},
             "$set": {"lastAccessAt": self.clock()}})
        pin = TilePin(tile, release, doc["file"], doc["sha256"], doc.get("features", 0), lease)
        if res.matched_count == 0:
            return None
        if not self.abs(doc["file"]).exists():
            self.unpin(pin)
            return None
        return pin

    def pin_saved(self, info: dict, *, job_id=None) -> TilePin | None:
        """Re-pin a tile a job used before (resume); None if that exact file is gone."""
        from ..osm.extracts import sha256_file

        p = self.abs(info["file"])
        if not p.exists() or sha256_file(p) != info["sha256"]:
            return None
        return self.pin(info["release"], info["tile"], job_id=job_id)

    def renew(self, pins: Iterable[TilePin]) -> None:
        until = self.clock() + timedelta(seconds=self.cfg.reader_lease_seconds)
        for p in pins:
            self.coll.update_one({"_id": self.doc_id(p.release, p.tile), "readers.lease": p.lease},
                                 {"$set": {"readers.$.until": until}})

    def unpin(self, pin: TilePin) -> None:
        self.coll.update_one({"_id": self.doc_id(pin.release, pin.tile)}, {"$pull": {"readers": {"lease": pin.lease}}})

    # -------- ensure

    def ensure(self, release: str, tiles: list[str], *, job_id=None, heartbeat: Callable[[], None] = lambda: None,
               on_stage: Callable[[str], None] = lambda s: None) -> list[TilePin]:
        """Pinned files for every tile of the area in this release. Missing tiles are built with ONE
        DuckDB query over their combined box; tiles another worker is building are waited for."""
        pins: dict[str, TilePin] = {}
        try:
            while True:
                for t in tiles:
                    if t not in pins:
                        p = self.pin(release, t, job_id=job_id)
                        if p:
                            pins[t] = p
                missing = [t for t in tiles if t not in pins]
                if not missing:
                    return [pins[t] for t in tiles]
                mine = [t for t in missing if self._acquire(release, t)]
                if mine:
                    try:
                        self._build(release, mine, heartbeat=heartbeat, on_stage=on_stage)
                    finally:
                        self._release([self.doc_id(release, t) for t in mine])
                else:
                    on_stage("Waiting for Overture Maps data (being prepared by another search)")
                    self.sleep(5)
                    heartbeat()
                    errs = [d for d in self.coll.find({"_id": {"$in": [self.doc_id(release, t) for t in missing]},
                                                       "lease": None, "lastError": {"$ne": None}})]
                    recent = [d for d in errs if self.clock() - _aware(d["lastError"]["at"]) < timedelta(seconds=30)]
                    if recent:
                        raise OvertureUnavailable(recent[0]["lastError"]["message"])
        except BaseException:
            for p in pins.values():
                self.unpin(p)
            raise

    def _build(self, release: str, tiles: list[str], *, heartbeat, on_stage) -> None:
        ids = [self.doc_id(release, t) for t in tiles]
        boxes = [tile_box(t) for t in tiles]
        bbox = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        on_stage(f"Downloading Overture Maps places (release {release}, {len(tiles)} map tile"
                 f"{'s' if len(tiles) != 1 else ''})")
        self.make_room(self.cfg.filtered_estimate_bytes * max(1, len(tiles) // 10 + 1))
        last = time.monotonic()

        def beat():
            nonlocal last
            heartbeat()
            if time.monotonic() - last > 30:
                self._renew(ids)
                last = time.monotonic()

        wanted = set(tiles)
        by_tile: dict[str, list[dict]] = defaultdict(list)
        try:
            for row in self.query_fn(release, bbox, beat):
                if row.get("lat") is None or row.get("lon") is None or not (row.get("name") or "").strip():
                    continue
                t = tile_id(row["lat"], row["lon"])
                if t in wanted:  # the combined box may touch tiles another search is building
                    by_tile[t].append(_row_element(row))
        except BaseException as exc:
            if isinstance(exc, ExtractError):
                for _id in ids:
                    self.coll.update_one({"_id": _id}, {"$set": {"lastError": {
                        "at": self.clock(), "message": str(exc)[:300], "type": type(exc).__name__}}})
            raise
        on_stage("Preparing Overture Maps gym data")
        for t in tiles:  # even an empty tile is stored: "this tile has no gyms" is also a fact
            self._publish(release, t, sorted(by_tile.get(t, []), key=lambda r: r["id"]))

    def _publish(self, release: str, tile: str, rows: list[dict]) -> None:
        folder = self.cfg.cache_dir / "files" / re.sub(r"[^A-Za-z0-9._-]", "_", release)
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / f".{tile}.{uuid.uuid4().hex}.jsonl.gz.part"
        try:
            count, sha, size = write_filtered(rows, part)
            validate_filtered(part, count=count, sha256=sha)
            rel = f"files/{folder.name}/{tile}-{sha[:12]}.jsonl.gz"
            os.replace(part, self.abs(rel))
            _fsync_dir(folder)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        res = self.coll.update_one(
            {"_id": self.doc_id(release, tile), "lease.owner": self.owner},
            {"$set": {"status": "ready", "file": rel, "sha256": sha, "bytes": size, "features": count,
                      "builtAt": self.clock(), "lastAccessAt": self.clock(), "lastError": None,
                      "license": "CDLA-Permissive-2.0 / Apache-2.0 (per source, see sources[].license)"}})
        if res.matched_count == 0:
            self.abs(rel).unlink(missing_ok=True)
            raise OvertureUnavailable("Lost the tile lease before publishing; nothing was changed.")

    # -------- space

    def used_bytes(self) -> int:
        root = self.cfg.cache_dir / "files"
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())

    def make_room(self, need: int) -> None:
        def fits() -> bool:
            return (self.used_bytes() + need <= self.cfg.cache_max_bytes and
                    disk_free(self.cfg.cache_dir) - need >= self.cfg.volume_margin_bytes)

        if fits():
            return
        now = self.clock()
        current = self.db.overtureReleases.find_one({"_id": "latest"}) or {}
        docs = list(self.coll.find({"status": "ready"}))
        # Older releases first, then least recently used; never a pinned or leased tile.
        docs.sort(key=lambda d: (d.get("release") == current.get("release"),
                                 _aware(d.get("lastAccessAt")) or datetime.min.replace(tzinfo=now.tzinfo)))
        for d in docs:
            res = self.coll.update_one(
                {"_id": d["_id"], "file": d["file"], "lease": None,
                 "readers": {"$not": {"$elemMatch": {"until": {"$gt": now}}}}},
                {"$set": {"status": "evicted", "file": None}})
            if res.matched_count:
                self.abs(d["file"]).unlink(missing_ok=True)
                if fits():
                    return
        raise DiskSpaceError(f"Not enough space for Overture Maps data (budget {self.cfg.cache_max_bytes / MB:.0f} MB). "
                             "Increase OVERTURE_CACHE_MAX_BYTES or the volume size.")

    def recover(self) -> dict:
        stats = {"partsRemoved": 0, "orphansRemoved": 0, "pointersCleared": 0}
        now = time.time()
        referenced = {d["file"] for d in self.coll.find({"file": {"$ne": None}}, {"file": 1}) if d.get("file")}
        for p in (self.cfg.cache_dir / "files").rglob("*"):
            if not p.is_file() or now - p.stat().st_mtime <= self.cfg.orphan_grace_seconds:
                continue
            rel = str(p.relative_to(self.cfg.cache_dir)).replace("\\", "/")
            if p.name.endswith(".part"):
                p.unlink(missing_ok=True)
                stats["partsRemoved"] += 1
            elif rel not in referenced:
                p.unlink(missing_ok=True)
                stats["orphansRemoved"] += 1
        for d in self.coll.find({"status": "ready"}):
            if not self._file_ok(d):
                self.coll.update_one({"_id": d["_id"], "file": d.get("file")}, {"$set": {"status": "missing", "file": None}})
                stats["pointersCleared"] += 1
        self.coll.update_many({}, {"$pull": {"readers": {"until": {"$lt": self.clock()}}}})
        return stats


def read_rows(cache: TileCache, pin: TilePin, start: int = 0) -> Iterator[tuple[int, dict]]:
    return read_elements(cache.abs(pin.file), start)
