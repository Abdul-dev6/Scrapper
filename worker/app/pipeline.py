"""The scraping pipeline for one job.

Stages (progress bar):
  search area (offline: gazetteer place + Natural Earth boundary; no Nominatim)  0-10%
  OpenStreetMap from Geofabrik extracts -> save leads  10-60%   <- Phase 2b (no public Overpass)
     + Google Text Search in a parallel thread           <- Step 6 (only if the job asks for "google")
  merge Google results into leads                         <- Step 6
  crawl websites -> extract -> verify -> merge 60-100%  <- Step 3
"""

from __future__ import annotations

import asyncio
import logging
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
from .geo.boundaries import AreaError, Boundaries
from .osm import extracts as osm_extracts
from .overture import mapping as ov_mapping
from .overture import places as ov_places
from .osm import mapping
from .osm.ratelimit import RateLimiter


class JobCancelled(Exception):
    pass


class TransientJobError(RuntimeError):
    """A temporary problem (e.g. the map server is down): the job is retried later from its checkpoint."""


TRANSIENT_ERRORS = (
    TransientJobError,
    osm_extracts.ExtractUnavailable,  # Geofabrik / Overture down, download broken: retried with backoff
    httpx.TransportError,
    mongo_errors.AutoReconnect,  # includes NetworkTimeout / connection failures
    mongo_errors.ServerSelectionTimeoutError,
)


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, TRANSIENT_ERRORS)


# Kept for the run_job(limiter=...) signature; no rate-limited geocoding service is called any more.
NOMINATIM_LIMITER = RateLimiter(min_interval=0)
log = logging.getLogger(__name__)
SAVE_PROGRESS_EVERY = 25
OVERTURE_WRITE_BATCH = 500  # Overture leads per batched MongoDB write (2 round trips per batch)
_BOUNDARIES: Boundaries | None = None


def boundaries() -> Boundaries:
    """Pinned Natural Earth data, loaded once per worker process (tests replace this)."""
    global _BOUNDARIES
    if _BOUNDARIES is None:
        _BOUNDARIES = Boundaries.from_pinned()
    return _BOUNDARIES


NEEDS_PLACE = ("This search was created before place selection. Start a new search and pick the place "
               "from the list.")
DISCOVER_SHARE = 50  # % of the bar for the map search; the website check gets the rest


def user_agent(settings: Settings) -> str:
    return f"LeadFinderBot/0.2 (+{settings.crawler_contact_url})"


def make_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        transport=transport,
        headers={"User-Agent": user_agent(settings)},
        # Geofabrik: big streamed downloads. connect=15: a server that doesn't accept the connection
        # is down; read=60: no byte for a minute = broken connection (resumed/retried safely).
        timeout=httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=15.0),
        follow_redirects=False,  # followed explicitly: the dated extract URL is the version
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
    extract_filter: osm_extracts.FilterFn | None = None,
    overture_query: ov_places.QueryFn | None = None,
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
                    web_transport, mx_resolve, google, host_resolve, extract_filter, overture_query)
    finally:
        google.close()  # also stops the Google thread if the job was cancelled or crashed
        if own_client:
            client.close()


