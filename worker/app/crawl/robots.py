"""robots.txt: ask each site what our bot may visit, and remember the answer for 24 hours.

Rules follow RFC 9309 (the robots.txt standard):
  - 2xx: obey the file
  - 4xx (no file / not allowed to read it): everything is allowed
  - 5xx or network error: the site is unwell, treat everything as disallowed
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.robotparser import RobotFileParser

import httpx

from .ssrf import BlockedAddress

CACHE_SECONDS = 24 * 3600
MAX_ROBOTS_BYTES = 500_000  # RFC 9309 asks crawlers to read at least 500 KiB


class Robots:
    def __init__(self, user_agent_token: str, clock: Callable[[], float] = time.monotonic):
        self.token = user_agent_token
        self.clock = clock
        self._cache: dict[str, tuple[float, RobotFileParser | bool]] = {}

    async def _load(self, client: httpx.AsyncClient, origin: str) -> RobotFileParser | bool:
        """A parser, True (allow all) or False (disallow all)."""
        try:
            resp = await client.get(f"{origin}/robots.txt", timeout=10)
        except BlockedAddress:
            raise  # not a robots.txt answer: the site itself must not be contacted
        except httpx.HTTPError:
            return False
        if 400 <= resp.status_code < 500:
            return True
        if resp.status_code != 200:
            return False
        parser = RobotFileParser()
        parser.parse(resp.text[:MAX_ROBOTS_BYTES].splitlines())
        return parser

    async def rules(self, client: httpx.AsyncClient, origin: str) -> RobotFileParser | bool:
        hit = self._cache.get(origin)
        if hit and self.clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
        rules = await self._load(client, origin)
        self._cache[origin] = (self.clock(), rules)
        return rules

    async def allowed(self, client: httpx.AsyncClient, origin: str, url: str) -> bool:
        rules = await self.rules(client, origin)
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self.token, url)

    async def crawl_delay(self, client: httpx.AsyncClient, origin: str) -> float | None:
        rules = await self.rules(client, origin)
        if isinstance(rules, bool):
            return None
        delay = rules.crawl_delay(self.token)
        return float(delay) if delay is not None else None
