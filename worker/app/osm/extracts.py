"""OpenStreetMap discovery from Geofabrik extracts (Phase 2b). Replaces public Overpass completely.

Flow for one job:
    area (Phase 2a) -> GeofabrikIndex.select() -> ExtractCache.ensure() -> pinned, immutable gym files
    -> read_elements() streams Overpass-compatible elements -> existing area filter / mapping / upsert

Storage (decisions 2026-11-15):
  - Original .osm.pbf downloads go to OSM_TMP_DIR (container disk) and are deleted after filtering.
  - Filtered gym files (gzip JSON Lines) live on the persistent volume OSM_CACHE_DIR/files/<region>/.
    Files are immutable: a new version is a new file; publishing switches the MongoDB pointer.
  - MongoDB `osmExtracts` holds metadata only: source version, checksums, paths, leases, status.

Freshness: < OSM_REFRESH_DAYS old -> use; older -> use it now AND request a background refresh;
missing -> download + filter before the job continues. A failed refresh never touches the last valid file.

Concurrency (works across processes, not only threads):
  - refreshLease {owner, until}: taken with one atomic update, renewed while working, released only
    by its owner; an expired lease (crashed worker) can be taken over.
  - readers[] {lease, owner, jobId, file, until}: a job "pins" the exact file it reads and renews the
    pin; eviction never deletes a file with a live pin, a running refresh, or the last valid version.
Crash safety: file written to *.part -> fsync -> validated -> renamed to its final name -> only then
the MongoDB pointer moves. A crash in between leaves an unreferenced file, removed by recover().
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from pymongo import ReturnDocument
from pymongo.database import Database
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .tags import is_wanted, selector_keys

GB = 1024**3
MB = 1024**2


# ---------------------------------------------------------------- errors


class ExtractError(RuntimeError):
    """Permanent problem; the message is safe to show to the user."""


class ExtractUnavailable(ExtractError):
    """Temporary: Geofabrik unreachable / busy / download broken. The job is retried with backoff."""


class ExtractTooLarge(ExtractError):
    """The area needs more map data than the configured limits allow."""


class DiskSpaceError(ExtractError):
    """Not enough room on the temporary disk or the cache volume, even after eviction."""


class ExtractCorrupt(ExtractUnavailable):
    """A download or filtered file failed validation (checksum, size, unreadable PBF)."""


class LeaseLost(ExtractUnavailable):
    """Another worker took over the refresh lease (ours expired); stop without publishing."""


# ---------------------------------------------------------------- configuration


@dataclass(frozen=True)
class ExtractConfig:
    cache_dir: Path  # persistent volume, e.g. /data/osm
    tmp_dir: Path  # container disk for the original downloads
    cache_max_bytes: int = 4 * GB  # budget for filtered files (Railway Hobby volume: 5 GB)
    volume_margin_bytes: int = 256 * MB  # always keep this much free on the volume
    tmp_margin_bytes: int = 1 * GB  # and on the temporary disk
    refresh_days: float = 7
    index_url: str = "https://download.geofabrik.de/index-v1.json"
    index_refresh_days: float = 7
    max_download_bytes: int = 6 * GB  # one extract (the full US is ~11 GB -> its states are used)
    max_job_download_bytes: int = 8 * GB  # all extracts one job may download
    max_extracts_per_job: int = 12
    refresh_lease_seconds: int = 30 * 60
    reader_lease_seconds: int = 15 * 60
    orphan_grace_seconds: int = 60 * 60
    filtered_estimate_bytes: int = 64 * MB  # room reserved for a new filtered file before it's written
    user_agent: str = "LeadFinderBot"

    @classmethod
    def from_settings(cls, s) -> "ExtractConfig":
        import tempfile

        tmp = Path(s.osm_tmp_dir) if s.osm_tmp_dir else Path(tempfile.gettempdir()) / "leadfinder-osm"
        return cls(
            cache_dir=Path(s.osm_cache_dir), tmp_dir=tmp, cache_max_bytes=s.osm_cache_max_bytes,
            volume_margin_bytes=s.osm_volume_margin_bytes, tmp_margin_bytes=s.osm_tmp_margin_bytes,
            refresh_days=s.osm_refresh_days, index_url=s.geofabrik_index_url,
            max_download_bytes=s.osm_max_download_bytes, max_job_download_bytes=s.osm_max_job_download_bytes,
            user_agent=f"LeadFinderBot/0.3 (+{s.crawler_contact_url})",
        )


def make_http_client(cfg: ExtractConfig, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport, headers={"User-Agent": cfg.user_agent}, follow_redirects=False,
        timeout=httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0),
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def disk_free(path: Path) -> int:
    """Free bytes where `path` lives (the real runtime figure; never assumed). Patched in tests."""
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def _fsync_file(path: Path) -> None:
    with open(path, "r+b") as f:  # read-write: Windows refuses fsync on a read-only handle
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # directories can't be fsynced on Windows; os.replace is still atomic there
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(MB), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- Geofabrik index + region choice


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    parent: str | None
    pbf_url: str
    updates_url: str | None
    geometry: BaseGeometry = field(compare=False, repr=False)

    @property
    def cost(self) -> float:
        return self.geometry.area


class GeofabrikIndex:
    """The official index (all regions, their download URLs and outlines), cached on the volume."""

    def __init__(self, regions: list[Region]):
        self.regions = regions
        self.by_id = {r.id: r for r in regions}

    @classmethod
    def parse(cls, data: dict) -> "GeofabrikIndex":
        regions = []
        for f in data.get("features", []):
            p = f.get("properties") or {}
            urls = p.get("urls") or {}
            if not f.get("geometry") or not urls.get("pbf") or not p.get("id"):
                continue
            regions.append(Region(p["id"], p.get("name", p["id"]), p.get("parent"), urls["pbf"], urls.get("updates"),
                                  shape(f["geometry"]).buffer(0)))
        if not regions:
            raise ExtractUnavailable("The Geofabrik index has no usable regions.")
        return cls(regions)

    @classmethod
    def load(cls, client: httpx.Client, cfg: ExtractConfig, now: datetime | None = None) -> "GeofabrikIndex":
        """Use the cached index if younger than index_refresh_days; else fetch it (atomic replace).
        If fetching fails, an older cached copy is still used."""
        now = now or utcnow()
        path = cfg.cache_dir / "index" / "index-v1.json"
        fresh = path.exists() and (now.timestamp() - path.stat().st_mtime) < cfg.index_refresh_days * 86400
        if not fresh:
            try:
                resp = client.get(cfg.index_url, follow_redirects=True)
                if resp.status_code != 200:
                    raise ExtractUnavailable(f"Geofabrik index HTTP {resp.status_code}")
                data = resp.json()
                cls.parse(data)  # validate before replacing the cached copy
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(f".{uuid.uuid4().hex}.part")
                tmp.write_bytes(resp.content)
                os.replace(tmp, path)
                return cls.parse(data)
            except (httpx.HTTPError, ValueError, ExtractUnavailable) as exc:
                if not path.exists():
                    raise ExtractUnavailable("Couldn't download the OpenStreetMap extract list from Geofabrik. "
                                             "The search will be retried automatically.") from exc
        return cls.parse(json.loads(path.read_text(encoding="utf-8")))

    COVER = 0.995  # a region "covers" an area if it holds 99.5% of it
    SLIVER = 0.005  # border slivers (Natural Earth vs Geofabrik outlines) smaller than 0.5% are ignored

    def candidates(self, area: BaseGeometry) -> list[Region]:
        return [r for r in self.regions if r.geometry.intersects(area)]

    def single_cover(self, area: BaseGeometry) -> Region | None:
        """Plan A: the smallest single region holding (almost) the whole area."""
        covering = [r for r in self.candidates(area) if r.geometry.intersection(area).area >= area.area * self.COVER]
        return min(covering, key=lambda r: r.cost) if covering else None

    def greedy_cover(self, area: BaseGeometry, *, cached: Iterable[str] = (), max_extracts: int = 12) -> list[Region]:
        """Plan B: greedy set cover. Cost = region size + a per-file overhead (~0 if already cached);
        tight regions win (London -> Greater London + neighbouring counties, not all of England)."""
        cached = set(cached)
        overhead = max(area.area * 0.02, 0.05)  # degrees²
        cands = self.candidates(area)
        remaining = area
        chosen: list[Region] = []
        while remaining.area > area.area * self.SLIVER:
            if len(chosen) >= max_extracts:
                raise ExtractTooLarge(f"This area needs more than {max_extracts} OpenStreetMap extracts. "
                                      "Search a smaller area (for example a state or a city).")
            best, best_ratio = None, None
            for r in cands:
                if r in chosen:
                    continue
                gain = remaining.intersection(r.geometry).area
                if gain < area.area * self.SLIVER:
                    continue  # only a border sliver: not worth a download
                cost = 1e-9 if r.id in cached else r.cost + overhead
                if best_ratio is None or cost / gain < best_ratio:
                    best, best_ratio = r, cost / gain
            if best is None:
                break
            chosen.append(best)
            remaining = remaining.difference(best.geometry)
        if remaining.area > area.area * 0.02:
            raise ExtractError("No OpenStreetMap extract covers this whole area. Try a smaller area.")
        return chosen

    def plan(self, area: BaseGeometry, *, cached: Iterable[str] = (), size_of: Callable[["Region"], int | None],
             max_download_bytes: int, max_job_download_bytes: int, max_extracts: int = 12) -> list[Region]:
        """Choose between plan A (one covering file) and plan B (several smaller files) by the ACTUAL
        bytes to download (HEAD Content-Length; cached regions cost 0), then fewer files.
        Raises ExtractTooLarge if no plan fits the per-file and per-job limits."""
        if area.is_empty:
            return []
        cached = set(cached)
        plans: list[list[Region]] = []
        a = self.single_cover(area)
        if a is not None:
            plans.append([a])
        try:
            b = self.greedy_cover(area, cached=cached, max_extracts=max_extracts)
            if b and b not in plans:
                plans.append(b)
        except ExtractTooLarge:
            if not plans:
                raise
        if not plans:
            raise ExtractError("No OpenStreetMap extract covers this whole area. Try a smaller area.")
        sizes: dict[str, int] = {}

        def bytes_of(r: Region) -> int:
            if r.id in cached:
                return 0
            if r.id not in sizes:
                sizes[r.id] = size_of(r) or 0
            return sizes[r.id]

        best, best_key, reason = None, None, ""
        for p in plans:
            per_file = [bytes_of(r) for r in p]
            total = sum(per_file)
            if any(n > max_download_bytes for n in per_file):
                reason = (f"one OpenStreetMap extract is {max(per_file) / GB:.1f} GB, over the "
                          f"{max_download_bytes / GB:.1f} GB limit")
                continue
            if total > max_job_download_bytes:
                reason = f"it needs {total / GB:.1f} GB of downloads, over the {max_job_download_bytes / GB:.1f} GB limit"
                continue
            key = (total, len(p))
            if best_key is None or key < best_key:
                best, best_key = p, key
        if best is None:
            raise ExtractTooLarge(f"This area is too large to search: {reason}. Search a smaller area "
                                  "(for example a state, region or city).")
        return best


# ---------------------------------------------------------------- download


@dataclass
class SourceInfo:
    region_id: str
    url: str  # dated, immutable file, e.g. .../great-britain-261113.osm.pbf
    version: str  # "great-britain-261113" (falls back to the ETag when the URL isn't dated)
    etag: str | None
    length: int | None
    md5: str | None  # only if Geofabrik publishes <file>.md5 (it does today; never assumed)
    accept_ranges: bool


_MD5_RE = re.compile(r"^([0-9a-f]{32})\s")


def resolve_source(client: httpx.Client, region: Region) -> SourceInfo:
    """`-latest.osm.pbf` redirects to the dated file; HEAD that for size / ETag / Range support."""
    try:
        resp = client.head(region.pbf_url)
        url = region.pbf_url
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
            url = str(httpx.URL(region.pbf_url).join(resp.headers["location"]))
            resp = client.head(url)
        if resp.status_code == 404:
            raise ExtractError(f"Geofabrik has no extract for {region.name} right now (HTTP 404).")
        if resp.status_code != 200:
            raise ExtractUnavailable(f"Geofabrik answered HTTP {resp.status_code} for {region.name}.")
        length = int(resp.headers["content-length"]) if resp.headers.get("content-length", "").isdigit() else None
        etag = resp.headers.get("etag")
        name = url.rsplit("/", 1)[-1]
        version = name.removesuffix(".osm.pbf") if name != region.pbf_url.rsplit("/", 1)[-1] else (etag or name)
        md5 = None
        m = client.get(url + ".md5")
        if m.status_code == 200:
            match = _MD5_RE.match(m.text.strip() + " ")
            md5 = match.group(1) if match else None
        elif m.status_code >= 500:
            raise ExtractUnavailable(f"Geofabrik checksum file HTTP {m.status_code}.")
        return SourceInfo(region.id, url, version, etag, length,
                          md5, resp.headers.get("accept-ranges", "").lower() == "bytes")
    except httpx.HTTPError as exc:
        raise ExtractUnavailable(f"Couldn't reach Geofabrik for {region.name}.") from exc


def download(client: httpx.Client, src: SourceInfo, cfg: ExtractConfig, *, heartbeat: Callable[[], None],
             on_progress: Callable[[int, int | None], None] = lambda done, total: None) -> Path:
    """Stream the PBF to the temporary disk. Resumes a partial file only when the server supports Range
    AND the ETag is unchanged (If-Range); otherwise starts again. Verifies length and MD5 (if published).
    Never leaves a file that looks complete but isn't."""
    tmp_dir = cfg.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", src.version)
    final = tmp_dir / f"{safe}.osm.pbf"
    part = tmp_dir / f"{safe}.osm.pbf.part"
    meta = tmp_dir / f"{safe}.osm.pbf.part.json"
    if final.exists():
        final.unlink()  # never trust a leftover "complete" file; it is cheap to re-verify via the part path

    have = part.stat().st_size if part.exists() else 0
    if have:
        info = json.loads(meta.read_text()) if meta.exists() else {}
        if not (src.accept_ranges and src.etag and info.get("etag") == src.etag and info.get("url") == src.url
                and (src.length is None or have < src.length)):
            part.unlink()
            have = 0
    need = (src.length or 0) - have
    if disk_free(tmp_dir) < need + cfg.tmp_margin_bytes:
        raise DiskSpaceError(f"Not enough temporary disk space to download the map data "
                             f"({(need + cfg.tmp_margin_bytes) / GB:.1f} GB needed, {disk_free(tmp_dir) / GB:.1f} GB free).")
    meta.write_text(json.dumps({"url": src.url, "etag": src.etag}))

    headers = {}
    if have:
        headers = {"Range": f"bytes={have}-", "If-Range": src.etag or ""}
    last_beat = time.monotonic()
    try:
        with client.stream("GET", src.url, headers=headers) as resp:
            if have and resp.status_code == 200:  # server ignored the range (or file changed): start again
                part.unlink(missing_ok=True)
                have = 0
            elif resp.status_code not in (200, 206):
                raise (ExtractError if resp.status_code == 404 else ExtractUnavailable)(
                    f"Geofabrik download failed (HTTP {resp.status_code}).")
            with open(part, "ab" if have else "wb") as f:
                done = have
                for chunk in resp.iter_bytes():  # unbuffered: every received byte lands on disk (resumable)
                    f.write(chunk)
                    done += len(chunk)
                    if src.length is not None and done > src.length:
                        raise ExtractCorrupt("The download is larger than Geofabrik said; discarding it.")
                    if time.monotonic() - last_beat > 2:
                        heartbeat()  # also notices Cancel; a partial file is kept for a safe resume
                        on_progress(done, src.length)
                        last_beat = time.monotonic()
                f.flush()
                os.fsync(f.fileno())
    except httpx.HTTPError as exc:
        raise ExtractUnavailable("The map data download was interrupted. It will resume or restart automatically.") from exc

    size = part.stat().st_size
    if src.length is not None and size != src.length:
        part.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        raise ExtractCorrupt(f"Incomplete download ({size} of {src.length} bytes); discarded.")
    if src.md5:
        h = hashlib.md5()  # noqa: S324  (integrity check against Geofabrik's published MD5, not security)
        with open(part, "rb") as f:
            for chunk in iter(lambda: f.read(MB), b""):
                h.update(chunk)
        if h.hexdigest() != src.md5:
            part.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            raise ExtractCorrupt("The downloaded map data doesn't match Geofabrik's checksum; discarded.")
    os.replace(part, final)
    meta.unlink(missing_ok=True)
    return final


