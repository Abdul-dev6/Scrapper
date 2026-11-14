"""Checkpoints, resumable jobs, exponential backoff and the crawler's single retry."""

import asyncio
import json
import re
from datetime import timedelta

import httpx
import pytest

from app import jobs as q
from app import main
from app.crawl.fetcher import Fetcher, FetchError
from app.crawl.robots import Robots
from app.crawl.runner import make_async_client
from app.main import process
from app.geo.boundaries import AreaError
from app.osm.overpass import OverpassBusy
from app.pipeline import run_job
from tests.conftest import FakeOSM, FakeWeb, add_job

# ---------------------------------------------------------------- backoff + queue


def test_backoff_grows_exponentially_with_a_cap():
    no_jitter = lambda: 0.0  # noqa: E731
    assert [q.backoff_seconds(n, no_jitter) for n in (1, 2, 3)] == [60, 240, 960]
    assert q.backoff_seconds(10, no_jitter) == q.BACKOFF_MAX_SECONDS
    assert q.backoff_seconds(1, lambda: 1.0) == pytest.approx(72)  # max +20% jitter


def test_job_waiting_for_retry_is_not_claimed_early(db):
    waiting = add_job(db, nextAttemptAt=q.now() + timedelta(minutes=5))
    assert q.claim_next_job(db.scrapingJobs, "w1") is None
    db.scrapingJobs.update_one({"_id": waiting}, {"$set": {"nextAttemptAt": q.now() - timedelta(seconds=1)}})
    assert q.claim_next_job(db.scrapingJobs, "w1")["_id"] == waiting


def test_retry_later_requeues_then_gives_up(db):
    job_id = add_job(db)
    q.claim_next_job(db.scrapingJobs, "w1")
    assert q.retry_later(db.scrapingJobs, job_id, attempts=1, max_attempts=3, message="busy", rand=lambda: 0) == "queued"
    job = db.scrapingJobs.find_one({"_id": job_id})
    assert job["status"] == "queued" and "lockedAt" not in job and "workerId" not in job
    assert timedelta(seconds=59) < job["nextAttemptAt"] - q.now() <= timedelta(seconds=60)
    assert q.retry_later(db.scrapingJobs, job_id, attempts=3, max_attempts=3, message="busy") == "failed"
    assert db.scrapingJobs.find_one({"_id": job_id})["status"] == "failed"


def test_stale_job_is_requeued_with_backoff_and_keeps_its_checkpoint(db):
    job_id = add_job(db, status="running", lockedAt=q.now() - timedelta(hours=1), attempts=2,
                     checkpoint={"place": {"bbox": [1, 2, 3, 4]}})
    assert q.recover_stale_jobs(db.scrapingJobs, 600, 3, rand=lambda: 0) == (1, 0)
    job = db.scrapingJobs.find_one({"_id": job_id})
    assert job["status"] == "queued" and job["checkpoint"]["place"]["bbox"] == [1, 2, 3, 4]
    assert job["nextAttemptAt"] - q.now() > timedelta(seconds=230)  # attempt 2 -> ~240 s


@pytest.mark.parametrize("error,status", [
    (OverpassBusy("The OpenStreetMap search service is busy."), "queued"),  # temporary: retry later
    (httpx.ConnectError("network down"), "queued"),
    (AreaError("No boundary is available for Atlantis."), "failed"),  # permanent: retrying won't help
    (RuntimeError("bug"), "failed"),
])
def test_temporary_errors_retry_permanent_errors_fail(db, monkeypatch, error, status):
    def boom(*_a, **_k):
        raise error

    monkeypatch.setattr(main, "run_job", boom)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    process(db, job, settings=None)
    assert db.scrapingJobs.find_one({"_id": job["_id"]})["status"] == status


# ---------------------------------------------------------------- resume after a crash


class Crash(BaseException):
    """Simulates the worker process dying (not an Exception, so nothing in the job catches it)."""


