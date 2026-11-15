import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import mongomock
import pytest
from bson import ObjectId

from app import jobs as q
from app.config import Settings
from app.osm.ratelimit import RateLimiter

OWNER = ObjectId()


PUBLIC_TEST_IP = "93.184.216.34"
PRIVATE_TEST_HOSTS = {"intranet.test": "10.0.0.5", "metadata.test": "169.254.169.254", "rebind.test": "127.0.0.1"}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """The crawler's SSRF guard resolves every host. Test hosts (*.test) resolve to a public IP,
    except a few that deliberately point inside a private network."""
    from app.crawl import ssrf

    async def resolve(host, _port):
        return [PRIVATE_TEST_HOSTS.get(host, PUBLIC_TEST_IP)]

    monkeypatch.setattr(ssrf, "system_resolve", resolve)


# The place the user picked (a gazetteer document, as the web app copies it onto the job).
LONDON_PLACE = {
    "_id": "gn:2643743", "kind": "city", "name": "London", "countryCode": "GB", "countryName": "United Kingdom",
    "admin1Code": "ENG", "admin1Name": "England", "lat": 51.50853, "lon": -0.12574, "population": 8961989,
}
LONDON_BOX = [51.28, -0.51, 51.69, 0.33]  # [south, west, north, east] of the test urban outline


def _write_zip(folder, name, shape_type, fields, rows):
    """A tiny Natural-Earth-like shapefile zip (polygons given as [south, west, north, east] boxes)."""
    import zipfile

    import shapefile

    base = folder / name
    with shapefile.Writer(str(base), shapeType=shape_type) as w:
        for f in fields:
            w.field(*f)
        for (s, west, n, e), rec in rows:
            w.poly([[[west, s], [e, s], [e, n], [west, n], [west, s]]])
            w.record(*rec)
    zpath = folder / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for ext in ("shp", "shx", "dbf"):
            z.write(f"{base}.{ext}", f"{name}.{ext}")
    return zpath


@pytest.fixture(scope="session")
def test_boundaries(tmp_path_factory):
    """Offline boundary data for tests: Great Britain, one US state, and London's urban outline."""
    import shapefile

    from app.geo.boundaries import Boundaries

    d = tmp_path_factory.mktemp("geo")
    paths = {
        "ne_admin0": _write_zip(d, "admin0", shapefile.POLYGON, [("NAME", "C", 40), ("ISO_A2_EH", "C", 3)], [
            ([49.9, -8.0, 60.9, 1.8], ["United Kingdom", "GB"]),
            ([24.5, -125.0, 49.4, -66.9], ["United States", "US"]),
        ]),
        "ne_admin1": _write_zip(d, "admin1", shapefile.POLYGON,
                                [("name", "C", 40), ("gn_a1_code", "C", 12), ("iso_a2", "C", 3), ("geonunit", "C", 40)], [
            ([45.5, -124.8, 49.0, -116.9], ["Washington", "US.WA", "US", "United States of America"]),
            ([50.0, -2.5, 55.8, 1.7], ["English county", "GB.Z9", "GB", "England"]),
            ([51.4, -5.3, 53.4, -2.7], ["Welsh county", "GB.Y1", "GB", "Wales"]),
        ]),
        "ne_urban": _write_zip(d, "urban", shapefile.POLYGON, [("area_sqkm", "N", 12, 2)], [
            (LONDON_BOX, [2000.0]),
        ]),
    }
    return Boundaries(paths, {"ne_admin0": "test-admin0", "ne_admin1": "test-admin1", "ne_urban": "test-urban"})


@pytest.fixture(autouse=True)
def offline_boundaries(monkeypatch, test_boundaries):
    from app import pipeline

    monkeypatch.setattr(pipeline, "_BOUNDARIES", test_boundaries)


@pytest.fixture
def db():
    return mongomock.MongoClient(tz_aware=True).db


@pytest.fixture
def settings(tmp_path):
    return Settings(
        mongodb_uri="mongodb://test", mongodb_db="test", worker_id="w-test", poll_interval=0,
        stale_after=600, max_attempts=3, crawler_contact_url="https://lead-finder.test/crawler",
        crawl_concurrency=5, crawl_delay=0, crawl_max_pages=5, crawl_timeout=5, crawl_recheck_days=7,
        osm_cache_dir=str(tmp_path / "volume" / "osm"), osm_tmp_dir=str(tmp_path / "tmp"),
        osm_volume_margin_bytes=0, osm_tmp_margin_bytes=0, geofabrik_index_url=GEOFABRIK_INDEX,
    )