# ---------------------------------------------------------------- PBF -> gym elements (pyosmium)


def filter_pbf_osmium(pbf: Path, heartbeat: Callable[[], None]) -> Iterator[dict]:
    """Named gyms from a PBF, in Overpass "out center tags" format, using little memory:

      pass 1: wanted nodes (with their location), wanted ways (node refs), wanted relations (members)
      pass 2: only if relations: the node refs of their member ways
      pass 3: the locations of exactly the nodes needed for the ways/relations
    Centre = middle of the bounding box, exactly like Overpass's `out center`.
    Raises ExtractCorrupt if the file can't be read as a PBF.
    """
    import osmium  # imported here: the worker only needs it when an extract is built

    keys = sorted(selector_keys())
    ways: dict[int, tuple[dict, list[int]]] = {}
    relations: dict[int, tuple[dict, list[int]]] = {}
    beat_every = 200_000
    try:
        n = 0
        fp = osmium.FileProcessor(str(pbf)).with_filter(osmium.filter.KeyFilter(*keys))
        for obj in fp:
            n += 1
            if n % beat_every == 0:
                heartbeat()
            tags = {t.k: t.v for t in obj.tags}
            if not is_wanted(tags):
                continue
            if obj.is_node():
                if obj.location.valid():
                    yield {"type": "node", "id": obj.id, "lat": obj.location.lat, "lon": obj.location.lon, "tags": tags}
            elif obj.is_way():
                ways[obj.id] = (tags, [nd.ref for nd in obj.nodes])
            elif obj.is_relation():
                relations[obj.id] = (tags, [m.ref for m in obj.members if m.type == "w"])

        member_ways: dict[int, list[int]] = {}
        wanted_member_ids = {w for _, refs in relations.values() for w in refs} - set(ways)
        if wanted_member_ids:
            # IdFilter runs inside libosmium (C++): only the wanted ways ever reach Python.
            fp = osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(osmium.filter.IdFilter(wanted_member_ids))
            for i, w in enumerate(fp):
                if i % 1000 == 0:
                    heartbeat()
                member_ways[w.id] = [nd.ref for nd in w.nodes]
        heartbeat()

        need = {ref for _, refs in ways.values() for ref in refs}
        need.update(ref for refs in member_ways.values() for ref in refs)
        loc: dict[int, tuple[float, float]] = {}
        if need:
            fp = osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(need))
            for i, nd in enumerate(fp):
                if i % 10_000 == 0:
                    heartbeat()
                if nd.location.valid():
                    loc[nd.id] = (nd.location.lat, nd.location.lon)
        heartbeat()
    except RuntimeError as exc:  # libosmium: "PBF error", "invalid BlobHeader", truncated file...
        raise ExtractCorrupt(f"The map data file can't be read ({exc}).") from exc

    def centre(refs: Iterable[int]) -> dict | None:
        pts = [loc[r] for r in refs if r in loc]
        if not pts:
            return None
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        return {"lat": (min(lats) + max(lats)) / 2, "lon": (min(lons) + max(lons)) / 2}

    for wid, (tags, refs) in ways.items():
        c = centre(refs)
        if c:
            yield {"type": "way", "id": wid, "center": c, "tags": tags}
    for rid, (tags, member_ids) in relations.items():
        refs = [r for w in member_ids for r in (ways[w][1] if w in ways else member_ways.get(w, []))]
        c = centre(refs)
        if c:
            yield {"type": "relation", "id": rid, "center": c, "tags": tags}


