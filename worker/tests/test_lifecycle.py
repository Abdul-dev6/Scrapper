"""Phase 3: job ownership (lease fencing), graceful shutdown, progress on skipped rows, and pin renewal.

  - A worker that lost its job (paused longer than STALE_AFTER_SECONDS, the job was re-queued and
    claimed by another worker) must stop without touching the job.
  - SIGTERM (a redeploy) hands the job back at once, with its checkpoint, without using up an attempt.
  - Rows skipped with `continue` (outside the area...) still send heartbeats, checkpoints and see Cancel.
  - Overture tile pins are renewed at most once a minute, not every 25 rows.
"""

from datetime import timedelta
from functools import partial

import httpx
import pytest

from app import jobs as q
from app import leads as leads_mod
from app import main, pipeline
from app.osm import extracts as ex
from app.overture import places as ov
from app.pipeline import JobCancelled, run_job
from tests.conftest import FakeOSM, FakeWeb, add_job

GYMS = [{"type": "node", "id": i, "lat": 51.5, "lon": -0.1, "tags": {"name": f"Gym {i}", "sport": "boxing"}}
        for i in range(1, 61)]


def _run(db, settings, fake, job, **kw):
    return run_job(db, job, settings, client=fake.client(settings), sleep=lambda _s: None,
                   web_transport=FakeWeb().transport(), mx_resolve=FakeWeb().resolve, **kw)


def _take_over(db, job_id, worker="w2") -> dict:
    """What happens when a worker stops reporting: recovery re-queues the job, another worker claims it."""
    db.scrapingJobs.update_one({"_id": job_id}, {"$set": {"lockedAt": q.now() - timedelta(hours=1)}})
    assert q.recover_stale_jobs(db.scrapingJobs, 600, 3, rand=lambda: 0) == (1, 0)
    db.scrapingJobs.update_one({"_id": job_id}, {"$set": {"nextAttemptAt": q.now()}})
    return q.claim_next_job(db.scrapingJobs, worker)


# ---------------------------------------------------------------- lease fencing


def test_every_claim_gets_a_new_lease(db):
    job_id = add_job(db)
    first = q.claim_next_job(db.scrapingJobs, "w1")
    second = _take_over(db, job_id)
    assert first["lease"] and second["lease"] and first["lease"] != second["lease"]


def test_the_old_owner_cannot_report_checkpoint_or_finish_after_a_takeover(db):
    job_id = add_job(db)
    old = q.claim_next_job(db.scrapingJobs, "w1")
    new = _take_over(db, job_id)
    q.save_checkpoint(db.scrapingJobs, job_id, "osm", {"position": 7}, lease=new["lease"])

    with pytest.raises(q.LeaseLost):
        q.heartbeat(db.scrapingJobs, job_id, stage="old worker", lease=old["lease"])
    with pytest.raises(q.LeaseLost):
        q.save_checkpoint(db.scrapingJobs, job_id, "osm", {"position": 999}, lease=old["lease"])
    assert q.finish(db.scrapingJobs, job_id, "completed", lease=old["lease"]) is False
    assert q.retry_later(db.scrapingJobs, job_id, attempts=1, max_attempts=3, message="x", lease=old["lease"]) == "lost"

    doc = db.scrapingJobs.find_one({"_id": job_id})
    assert doc["status"] == "running" and doc["workerId"] == "w2" and doc["lease"] == new["lease"]
    assert doc["checkpoint"]["osm"] == {"position": 7} and doc["progress"]["stage"] != "old worker"
    assert q.heartbeat(db.scrapingJobs, job_id, stage="new worker", lease=new["lease"]) is True
    assert q.finish(db.scrapingJobs, job_id, "completed", lease=new["lease"]) is True
    assert "lease" not in db.scrapingJobs.find_one({"_id": job_id})


def test_heartbeat_without_a_lease_keeps_the_old_behaviour(db):
    job_id = add_job(db, status="running")  # e.g. a job claimed by a worker from before this change
    assert q.heartbeat(db.scrapingJobs, job_id, stage="x") is True
    db.scrapingJobs.update_one({"_id": job_id}, {"$set": {"cancelRequested": True}})
    assert q.heartbeat(db.scrapingJobs, job_id) is False


