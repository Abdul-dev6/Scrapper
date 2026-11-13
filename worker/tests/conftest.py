import json
from urllib.parse import parse_qs

import httpx
import mongomock
import pytest
from bson import ObjectId

from app import jobs as q
from app.config import Settings
from app.osm.ratelimit import RateLimiter

NOMINATIM = "https://nominatim.test/search"
OVERPASS = "https://overpass.test/api/interpreter"
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


@pytest.fixture
def db():
    return mongomock.MongoClient(tz_aware=True).db


@pytest.fixture
def settings():
    return Settings(
        mongodb_uri="mongodb://test", mongodb_db="test", worker_id="w-test", poll_interval=0,
        stale_after=600, max_attempts=3, crawler_contact_url="https://lead-finder.test/crawler",
        nominatim_url=NOMINATIM, overpass_url=OVERPASS, overpass_timeout=60, max_discovery_queries=200,
        crawl_concurrency=5, crawl_delay=0, crawl_max_pages=5, crawl_timeout=5, crawl_recheck_days=7,
    )


@pytest.fixture
def no_wait_limiter():
    return RateLimiter(0)


def add_job(db, **over):
    doc = {
        "ownerId": OWNER,
        "location": {"query": "London, UK"},
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


LONDON = {
    "osm_type": "relation", "osm_id": 65606, "addresstype": "city",
    "display_name": "London, Greater London, England, United Kingdom",
    "boundingbox": ["51.28", "51.69", "-0.51", "0.33"],
    "address": {"city": "London", "country": "United Kingdom", "country_code": "gb"},
}


class FakeOSM:
    """httpx MockTransport that plays Nominatim and Overpass, and records calls.

    overpass_responses: queue of responses for the first queries, then `elements`.
    overpass_status:    if set, every query gets this HTTP status (e.g. 503 = always busy).
    overpass_fn:        if set, called with the query text; returns an httpx.Response.
    status_text:        what /api/status says (default: a free slot).
    """

    def __init__(self, places=None, elements=None, overpass_responses=None, overpass_status=None,
                 overpass_fn=None, status_text="Rate limit: 2\n2 slots available now.\n"):
        self.places = [LONDON] if places is None else places
        self.elements = elements or []
        self.overpass_responses = list(overpass_responses or [])
        self.overpass_status = overpass_status
        self.overpass_fn = overpass_fn
        self.status_text = status_text
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "nominatim.test":
            return httpx.Response(200, json=self.places)
        if request.url.path.endswith("/status"):
            return httpx.Response(200, text=self.status_text)
        if self.overpass_status:
            return httpx.Response(self.overpass_status)
        if self.overpass_fn:
            return self.overpass_fn(parse_qs(request.content.decode())["data"][0])
        if self.overpass_responses:
            return self.overpass_responses.pop(0)
        return httpx.Response(200, content=json.dumps({"elements": self.elements}))

    def client(self, settings=None) -> httpx.Client:
        """With settings: the worker's real client (headers, timeouts), just without network."""
        transport = httpx.MockTransport(self.handler)
        if settings is not None:
            from app.pipeline import make_client

            return make_client(settings, transport=transport)
        return httpx.Client(transport=transport)

    def queries(self) -> list[str]:
        """Text of every Overpass query sent (status checks excluded)."""
        return [parse_qs(r.content.decode())["data"][0] for r in self.calls
                if r.url.host == "overpass.test" and r.url.path.endswith("/interpreter")]

    def count(self, host: str) -> int:
        if host == "overpass.test":
            return len(self.queries())
        return sum(1 for r in self.calls if r.url.host == host)


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
