"""Worker entry point: python -m app.main"""

import logging
import signal
import time
import traceback

from . import jobs as q
from .config import load_settings
from .db import connect
from .google.merge import purge_expired_locations
from .osm.extracts import BackgroundRefresher, ExtractConfig
from .pipeline import JobCancelled, is_transient, run_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

_stop = False


def _request_stop(signum, _frame):
    global _stop
    _stop = True
    log.info("Received signal %s, finishing current step then exiting", signum)


def process(db, job, settings) -> None:
    jobs = db.scrapingJobs
    job_id = job["_id"]
    log.info("Claimed job %s (%s, attempt %s)", job_id, job["location"]["query"], job.get("attempts"))
    try:
        status = run_job(db, job, settings)
        q.finish(jobs, job_id, status)
        log.info("Job %s finished: %s", job_id, status)
    except JobCancelled:
        q.finish(jobs, job_id, "cancelled")
        log.info("Job %s cancelled", job_id)
    except Exception as exc:  # one bad job must never kill the worker
        if is_transient(exc):
            # Busy map server, network or database hiccup: try again later from the checkpoint.
            q.log_error(jobs, job_id, "pipeline", f"{type(exc).__name__}: {exc}")
            outcome = q.retry_later(jobs, job_id, attempts=job.get("attempts", 1),
                                    max_attempts=getattr(settings, "max_attempts", 3), message=str(exc)[:150])
            log.warning("Job %s hit a temporary problem (%s): %s", job_id, exc, outcome)
            return
        q.log_error(jobs, job_id, "pipeline", f"{type(exc).__name__}: {exc}")
        q.finish(jobs, job_id, "failed", stage=str(exc)[:200])
        log.error("Job %s failed\n%s", job_id, traceback.format_exc())


def main() -> None:
    settings = load_settings()
    client, db = connect(settings)
    signal.signal(signal.SIGTERM, _request_stop)  # Railway sends SIGTERM on redeploy
    signal.signal(signal.SIGINT, _request_stop)
    log.info("Worker %s connected to database '%s'", settings.worker_id, settings.mongodb_db)
    if not settings.crawler_contact_url or "example.com" in settings.crawler_contact_url:
        log.warning("CRAWLER_CONTACT_URL is empty or a placeholder. Set a real page/email before crawling (Step 3).")
    if settings.google_enabled:
        log.info("Google Places enabled (monthly limits: Text Search Pro %s, Place Details Essentials %s; "
                 "per job: %s / %s)", settings.google_text_search_monthly_limit, settings.google_details_monthly_limit,
                 settings.google_max_text_search_per_job, settings.google_max_details_per_job)
    else:
        log.info("Google Places disabled (no GOOGLE_PLACES_API_KEY): jobs run OpenStreetMap-only")

    # OpenStreetMap comes only from Geofabrik extracts (no public Overpass). Clean up after a crash,
    # then refresh stale extracts in the background (MongoDB leases make this safe across processes).
    extract_cfg = ExtractConfig.from_settings(settings)
    refresher = BackgroundRefresher(db, extract_cfg, owner=f"{settings.worker_id}:refresher")
    refresher.start()
    log.info("OpenStreetMap cache budget %.1f GB on %s; temporary downloads in %s",
             extract_cfg.cache_max_bytes / 1024**3, extract_cfg.cache_dir, extract_cfg.tmp_dir)

    last_recovery = 0.0
    try:
        while not _stop:
            if time.monotonic() - last_recovery > 60:
                requeued, failed = q.recover_stale_jobs(db.scrapingJobs, settings.stale_after, settings.max_attempts)
                cancelled = q.cancel_queued(db.scrapingJobs)
                purge_expired_locations(db, q.now())  # Google policy: cached coordinates max 30 days
                if requeued or failed or cancelled:
                    log.info("Recovery: requeued=%s failed=%s cancelled=%s", requeued, failed, cancelled)
                last_recovery = time.monotonic()

            job = q.claim_next_job(db.scrapingJobs, settings.worker_id)
            if job:
                process(db, job, settings)
            else:
                time.sleep(settings.poll_interval)
    finally:
        refresher.stop()  # stops a running background download at its next heartbeat
        client.close()
        log.info("Worker stopped")


if __name__ == "__main__":
    main()
