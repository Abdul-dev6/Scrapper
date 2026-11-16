"""Regression tests for the 2026-11-16 security review (worker side): M1, M2, L6, L10."""

import asyncio
import gzip

import httpcore
import httpx
import pytest

from app.crawl import fetcher as fetcher_mod
from app.crawl import robots as robots_mod
from app.crawl.fetcher import Fetcher, FetchError
from app.crawl.robots import MAX_ROBOTS_BYTES, Robots
from app.crawl.runner import BOT_TOKEN, make_async_client
from app.crawl.ssrf import BlockedAddress, PinnedBackend
from tests.conftest import FakeWeb

UA = "LeadFinderBot/test"


def run(coro):
    return asyncio.run(coro)


# ---------- M1: robots.txt is read with a size cap ----------

def test_huge_robots_txt_is_not_read_to_the_end():
    sent = []

    async def endless():  # 100 MB of comments, 64 KB at a time
        for _ in range(1600):
            sent.append(1)
            yield b"#" * 65536 + b"\n"

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=endless(), headers={"content-type": "text/plain"})
        return httpx.Response(200, text="<html></html>", headers={"content-type": "text/html"})

    async def go():
        async with make_async_client(UA, 5, httpx.MockTransport(handler)) as client:
            return await Robots(BOT_TOKEN).allowed(client, "https://bomb.test", "https://bomb.test/")

    assert run(go()) is True  # only comments in the part we read: allowed
    assert len(sent) * 65536 <= MAX_ROBOTS_BYTES + 2 * 65536  # stopped right after the cap, not after 100 MB


def test_zip_bomb_robots_txt_is_capped_after_decompression():
    bomb = gzip.compress(b"# padding\n" * 20_000_000)  # 200 MB unpacked, ~200 KB packed
    seen = {}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=bomb, headers={"content-type": "text/plain", "content-encoding": "gzip"})
        return httpx.Response(404)

    async def go():
        async with make_async_client(UA, 5, httpx.MockTransport(handler)) as client:
            r = Robots(BOT_TOKEN)
            rules = await r.rules(client, "https://zip.test")
            seen["rules"] = rules
            return rules

    rules = run(go())
    assert not isinstance(rules, bool)  # parsed (the first 500 KB), the worker didn't run out of memory


# ---------- M2: connections are pinned to the checked IP (DNS rebinding) ----------

class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.connected: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append(host)
        raise httpcore.ConnectError("test backend: no real network")

    async def connect_unix_socket(self, *a, **k):
        raise AssertionError("never")

    async def sleep(self, seconds):
        await asyncio.sleep(0)


def test_pinned_transport_is_wired():
    client = make_async_client(UA, 5)
    try:
        assert isinstance(client._transport._pool._network_backend, PinnedBackend)
    finally:
        run(client.aclose())


def test_dns_rebinding_is_blocked_at_connect_time():
    """First lookup (the request hook) says public, the second (the connection) says private."""
    answers = iter(["93.184.216.34", "10.0.0.5", "10.0.0.5", "10.0.0.5"])
    given: list[str] = []

    async def rebinding(host, port):
        given.append(next(answers))
        return [given[-1]]

    recorder = RecordingBackend()

    async def go():
        client = make_async_client(UA, 5, host_resolve=rebinding)
        client._transport._pool._network_backend.inner = recorder
        async with client:
            await client.get("http://rebind.test/robots.txt")

    with pytest.raises(BlockedAddress):
        run(go())
    assert recorder.connected == []  # never connected anywhere, least of all to 10.0.0.5
    assert given[:2] == ["93.184.216.34", "10.0.0.5"]  # the hook's check PASSED; the connect-time check blocked


def test_pinned_backend_connects_to_the_checked_ip():
    recorder = RecordingBackend()

    async def resolve(host, port):
        return ["93.184.216.34"]

    backend = PinnedBackend(resolve, inner=recorder)
    with pytest.raises(httpcore.ConnectError):
        run(backend.connect_tcp("gym.test", 443))
    assert recorder.connected == ["93.184.216.34"]  # the IP itself, not the name (no second lookup)


@pytest.mark.parametrize("ips", [["10.0.0.5"], ["93.184.216.34", "127.0.0.1"], ["169.254.169.254"], []])
def test_pinned_backend_refuses_private_addresses(ips):
    recorder = RecordingBackend()

    async def resolve(host, port):
        return ips

    with pytest.raises(BlockedAddress):
        run(PinnedBackend(resolve, inner=recorder).connect_tcp("gym.test", 443))
    assert recorder.connected == []


def test_pinned_backend_refuses_odd_ports_and_unix_sockets():
    backend = PinnedBackend(inner=RecordingBackend())
    with pytest.raises(BlockedAddress):
        run(backend.connect_tcp("93.184.216.34", 27017))
    with pytest.raises(BlockedAddress):
        run(backend.connect_unix_socket("/var/run/docker.sock"))