def _run(db: Database, job: dict, settings: Settings, client: httpx.Client, sleep, limiter,
         web_transport=None, mx_resolve=None, google: GoogleRun | None = None, host_resolve=None,
         extract_filter=None, overture_query=None) -> str:
    jobs = db.scrapingJobs
    job_id, owner_id = job["_id"], job["ownerId"]

    def progress(**kw) -> None:
        if not q.heartbeat(jobs, job_id, **kw):
            raise JobCancelled()

    # A retried or restarted job continues from its checkpoint instead of starting over.
    checkpoint = job.get("checkpoint") or {}

    # 1. Where is the place? The user picked it from the gazetteer; its area comes from pinned,
    # offline boundary data (no external geocoding service). Same inputs -> same area, so a resumed
    # job simply resolves it again.
    progress(stage="Preparing the search area", pct=5)
    picked = (job.get("location") or {}).get("place")
    if not picked:
        raise AreaError(NEEDS_PLACE)
    area = boundaries().resolve(picked)
    place = area.to_checkpoint()
    jobs.update_one(
        {"_id": job_id},
        {"$set": {
            "location.displayName": area.display_name,
            "location.country": area.country,
            "location.countryCode": area.country_code,
            "location.bbox": area.bbox,
            "location.boundary": area.boundary,
        }},
    )

    # Google searches the same area in a background thread while OpenStreetMap runs below.
    google_merge.purge_expired_locations(db, q.now())  # Google policy: coordinates max 30 days
    if google is not None:
        google.start(place["bbox"], q.now())
        jobs.update_one({"_id": job_id}, {"$set": {"google": {"mode": google.mode, "note": google.note}}})

    # 2 + 3. OpenStreetMap from Geofabrik extracts (no public Overpass). Each job pins the exact,
    # immutable extract versions it reads, start to end; progress is the element number inside
    # those files, so a restarted job continues exactly there (never mixing two versions).
    area_cc = area.country_code or None  # only places inside the area are kept, so this is a fact
    hashes = suppression.load_hashes(db.suppressionList, owner_id)
    stats = dict.fromkeys(("discovered", "withWebsite", "newLeads", "updated", "merged", "duplicates", "suppressed",
                           "outsideArea", "otherCategory"), 0)
    seen: set[str] = set()
    processed = element_failures = 0
    # The cached extract holds every category (it is shared by all searches); like the old Overpass
    # query, a job keeps only places matching at least one of ITS selected categories.
    wanted = set(job.get("categories") or [])
    sources = set(job.get("sources") or ["osm"])
    run_overture = "overture" in sources
    # OSM runs unless the job chose ONLY Overture (older jobs, e.g. sources ["google"], always ran OSM).
    run_osm = "osm" in sources or not run_overture
    # The map stages share 10-60% of the progress bar.
    osm_share = DISCOVER_SHARE / 2 if (run_osm and run_overture) else DISCOVER_SHARE
    saved = checkpoint.get("osm") or {}
    if not run_osm:
        saved = {"complete": True}  # Overture-only search: the OSM stage is skipped entirely
    if saved:  # resuming: keep counting from where the last attempt stopped
        stats.update({k: (job.get("stats") or {}).get(k, 0) for k in stats})
        processed = saved.get("counters", {}).get("processed", 0)
        element_failures = saved.get("counters", {}).get("elementFailures", 0)

    def counters() -> dict:
        return {"processed": processed, "elementFailures": element_failures}

    cfg = osm_extracts.ExtractConfig.from_settings(settings)
    cache = osm_extracts.ExtractCache(db, cfg, owner=f"{settings.worker_id}:{job_id}", client=client,
                                      filter_fn=extract_filter, sleep=sleep)
    pins: list[osm_extracts.Pinned] = []
    try:
        if saved.get("extracts") and not saved.get("complete"):
            for info in saved["extracts"]:
                pin = cache.pin_version(info, job_id=job_id)
                if pin is None:  # that exact version is gone: restart the OSM stage with current data
                    for p in pins:
                        cache.unpin(p)
                    pins = []
                    q.log_error(jobs, job_id, "discover", "The OpenStreetMap data used before the restart was "
                                "replaced; the map stage starts again (saved leads are kept, not duplicated).")
                    saved = {}
                    break
                pins.append(pin)
        if not saved.get("complete") and not pins:
            progress(stage="Selecting OpenStreetMap extract", pct=7)
            index = osm_extracts.GeofabrikIndex.load(client, cfg)
            ready = [d["_id"] for d in db.osmExtracts.find({"status": "ready", "current": {"$ne": None}}, {"_id": 1})]
            regions = index.plan(
                area.geometry, cached=ready,
                size_of=lambda r: osm_extracts.resolve_source(client, r).length,
                max_download_bytes=cfg.max_download_bytes, max_job_download_bytes=cfg.max_job_download_bytes,
                max_extracts=cfg.max_extracts_per_job)
            pins = cache.ensure(regions, job_id=job_id, heartbeat=lambda: progress(),
                                on_stage=lambda st: progress(stage=st))
            saved = {"extracts": [p.as_checkpoint() for p in pins], "index": 0, "position": 0, "counters": counters()}
            q.save_checkpoint(jobs, job_id, "osm", saved)
        if pins:
            jobs.update_one({"_id": job_id}, {"$set": {"location.osmExtract": {
                "provider": "geofabrik", "license": "ODbL 1.0 (OpenStreetMap contributors)",
                "extracts": [{k: v for k, v in p.as_checkpoint().items() if k != "file"} for p in pins]}}})

        total = sum(p.features for p in pins) or 1
        first = saved.get("index", 0)
        done_before = sum(p.features for p in pins[:first]) + saved.get("position", 0)
        last_renew = time.monotonic()
        for k, pin in enumerate(pins):
            if k < first:
                continue
            start = saved.get("position", 0) if k == first else 0
            data_date = (pin.osm_timestamp or pin.version)[:10]
            progress(stage=f"Processing gyms from {pin.name} (OpenStreetMap data of {data_date})",
                     pct=10 + osm_share * min(1.0, done_before / total))
            for i, element in osm_extracts.read_elements(cache.abs(pin.file), start):
                processed += 1
                try:
                    point = element if "lat" in element else element.get("center") or {}
                    if "lat" in point and not area.contains(float(point["lat"]), float(point["lon"])):
                        stats["outsideArea"] += 1  # in the extract, but outside the chosen place
                        continue
                    lead = mapping.element_to_lead(element, now=q.now(), area_country_code=area_cc)
                    if lead is None:
                        continue
                    if wanted and not wanted & set(lead["categories"]):
                        stats["otherCategory"] += 1  # e.g. a sports shop in a "boxing clubs" search
                        continue
                    osm_id = lead["externalIds"]["osm"]
                    if osm_id in seen:  # overlapping extracts (neighbouring counties) hold the same element
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
                    # Everything up to element i is saved: a restart continues at i + 1 (same version).
                    q.save_checkpoint(jobs, job_id, "osm", {**saved, "index": k, "position": i + 1, "counters": counters()})
                    progress(pct=10 + osm_share * min(1.0, (done_before + i - start + 1) / total),
                             processed=processed, total=total, stage=f"Processing gyms: {stats['discovered']} found")
                    if time.monotonic() - last_renew > 60:
                        cache.renew(pins)  # long jobs keep their files pinned
                        last_renew = time.monotonic()
            done_before += pin.features - start
            _save_stats(jobs, job_id, stats)
            q.save_checkpoint(jobs, job_id, "osm", {**saved, "index": k + 1, "position": 0, "counters": counters()})
        q.save_checkpoint(jobs, job_id, "osm", {**saved, "complete": True, "counters": counters()})
    finally:
        for p in pins:
            cache.unpin(p)
    _save_stats(jobs, job_id, stats)
    if run_osm:
        progress(stage=f"OpenStreetMap: {stats['discovered']} gyms found", pct=10 + osm_share)

    if processed and element_failures == processed:
        raise RuntimeError("None of the businesses could be saved. Check the worker logs.")

    # 3c. Overture Maps (separate lane: never matched with the OSM leads above).
    if run_overture:
        ov_failures = _overture_stage(db, job, settings, client, area, progress, hashes, wanted, overture_query,
                                      sleep, checkpoint, pct_from=10 + (osm_share if run_osm else 0),
                                      pct_to=10 + DISCOVER_SHARE)
        element_failures += ov_failures

    # 3b. Merge what Google found (waits for the Google thread, with heartbeats).
    google_problem = False
    if google is not None and google.mode != "off":
        progress(stage="Recording Google Maps places (not linked to leads)", pct=10 + DISCOVER_SHARE)
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
    return "partial" if (element_failures or cs.failed or google_problem) else "completed"