@pytest.fixture
def no_wait_limiter():
    return RateLimiter(0)


def add_job(db, **over):
    doc = {
        "ownerId": OWNER,
        "location": {"query": "London, UK", "place": dict(LONDON_PLACE)},
        "categories": ["boxing_club"],
        "sources": ["osm"],
        "status": "queued",
        "attempts": 0,
        "progress": {"stage": "queued", "pct": 0},
        "stats": {"errors": 0},
        "errorLog": [],
        "createdAt": q.now(),
    }
    doc.update(over)
    return db.scrapingJobs.insert_one(doc).inserted_id


GEOFABRIK = "https://geofabrik.test"
GEOFABRIK_INDEX = f"{GEOFABRIK}/index-v1.json"
TEST_REGION_BOX = [49.9, -8.0, 60.9, 1.8]  # [south, west, north, east]: the test "great-britain" region


def _box_geojson(s, w, n, e):
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def fake_filter(pbf_path, heartbeat):
    """Stands in for pyosmium in local tests: the fake "PBF" is the element list as JSON.
    Applies the same rule as the real filter (named + matching a category). The real pyosmium
    filter is tested against generated PBF files in tests/test_extracts_osmium.py (Docker)."""
    from app.osm.tags import is_wanted

    from app.osm.extracts import ExtractCorrupt

    try:
        data = json.loads(Path(pbf_path).read_bytes() or b"null")
    except ValueError:
        data = None
    if not isinstance(data, list):  # like libosmium refusing a file that isn't a PBF
        raise ExtractCorrupt("The map data file can't be read (test: not a valid extract).")
    for el in data:
        heartbeat()
        if is_wanted(el.get("tags") or {}):
            yield el


def pytest_configure(config):
    config.addinivalue_line("markers", "real_osmium: needs pyosmium (runs in the production Docker image)")


@pytest.fixture(autouse=True)
def fake_pbf_filter(request, monkeypatch):
    """Local tests use fake_filter; tests marked `real_osmium` use pyosmium itself (Docker)."""
    if request.node.get_closest_marker("real_osmium"):
        return
    from app.osm import extracts

    monkeypatch.setattr(extracts, "filter_pbf_osmium", fake_filter)


