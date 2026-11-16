"""A polite page fetcher.

Before every page: check robots.txt, then wait so the same website gets at most
one request every `delay` seconds (or longer, if its robots.txt asks for that).
Only HTML is read, and only the first 2 MB.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .robots import Robots
from .ssrf import BlockedAddress

MAX_PAGE_BYTES = 2_000_000
MAX_CRAWL_DELAY = 30.0  # a site asking for more than this is waited on at most 30 s
PAGE_DEADLINE = 30.0  # seconds for one whole page: redirects + headers + body
MAX_REDIRECTS = 5


class FetchError(Exception):
    """Message is short and safe to store in the job's error log."""


class RetryableFetch(FetchError):
    """Timeout, connection reset, HTTP 429 or 5xx: worth ONE more polite try."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 30.0


def _retry_after(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None  # HTTP-date form: just use our normal delay


@dataclass
class Page:
    url: str  # final URL after redirects
    html: str


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        robots: Robots,
        *,
        delay: float = 2.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.robots = robots
        self.delay = delay
        self.sleep = sleep
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def _wait_turn(self, host: str, origin: str) -> None:
        delay = self.delay
        asked = await self.robots.crawl_delay(self.client, origin)
        if asked:
            delay = max(delay, min(asked, MAX_CRAWL_DELAY))
        last = self._last.get(host)
        if last is not None:
            wait = delay - (self.clock() - last)
            if wait > 0:
                await self.sleep(wait)
        self._last[host] = self.clock()

    async def fetch(self, url: str) -> Page:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise FetchError("Not a web address")
        host, origin = parts.hostname.lower(), origin_of(url)

        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:  # one request at a time per website
            try:
                try:
                    return await self._fetch_locked(url, host, origin)
                except RetryableFetch as exc:
                    # One retry, never sooner than the site's delay (or its Retry-After, max 30 s).
                    if exc.retry_after:
                        await self.sleep(min(exc.retry_after, MAX_RETRY_AFTER))
                    try:
                        return await self._fetch_locked(url, host, origin)
                    except RetryableFetch as again:
                        raise FetchError(str(again)) from None
            except BlockedAddress as exc:
                raise FetchError(str(exc)) from None

    async def _fetch_locked(self, url: str, host: str, origin: str) -> Page:
        return await self._follow(url, host, origin)

    async def _follow(self, url: str, host: str, origin: str) -> Page:
        """Redirects are followed here, one hop at a time, so robots.txt is checked for EVERY page we
        request, including the pages in between (security review L10). The SSRF hook still runs per hop."""
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            cur_origin = origin_of(current)
            if not await self.robots.allowed(self.client, cur_origin, current):
                raise FetchError("Blocked by robots.txt")
            if cur_origin == origin:
                await self._wait_turn(host, origin)
            redirect: str | None = None
            try:
                # Each request + its body within PAGE_DEADLINE: per-read timeouts alone let a site that sends
                # one byte every few seconds hold a crawl slot for hours (security review L6). Only the network
                # part: the polite waits above (Crawl-delay up to 30 s, robots.txt) must not count against it.
                async with asyncio.timeout(PAGE_DEADLINE):
                    async with self.client.stream("GET", current, follow_redirects=False) as resp:
                        if resp.is_redirect and resp.headers.get("location"):
                            redirect = str(resp.url.join(resp.headers["location"]))
                        else:
                            return await self._read(resp)
            except TimeoutError:
                raise FetchError("The website was too slow to send the page") from None
            except (BlockedAddress, FetchError):
                raise
            except httpx.TimeoutException as exc:
                raise RetryableFetch("The website took too long to answer") from exc
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                raise RetryableFetch("Couldn't connect to the website") from exc
            except httpx.HTTPError as exc:
                raise FetchError("Couldn't connect to the website") from exc
            if urlsplit(redirect).scheme not in ("http", "https"):
                raise FetchError("Redirected to a non-web address")
            current = redirect
        raise FetchError("Too many redirects")

    async def _read(self, resp: httpx.Response) -> Page:
        if resp.status_code in RETRY_STATUS:
            raise RetryableFetch(f"HTTP {resp.status_code}", _retry_after(resp.headers.get("retry-after")))
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "").lower()
        if ctype and "html" not in ctype:
            raise FetchError("Not an HTML page")
        body = bytearray()
        async for chunk in resp.aiter_bytes():
            body += chunk
            if len(body) > MAX_PAGE_BYTES:
                break
        encoding = resp.encoding or "utf-8"
        return Page(str(resp.url), bytes(body[:MAX_PAGE_BYTES]).decode(encoding, errors="replace"))
