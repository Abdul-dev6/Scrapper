from datetime import timedelta

from app import jobs as q
from app import main
from app.main import process
from tests.conftest import add_job


def test_claims_oldest_job_once(db):
    old = add_job(db, createdAt=q.now() - timedelta(minutes=5))
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    assert job["_id"] == old
    assert job["status"] == "running" and job["attempts"] == 1
    # the same job can't be claimed again
    second = q.claim_next_job(db.scrapingJobs, "w2")
    assert second["_id"] != old
    assert q.claim_next_job(db.scrapingJobs, "w3") is None


def test_skips_cancelled_queued_jobs(db):
    add_job(db, cancelRequested=True)
    assert q.claim_next_job(db.scrapingJobs, "w1") is None
    assert q.cancel_queued(db.scrapingJobs) == 1


def test_heartbeat_reports_cancellation(db):
    job_id = add_job(db)
    q.claim_next_job(db.scrapingJobs, "w1")
    assert q.heartbeat(db.scrapingJobs, job_id, stage="discover", pct=40) is True
    db.scrapingJobs.update_one({"_id": job_id}, {"$set": {"cancelRequested": True}})
    assert q.heartbeat(db.scrapingJobs, job_id) is False
    assert db.scrapingJobs.find_one({"_id": job_id})["progress"]["pct"] == 40


def test_stale_jobs_are_requeued_then_failed(db):
    stale_time = q.now() - timedelta(hours=1)
    retry = add_job(db, status="running", lockedAt=stale_time, attempts=1)
    give_up = add_job(db, status="running", lockedAt=stale_time, attempts=3)
    fresh = add_job(db, status="running", lockedAt=q.now(), attempts=1)

    requeued, failed = q.recover_stale_jobs(db.scrapingJobs, stale_after_seconds=600, max_attempts=3)
    assert (requeued, failed) == (1, 1)
    status = {d["_id"]: d["status"] for d in db.scrapingJobs.find()}
    assert status == {retry: "queued", give_up: "failed", fresh: "running"}


def test_failing_pipeline_marks_job_failed_without_crashing(db, monkeypatch):
    def boom(*_args, **_kw):
        raise RuntimeError("Couldn't find \"Atlantis\".")

    monkeypatch.setattr(main, "run_job", boom)
    add_job(db)
    job = q.claim_next_job(db.scrapingJobs, "w1")
    process(db, job, settings=None)
    saved = db.scrapingJobs.find_one({"_id": job["_id"]})
    assert saved["status"] == "failed"
    assert saved["stats"]["errors"] == 1
    assert "Atlantis" in saved["errorLog"][0]["message"]
    assert "Atlantis" in saved["progress"]["stage"]  # the friendly message is what the UI shows
    assert "lockedAt" not in saved