class FakeOSM:
    """httpx MockTransport that plays Geofabrik (the only OpenStreetMap source), and records calls.

    elements:      the OSM elements (Overpass-compatible dicts) inside the test extract
    regions:       {id: (box, version)}; default one "great-britain" region covering TEST_REGION_BOX
    status:        if set, every extract request gets this HTTP status (e.g. 503 = Geofabrik down)
    md5:           publish <file>.md5 (Geofabrik does); False = 404
    ranges:        honour Range requests (Accept-Ranges: bytes)
    cut_after:     simulate a dropped connection after this many body bytes (once, then normal)
    wrong_length:  announce a Content-Length different from the body
    """

    def __init__(self, elements=None, regions=None, status=None, md5=True, ranges=True, cut_after=None,
                 wrong_length=False, index_status=None, bodies=None, bad_md5=False):
        self.elements = elements or []
        self.regions = regions or {"great-britain": (TEST_REGION_BOX, "great-britain-261113")}
        self.status = status
        self.index_status = index_status
        self.md5 = md5
        self.ranges = ranges
        self.cut_after = cut_after
        self.wrong_length = wrong_length
        self.bodies = bodies or {}  # region id -> raw bytes (default: json of elements)
        self.bad_md5 = bad_md5  # publish a checksum that doesn't match (corrupted download)
        self.calls: list[httpx.Request] = []

    # --- what Geofabrik serves
    def index(self) -> dict:
        feats = []
        for rid, (box, _v) in self.regions.items():
            feats.append({"type": "Feature", "geometry": _box_geojson(*box), "properties": {
                "id": rid, "name": rid.replace("-", " ").title(), "parent": "europe",
                "urls": {"pbf": f"{GEOFABRIK}/europe/{rid}-latest.osm.pbf", "updates": f"{GEOFABRIK}/europe/{rid}-updates"}}})
        return {"type": "FeatureCollection", "features": feats}

    def body(self, rid: str) -> bytes:
        return self.bodies.get(rid, json.dumps(self.elements).encode())

    def set_version(self, rid: str, version: str) -> None:
        box, _ = self.regions[rid]
        self.regions[rid] = (box, version)

    def handler(self, request: httpx.Request) -> httpx.Response:
        import hashlib

        self.calls.append(request)
        path = request.url.path
        if request.url.host != "geofabrik.test":
            return httpx.Response(599, text="unexpected host")
        if path == "/index-v1.json":
            if self.index_status:
                return httpx.Response(self.index_status)
            return httpx.Response(200, json=self.index())
        if path.endswith("/state.txt"):
            return httpx.Response(200, text="sequenceNumber=4963\ntimestamp=2026-11-13T21\\:22\\:20Z\n")
        if self.status:
            return httpx.Response(self.status)
        for rid, (_box, version) in self.regions.items():
            if path == f"/europe/{rid}-latest.osm.pbf":
                return httpx.Response(307, headers={"location": f"/europe/{version}.osm.pbf"})
            if path == f"/europe/{version}.osm.pbf.md5":
                if not self.md5:
                    return httpx.Response(404)
                digest = "0" * 32 if self.bad_md5 else hashlib.md5(self.body(rid)).hexdigest()
                return httpx.Response(200, text=f"{digest}  {version}.osm.pbf\n")
            if path == f"/europe/{version}.osm.pbf":
                data = self.body(rid)
                headers = {"etag": f'"{version}"', "content-length": str(len(data) + (7 if self.wrong_length else 0))}
                if self.ranges:
                    headers["accept-ranges"] = "bytes"
                if request.method == "HEAD":
                    return httpx.Response(200, headers=headers)
                rng = request.headers.get("range")
                if rng and self.ranges and request.headers.get("if-range") == f'"{version}"':
                    first = int(rng.split("=")[1].split("-")[0])
                    part = data[first:]
                    return httpx.Response(206, content=part, headers={**headers, "content-length": str(len(part)),
                                          "content-range": f"bytes {first}-{len(data) - 1}/{len(data)}"})
                if self.cut_after is not None:
                    cut, self.cut_after = self.cut_after, None
                    return httpx.Response(200, headers={**headers, "content-length": str(len(data))},
                                          stream=_DroppingStream(data[:cut]))
                return httpx.Response(200, content=data, headers=headers)
        return httpx.Response(404)

    def client(self, settings=None) -> httpx.Client:
        """With settings: the worker's real client (headers, timeouts), just without network."""
        transport = httpx.MockTransport(self.handler)
        if settings is not None:
            from app.pipeline import make_client

            return make_client(settings, transport=transport)
        return httpx.Client(transport=transport)

    def downloads(self) -> list[httpx.Request]:
        """Every GET of an actual extract file (not index / md5 / state)."""
        return [r for r in self.calls if r.method == "GET" and r.url.path.endswith(".osm.pbf")
                and not r.url.path.endswith("-latest.osm.pbf")]

    def count(self, host: str) -> int:
        return sum(1 for r in self.calls if r.url.host == host)


class _DroppingStream(httpx.SyncByteStream):
    """Sends part of the body, then breaks the connection (like a network drop mid-download)."""

    def __init__(self, data: bytes):
        self.data = data

    def __iter__(self):
        yield self.data
        raise httpx.ReadError("connection reset by peer")


class FakeWeb:
    """Plays business websites. pages: {"https://gym.test/contact": "<html>..."}.

    robots: {"gym.test": "User-agent: *
Disallow: /"}. Unknown pages are 404.
    no_mx: email domains whose DNS says "no email here".
    """

    def __init__(self, pages=None, robots=None, no_mx=(), status=None):
        self.pages = {k.rstrip("/"): v for k, v in (pages or {}).items()}
        self.robots = robots or {}
        self.no_mx = set(no_mx)
        self.status = status or {}  # {"https://x.test": 500}
        self.calls: list[httpx.Request] = []
        self.mx_lookups: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url).split("?")[0].rstrip("/")
        host = request.url.host
        if request.url.path == "/robots.txt":
            if host in self.robots:
                return httpx.Response(200, text=self.robots[host])
            return httpx.Response(404)
        if url in self.status:
            return httpx.Response(self.status[url])
        if url in self.pages:
            page = self.pages[url]
            if isinstance(page, httpx.Response):
                return page
            return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    async def resolve(self, domain: str):
        self.mx_lookups.append(domain)
        return domain not in self.no_mx

    def visited(self) -> list[str]:
        return [str(r.url) for r in self.calls if r.url.path != "/robots.txt"]