# ---------- L6: one page has a total time limit ----------

def test_slow_drip_page_is_given_up(monkeypatch):
    monkeypatch.setattr(fetcher_mod, "PAGE_DEADLINE", 0.3)

    async def drip():
        for _ in range(100):
            yield b"<p>"
            await asyncio.sleep(0.05)  # each read is quick, so per-read timeouts never fire

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=drip(), headers={"content-type": "text/html"})

    async def go():
        async with make_async_client(UA, 5, httpx.MockTransport(handler)) as client:
            await Fetcher(client, Robots(BOT_TOKEN), delay=0).fetch("https://slow.test/")

    with pytest.raises(FetchError, match="too slow"):
        run(go())


def test_polite_waiting_does_not_count_against_the_page_limit(monkeypatch):
    """Regression (found in the pre-deploy review): the limit once also covered the Crawl-delay pause,
    so every page after the first timed out on sites with a long Crawl-delay."""
    monkeypatch.setattr(fetcher_mod, "PAGE_DEADLINE", 0.3)
    web = FakeWeb(pages={"https://ironfist.test/": "<a href='/contact'>Contact</a>", "https://ironfist.test/contact": "<p>hi</p>"},
                  robots={"ironfist.test": "User-agent: *\nCrawl-delay: 10"})

    async def real_pause(seconds):
        await asyncio.sleep(0.5)  # longer than PAGE_DEADLINE, like a real 10-30 s Crawl-delay

    async def go():
        async with make_async_client(UA, 5, web.transport()) as client:
            f = Fetcher(client, Robots(BOT_TOKEN), delay=0, sleep=real_pause)
            await f.fetch("https://ironfist.test/")
            return await f.fetch("https://ironfist.test/contact")

    assert "hi" in run(go()).html


def test_robots_has_a_total_time_limit_too(monkeypatch):
    monkeypatch.setattr(robots_mod, "ROBOTS_DEADLINE", 0.3)

    async def drip():
        for _ in range(100):
            yield b"# x\n"
            await asyncio.sleep(0.05)

    def handler(request):
        return httpx.Response(200, content=drip(), headers={"content-type": "text/plain"})

    async def go():
        async with make_async_client(UA, 5, httpx.MockTransport(handler)) as client:
            return await Robots(BOT_TOKEN).allowed(client, "https://slow.test", "https://slow.test/")

    assert run(go()) is False  # couldn't read the rules in time: stay away (RFC 9309, like a 5xx)


# ---------- L10: robots.txt is checked on every redirect hop ----------

def test_redirect_into_a_disallowed_page_is_not_followed():
    web = FakeWeb(
        pages={
            "https://a.test/": httpx.Response(301, headers={"location": "https://b.test/private/contact"}),
            "https://b.test/private/contact": "<html>secret</html>",
        },
        robots={"b.test": "User-agent: *\nDisallow: /private"},
    )

    async def go():
        async with make_async_client(UA, 5, web.transport()) as client:
            await Fetcher(client, Robots(BOT_TOKEN), delay=0).fetch("https://a.test/")

    with pytest.raises(FetchError, match="robots"):
        run(go())
    assert "https://b.test/private/contact" not in web.visited()


def test_intermediate_hop_is_checked_too():
    web = FakeWeb(
        pages={
            "https://a.test/": httpx.Response(302, headers={"location": "/tracking/hop"}),
            "https://a.test/tracking/hop": httpx.Response(302, headers={"location": "/contact"}),
            "https://a.test/contact": "<html>ok</html>",
        },
        robots={"a.test": "User-agent: *\nDisallow: /tracking"},
    )

    async def go():
        async with make_async_client(UA, 5, web.transport()) as client:
            await Fetcher(client, Robots(BOT_TOKEN), delay=0).fetch("https://a.test/")

    with pytest.raises(FetchError, match="robots"):
        run(go())
    assert "https://a.test/tracking/hop" not in web.visited()


def test_normal_redirect_still_works():
    web = FakeWeb(pages={
        "http://gym.test/": httpx.Response(301, headers={"location": "https://www.gym.test/"}),
        "https://www.gym.test/": "<html><a href='mailto:info@gym.test'>m</a></html>",
    })

    async def go():
        async with make_async_client(UA, 5, web.transport()) as client:
            return await Fetcher(client, Robots(BOT_TOKEN), delay=0).fetch("http://gym.test/")

    page = run(go())
    assert page.url == "https://www.gym.test/" and "info@gym.test" in page.html


def test_redirect_loop_stops():
    web = FakeWeb(pages={"https://loop.test/": httpx.Response(302, headers={"location": "https://loop.test/"})})

    async def go():
        async with make_async_client(UA, 5, web.transport()) as client:
            await Fetcher(client, Robots(BOT_TOKEN), delay=0).fetch("https://loop.test/")

    with pytest.raises(FetchError, match="redirects"):
        run(go())