FilterFn = Callable[[Path, Callable[[], None]], Iterable[dict]]


def read_elements(path: Path, start: int = 0) -> Iterator[tuple[int, dict]]:
    """(line number, element) from a filtered gym file, skipping the first `start` elements."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= start:
                yield i, json.loads(line)


def write_filtered(elements: Iterable[dict], part: Path) -> tuple[int, str, int]:
    """Write gzip JSONL, then fsync. Returns (count, sha256 of the file, bytes)."""
    count = 0
    with gzip.open(part, "wt", encoding="utf-8", compresslevel=6) as f:
        for el in elements:
            f.write(json.dumps(el, separators=(",", ":"), ensure_ascii=False) + "\n")
            count += 1
    _fsync_file(part)
    return count, sha256_file(part), part.stat().st_size


def validate_filtered(path: Path, *, count: int, sha256: str) -> None:
    """A filtered file is only published (or used) if it reads back completely and matches."""
    if sha256_file(path) != sha256:
        raise ExtractCorrupt("Cached map data failed its checksum.")
    try:
        n = sum(1 for _ in read_elements(path))
    except (OSError, EOFError, ValueError) as exc:
        raise ExtractCorrupt("Cached map data can't be read.") from exc
    if n != count:
        raise ExtractCorrupt("Cached map data is incomplete.")


# ---------------------------------------------------------------- cache + leases


@dataclass(frozen=True)
class Pinned:
    """One extract version a job reads from start to end (never mixed with another version)."""
    region_id: str
    name: str
    version: str
    sha256: str
    file: str  # path relative to cache_dir
    features: int
    osm_timestamp: str | None
    lease: str

    def as_checkpoint(self) -> dict:
        return {"regionId": self.region_id, "name": self.name, "version": self.version, "sha256": self.sha256,
                "file": self.file, "features": self.features, "osmTimestamp": self.osm_timestamp}


def _safe_region(region_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "__", region_id)


class ExtractCache:
    def __init__(self, db: Database, cfg: ExtractConfig, *, owner: str, client: httpx.Client,
                 filter_fn: FilterFn | None = None, clock: Callable[[], datetime] = utcnow,
                 sleep: Callable[[float], None] = time.sleep):
        self.db, self.coll, self.cfg = db, db.osmExtracts, cfg
        self.owner = owner
        self.client = client
        self.filter_fn = filter_fn or filter_pbf_osmium
        self.clock = clock
        self.sleep = sleep
        (cfg.cache_dir / "files").mkdir(parents=True, exist_ok=True)

    # -------- paths / state

    def abs(self, rel: str) -> Path:
        return self.cfg.cache_dir / rel

    def _file_ok(self, cur: dict | None) -> bool:
        if not cur or not cur.get("file"):
            return False
        p = self.abs(cur["file"])
        return p.exists() and p.stat().st_size == cur.get("bytes")

    def _is_fresh(self, doc: dict) -> bool:
        at = _aware((doc.get("current") or {}).get("refreshedAt"))
        return bool(at) and (self.clock() - at) < timedelta(days=self.cfg.refresh_days)

    def _ensure_doc(self, region: Region) -> None:
        self.coll.update_one(
            {"_id": region.id},
            {"$setOnInsert": {"status": "missing", "readers": [], "createdAt": self.clock()},
             "$set": {"name": region.name, "parent": region.parent, "pbfUrl": region.pbf_url}},
            upsert=True,
        )

    # -------- refresh lease (cross-process)

    def acquire_refresh(self, region_id: str) -> bool:
        now = self.clock()
        doc = self.coll.find_one_and_update(
            {"_id": region_id, "$or": [{"refreshLease": None}, {"refreshLease.until": {"$lt": now}}]},
            {"$set": {"refreshLease": {"owner": self.owner, "until": now + timedelta(seconds=self.cfg.refresh_lease_seconds)}}},
            return_document=ReturnDocument.AFTER,
        )
        return doc is not None

    def renew_refresh(self, region_id: str) -> None:
        res = self.coll.update_one(
            {"_id": region_id, "refreshLease.owner": self.owner},
            {"$set": {"refreshLease.until": self.clock() + timedelta(seconds=self.cfg.refresh_lease_seconds)}},
        )
        if res.matched_count == 0:
            raise LeaseLost("Another worker took over this map data refresh.")

    def release_refresh(self, region_id: str) -> None:
        # Only our own lease: a lease that expired and was taken over by someone else stays theirs.
        self.coll.update_one({"_id": region_id, "refreshLease.owner": self.owner}, {"$set": {"refreshLease": None}})

    # -------- reader pins

    def pin(self, region_id: str, *, job_id=None) -> Pinned | None:
        """Pin the region's current file for reading (None if it has no valid file)."""
        doc = self.coll.find_one({"_id": region_id})
        cur = (doc or {}).get("current")
        if not self._file_ok(cur):
            return None
        lease = uuid.uuid4().hex
        until = self.clock() + timedelta(seconds=self.cfg.reader_lease_seconds)
        res = self.coll.update_one(
            {"_id": region_id, "current.file": cur["file"]},  # still the same version (no race with publish)
            {"$push": {"readers": {"lease": lease, "owner": self.owner, "jobId": job_id, "file": cur["file"], "until": until}},
             "$set": {"lastAccessAt": self.clock()}},
        )
        if res.matched_count == 0:
            return None
        pin = Pinned(region_id, doc.get("name", region_id), cur["version"], cur["sha256"], cur["file"],
                     cur.get("features", 0), cur.get("osmTimestamp"), lease)
        if not self.abs(cur["file"]).exists():  # evicted in the instant before our pin landed
            self.unpin(pin)
            return None
        return pin

    def pin_version(self, info: dict, *, job_id=None) -> Pinned | None:
        """Re-pin the exact version a job used before (resume). None if that file is gone."""
        rel = info["file"]
        p = self.abs(rel)
        if not p.exists() or sha256_file(p) != info["sha256"]:
            return None
        lease = uuid.uuid4().hex
        self.coll.update_one(
            {"_id": info["regionId"]},
            {"$push": {"readers": {"lease": lease, "owner": self.owner, "jobId": job_id, "file": rel,
                                   "until": self.clock() + timedelta(seconds=self.cfg.reader_lease_seconds)}},
             "$set": {"lastAccessAt": self.clock()}},
        )
        # Pinned now; a file is only deleted if no live pin names it, so re-check it survived the race.
        if not p.exists():
            self.unpin(Pinned(info["regionId"], info.get("name", ""), info["version"], info["sha256"], rel, 0, None, lease))
            return None
        return Pinned(info["regionId"], info.get("name", info["regionId"]), info["version"], info["sha256"], rel,
                      info.get("features", 0), info.get("osmTimestamp"), lease)

    def renew(self, pins: Iterable[Pinned]) -> None:
        until = self.clock() + timedelta(seconds=self.cfg.reader_lease_seconds)
        for p in pins:
            self.coll.update_one({"_id": p.region_id, "readers.lease": p.lease}, {"$set": {"readers.$.until": until}})

    def unpin(self, pin: Pinned) -> None:
        self.coll.update_one({"_id": pin.region_id}, {"$pull": {"readers": {"lease": pin.lease}}})

    # -------- ensure (what a job calls)

    def ensure(self, regions: list[Region], *, job_id=None, heartbeat: Callable[[], None] = lambda: None,
               on_stage: Callable[[str], None] = lambda s: None) -> list[Pinned]:
        """A pinned, valid file for every region (downloading missing ones; stale ones are used and
        a background refresh is requested). On any error, pins taken so far are released."""
        pins: list[Pinned] = []
        try:
            self._check_job_download_size(regions, on_stage)
            for region in regions:
                self._ensure_doc(region)
                doc = self.coll.find_one({"_id": region.id})
                if self._file_ok(doc.get("current")):
                    if not self._is_fresh(doc):
                        on_stage(f"Using cached OpenStreetMap data for {region.name} (refreshing it in the background)")
                        self.coll.update_one({"_id": region.id}, {"$set": {"refreshRequestedAt": self.clock()}})
                    else:
                        on_stage(f"Using cached OpenStreetMap data for {region.name}")
                    pin = self.pin(region.id, job_id=job_id)
                    if pin:
                        pins.append(pin)
                        continue
                pins.append(self._wait_or_build(region, job_id=job_id, heartbeat=heartbeat, on_stage=on_stage))
            return pins
        except BaseException:
            for p in pins:
                self.unpin(p)
            raise

    def _check_job_download_size(self, regions: list[Region], on_stage) -> None:
        """Before downloading anything: the extracts this job must download must fit the limits."""
        missing = []
        for r in regions:
            self._ensure_doc(r)
            if not self._file_ok((self.coll.find_one({"_id": r.id}) or {}).get("current")):
                missing.append(r)
        if not missing:
            return
        on_stage("Selecting OpenStreetMap extract")
        sizes = [resolve_source(self.client, r) for r in missing]
        for src, r in zip(sizes, missing):
            if src.length and src.length > self.cfg.max_download_bytes:
                raise ExtractTooLarge(f"The OpenStreetMap extract for {r.name} is {src.length / GB:.1f} GB, over the "
                                      f"{self.cfg.max_download_bytes / GB:.1f} GB limit. Search a smaller area.")
        total = sum(s.length or 0 for s in sizes)
        if total > self.cfg.max_job_download_bytes:
            raise ExtractTooLarge(f"This area needs {total / GB:.1f} GB of OpenStreetMap downloads, over the "
                                  f"{self.cfg.max_job_download_bytes / GB:.1f} GB limit. Search a smaller area.")

    def _wait_or_build(self, region: Region, *, job_id, heartbeat, on_stage) -> Pinned:
        """No valid file: build it, or wait while another worker/job builds it (exactly one download)."""
        while True:
            if self.acquire_refresh(region.id):
                try:
                    self.refresh(region, heartbeat=heartbeat, on_stage=on_stage)
                finally:
                    self.release_refresh(region.id)
            else:
                on_stage(f"Waiting for OpenStreetMap data for {region.name} (being prepared by another search)")
                self.sleep(5)
                heartbeat()
            pin = self.pin(region.id, job_id=job_id)
            if pin:
                return pin
            doc = self.coll.find_one({"_id": region.id}) or {}
            if not doc.get("refreshLease") and doc.get("lastError") and _aware(doc["lastError"].get("at")) and \
                    self.clock() - _aware(doc["lastError"]["at"]) < timedelta(seconds=30):
                raise ExtractUnavailable(doc["lastError"].get("message") or "Preparing the map data failed.")

    # -------- refresh (build a new version; caller holds the lease)

    def refresh(self, region: Region, *, heartbeat: Callable[[], None] = lambda: None,
                on_stage: Callable[[str], None] = lambda s: None) -> bool:
        """Build and publish a new version if Geofabrik has one. Returns True if a new file was published.
        Any failure is recorded in lastError and the previous valid file stays in place."""
        def beat():
            heartbeat()
            self.renew_refresh(region.id)

        pbf = None
        try:
            self.renew_refresh(region.id)
            on_stage(f"Checking OpenStreetMap data for {region.name}")
            src = resolve_source(self.client, region)
            doc = self.coll.find_one({"_id": region.id}) or {}
            cur = doc.get("current")
            if cur and cur.get("version") == src.version and self._file_ok(cur):
                # Geofabrik hasn't published anything newer: the file is still the latest. Mark it fresh.
                self.coll.update_one({"_id": region.id, "refreshLease.owner": self.owner},
                                     {"$set": {"current.refreshedAt": self.clock(), "status": "ready",
                                               "refreshRequestedAt": None, "lastError": None}})
                return False
            if src.length and src.length > self.cfg.max_download_bytes:
                raise ExtractTooLarge(f"The OpenStreetMap extract for {region.name} is {src.length / GB:.1f} GB, "
                                      f"over the {self.cfg.max_download_bytes / GB:.1f} GB limit. Search a smaller area.")
            on_stage(f"Downloading OpenStreetMap data for {region.name}"
                     + (f" ({src.length / GB:.1f} GB)" if src.length else ""))
            pbf = download(self.client, src, self.cfg, heartbeat=beat)
            on_stage(f"Preparing gym data for {region.name}")
            published = self._build_and_publish(region, src, pbf, beat, old=cur)
            return published
        except BaseException as exc:
            if isinstance(exc, ExtractError) or isinstance(exc, (OSError, ValueError)):
                self.coll.update_one({"_id": region.id}, {"$set": {
                    "lastError": {"at": self.clock(), "message": str(exc)[:300], "type": type(exc).__name__}}})
            raise
        finally:
            if pbf is not None:
                Path(pbf).unlink(missing_ok=True)  # the original download is never kept

    def _build_and_publish(self, region: Region, src: SourceInfo, pbf: Path, beat, old: dict | None) -> bool:
        folder = self.cfg.cache_dir / "files" / _safe_region(region.id)
        folder.mkdir(parents=True, exist_ok=True)
        # Room for the new file while the old one still exists (both coexist until publish).
        estimate = max(int((old or {}).get("bytes", 0) * 2), self.cfg.filtered_estimate_bytes)
        self.make_room(estimate, protect_region=region.id)
        part = folder / f".{uuid.uuid4().hex}.jsonl.gz.part"
        try:
            count, sha, size = write_filtered(self.filter_fn(pbf, beat), part)
            validate_filtered(part, count=count, sha256=sha)
            if self.used_bytes() > self.cfg.cache_max_bytes or disk_free(folder) < self.cfg.volume_margin_bytes:
                self.make_room(0, protect_region=region.id)
            rel = f"files/{_safe_region(region.id)}/{re.sub(r'[^A-Za-z0-9._-]', '_', src.version)}-{sha[:12]}.jsonl.gz"
            final = self.abs(rel)
            os.replace(part, final)  # atomic on the same filesystem
            _fsync_dir(folder)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        new = {"version": src.version, "file": rel, "sha256": sha, "bytes": size, "features": count,
               "sourceUrl": src.url, "sourceBytes": src.length, "sourceMd5": src.md5, "sourceEtag": src.etag,
               "osmTimestamp": self._osm_timestamp(region), "createdAt": self.clock(), "refreshedAt": self.clock()}
        res = self.coll.update_one(
            {"_id": region.id, "refreshLease.owner": self.owner},  # only the lease holder may publish
            {"$set": {"current": new, "previous": old, "status": "ready", "refreshRequestedAt": None,
                      "lastError": None, "lastAccessAt": self.clock()}},
        )
        if res.matched_count == 0:
            if not (old and old.get("file") == rel):
                self.abs(rel).unlink(missing_ok=True)
            raise LeaseLost("Lost the refresh lease before publishing; nothing was changed.")
        return True

    def _osm_timestamp(self, region: Region) -> str | None:
        if not region.updates_url:
            return None
        try:
            r = self.client.get(region.updates_url.rstrip("/") + "/state.txt")
            m = re.search(r"^timestamp=(.+)$", r.text, re.M) if r.status_code == 200 else None
            return m.group(1).replace("\\", "") if m else None
        except httpx.HTTPError:
            return None

    # -------- space + eviction

    def used_bytes(self) -> int:
        root = self.cfg.cache_dir / "files"
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())

    def _protected_files(self, protect_region: str | None) -> set[str]:
        now = self.clock()
        files: set[str] = set()
        for d in self.coll.find({}, {"readers": 1, "refreshLease": 1, "current.file": 1, "previous.file": 1}):
            for r in d.get("readers") or []:
                if _aware(r.get("until")) and _aware(r["until"]) > now:
                    files.add(r["file"])
            lease = d.get("refreshLease")
            refreshing = lease and _aware(lease.get("until")) and _aware(lease["until"]) > now
            if refreshing or d["_id"] == protect_region:
                # A region being refreshed keeps its last valid file (and the target region always does).
                for k in ("current", "previous"):
                    if (d.get(k) or {}).get("file"):
                        files.add(d[k]["file"])
        return files

    def make_room(self, need: int, *, protect_region: str | None = None) -> None:
        """Make sure `need` more bytes fit in the budget and on the volume, evicting least-recently-used
        unprotected files. Raises DiskSpaceError if that's impossible."""
        def fits() -> bool:
            return (self.used_bytes() + need <= self.cfg.cache_max_bytes and
                    disk_free(self.cfg.cache_dir) - need >= self.cfg.volume_margin_bytes)

        if fits():
            return
        protected = self._protected_files(protect_region)
        for rel, kind, region_id in self._eviction_order():
            if rel in protected:
                continue
            self._evict(rel, kind, region_id)
            if fits():
                return
        raise DiskSpaceError(
            f"Not enough space for OpenStreetMap data on the cache volume (budget {self.cfg.cache_max_bytes / GB:.1f} GB, "
            f"{disk_free(self.cfg.cache_dir) / MB:.0f} MB free). Increase OSM_CACHE_MAX_BYTES or the volume size.")

    def _eviction_order(self) -> list[tuple[str, str, str | None]]:
        """Orphans first, then old versions, then current files by least recent use."""
        referenced: dict[str, tuple[str, str]] = {}
        currents = []
        for d in self.coll.find({}, {"current": 1, "previous": 1, "lastAccessAt": 1}):
            if (d.get("previous") or {}).get("file"):
                referenced[d["previous"]["file"]] = ("previous", d["_id"])
            if (d.get("current") or {}).get("file"):
                referenced[d["current"]["file"]] = ("current", d["_id"])
                currents.append((_aware(d.get("lastAccessAt")) or datetime.min.replace(tzinfo=timezone.utc),
                                 d["current"]["file"], d["_id"]))
        root = self.cfg.cache_dir / "files"
        now = time.time()
        orphans = [str(p.relative_to(self.cfg.cache_dir)).replace("\\", "/") for p in root.rglob("*")
                   if p.is_file() and not p.name.endswith(".part")
                   and str(p.relative_to(self.cfg.cache_dir)).replace("\\", "/") not in referenced
                   and now - p.stat().st_mtime > self.cfg.orphan_grace_seconds]
        out = [(o, "orphan", None) for o in orphans]
        out += [(f, "previous", rid) for f, (k, rid) in referenced.items() if k == "previous"]
        out += [(f, "current", rid) for _, f, rid in sorted(currents)]
        return out

    def _evict(self, rel: str, kind: str, region_id: str | None) -> None:
        # Metadata first (it must never point at a deleted file), then the file. The update is
        # conditional, in ONE atomic MongoDB operation, on "nobody started reading or refreshing it
        # meanwhile"; if that fails, the file is kept.
        no_live_reader = {"readers": {"$not": {"$elemMatch": {"file": rel, "until": {"$gt": self.clock()}}}}}
        if kind == "previous":
            res = self.coll.update_one({"_id": region_id, "previous.file": rel, **no_live_reader},
                                       {"$set": {"previous": None}})
        elif kind == "current":
            res = self.coll.update_one({"_id": region_id, "current.file": rel, "refreshLease": None, **no_live_reader},
                                       {"$set": {"current": None, "status": "evicted"}})
        else:  # orphan: referenced by no document; skip it if any live pin names it
            res = None
            if self.coll.find_one({"readers": {"$elemMatch": {"file": rel, "until": {"$gt": self.clock()}}}}):
                return
        if res is not None and res.matched_count == 0:
            return
        self.abs(rel).unlink(missing_ok=True)

    # -------- startup recovery + background refresh

    def recover(self) -> dict:
        """After a crash/restart: drop stale partial files, unreferenced files, and pointers to missing files."""
        stats = {"partsRemoved": 0, "orphansRemoved": 0, "pointersCleared": 0, "tmpRemoved": 0}
        now = time.time()
        for p in (self.cfg.cache_dir / "files").rglob("*.part"):
            if now - p.stat().st_mtime > self.cfg.orphan_grace_seconds:
                p.unlink(missing_ok=True)
                stats["partsRemoved"] += 1
        for d in self.coll.find({}):
            for k in ("current", "previous"):
                if d.get(k) and not self._file_ok(d[k]):
                    self.coll.update_one({"_id": d["_id"], f"{k}.file": d[k]["file"]},
                                         {"$set": {k: None, **({"status": "missing"} if k == "current" else {})}})
                    stats["pointersCleared"] += 1
        for rel, kind, _ in self._eviction_order():
            if kind == "orphan":
                self.abs(rel).unlink(missing_ok=True)
                stats["orphansRemoved"] += 1
        if self.cfg.tmp_dir.exists():  # original downloads never outlive a worker process
            for p in self.cfg.tmp_dir.glob("*.osm.pbf*"):
                if now - p.stat().st_mtime > self.cfg.refresh_lease_seconds:
                    p.unlink(missing_ok=True)
                    stats["tmpRemoved"] += 1
        self.coll.update_many({}, {"$pull": {"readers": {"until": {"$lt": self.clock()}}}})
        return stats

    def refresh_requested(self, index: GeofabrikIndex, *, stop=None,
                          heartbeat: Callable[[], None] = lambda: None) -> int:
        """Background work: refresh every region whose refresh was requested (one at a time, with the
        cross-process lease, so two workers never refresh the same region). Returns how many ran."""
        ran = 0
        for d in self.coll.find({"refreshRequestedAt": {"$ne": None}}, {"_id": 1}):
            if stop is not None and stop.is_set():
                break
            region = index.by_id.get(d["_id"])
            if region is None or not self.acquire_refresh(d["_id"]):
                continue
            try:
                self.refresh(region, heartbeat=heartbeat)
                ran += 1
            except RefreshStopped:
                raise
            except ExtractError:
                pass  # recorded in lastError; the previous file is still served
            finally:
                self.release_refresh(d["_id"])
        return ran


