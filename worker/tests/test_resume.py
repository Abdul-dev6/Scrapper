"""Checkpoints, resumable jobs, exponential backoff and the crawler's single retry."""

import asyncio
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
from app.osm.extracts import ExtractTooLarge, ExtractUnavailable
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
    (ExtractUnavailable("Geofabrik answered HTTP 503."), "queued"),  # temporary: retry later
    (ExtractTooLarge("This area is too large to search."), "failed"),  # permanent: needs a smaller area
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


# ---------------------------------------------------------------- resume after a crash (extract checkpoints)


class Crash(BaseException):
    """Simulates the worker process dying (not an Exception, so nothing in the job catches it)."""


GYMS = [{"type": "node", "id": i, "lat": 51.5, "lon": -0.1, "tags": {"name": f"Gym {i}", "sport": "boxing"}}
        for i in range(1, 61)]


def _run(db, settings, fake, job):
    return run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None,
                   web_transport=FakeWeb().transport())


def _crash_at_osm_checkpoint(monkeypatch, n):
    """Kill the worker right after the n-th mid-file OSM checkpoint was saved."""
    real_save = q.save_checkpoint
    saves = []

    def crash(jobs, job_id, key, value):
        real_save(jobs, job_id, key, value)
        if key == "osm" and value.get("position"):
            saves.append(value["position"])
            if len(saves) == n:
                raise Crash()

    monkeypatch.setattr(q, "save_checkpoint", crash)
    return real_save


def _restart(db, job_id):
    db.scrapingJobs.update_one({"_id": job_id}, {"$set": {"lockedAt": q.now() - timedelta(hours=1)}})
    q.recover_stale_jobs(db.scrapingJobs, 600, 3, rand=lambda: 0)
    db.scrapingJobs.update_one({"_id": job_id}, {"$set": {"nextAttemptAt": q.now()}})
    return q.claim_next_job(db.scrapingJobs, "w2")


def _count_processed(monkeypatch):
    from app import leads as leads_mod

    seen = []
    real = leads_mod.upsert_lead

    def spy(coll, **kw):
        seen.append(kw["lead"]["externalIds"]["osm"])
        return real(coll, **kw)

    monkeypatch.setattr("app.pipeline.leads.upsert_lead", spy)
    return seen


def test_crashed_job_resumes_from_checkpoint_with_the_same_extract_version(db, settings, monkeypatch):
    fake = FakeOSM(elements=GYMS)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    real_save = _crash_at_osm_checkpoint(monkeypatch, 2)  # after element 50 of 60
    with pytest.raises(Crash):
        _run(db, settings, fake, job)
    assert db.leads.count_documents({}) == 50
    pinned = db.scrapingJobs.find_one({"_id": job["_id"]})["checkpoint"]["osm"]["extracts"][0]
    monkeypatch.setattr(q, "save_checkpoint", real_save)

    processed = _count_processed(monkeypatch)
    job = _restart(db, job["_id"])
    assert _run(db, settings, fake, job) == "completed"
    assert processed == [f"node/{i}" for i in range(51, 61)]  # only the 10 elements not done before
    assert len(fake.downloads()) == 1  # the extract wasn't downloaded again
    done = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert db.leads.count_documents({}) == 60 and done["stats"]["newLeads"] == 60  # nothing twice, counts continued
    assert done["location"]["osmExtract"]["extracts"][0]["sha256"] == pinned["sha256"]
    assert done["checkpoint"]["osm"]["complete"] is True and done["progress"]["pct"] == 100
    assert db.osmExtracts.find_one({"_id": "great-britain"})["readers"] == []  # pins released


def test_a_new_extract_published_meanwhile_is_not_mixed_into_a_running_job(db, settings, monkeypatch):
    fake = FakeOSM(elements=GYMS)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    real_save = _crash_at_osm_checkpoint(monkeypatch, 1)  # after element 25
    with pytest.raises(Crash):
        _run(db, settings, fake, job)
    monkeypatch.setattr(q, "save_checkpoint", real_save)
    old_sha = db.scrapingJobs.find_one({"_id": job["_id"]})["checkpoint"]["osm"]["extracts"][0]["sha256"]

    # Meanwhile a refresh publishes a newer version with an extra gym.
    fake.elements = [*GYMS, {"type": "node", "id": 999, "lat": 51.5, "lon": -0.1, "tags": {"name": "New", "sport": "boxing"}}]
    fake.set_version("great-britain", "great-britain-261120")
    from app.osm import extracts
    cfg = extracts.ExtractConfig.from_settings(settings)
    cache = extracts.ExtractCache(db, cfg, owner="refresher", client=fake.client(settings))
    assert cache.acquire_refresh("great-britain")
    cache.refresh(extracts.GeofabrikIndex.load(fake.client(settings), cfg).by_id["great-britain"])
    cache.release_refresh("great-britain")

    processed = _count_processed(monkeypatch)
    job = _restart(db, job["_id"])
    _run(db, settings, fake, job)
    assert "node/999" not in processed and processed[0] == "node/26"  # finished on the version it started with
    done = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert done["location"]["osmExtract"]["extracts"][0]["sha256"] == old_sha


def test_if_the_pinned_version_is_gone_the_osm_stage_restarts_without_duplicates(db, settings, monkeypatch):
    fake = FakeOSM(elements=GYMS)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    real_save = _crash_at_osm_checkpoint(monkeypatch, 1)
    with pytest.raises(Crash):
        _run(db, settings, fake, job)
    monkeypatch.setattr(q, "save_checkpoint", real_save)
    info = db.scrapingJobs.find_one({"_id": job["_id"]})["checkpoint"]["osm"]["extracts"][0]
    from pathlib import Path
    (Path(settings.osm_cache_dir) / info["file"]).unlink()  # e.g. evicted after its pin expired
    db.osmExtracts.update_one({"_id": "great-britain"}, {"$set": {"current": None, "status": "evicted", "readers": []}})

    job = _restart(db, job["_id"])
    assert _run(db, settings, fake, job) == "completed"
    done = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert db.leads.count_documents({}) == 60  # restarted from the start: upserts, no duplicate leads
    assert any("starts again" in e["message"] for e in done["errorLog"])
    assert len(fake.downloads()) == 2


def test_finished_osm_stage_is_not_repeated_on_retry(db, settings):
    fake = FakeOSM(elements=GYMS)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    _run(db, settings, fake, job)
    calls = len(fake.calls)
    job = db.scrapingJobs.find_one({"_id": job["_id"]})  # e.g. the crawl stage crashed afterwards
    _run(db, settings, fake, job)
    assert len(fake.calls) == calls and db.leads.count_documents({}) == 60  # no request, no re-processing


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
