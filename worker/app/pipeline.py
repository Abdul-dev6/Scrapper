"""The scraping pipeline for one job.

Stages (progress bar):
  geocode (Nominatim)                          0-10%
  discover (Overpass) -> save leads            10-60%   <- Step 2
     + Google Text Search in a parallel thread           <- Step 6 (only if the job asks for "google")
  merge Google results into leads                         <- Step 6
  crawl websites -> extract -> verify -> merge 60-100%  <- Step 3
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import httpx
from pymongo import errors as mongo_errors
from pymongo.database import Database

from . import jobs as q
from . import leads, suppression
from .config import Settings
from .crawl import runner as crawl
from .google import merge as google_merge
from .google.runner import GoogleRun, make_usage
from .google.usage import UsageLimiter
from .osm import discovery, mapping, nominatim, overpass
from .osm.ratelimit import RateLimiter


class JobCancelled(Exception):
    pass


class TransientJobError(RuntimeError):
    """A temporary problem (e.g. the map server is down): the job is retried later from its checkpoint."""


TRANSIENT_ERRORS = (
    TransientJobError,
    nominatim.GeocodeUnavailable,
    overpass.OverpassBusy,
    httpx.TransportError,
    mongo_errors.AutoReconnect,  # includes NetworkTimeout / connection failures
    mongo_errors.ServerSelectionTimeoutError,
)


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_ERRORS)


# One limiter for the whole process: Nominatim allows max 1 request/second per application.
NOMINATIM_LIMITER = RateLimiter(min_interval=1.1)
SAVE_PROGRESS_EVERY = 25
DISCOVER_SHARE = 50  # % of the bar for the map search; the website check gets the rest


def user_agent(settings: Settings) -> str:
    return f"LeadFinderBot/0.2 (+{settings.crawler_contact_url})"


def make_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        headers={"User-Agent": user_agent(settings)},
        # Overpass may legitimately work for `overpass_timeout` seconds before answering.
        # connect=10: a server that does not even accept the connection in 10 s is down; giving up
        # quickly also means Cancel is noticed sooner.
        timeout=httpx.Timeout(30.0, connect=10.0, read=settings.overpass_timeout + 30),
        follow_redirects=True,
    )


def run_job(
    db: Database,
    job: dict,
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
    limiter: RateLimiter | None = None,
    web_transport: httpx.AsyncBaseTransport | None = None,
    mx_resolve=None,
    host_resolve=None,
    google_transport: httpx.BaseTransport | None = None,
    usage: UsageLimiter | None = None,
) -> str:
    """Run a job and return its final status: 'completed' or 'partial'.

    Raise JobCancelled if the user cancels, any other exception to fail the job
    (its message is shown to the user, so keep messages friendly).
    """
    if not settings.crawler_contact_url:
        raise RuntimeError("CRAWLER_CONTACT_URL is not set in worker/.env. OpenStreetMap requires a contact.")
    own_client = client is None
    client = client or make_client(settings)
    google = GoogleRun(db, job, settings, usage=usage or make_usage(db, settings),
                       transport=google_transport, sleep=sleep)
    try:
        return _run(db, job, settings, client, sleep or time.sleep, limiter or NOMINATIM_LIMITER,
                    web_transport, mx_resolve, google, host_resolve)
    finally:
        google.close()  # also stops the Google thread if the job was cancelled or crashed
        if own_client:
            client.close()


def _run(db: Database, job: dict, settings: Settings, client: httpx.Client, sleep, limiter,
         web_transport=None, mx_resolve=None, google: GoogleRun | None = None, host_resolve=None) -> str:
    jobs = db.scrapingJobs
    job_id, owner_id = job["_id"], job["ownerId"]

    def progress(**kw) -> None:
        if not q.heartbeat(jobs, job_id, **kw):
            raise JobCancelled()

    # A retried or restarted job continues from its checkpoint instead of starting over.
    checkpoint = job.get("checkpoint") or {}

    # 1. Where is the place?
    progress(stage="Finding the location", pct=5)
    place = checkpoint.get("place")
    if place is None:
        place = nominatim.geocode(
            job["location"]["query"],
            client=client,
            cache=db.geocodeCache,
            limiter=limiter,
            url=settings.nominatim_url,
            now=q.now(),
            sleep=sleep,
        )
        q.save_checkpoint(jobs, job_id, "place", place)
    jobs.update_one(
        {"_id": job_id},
        {"$set": {
            "location.displayName": place["displayName"],
            "location.country": place["country"],
            "location.countryCode": place["countryCode"],
            "location.bbox": place["bbox"],
        }},
    )

    # Google searches the same area in a background thread while OpenStreetMap runs below.
    google_merge.purge_expired_locations(db, q.now())  # Google policy: coordinates max 30 days
    if google is not None:
        google.start(place["bbox"], q.now())
        jobs.update_one({"_id": job_id}, {"$set": {"google": {"mode": google.mode, "note": google.note}}})

    # 2 + 3. Search the area piece by piece and save each piece's businesses straight away,
    # so a long country search shows results early and a crash doesn't lose finished pieces.
    progress(stage="Searching OpenStreetMap", pct=10)
    area_cc = place["countryCode"] if place["areaId"] else None
    hashes = suppression.load_hashes(db.suppressionList, owner_id)
    stats = dict.fromkeys(("discovered", "withWebsite", "newLeads", "updated", "merged", "duplicates", "suppressed"), 0)
    seen: set[str] = set()
    processed = element_failures = pieces_ok = pieces_failed = 0
    last_piece_error = ""
    saved_discovery = checkpoint.get("discovery") or {}
    if saved_discovery:  # resuming: keep counting from where the last attempt stopped
        stats.update({k: (job.get("stats") or {}).get(k, 0) for k in stats})
        counters = saved_discovery.get("counters", {})
        processed, element_failures = counters.get("processed", 0), counters.get("elementFailures", 0)
        pieces_ok, pieces_failed = counters.get("piecesOk", 0), counters.get("piecesFailed", 0)
        last_piece_error = counters.get("lastPieceError", "")

    def counters_now() -> dict:
        return {"processed": processed, "elementFailures": element_failures, "piecesOk": pieces_ok,
                "piecesFailed": pieces_failed, "lastPieceError": last_piece_error[:300]}

    pieces = [] if saved_discovery.get("complete") else discovery.discover(
        job["categories"],
        area_id=place["areaId"],
        bbox=place["bbox"],
        client=client,
        url=settings.overpass_url,
        status_url=status_url(settings.overpass_url),
        sleep=sleep,
        timeout=settings.overpass_timeout,
        max_queries=settings.max_discovery_queries,
        on_wait=progress,  # heartbeat + cancel check during long waits for the map server
        resume=saved_discovery if saved_discovery.get("pending") is not None else None,
    )
    fraction_before = 1.0 if saved_discovery.get("complete") else float(saved_discovery.get("done") or 0.0)
    for piece in pieces:
        if piece.failed_tile is not None:
            pieces_failed += 1
            last_piece_error = piece.error
            q.log_error(jobs, job_id, "discover", f"Area {piece.failed_tile.label()}: {piece.error}")
        else:
            pieces_ok += 1

        for i, element in enumerate(piece.elements, 1):
            processed += 1
            try:
                lead = mapping.element_to_lead(element, now=q.now(), area_country_code=area_cc)
                if lead is None:
                    continue
                osm_id = lead["externalIds"]["osm"]
                if osm_id in seen:  # a place on the border between two pieces comes back twice
                    stats["duplicates"] += 1
                    continue
                seen.add(osm_id)
                stats["discovered"] += 1
                stats["withWebsite"] += "website" in lead
                if suppression.is_suppressed(lead, hashes):
                    stats["suppressed"] += 1
                    continue
                result = leads.upsert_lead(db.leads, owner_id=owner_id, job_id=job_id, lead=lead, now=q.now())
                stats[{"new": "newLeads", "merged": "merged"}.get(result, "updated")] += 1
            except Exception as exc:  # one bad element must not lose the rest of the results
                element_failures += 1
                q.log_error(jobs, job_id, "save", f"{type(exc).__name__}: {exc}", url=mapping.osm_url(element))
            if processed % SAVE_PROGRESS_EVERY == 0:
                _save_stats(jobs, job_id, stats)
                # Move the bar while saving too, so a one-piece search doesn't sit at 10%.
                saving = fraction_before + (piece.fraction_done - fraction_before) * i / len(piece.elements)
                progress(pct=10 + DISCOVER_SHARE * saving, processed=processed, total=processed,
                         stage=f"Saving businesses: {stats['discovered']} so far")

        fraction_before = piece.fraction_done
        _save_stats(jobs, job_id, stats)
        # This piece's leads are saved: remember it, so a crash or retry continues with the next piece.
        q.save_checkpoint(jobs, job_id, "discovery", {**piece.state, "counters": counters_now()})
        left = f", {piece.pending} parts of the area left" if piece.pending else ""
        progress(
            stage=f"Searching OpenStreetMap: {stats['discovered']} businesses found{left}",
            pct=10 + DISCOVER_SHARE * piece.fraction_done,
            processed=processed,
            total=processed,
        )

    if not saved_discovery.get("complete"):
        q.save_checkpoint(jobs, job_id, "discovery", {"complete": True, "counters": counters_now()})

    google_active = google is not None and google.mode == "google+osm"
    osm_failed = pieces_ok == 0 and pieces_failed > 0
    if osm_failed and not google_active:
        # Every piece failed: almost always the shared map server being down/overloaded. Retry later
        # (the next attempt starts from scratch for OSM, because no piece finished).
        q.save_checkpoint(jobs, job_id, "discovery", {})
        raise TransientJobError(last_piece_error or "OpenStreetMap search failed. Try again later.")
    if processed and element_failures == processed:
        raise RuntimeError("None of the businesses could be saved. Check the worker logs.")

    # 3b. Merge what Google found (waits for the Google thread, with heartbeats).
    google_problem = False
    if google is not None and google.mode != "off":
        progress(stage="Matching Google results with OpenStreetMap", pct=10 + DISCOVER_SHARE)
        report = google.finish(bbox=place["bbox"], now=q.now(), heartbeat=lambda: progress())
        jobs.update_one({"_id": job_id}, {"$set": {"google": report}})
        if report.get("note") and report["mode"] == "osm-only" and google.mode == "google+osm":
            q.log_error(jobs, job_id, "google", report["note"])
        google_problem = google.mode == "google+osm" and report["mode"] == "osm-only" and "limit" not in report["note"]

    # 4. Visit each business's own website for its published contact details.
    crawl_start = 10 + DISCOVER_SHARE
    progress(stage="Checking websites", pct=crawl_start)

    def crawl_progress(done: int, total: int, cs: crawl.CrawlStats) -> None:
        if done == total or done % 5 == 0:
            jobs.update_one({"_id": job_id}, {"$set": {
                "stats.websitesChecked": cs.checked, "stats.websitesReachable": cs.reachable,
                "stats.suppressed": stats["suppressed"] + cs.suppressed}})
        progress(stage=f"Checking websites: {done} of {total}", pct=crawl_start + (100 - crawl_start) * done / total,
                 processed=done, total=total)

    kwargs = {"resolve": mx_resolve} if mx_resolve else {}
    if host_resolve:
        kwargs["host_resolve"] = host_resolve
    cs = asyncio.run(crawl.crawl_websites(
        db, job, settings, user_agent=user_agent(settings), on_progress=crawl_progress, transport=web_transport, **kwargs,
    ))
    jobs.update_one({"_id": job_id}, {"$set": {
        "stats.websitesChecked": cs.checked,
        "stats.websitesReachable": cs.reachable,
        "stats.suppressed": stats["suppressed"] + cs.suppressed,
        "stats.withEmail": db.leads.count_documents({"ownerId": owner_id, "jobIds": job_id, "emails.0": {"$exists": True}}),
    }})
    progress(stage="Done", pct=100)
    return "partial" if (element_failures or pieces_failed or cs.failed or google_problem) else "completed"


def status_url(interpreter_url: str) -> str:
    """https://overpass-api.de/api/interpreter -> https://overpass-api.de/api/status"""
    return interpreter_url.rsplit("/", 1)[0] + "/status"


def _save_stats(jobs, job_id, stats: dict) -> None:
    # Field by field, so stats.errors (incremented by log_error) isn't overwritten.
    jobs.update_one({"_id": job_id}, {"$set": {f"stats.{k}": v for k, v in stats.items()}})
