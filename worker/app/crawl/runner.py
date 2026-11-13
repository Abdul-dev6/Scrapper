"""Crawl the websites of a job's leads, many sites in parallel, each site politely.

Branches of a chain often share one website URL: that URL is crawled once and the
result is applied to every branch.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

import httpx
from pymongo.database import Database

from .. import jobs as q
from .. import suppression
from ..config import Settings
from .enrich import as_lead, build_update
from .fetcher import Fetcher
from .robots import Robots
from .site import SiteResult, crawl_site
from .ssrf import HostResolve, make_guard
from .verify import MxChecker, Resolve, dns_has_mx

BOT_TOKEN = "LeadFinderBot"  # the name robots.txt rules are matched against


@dataclass
class CrawlStats:
    checked: int = 0
    reachable: int = 0
    suppressed: int = 0
    failed: int = 0


def make_async_client(user_agent: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None,
                      host_resolve: HostResolve | None = None):
    return httpx.AsyncClient(
        transport=transport,
        event_hooks={"request": [make_guard(host_resolve)]},  # SSRF check on every request + redirect hop
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
        timeout=httpx.Timeout(timeout),
        follow_redirects=True,
        max_redirects=5,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )


def leads_to_crawl(db: Database, job: dict, settings: Settings) -> dict[str, list[dict]]:
    """website url -> the job's leads with that url (skipping sites checked recently)."""
    recent = q.now() - timedelta(days=settings.crawl_recheck_days)
    cursor = db.leads.find({
        "ownerId": job["ownerId"],
        "jobIds": job["_id"],
        "website.url": {"$exists": True},
        "$or": [{"website.checkedAt": {"$exists": False}}, {"website.checkedAt": {"$lt": recent}}],
    })
    groups: dict[str, list[dict]] = defaultdict(list)
    for lead in cursor:
        groups[lead["website"]["url"]].append(lead)
    return dict(groups)


async def crawl_websites(
    db: Database,
    job: dict,
    settings: Settings,
    *,
    user_agent: str,
    on_progress: Callable[[int, int, CrawlStats], None],
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Resolve = dns_has_mx,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    host_resolve: HostResolve | None = None,
) -> CrawlStats:
    """on_progress(done, total, stats) is called after each website; it may raise to stop (cancel)."""
    groups = leads_to_crawl(db, job, settings)
    stats = CrawlStats()
    total = len(groups)
    if not total:
        return stats
    hashes = suppression.load_hashes(db.suppressionList, job["ownerId"])
    mx = MxChecker(resolve)
    limit = asyncio.Semaphore(max(1, settings.crawl_concurrency))

    async with make_async_client(user_agent, settings.crawl_timeout, transport, host_resolve) as client:
        fetcher = Fetcher(client, Robots(BOT_TOKEN), delay=settings.crawl_delay, sleep=sleep)

        async def one(url: str) -> tuple[str, SiteResult | Exception]:
            async with limit:
                try:
                    return url, await crawl_site(fetcher, url, max_pages=settings.crawl_max_pages)
                except Exception as exc:  # a bug on one weird site must not stop the job
                    return url, exc

        tasks = [asyncio.ensure_future(one(url)) for url in groups]
        try:
            for done, next_task in enumerate(asyncio.as_completed(tasks), 1):
                url, result = await next_task
                stats.checked += 1
                if isinstance(result, Exception):
                    stats.failed += 1
                    q.log_error(db.scrapingJobs, job["_id"], "crawl", f"{type(result).__name__}: {result}", url=url)
                else:
                    stats.reachable += result.reachable
                    for lead in groups[url]:
                        await _save(db, lead, result, mx, hashes, stats)
                on_progress(done, total, stats)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return stats


async def _save(db: Database, lead: dict, result: SiteResult, mx: MxChecker, hashes, stats: CrawlStats) -> None:
    for attempt in range(3):
        update = await build_update(lead, result, now=q.now(), mx=mx)
        if suppression.is_suppressed(as_lead(lead, update), hashes):
            # A contact on this website is on the opt-out list: the business asked not to be contacted.
            db.leads.delete_one({"_id": lead["_id"]})
            stats.suppressed += 1
            return
        # Only write if nobody changed the lead since we read it (e.g. a person marked an email as
        # verified while this site was being crawled); otherwise rebuild from the fresh copy.
        res = db.leads.update_one({"_id": lead["_id"], "updatedAt": lead.get("updatedAt")}, {"$set": update})
        if res.matched_count or attempt == 2:
            return
        fresh = db.leads.find_one({"_id": lead["_id"]})
        if fresh is None:  # deleted meanwhile (opt-out): nothing to save
            return
        lead = fresh