def _quarter_elements(query: str) -> httpx.Response:
    """The whole London box is "too heavy" (504); each quarter returns one gym inside it."""
    south, west, north, east = (float(x) for x in re.search(r"\(([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\);", query).groups())
    if north - south > 0.3:
        return httpx.Response(504)
    lat, lon = (south + north) / 2, (west + east) / 2
    el = {"type": "node", "id": int(abs(lat * 1000) + abs(lon * 1000) * 7), "lat": lat, "lon": lon,
          "tags": {"name": f"Gym {lat:.3f} {lon:.3f}", "sport": "boxing"}}
    return httpx.Response(200, content=json.dumps({"elements": [el]}))


def test_crashed_job_resumes_from_checkpoint_without_redoing_work(db, settings, no_wait_limiter, monkeypatch):
    fake = FakeOSM(overpass_fn=_quarter_elements)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")

    real_save = q.save_checkpoint
    saves = []

    def crash_after_two_pieces(jobs, job_id, key, value):
        real_save(jobs, job_id, key, value)
        if key == "discovery" and not value.get("complete"):
            saves.append(1)
            if len(saves) == 2:
                raise Crash()

    monkeypatch.setattr(q, "save_checkpoint", crash_after_two_pieces)
    with pytest.raises(Crash):
        run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None, limiter=no_wait_limiter,
                web_transport=FakeWeb().transport())
    assert db.leads.count_documents({}) == 2
    queries_before = len(fake.queries())
    monkeypatch.setattr(q, "save_checkpoint", real_save)

    # The worker restarts: the job is re-queued (checkpoint kept) and claimed again.
    db.scrapingJobs.update_one({"_id": job["_id"]}, {"$set": {"lockedAt": q.now() - timedelta(hours=1)}})
    q.recover_stale_jobs(db.scrapingJobs, 600, 3, rand=lambda: 0)
    db.scrapingJobs.update_one({"_id": job["_id"]}, {"$set": {"nextAttemptAt": q.now()}})
    job = q.claim_next_job(db.scrapingJobs, "w2")
    status = run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None, limiter=no_wait_limiter,
                     web_transport=FakeWeb().transport())

    assert status == "completed"
    assert fake.count("nominatim.test") == 0  # no external geocoding service, ever
    assert len(fake.queries()) - queries_before == 2  # only the 2 quarters that weren't finished
    assert db.leads.count_documents({}) == 4  # all four quarters, nothing twice
    done = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert done["stats"]["newLeads"] == 4 and done["stats"]["discovered"] == 4  # counts continued
    assert done["checkpoint"]["discovery"]["complete"] is True and done["progress"]["pct"] == 100


def test_finished_discovery_is_not_repeated_on_retry(db, settings, no_wait_limiter):
    fake = FakeOSM(overpass_fn=_quarter_elements)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None, limiter=no_wait_limiter,
            web_transport=FakeWeb().transport())
    before = len(fake.queries())
    job = db.scrapingJobs.find_one({"_id": job["_id"]})  # e.g. the crawl stage crashed afterwards
    run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None, limiter=no_wait_limiter,
            web_transport=FakeWeb().transport())
    assert len(fake.queries()) == before and db.leads.count_documents({}) == 4


# ---------------------------------------------------------------- crawler retry


def _fetch(handler, url, sleeps):
    async def run():
        transport = httpx.MockTransport(handler)
        async with make_async_client("LeadFinderBot/test", 5, transport) as client:
            async def sleep(s):
                sleeps.append(s)
            return await Fetcher(client, Robots("LeadFinderBot"), delay=0, sleep=sleep).fetch(url)
    return asyncio.run(run())


def test_crawler_retries_once_after_a_temporary_error():
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, headers={"retry-after": "7"})
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    sleeps = []
    assert _fetch(handler, "https://gym.test/", sleeps).html == "<html>ok</html>"
    assert len(calls) == 2 and 7 in sleeps  # Retry-After respected


def test_crawler_gives_up_after_one_retry_and_never_retries_404():
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(request.url.path)
        return httpx.Response(500 if request.url.path == "/down" else 404)

    with pytest.raises(FetchError):
        _fetch(handler, "https://gym.test/down", [])
    with pytest.raises(FetchError):
        _fetch(handler, "https://gym.test/missing", [])
    assert calls.count("/down") == 2 and calls.count("/missing") == 1


def test_retry_after_is_capped():
    calls = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "3600"})

    sleeps = []
    with pytest.raises(FetchError):
        _fetch(handler, "https://gym.test/", sleeps)
    assert max(sleeps) == 30