def _save_stats(jobs, job_id, stats: dict) -> None:
    # Field by field, so stats.errors (incremented by log_error) isn't overwritten.
    jobs.update_one({"_id": job_id}, {"$set": {f"stats.{k}": v for k, v in stats.items()}})


def _overture_stage(db, job, settings, client, area, progress, hashes, wanted, query_fn, sleep, checkpoint,
                    *, pct_from: float, pct_to: float) -> int:
    """Overture places for the area, from the release pinned for this job, into the "overture" lane.
    Same safety as the OSM stage: pinned immutable tile files, element-position checkpoints, retries.
    Returns the number of elements that failed to save."""
    jobs = db.scrapingJobs
    job_id, owner_id = job["_id"], job["ownerId"]
    keys = ("discovered", "withWebsite", "newLeads", "updated", "merged", "duplicates", "suppressed",
            "outsideArea", "otherCategory", "processed", "failures")
    saved = checkpoint.get("overture") or {}
    stats = dict.fromkeys(keys, 0)
    if saved:
        stats.update({k: ((job.get("stats") or {}).get("overture") or {}).get(k, 0) for k in keys})
    cfg = ov_places.OvertureConfig.from_settings(settings)
    cache = ov_places.TileCache(db, cfg, owner=f"{settings.worker_id}:{job_id}", query_fn=query_fn, sleep=sleep)
    pins: list[ov_places.TilePin] = []

    def save_progress(extra: dict) -> None:
        jobs.update_one({"_id": job_id}, {"$set": {f"stats.overture.{k}": v for k, v in stats.items()}})
        q.save_checkpoint(jobs, job_id, "overture", {**saved, **extra})

    try:
        if saved.get("tiles") and not saved.get("complete"):
            for info in saved["tiles"]:
                pin = cache.pin_saved(info, job_id=job_id)
                if pin is None:  # that release's tile is gone: start this stage again (upserts: no duplicates)
                    for p in pins:
                        cache.unpin(p)
                    pins = []
                    q.log_error(jobs, job_id, "overture", "The Overture Maps data used before the restart is no "
                                "longer cached; the Overture stage starts again (saved leads are kept, not duplicated).")
                    saved = {}
                    break
                pins.append(pin)
        if not saved.get("complete") and not pins:
            progress(stage="Selecting Overture Maps release", pct=pct_from)
            release = ov_places.current_release(db, client, cfg)
            tiles = ov_places.tiles_for(area.geometry, cfg.max_tiles_per_job)
            pins = cache.ensure(release, tiles, job_id=job_id, heartbeat=lambda: progress(),
                                on_stage=lambda st: progress(stage=st))
            saved = {"release": release, "tiles": [p.as_checkpoint() for p in pins], "index": 0, "position": 0}
            save_progress({})
        if pins:
            jobs.update_one({"_id": job_id}, {"$set": {"location.overture": {
                "provider": "overture", "release": pins[0].release, "tiles": len(pins),
                "features": sum(p.features for p in pins),
                "license": "CDLA-Permissive-2.0 / Apache-2.0 (per source); Overture Maps Foundation, overturemaps.org"}}})
        seen: set[str] = set()
        total = sum(p.features for p in pins) or 1
        first = saved.get("index", 0)
        done = sum(p.features for p in pins[:first]) + saved.get("position", 0)
        release = saved.get("release", "")
        pending: list[tuple[dict, str]] = []  # (lead, provenance id) waiting for the next batched write

        def flush() -> None:
            """Write the pending leads (one read + one bulk write). Per-lead problems are logged, not fatal."""
            if not pending:
                return
            batch = [lead for lead, _ in pending]
            try:
                results = leads.upsert_overture_batch(db.leads, owner_id=owner_id, job_id=job_id, items=batch,
                                                      now=q.now())
            except Exception as exc:  # the batch itself failed (e.g. connection): one lead at a time
                log.warning("Overture batch write failed (%s); saving one by one", exc)
                results = []
                for lead in batch:
                    try:
                        results.append(leads.upsert_overture_lead(db.leads, owner_id=owner_id, job_id=job_id,
                                                                  lead=lead, now=q.now()))
                    except Exception as one:  # noqa: BLE001
                        results.append(one)
            for (_, prov), result in zip(pending, results, strict=True):
                if isinstance(result, Exception):
                    stats["failures"] += 1
                    q.log_error(jobs, job_id, "overture", f"{type(result).__name__}: {result}", url=prov)
                else:
                    stats[{"new": "newLeads", "merged": "merged"}.get(result, "updated")] += 1
            pending.clear()

        for k, pin in enumerate(pins):
            if k < first:
                continue
            start = saved.get("position", 0) if k == first else 0
            for i, row in ov_places.read_rows(cache, pin, start):
                stats["processed"] += 1
                try:
                    if not area.contains(float(row["lat"]), float(row["lon"])):
                        stats["outsideArea"] += 1
                        continue
                    lead = ov_mapping.row_to_lead(row, release=pin.release, now=q.now())
                    if lead is None:
                        continue
                    if wanted and not wanted & set(lead["categories"]):
                        stats["otherCategory"] += 1
                        continue
                    if lead["externalIds"]["overture"] in seen:
                        stats["duplicates"] += 1
                        continue
                    seen.add(lead["externalIds"]["overture"])
                    stats["discovered"] += 1
                    stats["withWebsite"] += "website" in lead
                    if suppression.is_suppressed(lead, hashes):
                        stats["suppressed"] += 1
                        continue
                    pending.append((lead, ov_mapping.provenance_id(release or pin.release, str(row.get("id")))))
                except Exception as exc:  # one bad record must not lose the rest
                    stats["failures"] += 1
                    q.log_error(jobs, job_id, "overture", f"{type(exc).__name__}: {exc}",
                                url=ov_mapping.provenance_id(release, str(row.get("id"))))
                if len(pending) >= OVERTURE_WRITE_BATCH:
                    flush()
                    # The checkpoint only moves past rows that are written (a restart redoes the rest).
                    save_progress({"index": k, "position": i + 1})
                if stats["processed"] % SAVE_PROGRESS_EVERY == 0:
                    progress(pct=pct_from + (pct_to - pct_from) * min(1.0, (done + i - start + 1) / total),
                             stage=f"Processing Overture Maps gyms: {stats['discovered']} found")
                    cache.renew(pins)
            flush()
            done += pin.features - start
            save_progress({"index": k + 1, "position": 0})
        flush()
        save_progress({"complete": True})
    finally:
        for p in pins:
            cache.unpin(p)
    progress(stage=f"Overture Maps: {stats['discovered']} gyms found", pct=pct_to)
    if stats["processed"] and stats["failures"] == stats["processed"]:
        raise RuntimeError("None of the Overture Maps places could be saved. Check the worker logs.")
    return stats["failures"]