def test_a_paused_worker_stops_when_it_wakes_up_and_the_new_owner_finishes_cleanly(db, settings, monkeypatch):
    """Worker A is paused mid-job (laptop sleep, long GC...) and the job moves to worker B. When A wakes up
    it stops at its next progress report; B's job, checkpoint and leads are not disturbed."""
    fake = FakeOSM(elements=GYMS)
    job_id = add_job(db)
    job_a = q.claim_next_job(db.scrapingJobs, "w1")
    real_upsert = leads_mod.upsert_lead
    calls = {"n": 0}
    taken = {}

    def upsert(coll, **kw):
        calls["n"] += 1
        if calls["n"] == 30:  # A "sleeps" here; meanwhile the job is recovered and claimed by B
            taken["job"] = _take_over(db, job_id)
            q.save_checkpoint(db.scrapingJobs, job_id, "osm", {"marker": "B"}, lease=taken["job"]["lease"])
        return real_upsert(coll, **kw)

    monkeypatch.setattr("app.pipeline.leads.upsert_lead", upsert)
    with pytest.raises(q.LeaseLost):
        _run(db, settings, fake, job_a)
    doc = db.scrapingJobs.find_one({"_id": job_id})
    assert doc["status"] == "running" and doc["workerId"] == "w2"
    assert doc["checkpoint"]["osm"] == {"marker": "B"}  # A didn't overwrite B's checkpoint
    assert calls["n"] < 60  # A stopped at its next report instead of running the whole job again

    monkeypatch.setattr("app.pipeline.leads.upsert_lead", real_upsert)
    db.scrapingJobs.update_one({"_id": job_id}, {"$unset": {"checkpoint": ""}})  # B starts its stage fresh
    job_b = db.scrapingJobs.find_one({"_id": job_id})
    assert _run(db, settings, fake, job_b) == "completed"
    assert db.leads.count_documents({}) == 60  # nothing twice, although A and B both saved leads


def test_process_leaves_a_taken_over_job_alone(db, monkeypatch):
    job_id = add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")

    def lost(*_a, **_k):
        _take_over(db, job_id)
        raise q.LeaseLost("taken over")

    monkeypatch.setattr(main, "run_job", lost)
    main.process(db, job, settings=None)
    doc = db.scrapingJobs.find_one({"_id": job_id})
    assert doc["status"] == "running" and doc["workerId"] == "w2"  # not failed, not cancelled
    assert doc["errorLog"] == [] and doc["stats"]["errors"] == 0  # nothing written about the old attempt


@pytest.mark.parametrize("error", [RuntimeError("bug in the old attempt"), httpx.ConnectError("network down")])
def test_a_failure_after_a_takeover_does_not_touch_the_new_owners_job(db, monkeypatch, error):
    """Permanent (fail) and temporary (retry later) errors alike: the job belongs to w2 now."""
    job_id = add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")

    def crash(*_a, **_k):
        _take_over(db, job_id)
        raise error

    monkeypatch.setattr(main, "run_job", crash)
    main.process(db, job, settings=None)
    doc = db.scrapingJobs.find_one({"_id": job_id})
    assert doc["status"] == "running" and doc["workerId"] == "w2" and doc["errorLog"] == []


# ---------------------------------------------------------------- graceful shutdown (SIGTERM)


def test_sigterm_hands_the_job_back_with_its_checkpoint_and_without_using_an_attempt(db, settings, monkeypatch):
    fake = FakeOSM(elements=GYMS)
    job_id = add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    assert job["attempts"] == 1

    def stop_after_a_while() -> bool:  # SIGTERM arrives in the middle of the OSM file
        return db.leads.count_documents({}) >= 30

    monkeypatch.setattr(main, "run_job", partial(run_job, client=fake.client(settings), sleep=lambda _s: None,
                                                 web_transport=FakeWeb().transport(), mx_resolve=FakeWeb().resolve))
    main.process(db, job, settings, stop=stop_after_a_while)
    doc = db.scrapingJobs.find_one({"_id": job_id})
    assert doc["status"] == "queued" and doc["attempts"] == 0  # the attempt was given back
    assert doc["nextAttemptAt"] <= q.now()  # no backoff: the next worker continues right away
    assert "lease" not in doc and "lockedAt" not in doc and "workerId" not in doc
    position = doc["checkpoint"]["osm"]["position"]
    assert 0 < position < 60 and db.leads.count_documents({}) == position  # stopped mid-file, nothing lost

    again = q.claim_next_job(db.scrapingJobs, "w2")
    assert again["attempts"] == 1
    main.process(db, again, settings, stop=lambda: False)
    done = db.scrapingJobs.find_one({"_id": job_id})
    assert done["status"] == "completed" and db.leads.count_documents({}) == 60
    assert done["stats"]["newLeads"] == 60  # counts continued, nothing twice


