"""Job queue on MongoDB.

Why MongoDB instead of Redis for v1: one job type, one worker, low volume.
find_one_and_update is atomic, so two workers can never claim the same job.
"""

import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.collection import Collection

OPEN = ("queued", "running")
BACKOFF_BASE_SECONDS = 60  # 1st retry after ~1 min, then ~4, ~16 (x4 each time), plus up to 20% jitter
BACKOFF_MAX_SECONDS = 3600


def now() -> datetime:
    return datetime.now(timezone.utc)


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
            },
            "$inc": {"attempts": 1},
        },
        sort=[("createdAt", 1)],
        return_document=ReturnDocument.AFTER,
    )


def heartbeat(jobs: Collection, job_id, stage: str | None = None, pct: float | None = None, **progress) -> bool:
    """Refresh the lock and report progress. Returns False if the job was cancelled."""
    update: dict = {"lockedAt": now(), "updatedAt": now()}
    if stage is not None:
        update["progress.stage"] = stage
    if pct is not None:
        update["progress.pct"] = max(0, min(100, round(pct)))
    for key, value in progress.items():
        update[f"progress.{key}"] = value
    doc = jobs.find_one_and_update(
        {"_id": job_id, "status": "running"},
        {"$set": update},
        projection={"cancelRequested": 1},
        return_document=ReturnDocument.AFTER,
    )
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


def finish(jobs: Collection, job_id, status: str, stage: str | None = None) -> None:
    assert status in ("completed", "partial", "failed", "cancelled")
    t = now()
    update = {"status": status, "finishedAt": t, "updatedAt": t, "progress.stage": stage or status}
    if status == "completed":
        update["progress.pct"] = 100
    jobs.update_one({"_id": job_id}, {"$set": update, "$unset": {"lockedAt": ""}})


def backoff_seconds(attempts: int, rand: Callable[[], float] = random.random) -> float:
    """Exponential backoff with jitter: ~60 s, ~240 s, ~960 s ... (capped at 1 hour)."""
    delay = BACKOFF_BASE_SECONDS * (4 ** max(0, attempts - 1))
    return min(BACKOFF_MAX_SECONDS, delay * (1 + 0.2 * rand()))


def retry_later(jobs: Collection, job_id, *, attempts: int, max_attempts: int, message: str,
                rand: Callable[[], float] = random.random) -> str:
    """A temporary problem (map server busy, network down): queue the job again after a backoff,
    keeping its checkpoint so it resumes where it stopped. Returns "queued" or "failed"."""
    t = now()
    if attempts >= max_attempts:
        finish(jobs, job_id, "failed", stage=f"Gave up after {attempts} attempts: {message}"[:200])
        return "failed"
    at = t + timedelta(seconds=backoff_seconds(attempts, rand))
    jobs.update_one(
        {"_id": job_id},
        {"$set": {"status": "queued", "nextAttemptAt": at, "updatedAt": t,
                  "progress.stage": f"Temporary problem, retrying at {at:%H:%M} UTC: {message}"[:200]},
         "$unset": {"lockedAt": "", "workerId": ""}},
    )
    return "queued"


def save_checkpoint(jobs: Collection, job_id, key: str, value) -> None:
    """Remember how far a job got, so a restarted or retried job continues instead of starting over."""
    jobs.update_one({"_id": job_id}, {"$set": {f"checkpoint.{key}": value, "updatedAt": now()}})


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
         "$unset": {"lockedAt": ""}},
    ).modified_count
    requeued = 0
    for doc in jobs.find(stale, {"attempts": 1}):
        at = now() + timedelta(seconds=backoff_seconds(doc.get("attempts", 1), rand))
        requeued += jobs.update_one(
            {"_id": doc["_id"], "status": "running", "lockedAt": {"$lt": cutoff}},  # still stale (no race)
            {"$set": {"status": "queued", "nextAttemptAt": at,
                      "progress.stage": "re-queued after worker restart; continues where it stopped"},
             "$unset": {"lockedAt": "", "workerId": ""}},
        ).modified_count
    return requeued, failed


def cancel_queued(jobs: Collection) -> int:
    """Queued jobs the user cancelled before any worker picked them up."""
    return jobs.update_many(
        {"status": "queued", "cancelRequested": True},
        {"$set": {"status": "cancelled", "finishedAt": now(), "progress.stage": "cancelled"}},
    ).modified_count