# ---------------------------------------------------------------- background refresh thread


def check_writable(path: Path, name: str) -> None:
    """Fail fast at startup with a clear message instead of failing every job later."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"{name}={path} is not writable by the worker ({type(exc).__name__}: {exc}). On Railway, mount the "
            f"Volume at /data and make sure the worker user (uid 10001) can write to it.") from exc


class RefreshStopped(ExtractUnavailable):
    """The worker is shutting down: stop the refresh now (the partial download is resumable)."""


class BackgroundRefresher:
    """Refreshes stale extracts that searches asked for, outside of any job.

    Cross-process safety comes from the MongoDB refresh lease, not from this thread: two worker
    processes may both run a refresher and still never refresh the same region twice.
    Start: recover() once (crash clean-up), then a pass every `interval` seconds.
    Stop: stop() sets an event that the loop AND the download/filter heartbeats check, then joins.
    """

    def __init__(self, db: Database, cfg: ExtractConfig, *, owner: str, interval: float = 60,
                 transport: httpx.BaseTransport | None = None, logger=None):
        import logging
        import threading

        self.db, self.cfg, self.owner, self.interval = db, cfg, owner, interval
        self.transport = transport
        self.log = logger or logging.getLogger("worker.extracts")
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="extract-refresher", daemon=True)
        self.client = make_http_client(cfg, transport)
        self.cache = ExtractCache(db, cfg, owner=owner, client=self.client)

    def start(self) -> dict:
        check_writable(self.cfg.cache_dir, "OSM_CACHE_DIR")
        check_writable(self.cfg.tmp_dir, "OSM_TMP_DIR")
        stats = self.cache.recover()
        self.log.info("OpenStreetMap extract cache at %s: recovery %s", self.cfg.cache_dir, stats)
        self.thread.start()
        return stats

    def _heartbeat(self) -> None:
        if self.stop_event.is_set():
            raise RefreshStopped("Worker is shutting down.")

    def run_once(self) -> int:
        if not self.db.osmExtracts.find_one({"refreshRequestedAt": {"$ne": None}}, {"_id": 1}):
            return 0
        index = GeofabrikIndex.load(self.client, self.cfg)
        ran = self.cache.refresh_requested(index, stop=self.stop_event, heartbeat=self._heartbeat)
        if ran:
            self.log.info("Refreshed %s OpenStreetMap extract(s) in the background", ran)
        return ran

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.run_once()
            except RefreshStopped:
                break
            except Exception:  # never let the refresher die; the next pass tries again
                self.log.exception("Background extract refresh failed")

    def stop(self, timeout: float = 30) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout)
        self.client.close()
