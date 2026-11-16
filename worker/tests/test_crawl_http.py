"""Website crawling against a REAL local HTTP server (real sockets, real HTTP, the real httpx client with
its redirects, timeouts and streaming), through the whole job: OSM leads -> crawler -> saved contacts.

Business sites are virtual hosts (ironfist.test, ...) on one ThreadingHTTPServer on 127.0.0.1. A small
transport sends each request to that server, keeping its Host header; everything above it is production
code, including the SSRF guard (it still sees and checks the original host name, so a redirect to a
private address is refused before any connection is made).
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app import jobs as q
from app.pipeline import run_job
from tests.conftest import FakeOSM, add_job

PUBLIC_IP = "93.184.216.34"
PRIVATE = {"intranet.test": "10.0.0.5"}
CRAWL_DELAY = 0.3  # our own minimum between two requests to one site (production: 2.5 s)
ROBOTS_DELAY = 1.0  # what ironfist.test's robots.txt asks for

HOME_IRONFIST = """<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@type": "SportsActivityLocation", "name": "Iron Fist Boxing",
 "telephone": "+44 20 7946 0123", "email": "info@ironfist.test"}</script></head>
<body><nav><a href="/members/join">Join</a> <a href="/admin/">Admin</a></nav>
<footer><a href="/contact-us">Contact us</a> <a href="/privacy-policy">Privacy</a>
<a href="https://www.instagram.com/ironfistboxing">Instagram</a></footer></body></html>"""
CONTACT_IRONFIST = """<html><body><h1>Contact</h1><a href="mailto:coach@ironfist.test">Email the coach</a>
<a href="tel:+442079460124">Call</a> <a href="https://wa.me/447911123456">WhatsApp</a></body></html>"""


class Sites(BaseHTTPRequestHandler):
    """The business websites. Every request is recorded (host, path, time, user agent)."""

    log: list[dict] = []
    hits: dict[str, int] = defaultdict(int)
    lock = threading.Lock()

    def log_message(self, *_a):  # keep pytest output clean
        pass

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the crawler stopped reading (e.g. the 2 MB cap)

    def do_GET(self):  # noqa: N802
        host = self.headers.get("Host", "").split(":")[0]
        path = self.path.split("?")[0]
        with self.lock:
            self.log.append({"host": host, "path": path, "t": time.monotonic(), "ua": self.headers.get("User-Agent", "")})
            self.hits[f"{host}{path}"] += 1
            n = self.hits[f"{host}{path}"]
        html = lambda s: s.encode()  # noqa: E731
        if path == "/robots.txt":
            if host == "ironfist.test":
                return self._send(200, b"User-agent: *\nDisallow: /members\nDisallow: /admin\nCrawl-delay: 1\n", "text/plain")
            if host == "blocked.test":
                return self._send(200, b"User-agent: LeadFinderBot\nDisallow: /\n", "text/plain")
            return self._send(404, b"")
        if host == "ironfist.test":
            pages = {"/": HOME_IRONFIST, "/contact-us": CONTACT_IRONFIST,
                     "/privacy-policy": "<p>Data protection: dpo@legal-firm.test, 020 7946 0999</p>"}
            return self._send(200, html(pages[path])) if path in pages else self._send(404)
        if host == "redirector.test":  # the old domain redirects (twice) to the business's real site
            return self._send(301, headers={"Location": "http://www.redirector.test/home"})
        if host == "www.redirector.test":
            if path == "/home":
                return self._send(302, headers={"Location": "/welcome"})
            return self._send(200, html("<p>Mail us: hello@redirector.test</p>"))
        if host == "evil.test":  # a "website" that sends the crawler to the cloud metadata service
            return self._send(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        if host == "sneaky.test":  # ...or to a name that resolves to a private address
            return self._send(302, headers={"Location": "http://intranet.test/admin"})
        if host == "flaky.test":  # busy once, fine on the polite retry
            if n == 1:
                return self._send(503, headers={"Retry-After": "0"})
            return self._send(200, html("<p>info@flaky.test</p>"))
        if host == "slow.test":
            time.sleep(2.5)  # longer than the crawl timeout (1 s), both times
            return self._send(200, html("<p>never@slow.test</p>"))
        if host == "pdf.test":
            return self._send(200, b"%PDF-1.4 info@pdf.test", "application/pdf")
        if host == "huge.test":  # 3 MB page: only the first 2 MB are read
            body = "<p>front@huge.test</p>" + "<p>" + "x" * 3_000_000 + "</p><p>back@huge.test</p>"
            return self._send(200, html(body))
        if host == "chain.test":  # one website shared by two branches
            return self._send(200, html('<a href="mailto:bookings@chain.test">Book</a>'))
        if host == "manypages.test":  # many contact-ish links and no contact details anywhere
            links = "".join(f'<a href="/contact-{i}">Contact {i}</a>' for i in range(10))
            return self._send(200, html(f"<p>Welcome</p>{links}"))
        if host == "blocked.test":
            return self._send(200, html("<p>secret@blocked.test</p>"))
        return self._send(404)


class LocalSites(httpx.AsyncBaseTransport):
    """Sends every request to the local server (any scheme -> plain HTTP), keeping the Host header."""

    def __init__(self, port: int):
        self.port = port
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        local = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"host"]
        headers.append((b"Host", request.url.host.encode()))
        return await self.inner.handle_async_request(
            httpx.Request(request.method, local, headers=headers, stream=request.stream, extensions=request.extensions))

    async def aclose(self) -> None:
        await self.inner.aclose()


async def resolve(host: str, _port: int) -> list[str]:
    return [PRIVATE.get(host, PUBLIC_IP)]


async def mx(domain: str):
    return domain != "flaky.test"  # flaky.test's email domain has no mail server


@pytest.fixture
def server():
    Sites.log.clear()
    Sites.hits.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Sites)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


SITES = ["ironfist.test", "redirector.test", "evil.test", "sneaky.test", "flaky.test", "slow.test", "pdf.test",
         "huge.test", "manypages.test", "blocked.test"]


def gym(i, site):
    """Each gym ~1 km from the next, with its own phone: separate businesses (no duplicate rule applies)."""
    return {"type": "node", "id": i, "lat": 51.40 + 0.01 * (i % 20), "lon": -0.1,
            "tags": {"name": f"Gym {site}", "sport": "boxing", "website": f"https://{site}", "phone": osm_phone(i)}}


def osm_phone(i):
    return f"020 7946 {i:04d}"


def e164(i):
    return f"+44207946{i:04d}"


def run(db, settings, server, elements):
    s = replace(settings, crawl_delay=CRAWL_DELAY, crawl_timeout=1.0, crawl_concurrency=4)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    status = run_job(db, job, s, client=FakeOSM(elements=elements).client(s), sleep=lambda _s: None,
                     web_transport=LocalSites(server.server_address[1]), host_resolve=resolve, mx_resolve=mx)
    return status, db.scrapingJobs.find_one({"_id": job["_id"]})


def lead(db, site):
    return db.leads.find_one({"website.url": f"https://{site}"}) or db.leads.find_one({"website.domain": site})


def values(items):
    return sorted(i.get("value") or i.get("e164") for i in items or [])


def test_a_job_crawls_real_http_sites_politely_and_safely(db, settings, server):
    elements = [gym(i, site) for i, site in enumerate(SITES, 1)]
    elements += [gym(100, "chain.test"), gym(102, "chain.test")]  # two branches (2 km apart), one website
    elements[-1]["tags"]["name"] = "Gym chain.test Northside"
    status, job = run(db, settings, server, elements)
    log = Sites.log

    # -- the job finished; site problems are normal outcomes, not crashes
    assert status == "completed", job["errorLog"]
    assert job["stats"]["websitesChecked"] == 11  # 10 sites + chain.test once

    # -- contact pages are found and every value says where it came from
    iron = lead(db, "ironfist.test")
    assert values(iron["emails"]) == ["coach@ironfist.test", "info@ironfist.test"]
    assert "dpo@legal-firm.test" not in values(iron["emails"])  # privacy-policy fine print is skipped
    assert {e["sourceUrl"].rstrip("/") for e in iron["emails"]} == {"https://ironfist.test", "https://ironfist.test/contact-us"}
    assert "+442079460124" in values(iron["phones"]) and e164(1) in values(iron["phones"])  # site + OSM
    assert values(iron["whatsapp"]) == ["+447911123456"]
    assert iron["socials"]["instagram"]["url"] == "https://www.instagram.com/ironfistboxing"
    assert iron["contactStatus"] == "checked" and iron["website"]["reachable"] is True
    assert all(e["checks"]["mx"] == "valid" and e["checks"]["manuallyVerified"] is False for e in iron["emails"])

    # -- robots.txt: disallowed paths are never requested; its Crawl-delay is respected
    paths = [r["path"] for r in log if r["host"] == "ironfist.test"]
    assert not any(p.startswith(("/members", "/admin")) for p in paths)
    times = [r["t"] for r in log if r["host"] == "ironfist.test" and r["path"] != "/robots.txt"]
    assert len(times) >= 2 and min(b - a for a, b in zip(times, times[1:])) >= ROBOTS_DELAY - 0.05
    assert [r["path"] for r in log if r["host"] == "blocked.test"] == ["/robots.txt"]  # "Disallow: /" for us
    assert not lead(db, "blocked.test").get("emails")

    # -- our own per-site delay holds for every site, and every request names the bot + contact page
    by_host = defaultdict(list)
    for r in log:
        if r["path"] != "/robots.txt":
            by_host[r["host"]].append(r["t"])
    for host, ts in by_host.items():
        if host == "www.redirector.test":
            continue  # /home -> 302 -> /welcome is ONE page fetch: redirect hops follow at once (like a browser)
        assert all(b - a >= CRAWL_DELAY - 0.05 for a, b in zip(ts, ts[1:])), host
    assert log and all("LeadFinderBot" in r["ua"] and "lead-finder.test/crawler" in r["ua"] for r in log)

    # -- redirects are followed to the real site; redirects to private addresses are refused
    assert values(lead(db, "redirector.test")["emails"]) == ["hello@redirector.test"]
    assert {r["host"] for r in log} <= set(SITES) | {"www.redirector.test", "chain.test"}  # nothing else was contacted
    for site in ("evil.test", "sneaky.test"):
        bad = lead(db, site)
        assert bad["website"]["reachable"] is False and not bad.get("emails")
        assert values(bad["phones"]) == [e164(SITES.index(site) + 1)]  # the OSM phone is kept

    # -- retry once on 503; timeouts, non-HTML and oversized pages are handled
    flaky = lead(db, "flaky.test")
    assert values(flaky["emails"]) == ["info@flaky.test"] and flaky["emails"][0]["checks"]["mx"] == "no_mx"
    assert Sites.hits["flaky.test/"] == 2
    assert lead(db, "slow.test")["website"]["reachable"] is False and Sites.hits["slow.test/"] == 2
    assert lead(db, "pdf.test")["website"]["reachable"] is False
    assert values(lead(db, "huge.test")["emails"]) == ["front@huge.test"]  # read up to 2 MB, not the rest

    # -- at most 5 pages per site; one shared website is fetched once and applied to both branches
    assert len([r for r in log if r["host"] == "manypages.test" and r["path"] != "/robots.txt"]) == 5
    branches = list(db.leads.find({"website.domain": "chain.test"}))
    assert len(branches) == 2 and all(values(b["emails"]) == ["bookings@chain.test"] for b in branches)
    assert Sites.hits["chain.test/"] == 1


def test_a_recrawl_keeps_manual_verification_and_skips_recently_checked_sites(db, settings, server):
    run(db, settings, server, [gym(1, "ironfist.test")])
    iron = lead(db, "ironfist.test")
    emails = [{**e, "checks": {**e["checks"], "manuallyVerified": True}} if e["value"] == "coach@ironfist.test" else e
              for e in iron["emails"]]
    db.leads.update_one({"_id": iron["_id"]}, {"$set": {"emails": emails, "contactStatus": "verified"}})

    Sites.log.clear()
    run(db, settings, server, [gym(1, "ironfist.test")])
    assert Sites.log == []  # checked minutes ago: not crawled again (CRAWL_RECHECK_DAYS)

    db.leads.update_one({"_id": iron["_id"]}, {"$set": {"website.checkedAt": q.now().replace(year=2020)}})
    run(db, settings, server, [gym(1, "ironfist.test")])
    assert any(r["path"] == "/contact-us" for r in Sites.log)  # crawled again...
    again = lead(db, "ironfist.test")
    coach = next(e for e in again["emails"] if e["value"] == "coach@ironfist.test")
    assert coach["checks"]["manuallyVerified"] is True  # ...and the person's check survived
    assert again["contactStatus"] == "verified"


def test_the_ssrf_guard_is_not_bypassed_by_the_local_transport(server):
    """Sanity check of the harness itself: the guard runs before the transport, on the original host."""
    from app.crawl.runner import make_async_client
    from app.crawl.ssrf import BlockedAddress

    async def go():
        async with make_async_client("LeadFinderBot/test", 2, LocalSites(server.server_address[1]), resolve) as client:
            ok = await client.get("http://chain.test/")
            with pytest.raises(BlockedAddress):
                await client.get("http://intranet.test/")
            return ok.status_code

    import asyncio

    assert asyncio.run(go()) == 200
    assert "intranet.test" not in {r["host"] for r in Sites.log}
    assert json.dumps(Sites.log)  # recorded, serialisable
