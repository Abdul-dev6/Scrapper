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
        if not await self.robots.allowed(self.client, origin, url):
            raise FetchError("Blocked by robots.txt")
        await self._wait_turn(host, origin)
        try:
            async with self.client.stream("GET", url) as resp:
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
                final_url = str(resp.url)
                encoding = resp.encoding or "utf-8"
        except BlockedAddress:
            raise
        except FetchError:
            raise
        except httpx.TimeoutException as exc:
            raise RetryableFetch("The website took too long to answer") from exc
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            raise RetryableFetch("Couldn't connect to the website") from exc
        except httpx.HTTPError as exc:
            raise FetchError("Couldn't connect to the website") from exc
        if final_url != url and origin_of(final_url) != origin:
            # Redirected to another site: check that site's robots.txt too.
            if not await self.robots.allowed(self.client, origin_of(final_url), final_url):
                raise FetchError("Blocked by robots.txt")
        return Page(final_url, bytes(body).decode(encoding, errors="replace"))
