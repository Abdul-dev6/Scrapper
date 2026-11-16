"""Job queue on MongoDB.

Why MongoDB instead of Redis for v1: one job type, one worker, low volume.
find_one_and_update is atomic, so two workers can never claim the same job.
"""

import random
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.collection import Collection

OPEN = ("queued", "running")
BACKOFF_BASE_SECONDS = 60  # 1st retry after ~1 min, then ~4, ~16 (x4 each time), plus up to 20% jitter
BACKOFF_MAX_SECONDS = 3600


def now() -> datetime:
    return datetime.now(timezone.utc)


class LeaseLost(Exception):
    """This worker no longer owns the job: it was re-queued (e.g. the worker was paused longer than
    STALE_AFTER_SECONDS) and possibly claimed by another worker. Stop WITHOUT touching the job."""


def _mine(job_id, lease: str | None) -> dict:
    # Every claim gets a new random lease token. Writes about the job's state are fenced with it, so a
    # worker that lost the job (stale recovery + a new claim) can't overwrite the new owner's progress.
    # lease=None: unfenced (jobs claimed before this field existed, and unit tests).
    return {"_id": job_id} if lease is None else {"_id": job_id, "lease": lease}


def claim_next_job(jobs: Collection, worker_id: str) -> dict | None:
    """Atomically move the oldest queued job to 'running' and return it."""
    t = now()
    return jobs.find_one_and_update(
        # A job waiting for a retry (nextAttemptAt in the future) is skipped until its time comes.
        {"status": "queued", "cancelRequested": {"$ne": True},
         "$or": [{"nextAttemptAt": {"$exists": False}}, {"nextAttemptAt": None}, {"nextAttemptAt": {"$lte": t}}]},
        {
            "$set": {
                "status": "running",
                "workerId": worker_id,
                "lockedAt": t,
                "startedAt": t,
                "progress.stage": "starting",
                "updatedAt": t,
                "lease": uuid.uuid4().hex,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("createdAt", 1)],
        return_document=ReturnDocument.AFTER,
    )


def heartbeat(jobs: Collection, job_id, stage: str | None = None, pct: float | None = None, *,
              lease: str | None = None, **progress) -> bool:
    """Refresh the lock and report progress. Returns False if the job was cancelled.
    With a lease: raises LeaseLost if this worker no longer owns the job."""
    update: dict = {"lockedAt": now(), "updatedAt": now()}
    if stage is not None:
        update["progress.stage"] = stage
    if pct is not None:
        update["progress.pct"] = max(0, min(100, round(pct)))
    for key, value in progress.items():
        update[f"progress.{key}"] = value
    doc = jobs.find_one_and_update(
        {**_mine(job_id, lease), "status": "running"},
        {"$set": update},
        projection={"cancelRequested": 1},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None and lease is not None:
        current = jobs.find_one({"_id": job_id}, {"lease": 1, "status": 1})
        if current is None or current.get("lease") != lease:
            raise LeaseLost(f"job {job_id} was taken over (status {current and current.get('status')})")
        return False  # still ours but no longer running
    return bool(doc) and not doc.get("cancelRequested", False)


def log_error(jobs: Collection, job_id, stage: str, message: str, url: str | None = None, keep: int = 200) -> None:
    """Append to the job's error log (capped so one bad job can't grow the document forever)."""
    entry = {"at": now(), "stage": stage, "message": message[:500]}
    if url:
        entry["url"] = url[:500]
    jobs.update_one(
        {"_id": job_id},
        {"$push": {"errorLog": {"$each": [entry], "$slice": -keep}}, "$inc": {"stats.errors": 1}},
    )


def finish(jobs: Collection, job_id, status: str, stage: str | None = None, *, lease: str | None = None) -> bool:
    """Returns False if nothing was changed because another worker owns the job now (lease)."""
    assert status in ("completed", "partial", "failed", "cancelled")
    t = now()
    update = {"status": status, "finishedAt": t, "updatedAt": t, "progress.stage": stage or status}
    if status == "completed":
        update["progress.pct"] = 100
    res = jobs.update_one(_mine(job_id, lease), {"$set": update, "$unset": {"lockedAt": "", "lease": ""}})
    return res.matched_count == 1


def backoff_seconds(attempts: int, rand: Callable[[], float] = random.random) -> float:
    """Exponential backoff with jitter: ~60 s, ~240 s, ~960 s ... (capped at 1 hour)."""
    delay = BACKOFF_BASE_SECONDS * (4 ** max(0, attempts - 1))
    return min(BACKOFF_MAX_SECONDS, delay * (1 + 0.2 * rand()))


def retry_later(jobs: Collection, job_id, *, attempts: int, max_attempts: int, message: str,
                rand: Callable[[], float] = random.random, lease: str | None = None) -> str:
    """A temporary problem (map server busy, network down): queue the job again after a backoff,
    keeping its checkpoint so it resumes where it stopped. Returns "queued", "failed" or "lost"
    (another worker owns the job now: nothing was changed)."""
    t = now()
    if attempts >= max_attempts:
        ok = finish(jobs, job_id, "failed", stage=f"Gave up after {attempts} attempts: {message}"[:200], lease=lease)
        return "failed" if ok else "lost"
    at = t + timedelta(seconds=backoff_seconds(attempts, rand))
    res = jobs.update_one(
        _mine(job_id, lease),
        {"$set": {"status": "queued", "nextAttemptAt": at, "updatedAt": t,
                  "progress.stage": f"Temporary problem, retrying at {at:%H:%M} UTC: {message}"[:200]},
         "$unset": {"lockedAt": "", "workerId": "", "lease": ""}},
    )
    return "queued" if res.matched_count else "lost"


def release_for_restart(jobs: Collection, job_id, *, lease: str | None) -> bool:
    """The worker is shutting down (SIGTERM on a redeploy): hand the job back right away, keeping its
    checkpoint. The attempt is given back too: a redeploy is not the job's fault. (Before, every redeploy
    during a long job waited for the stale-lock timeout and used up one of the job's MAX_ATTEMPTS.)"""
    t = now()
    res = jobs.update_one(
        {**_mine(job_id, lease), "status": "running"},
        {"$set": {"status": "queued", "nextAttemptAt": t, "updatedAt": t,
                  "progress.stage": "Paused while the worker restarts; continues where it stopped"},
         "$unset": {"lockedAt": "", "workerId": "", "lease": ""},
         "$inc": {"attempts": -1}},
    )
    return res.matched_count == 1


def save_checkpoint(jobs: Collection, job_id, key: str, value, *, lease: str | None = None) -> None:
    """Remember how far a job got, so a restarted or retried job continues instead of starting over.
    Raises LeaseLost if another worker owns the job now (its checkpoint must not be overwritten)."""
    res = jobs.update_one(_mine(job_id, lease), {"$set": {f"checkpoint.{key}": value, "updatedAt": now()}})
    if lease is not None and res.matched_count == 0:
        raise LeaseLost(f"job {job_id} was taken over; checkpoint not saved")


def recover_stale_jobs(jobs: Collection, stale_after_seconds: int, max_attempts: int,
                       rand: Callable[[], float] = random.random) -> tuple[int, int]:
    """Running jobs with an old heartbeat belong to a worker that crashed.

    Re-queue them with a backoff (their checkpoint is kept), or fail them once they've used up
    their attempts. Returns (requeued, failed).
    """
    cutoff = now() - timedelta(seconds=stale_after_seconds)
    stale = {"status": "running", "lockedAt": {"$lt": cutoff}}
    failed = jobs.update_many(
        {**stale, "attempts": {"$gte": max_attempts}},
        {"$set": {"status": "failed", "finishedAt": now(), "progress.stage": "worker stopped responding"},
         "$unset": {"lockedAt": "", "lease": ""}},
    ).modified_count
    requeued = 0
    for doc in jobs.find(stale, {"attempts": 1}):
        at = now() + timedelta(seconds=backoff_seconds(doc.get("attempts", 1), rand))
        requeued += jobs.update_one(
            {"_id": doc["_id"], "status": "running", "lockedAt": {"$lt": cutoff}},  # still stale (no race)
            {"$set": {"status": "queued", "nextAttemptAt": at,
                      "progress.stage": "re-queued after worker restart; continues where it stopped"},
             "$unset": {"lockedAt": "", "workerId": "", "lease": ""}},
        ).modified_count
    return requeued, failed


def cancel_queued(jobs: Collection) -> int:
    """Queued jobs the user cancelled before any worker picked them up."""
    return jobs.update_many(
        {"status": "queued", "cancelRequested": True},
        {"$set": {"status": "cancelled", "finishedAt": now(), "progress.stage": "cancelled"}},
    ).modified_count