def test_stopping_with_a_lost_lease_does_not_requeue_the_job(db):
    job_id = add_job(db)
    old = q.claim_next_job(db.scrapingJobs, "w1")
    _take_over(db, job_id)
    assert q.release_for_restart(db.scrapingJobs, job_id, lease=old["lease"]) is False
    doc = db.scrapingJobs.find_one({"_id": job_id})
    assert doc["status"] == "running" and doc["workerId"] == "w2" and doc["attempts"] == 2


# ---------------------------------------------------------------- skipped rows still report progress


def test_skipped_rows_still_send_heartbeats_and_see_cancel(db, settings, monkeypatch):
    """A city search in a country extract skips almost every element (outside the area). Progress
    reports used to sit at the END of the loop, after those `continue`s, so they almost never ran."""
    far = [{"type": "node", "id": i, "lat": 55.9, "lon": -3.2, "tags": {"name": f"Edinburgh Gym {i}", "sport": "boxing"}}
           for i in range(1, 201)]  # Edinburgh: in the extract, outside the London search area
    fake = FakeOSM(elements=far)
    job_id = add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    real = q.heartbeat
    in_loop = []

    def spy(jobs, jid, *a, **kw):
        if "processed" in kw:
            in_loop.append(kw["processed"])
            if kw["processed"] >= 100:  # the user presses Cancel in the middle of the file
                db.scrapingJobs.update_one({"_id": jid}, {"$set": {"cancelRequested": True}})
        return real(jobs, jid, *a, **kw)

    monkeypatch.setattr(q, "heartbeat", spy)
    with pytest.raises(JobCancelled):
        _run(db, settings, fake, job)
    assert in_loop[:4] == [25, 50, 75, 100]  # every 25 elements, although all of them were skipped
    assert db.scrapingJobs.find_one({"_id": job_id})["checkpoint"]["osm"]["position"] == 99


# ---------------------------------------------------------------- Overture pin renewal


def _overture_rows(n):
    from tests.test_overture import place

    return [place(f"gers-{i}", f"Gym {i}", 51.40 + i * 0.0005, -0.20, website=f"https://gym{i}.test") for i in range(n)]


@pytest.mark.parametrize("interval,expected", [(60, 0), (0, 8)])
def test_overture_pins_are_renewed_by_time_not_every_25_rows(db, settings, monkeypatch, interval, expected):
    from tests.test_overture import REL, ov_job, write_release

    write_release(settings, REL, _overture_rows(200))
    renewals = []
    real = ov.TileCache.renew
    monkeypatch.setattr(ov.TileCache, "renew", lambda self, pins: (renewals.append(1), real(self, pins))[1])
    monkeypatch.setattr(pipeline, "RENEW_PINS_SECONDS", interval)
    status, job, _ = ov_job(db, settings)
    assert status == "completed" and job["stats"]["overture"]["newLeads"] == 200
    assert len(renewals) == expected  # was 8 calls x every pinned tile, regardless of time


# ---------------------------------------------------------------- Geofabrik index cache


def test_index_download_survives_a_concurrent_replace(db, settings, monkeypatch):
    """Two jobs refreshing the Geofabrik index at once: on Windows os.replace fails if the other one has the
    file open. The freshly downloaded list is still used (this made a concurrency test flaky)."""
    cfg = ex.ExtractConfig.from_settings(settings)

    def busy(_src, _dst):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(ex.os, "replace", busy)
    index = ex.GeofabrikIndex.load(FakeOSM().client(settings), cfg)
    assert index.regions  # parsed from the download
    assert not list((cfg.cache_dir / "index").glob("*.part"))  # no temporary file left behind
